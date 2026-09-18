# -*- coding: utf-8 -*-
"""安装执行。

策略：
  · 默认先 **push 到 /data/local/tmp + pm install** —— 能拿到真实进度百分比
    （`adb install` 本身不吐进度，大文件时用户只能干等）
  · push 通道有问题时**自动回退**到 `adb install`（一条命令更简单）
  · 每次安装都做预检，失败附带中文解释与一键补救
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..device import adb as adb_mod
from . import errors as err_mod
from . import precheck as precheck_mod

Progress = Optional[Callable[[str, str], None]]

_PUSH_PCT = re.compile(r"\[\s*(\d+)%\]")


def _say(progress: Progress, stage: str, msg: str) -> None:
    if progress:
        try:
            progress(stage, msg)
        except Exception:
            pass


def _flags(reinstall: bool, downgrade: bool, test: bool, grant: bool) -> List[str]:
    f = []
    if reinstall:
        f.append("-r")
    if downgrade:
        f.append("-d")
    if test:
        f.append("-t")
    if grant:
        f.append("-g")
    return f


# ---------------------------------------------------------------- 单包安装

def install_one(apk, serial: str, reinstall: bool = True, downgrade: bool = False,
                test: bool = False, grant: bool = False, check_only: bool = False,
                progress: Progress = None, do_precheck: bool = True) -> dict:
    """安装一个 APK 到一个设备。"""
    p = Path(apk)
    commands: List[str] = []
    flags = _flags(reinstall, downgrade, test, grant)

    result: Dict = {
        "file": p.name, "path": str(p), "device": serial,
        "ok": False, "detail": "", "commands": commands, "log": [],
        "precheck": [], "duration": "",
    }

    if not p.is_file():
        result["detail"] = "文件不存在"
        return result

    # ---- 预检 ----
    if do_precheck:
        _say(progress, "precheck", "安装前预检…")
        items = precheck_mod.precheck(p, serial, deep_sign=False)
        result["precheck"] = items
        stat = precheck_mod.summary(items)
        result["precheckSummary"] = stat
        if stat["blocking"] and not downgrade:
            # 硬性冲突（如版本降级）先拦下来，但可以通过勾选参数放行
            first = next(i for i in items if i["level"] == "fail")
            result["detail"] = "预检未通过：%s" % first["detail"]
            result["explain"] = err_mod.explain(first["code"]) if first["code"] else None
            result["blocked"] = True
            return result
        if stat["blocking"] and downgrade:
            result["log"].append("提示：预检有问题，但你选了「允许降级」，继续尝试安装")

    if check_only:
        result["ok"] = True
        result["detail"] = "仅预检（未实际安装）"
        return result

    size = p.stat().st_size
    t0 = time.time()

    # ---- 主路径：push + pm install（有进度） ----
    got = _install_via_push(p, serial, flags, progress, commands)
    if got is not None:
        result.update(got)
    else:
        # ---- 回退：adb install ----
        got = _install_direct(p, serial, flags, progress, commands)
        result.update(got)

    result["duration"] = "%.1fs" % (time.time() - t0)
    result["sizeBytes"] = size

    # ---- 失败 → 中文化 ----
    if not result.get("ok"):
        blob = "\n".join(result.get("log", [])) + "\n" + result.get("detail", "")
        e = err_mod.from_output(blob)
        if e is None and not result.get("explain"):
            e = err_mod.explain("INSTALL_FAILED_INTERNAL_ERROR")
        if e:
            result["explain"] = e
            result["detail"] = "%s → %s" % (e["code"], e["meaning"])
    else:
        # 装成功之后，之前缓存的签名/组件解析都该失效
        try:
            from ..device import signature as sig_mod
            sig_mod.invalidate(pkg_of(result))
        except Exception:
            pass

    return result


def pkg_of(result: dict) -> str:
    for item in (result.get("precheck") or []):
        d = item.get("detail", "")
        if " · " in d:
            return d.split(" · ")[0].strip()
    return ""


def _install_via_push(p: Path, serial: str, flags: List[str],
                      progress: Progress, commands: List[str]) -> Optional[dict]:
    """push 到 /data/local/tmp 再 pm install。返回 None 表示通道不可用，需回退。"""
    from ...platform import shell

    remote = "/data/local/tmp/.apkparser-%d.apk" % int(time.time() * 1000)
    exe = str(adb_mod.adb_bin())

    _say(progress, "push", "推送安装包到设备…")
    argv = [exe, "-s", serial, "push", str(p), remote]
    commands.append(adb_mod.command_preview(["push", str(p), remote], serial))

    proc = shell.spawn(argv)
    if proc is None:
        return None

    log: List[str] = []
    last_pct = -1
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            log.append(line)
            m = _PUSH_PCT.search(line)
            if m:
                pct = int(m.group(1))
                if pct != last_pct:
                    last_pct = pct
                    _say(progress, "push", "推送到设备 %d%%" % pct)
        proc.wait(timeout=600)
    except Exception as e:
        shell.kill_tree(proc.pid)
        log.append("push 异常：%s" % e)

    if proc.returncode not in (0, None) or last_pct < 100:
        if last_pct < 100 and proc.returncode not in (0, None):
            return None  # 让上层回退

    _say(progress, "install", "设备端安装中…")
    args = ["shell", "pm", "install"] + flags + [remote]
    # pm install 的位置参数要在最后
    args = ["shell", "pm", "install"] + [f for f in flags] + [remote]
    commands.append(adb_mod.command_preview(args, serial))
    r = adb_mod.run(args, serial=serial, timeout=420)
    out = (r.out or "") + ("\n" + r.err if r.err else "")
    log.extend(out.strip().splitlines())

    adb_mod.run(["shell", "rm", "-f", remote], serial=serial, timeout=15)

    ok = "Success" in out
    detail = "安装成功" if ok else out.strip().splitlines()[-1] if out.strip() else "安装失败"
    return {"ok": ok, "detail": detail, "log": log[-40:], "mode": "push+pm"}


def _install_direct(p: Path, serial: str, flags: List[str],
                    progress: Progress, commands: List[str]) -> dict:
    """`adb install`（一条命令，简单稳）。"""
    from ...platform import shell

    _say(progress, "install", "通过 adb install 安装…")
    args = ["install"] + flags + [str(p)]
    commands.append(adb_mod.command_preview(args, serial))

    exe = str(adb_mod.adb_bin())
    proc = shell.spawn([exe, "-s", serial] + args)
    log: List[str] = []
    if proc is not None:
        try:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                log.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            proc.wait(timeout=600)
        except Exception:
            shell.kill_tree(proc.pid)
    else:
        r = adb_mod.run(args, serial=serial, timeout=420)
        log = r.lines()

    out = "\n".join(log)
    ok = "Success" in out
    detail = "安装成功" if ok else (
        next((l.strip() for l in reversed(log) if l.strip()), "安装失败"))
    return {"ok": ok, "detail": detail, "log": log[-40:], "mode": "adb install"}


# ---------------------------------------------------------------- 批量

def install(apks: List[str], device: str = "", reinstall: bool = True,
            downgrade: bool = False, test: bool = False, grant: bool = False,
            check_only: bool = False, parallel: bool = False,
            progress: Progress = None) -> dict:
    """安装一个或多个 APK（同一个设备）。"""
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "detail": str(e), "results": [], "precheck": []}

    all_pre: List[dict] = []
    results: List[dict] = []

    if parallel and len(apks) > 1:
        # 同设备并发装会互相抢 PackageManager，串行更可靠；这里只在多文件时并行预检
        with ThreadPoolExecutor(max_workers=4) as pool:
            for pre in pool.map(lambda a: precheck_mod.precheck(a, serial), apks):
                all_pre.extend(pre)

    for i, apk in enumerate(apks, 1):
        _say(progress, "file", "（%d/%d）%s" % (i, len(apks), Path(apk).name))
        r = install_one(apk, serial, reinstall=reinstall, downgrade=downgrade,
                        test=test, grant=grant, check_only=check_only,
                        progress=progress, do_precheck=not all_pre)
        results.append(r)
        if not all_pre:
            all_pre = r.get("precheck", []) or all_pre

    ok_all = all(r.get("ok") for r in results)
    succ = len([r for r in results if r.get("ok")])
    return {
        "ok": ok_all,
        "device": serial,
        "detail": "%d/%d 成功" % (succ, len(results)),
        "results": results,
        "precheck": all_pre,
    }


def install_multi(apk: str, devices: List[str], progress: Progress = None) -> dict:
    """同一个 APK 并发装到多台设备（对应 `batch_install.sh` 的场景）。"""
    results = []

    def one(serial: str) -> dict:
        try:
            return install_one(apk, serial, progress=progress)
        except Exception as e:
            return {"file": Path(apk).name, "device": serial, "ok": False,
                    "detail": "%s: %s" % (type(e).__name__, e)}

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(devices)))) as pool:
        for r in pool.map(one, devices):
            results.append(r)

    ok_all = all(r.get("ok") for r in results)
    return {"ok": ok_all, "apk": apk,
            "detail": "%d/%d 台设备成功" % (len([r for r in results if r.get("ok")]), len(results)),
            "results": results}
