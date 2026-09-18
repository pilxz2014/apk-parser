# -*- coding: utf-8 -*-
"""日志：流式 tail、快照、崩溃捕获。

按包过滤优先用 `--pid`（比 grep 包名准得多，能躲开别的进程的同名日志）。
"""

import re
import time
from typing import Dict, Iterator, List, Optional

from ...platform import shell
from ..device import adb as adb_mod

LEVELS = ("V", "D", "I", "W", "E", "F", "S")

# 崩溃特征行
_CRASH_PAT = re.compile(
    r"FATAL EXCEPTION|AndroidRuntime.*(FATAL|Shutting down)|"
    r"beginning of crash|\*\*\* \*\*\*|ANR in |Fatal signal|native crash",
    re.I)


def pid_of(package: str, serial: str) -> int:
    """拿进程 pid（可能多个，取第一个）。"""
    for cmd in ("pidof %s" % package, "pgrep -f %s" % package):
        r = adb_mod.run(["shell", cmd], serial=serial, timeout=12)
        m = re.search(r"\b(\d+)\b", r.out or "")
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return 0


def _args(package: str, level: str, serial: str, dump: bool, lines: int) -> List[str]:
    lv = level.upper() if level.upper() in LEVELS else "V"
    args = ["logcat", "-v", "time", "*:%s" % lv]
    if dump:
        args = ["logcat", "-d", "-v", "time", "-t", str(int(lines)), "*:%s" % lv]
    pid = pid_of(package, serial) if package else 0
    if pid:
        args.append("--pid=%d" % pid)
    return args


def snapshot(package: str = "", level: str = "V", filter_kw: str = "",
             device: str = "", lines: int = 200) -> dict:
    """一次性 dump 最近 N 行（不阻塞）。"""
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "error": str(e), "lines": []}

    args = _args(package, level, serial, dump=True, lines=lines)
    r = adb_mod.run(args, serial=serial, timeout=40)
    out = r.out or ""
    got = out.splitlines()
    if filter_kw:
        got = [l for l in got if filter_kw.lower() in l.lower()]
    return {
        "ok": bool(out.strip()),
        "lines": got,
        "count": len(got),
        "command": adb_mod.command_preview(args, serial),
        "error": "" if out.strip() else (r.err or "没有日志输出（检查设备是否授权）")[:200],
    }


def stream(package: str = "", level: str = "V", filter_kw: str = "",
           device: str = "", duration: float = 0) -> Iterator[str]:
    """流式输出日志（生成器）。duration>0 时到点自动结束。"""
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        yield "!! %s" % e
        return

    args = _args(package, level, serial, dump=False, lines=0)
    exe = str(adb_mod.adb_bin())
    proc = shell.spawn([exe, "-s", serial] + args)
    if proc is None:
        yield "!! 无法启动 logcat"
        return

    t0 = time.time()
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if filter_kw and filter_kw.lower() not in line.lower():
                continue
            yield line
            if duration and (time.time() - t0) >= duration:
                break
    except Exception as e:
        yield "!! 读取日志出错：%s" % e
    finally:
        shell.kill_tree(proc.pid)


def crash_watch(package: str = "", device: str = "", seconds: int = 15) -> dict:
    """抓 N 秒日志并挑出崩溃/ANR 相关行。"""
    lines: List[str] = []
    for line in stream(package=package, device=device, duration=seconds):
        lines.append(line)

    hits = [l for l in lines if _CRASH_PAT.search(l)]
    # 崩溃时把堆栈也带上（FATAL EXCEPTION 后的若干行）
    detail: List[str] = []
    for i, l in enumerate(lines):
        if _CRASH_PAT.search(l):
            detail.extend(lines[i:i + 30])
            break

    return {
        "ok": True,
        "crashed": bool(hits),
        "hits": hits[:20],
        "detail": detail,
        "total": len(lines),
        "seconds": seconds,
        "summary": ("捕获到崩溃/ANR，共 %d 条特征行" % len(hits)) if hits else "未发现崩溃",
    }


def clear(device: str = "") -> dict:
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "detail": str(e), "commands": []}
    args = ["logcat", "-c"]
    r = adb_mod.run(args, serial=serial, timeout=15)
    return {"ok": r.ok, "detail": "日志缓冲区已清空" if r.ok else (r.err or "清空失败"),
            "commands": [adb_mod.command_preview(args, serial)]}
