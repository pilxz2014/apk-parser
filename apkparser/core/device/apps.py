# -*- coding: utf-8 -*-
"""已安装应用的检索、详情与快捷操作。

性能取向：
  · 列表走 `pm list packages -f --show-versioncode`（一次调用拿全量，~0.5s）
  · 详情走 `dumpsys package <pkg>`（单包，~0.3s），**不**做全量 dumpsys
  · 应用名（label）需要解析 APK 才能拿到 → 懒加载，且复用签名功能已缓存的包
"""

import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...platform import paths, shell
from . import adb as adb_mod
from .model import AppInfo, sanitize_serial

# package:/data/app/~~x/com.foo-abc/base.apk=com.foo versionCode:123
_PKG_RE = re.compile(
    r"^package:(?P<path>.+?)=(?P<pkg>[^\s=]+)(?:\s+versionCode:(?P<vc>\d+))?\s*$")


# ---------------------------------------------------------------- 列表

def _list_args(include_system: bool, with_versioncode: bool = True) -> List[str]:
    """拼 `pm list packages` 的参数。

    **坑**：`pm list packages` 的各种开关必须排在 `packages` **之后**。
    写成 `pm list -3 packages` 时 adb 的退出码仍是 0、也不报错，但输出为空 ——
    静默地把"第三方应用"全滤没了。所以这里用追加而不是插入。
    """
    args = ["shell", "pm", "list", "packages"]
    if not include_system:
        args.append("-3")
    args.append("-f")
    if with_versioncode:
        args.append("--show-versioncode")
    return args


def list_packages(include_system: bool = False, device: str = "") -> Tuple[List[AppInfo], List[str]]:
    """拿设备上的应用列表。返回 (应用列表, 执行过的命令)。"""
    serial = adb_mod.pick_device(device)
    commands: List[str] = []

    args = _list_args(include_system, with_versioncode=True)
    r = adb_mod.run(args, serial=serial, timeout=40)
    commands.append(adb_mod.command_preview(args, serial))

    if not r.ok or not r.out.strip():
        # `--show-versioncode` 在 Android 8 以下不支持 → 退回不带该参数
        args = _list_args(include_system, with_versioncode=False)
        r = adb_mod.run(args, serial=serial, timeout=40)
        commands.append(adb_mod.command_preview(args, serial))

    apps: Dict[str, AppInfo] = {}
    for line in r.lines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        m = _PKG_RE.match(line)
        if not m:
            continue
        pkg = m.group("pkg")
        apps[pkg] = AppInfo(
            package=pkg,
            apk_path=m.group("path") or "",
            version_code=int(m.group("vc") or 0),
        )

    # 系统包集合（用来标 is_system）
    sys_set = set()
    r2 = adb_mod.run(["shell", "pm", "list", "packages", "-s"], serial=serial, timeout=30)
    commands.append(adb_mod.command_preview(["shell", "pm", "list", "packages", "-s"], serial))
    if r2.ok:
        for line in r2.lines():
            line = line.strip()
            if line.startswith("package:"):
                sys_set.add(line.split(":", 1)[1].strip())

    for pkg, app in apps.items():
        app.is_system = pkg in sys_set

    return sorted(apps.values(), key=lambda a: a.package), commands


def list_apps(filter_kw: str = "", include_system: bool = False,
              device: str = "") -> List[AppInfo]:
    """列表 + 模糊过滤（包名/应用名，空格分词 AND）。

    **顺序要紧**：先把应用名补上再过滤 —— 反过来的话"按应用名搜"永远搜不到
    （过滤时 label 还是空的）。
    """
    apps, _cmds = list_packages(include_system=include_system, device=device)
    _enrich_labels(apps)
    if filter_kw:
        apps = [a for a in apps if a.matches(filter_kw)]
    return apps


def _enrich_labels(apps: List[AppInfo]) -> None:
    """补应用名。两级来源，都是**纯本地查表**，列表接口不会因此变慢。

    1. `labels` 缓存（按 包名|versionCode）—— 设备端定点解析过就有了
    2. 本地已缓存的 APK（导出 / 看签名时留下的）—— 顺手解析并回写一级缓存
    """
    # 一级：应用名缓存
    try:
        from . import labels as labels_mod
        labels_mod.enrich(apps)
    except Exception:
        pass

    # 二级：本地 APK 缓存
    try:
        from ..apk.info import parse_apk
    except Exception:
        return
    pending = [a for a in apps if not a.label]
    if not pending:
        return
    cache_root = paths.apk_cache_dir()
    if not cache_root.is_dir():
        return
    by_pkg: Dict[str, List[Path]] = {}
    for d in cache_root.iterdir():
        if not d.is_dir():
            continue
        for f in d.glob("*.apk"):
            pkg = f.name.split("__")[0]
            by_pkg.setdefault(pkg, []).append(f)

    for app in pending:
        cands = by_pkg.get(app.package)
        if not cands:
            continue
        f = max(cands, key=lambda p: p.stat().st_mtime)
        try:
            info = parse_apk(f, with_signature=False, with_icon=False, with_components=False)
        except Exception:
            continue
        if info.app_label:
            app.label = info.app_label
            try:
                from . import labels as labels_mod
                labels_mod.put(app.package, app.version_code, info.app_label, source="local-apk")
            except Exception:
                pass
        if not app.version_name:
            app.version_name = info.version_name


