# -*- coding: utf-8 -*-
"""录屏与实时镜像。

录屏走设备端 `screenrecord`：它在 adb shell 断连时会正常收尾并写出可播放的
mp4，所以我们用"启动后台进程 → 停止时 terminate → pull"的方式控制起停。
（实测：直接 kill adb 会留下可播放文件，比发信号更省事。）

镜像走 `scrcpy`（可选依赖），支持"边镜像边录"。
"""

import threading
import time
from pathlib import Path
from typing import Dict, Optional

from ...platform import finders, paths, shell
from ..device import adb as adb_mod

_lock = threading.RLock()
_sessions: Dict[str, dict] = {}     # serial → {proc, remote, local, started}


# ---------------------------------------------------------------- 录屏

def start(bitrate: str = "8M", size: str = "", time_limit: int = 180,
          device: str = "") -> dict:
    """开始录屏。`time_limit` 到点设备会自己停。"""
    serial = adb_mod.pick_device(device)

    with _lock:
        if serial in _sessions:
            return {"ok": False, "detail": "该设备已在录屏中（先 stop）",
                    "commands": []}

        stamp = time.strftime("%Y%m%d-%H%M%S")
        remote = "/sdcard/.apkparser-rec-%s.mp4" % stamp
        local = paths.captures_dir() / ("record-%s.mp4" % stamp)
        local.parent.mkdir(parents=True, exist_ok=True)

        args = ["-s", serial, "shell", "screenrecord"]
        if size:
            args += ["--size", size]
        if bitrate:
            args += ["--bit-rate", bitrate]
        if time_limit and time_limit > 0:
            args += ["--time-limit", str(min(int(time_limit), 180))]
        args.append(remote)

        exe = str(adb_mod.adb_bin())
        proc = shell.spawn([exe] + args)
        if proc is None:
            return {"ok": False, "detail": "无法启动 screenrecord 进程", "commands": []}

        _sessions[serial] = {
            "proc": proc, "remote": remote, "local": str(local),
            "started": time.time(), "timeLimit": min(int(time_limit), 180) if time_limit else 180,
        }

    return {
        "ok": True,
        "detail": "已开始录屏（上限 %ds）" % (min(int(time_limit), 180) if time_limit else 180),
        "remote": remote,
        "local": str(local),
        "commands": [adb_mod.command_preview(["shell", "screenrecord", remote], serial)],
    }


def stop(device: str = "", pull: bool = True, timeout: float = 90.0) -> dict:
    """停止录屏并把文件拉回本地。"""
    serial = adb_mod.pick_device(device)
    with _lock:
        sess = _sessions.pop(serial, None)
    if not sess:
        return {"ok": False, "detail": "当前没有在录屏", "commands": []}

    proc = sess["proc"]
    commands: list = []
    try:
        proc.terminate()
        try:
            proc.wait(timeout=12)
        except Exception:
            shell.kill_tree(proc.pid)
    except Exception:
        pass

    # screenrecord 收尾写文件需要一点时间
    time.sleep(1.2)

    if not pull:
        return {"ok": True, "detail": "已停止录屏（未拉取文件）",
                "remote": sess["remote"], "commands": commands}

    local = Path(sess["local"])
    r = adb_mod.run(["pull", sess["remote"], str(local)], serial=serial, timeout=timeout)
    commands.append(adb_mod.command_preview(["pull", sess["remote"], str(local)], serial))
    adb_mod.run(["shell", "rm", "-f", sess["remote"]], serial=serial, timeout=15)

    dur = int(time.time() - sess["started"])
    if r.ok and local.is_file() and local.stat().st_size > 1024:
        return {"ok": True, "detail": "录屏已保存（%ds，%.1f MB）" % (
            dur, local.stat().st_size / 1048576.0),
            "path": str(local), "bytes": local.stat().st_size,
            "duration": dur, "commands": commands}

    return {"ok": False,
            "detail": "停止成功但拉取失败：%s" % (r.err or r.out or "").strip()[:150],
            "remote": sess["remote"], "commands": commands}


def session_state(device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    with _lock:
        sess = _sessions.get(serial)
    if not sess:
        return {"recording": False}
    return {"recording": True, "elapsed": int(time.time() - sess["started"]),
            "limit": sess["timeLimit"], "local": sess["local"]}


# ---------------------------------------------------------------- 镜像

def mirror(record_to: str = "", device: str = "", args: str = "") -> dict:
    """拉起 scrcpy（可选依赖）。record_to 非空则边镜像边录。"""
    scrcpy = finders.find_scrcpy()
    if not scrcpy:
        return {"ok": False,
                "detail": "没找到 scrcpy。实时镜像需要它（下载：github.com/Genymobile/scrcpy）",
                "hint": "也可以只用「截图 / 录屏」，它们只需要 adb。",
                "commands": []}

    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "detail": str(e), "commands": []}

    argv = [str(scrcpy), "-s", serial]
    if record_to:
        out = Path(record_to)
        if not out.is_absolute():
            out = paths.captures_dir() / out
        out.parent.mkdir(parents=True, exist_ok=True)
        argv += ["--record", str(out)]
    if args:
        argv += args.split()

    proc = shell.spawn(argv)
    if proc is None:
        return {"ok": False, "detail": "无法启动 scrcpy", "commands": []}
    return {
        "ok": True,
        "detail": "已启动 scrcpy 镜像%s" % ("（同时录到 %s）" % record_to if record_to else ""),
        "command": " ".join(argv),
        "commands": [" ".join(argv)],
    }
