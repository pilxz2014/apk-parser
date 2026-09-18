# -*- coding: utf-8 -*-
"""截图。

两条路，优先走二进制安全的 `exec-out`：
  ① `adb exec-out screencap -p`            —— 一次调用拿 PNG 字节
  ② 回退：设备端落文件 → pull → 删文件    —— 个别 adb/ROM 组合下 ① 会失败

刻意不走 `adb shell screencap -p > file`：Windows 上 shell 通道会把
`\\n` 转成 `\\r\\n`，PNG 直接损坏（经典坑）。
"""

import time
from pathlib import Path
from typing import Dict, List

from ...platform import paths, shell
from ..device import adb as adb_mod

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def screenshot(out: str = "", device: str = "", timeout: float = 25.0) -> dict:
    """截屏并保存到本地。返回 {ok, path, bytes, ms, command}。"""
    serial = adb_mod.pick_device(device)
    exe = adb_mod.adb_bin()

    dest = Path(out) if out else (
        paths.captures_dir() / ("shot-%s.png" % time.strftime("%Y%m%d-%H%M%S")))
    dest.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    argv = [str(exe), "-s", serial, "exec-out", "screencap", "-p"]
    command = adb_mod.command_preview(["exec-out", "screencap", "-p"], serial)

    rc, data, err = shell.run_bytes(argv, timeout=timeout)
    ms = int((time.time() - t0) * 1000)

    if rc == 0 and data.startswith(PNG_MAGIC):
        dest.write_bytes(data)
        return {"ok": True, "path": str(dest), "bytes": len(data), "ms": ms,
                "command": command, "mode": "exec-out"}

    # ---- 回退：设备端文件 + pull ----
    remote = "/sdcard/.apkparser-shot.png"
    r1 = adb_mod.run(["shell", "screencap", "-p", remote], serial=serial, timeout=timeout)
    r2 = adb_mod.run(["pull", remote, str(dest)], serial=serial, timeout=60)
    adb_mod.run(["shell", "rm", "-f", remote], serial=serial, timeout=10)
    ms = int((time.time() - t0) * 1000)

    if r2.ok and dest.is_file() and dest.stat().st_size > 100:
        blob = dest.read_bytes()
        if blob.startswith(PNG_MAGIC):
            return {"ok": True, "path": str(dest), "bytes": len(blob), "ms": ms,
                    "command": adb_mod.command_preview(["pull", remote, str(dest)], serial),
                    "mode": "screencap+pull"}

    reason = err.strip() or (r1.err or r1.out or "").strip() or "未知原因"
    return {"ok": False, "error": "截图失败：%s" % reason[:200],
            "command": command, "ms": ms}


def screenshot_bytes(device: str = "", timeout: float = 25.0):
    """只拿字节不落盘（Web 端直接回图用）。"""
    serial = adb_mod.pick_device(device)
    exe = adb_mod.adb_bin()
    rc, data, err = shell.run_bytes(
        [str(exe), "-s", serial, "exec-out", "screencap", "-p"], timeout=timeout)
    if rc == 0 and data.startswith(PNG_MAGIC):
        return data
    res = screenshot(device=device, timeout=timeout)
    if res.get("ok"):
        return Path(res["path"]).read_bytes()
    return None