# ---------------------------------------------------------------- 详情

_DUMPSYS_FIELDS = {
    "versionCode": r"versionCode=(\d+)",
    "versionName": r"versionName=(\S+)",
    "minSdk": r"minSdk=(\d+)",
    "targetSdk": r"targetSdk=(\d+)",
    "uid": r"userId=(\d+)",
    "codePath": r"codePath=(\S+)",
    "dataDir": r"dataDir=(\S+)",
    "primaryCpuAbi": r"primaryCpuAbi=(\S+)",
    "firstInstallTime": r"firstInstallTime=([\d\-: ]+)",
    "lastUpdateTime": r"lastUpdateTime=([\d\-: ]+)",
    "installerPackageName": r"installerPackageName=(\S+)",
    "apkSigningVersion": r"apkSigningVersion=(\d+)",
}


def app_detail(package: str, device: str = "") -> dict:
    """单个应用的详细信息。"""
    serial = adb_mod.pick_device(device)
    args = ["shell", "dumpsys", "package", package]
    r = adb_mod.run(args, serial=serial, timeout=40)
    command = adb_mod.command_preview(args, serial)

    if not r.ok or "Unable to find package" in r.text or not r.out.strip():
        return {"error": "找不到应用 %s（或 dumpsys 被限制）" % package,
                "package": package, "command": command}

    text = r.out
    info: Dict[str, object] = {"package": package, "command": command}
    for key, pat in _DUMPSYS_FIELDS.items():
        m = re.search(pat, text)
        if m:
            info[key] = m.group(1).strip()

    for k in ("versionCode", "minSdk", "targetSdk", "uid"):
        if k in info:
            try:
                info[k] = int(info[k])
            except (TypeError, ValueError):
                pass

    # `flags=[...]` 与 `pkgFlags=[...]` 含义相同，不同 ROM 吐的不一样，两个都收
    flags = re.search(r"^\s+(?:pkg)?[Ff]lags=\[\s*([^\]]*)\]", text, re.M)
    if flags:
        info["flags"] = flags.group(1).split()
    pflags = re.search(r"^\s+privateFlags=\[\s*([^\]]*)\]", text, re.M)
    if pflags:
        info["privateFlags"] = pflags.group(1).split()

    en = re.search(r"enabled=(\d+)", text)
    if en:
        info["enabled"] = int(en.group(1)) in (0, 1)  # 0/1 = 默认/启用

    # 拆分 APK
    splits = re.findall(r"^\s+splitConfigs|^\s+splits=\[(.*?)\]", text, re.M)
    if splits:
        info["splits"] = [s.strip() for s in splits[0].split(",") if s.strip()]

    # 本地缓存里有包 → 顺便补上应用名与图标
    cached = find_cached_apk(package)
    if cached:
        try:
            from ..apk.info import parse_apk
            apk_info = parse_apk(cached, with_signature=False, with_components=False)
            info["label"] = apk_info.app_label
            info["icon"] = apk_info.icon_data_uri
            info["apkFileName"] = apk_info.file_name
        except Exception:
            pass

    # ---- 字段名归一化 --------------------------------------------------
    # dumpsys 吐的是原始名（codePath / primaryCpuAbi / installerPackageName），
    # 但 CLI 与 Web 界面统一读 apkPath / abi / installer / isSystem。
    # 在**源头**补齐别名，免得每个消费方各写一套映射表（写漏就是"界面上莫名空白"）。
    info["isSystem"] = "SYSTEM" in (info.get("flags") or [])
    abi = str(info.get("primaryCpuAbi") or "").strip()
    info["abi"] = "" if abi.lower() in ("null", "none") else abi
    info["installer"] = str(info.get("installerPackageName") or "")

    # codePath 在这台 ROM 上是**目录**（split 安装），真正的 apk 得去 `pm path` 问。
    code_path = str(info.get("codePath") or "")
    apk_path = code_path if code_path.endswith(".apk") else ""
    if not apk_path:
        try:
            found, _cmds = package_paths(package, serial)
            if found:
                apk_path = found[0]          # base.apk 已排在最前
        except Exception:
            pass
    if not apk_path and code_path:
        apk_path = code_path.rstrip("/") + "/base.apk"
    info["apkPath"] = apk_path

    if "label" not in info:
        info["label"] = ""
    return info


# ---------------------------------------------------------------- 缓存定位

def find_cached_apk(package: str) -> Optional[Path]:
    """在 cache/apk/<serial>/ 里找该包已下载的 base.apk（最新的一份）。"""
    root = paths.apk_cache_dir()
    if not root.is_dir():
        return None
    best: Optional[Path] = None
    best_m = -1.0
    for d in root.iterdir():
        if not d.is_dir():
            continue
        for f in d.glob("%s__*.apk" % _safe(package)):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m > best_m:
                best, best_m = f, m
    return best


def _safe(s: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in s)


# ---------------------------------------------------------------- 路径 / 导出

def package_paths(package: str, device: str = "") -> Tuple[List[str], List[str]]:
    """`pm path` 的结果（base 在前）。"""
    serial = adb_mod.pick_device(device)
    args = ["shell", "pm", "path", package]
    r = adb_mod.run(args, serial=serial, timeout=25)
    commands = [adb_mod.command_preview(args, serial)]
    out = []
    for line in r.lines():
        line = line.strip()
        if line.startswith("package:"):
            out.append(line.split(":", 1)[1].strip())
    # base.apk 一定排最前
    out.sort(key=lambda p: (0 if p.endswith("base.apk") else 1, p))
    return out, commands


def export_apk(package: str, out: str = "", device: str = "") -> dict:
    """把设备上已安装的 APK 拉到本地（split 会全部拉下来）。"""
    serial = adb_mod.pick_device(device)
    paths_on_dev, commands = package_paths(package, serial)
    if not paths_on_dev:
        return {"ok": False, "detail": "取不到 APK 路径（应用可能已被卸载或受系统保护）",
                "commands": commands}

    dest_dir = Path(out) if out else (paths.output_dir() / "exported" / _safe(package))
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for remote in paths_on_dev:
        name = remote.rsplit("/", 1)[-1]
        local = dest_dir / name
        args = ["pull", remote, str(local)]
        r = adb_mod.run(args, serial=serial, timeout=300)
        commands.append(adb_mod.command_preview(args, serial))
        if r.ok and local.is_file():
            saved.append({"remote": remote, "local": str(local), "bytes": local.stat().st_size})
        else:
            commands.append("# 失败：%s" % (r.err.strip() or r.out.strip())[:120])

    if not saved:
        return {"ok": False, "detail": "拉取失败", "commands": commands}
    return {
        "ok": True,
        "detail": "已导出 %d 个文件 → %s" % (len(saved), dest_dir),
        "dir": str(dest_dir),
        "files": saved,
        "commands": commands,
    }


# ---------------------------------------------------------------- 快捷操作

_ACTIONS = {
    "stop": ("强制停止", lambda pkg: ["shell", "am", "force-stop", pkg], False),
    "clear": ("清除数据", lambda pkg: ["shell", "pm", "clear", pkg], True),
    "uninstall": ("卸载", lambda pkg: ["shell", "pm", "uninstall", pkg], True),
    "enable": ("启用", lambda pkg: ["shell", "pm", "enable", pkg], False),
    "disable": ("禁用", lambda pkg: ["shell", "pm", "disable-user", "--user", "0", pkg], False),
}

ACTION_LABELS = {k: v[0] for k, v in _ACTIONS.items()}
ACTION_RISK = {k: ("dangerous" if v[2] else "confirm") for k, v in _ACTIONS.items()}


def app_action(package: str, op: str, device: str = "") -> dict:
    """对应用执行一个操作。返回结果里**必定**带执行的命令（可审计）。"""
    serial = adb_mod.pick_device(device)

    if op == "restart":
        cmds = [["shell", "am", "force-stop", package],
                ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"]]
        results = []
        for args in cmds:
            r = adb_mod.run(args, serial=serial, timeout=40)
            results.append({"args": args, "ok": r.ok, "out": r.out.strip()[:300]})
        return {
            "ok": all(x["ok"] for x in results),
            "detail": "已重启 %s" % package,
            "commands": [adb_mod.command_preview(a, serial) for a in cmds],
        }

    spec = _ACTIONS.get(op)
    if not spec:
        return {"ok": False, "detail": "未知操作：%s" % op, "commands": []}

    label, builder, _risky = spec
    args = builder(package)
    r = adb_mod.run(args, serial=serial, timeout=60)
    out = (r.out or "").strip()
    ok = r.ok and ("Success" in out or "success" in out or op in ("stop", "enable", "disable"))
    detail = "%s：%s" % (label, out.splitlines()[-1] if out else ("完成" if r.ok else "失败"))
    return {
        "ok": bool(ok),
        "detail": detail,
        "stdout": out[:1000],
        "stderr": r.err[:500],
        "commands": [adb_mod.command_preview(args, serial)],
    }
