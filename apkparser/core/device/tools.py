# -*- coding: utf-8 -*-
"""设备工具集：清数据、改 DPI、旋转、无线连接……

每个函数都返回统一结构 `{ok, detail, commands, ...}`，
`commands` 里是**真实执行过的命令**（对应"不猜命令"原则）。
"""

import re
import time
from typing import List, Optional

from . import adb as adb_mod


def _res(args: List[str], serial: str, timeout: float = 30.0, expect: str = "") -> dict:
    r = adb_mod.run(args, serial=serial, timeout=timeout)
    cmd = adb_mod.command_preview(args, serial)
    out = (r.out or "").strip()
    ok = r.ok and (not expect or expect.lower() in out.lower())
    detail = out.splitlines()[-1] if out else (r.err.strip() or ("完成" if r.ok else "失败"))
    return {"ok": bool(ok), "detail": detail, "stdout": out[:1500],
            "stderr": (r.err or "")[:500], "commands": [cmd]}


# ---------------------------------------------------------------- 基础操作

def stop(package: str, device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    res = _res(["shell", "am", "force-stop", package], serial)
    res["detail"] = "已强制停止 %s" % package if res["ok"] else res["detail"]
    return res


def clear(package: str, device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    res = _res(["shell", "pm", "clear", package], serial, expect="Success")
    res["detail"] = "已清除 %s 的数据" % package if res["ok"] else res["detail"]
    return res


def uninstall(package: str, device: str = "", keep_data: bool = False) -> dict:
    serial = adb_mod.pick_device(device)
    args = ["uninstall"] + (["-k"] if keep_data else []) + [package]
    res = _res(args, serial, timeout=90, expect="Success")
    res["detail"] = "已卸载 %s%s" % (package, "（保留数据）" if keep_data else "") \
        if res["ok"] else res["detail"]
    if res["ok"]:
        try:
            from . import signature as sig_mod
            sig_mod.invalidate(package)
        except Exception:
            pass
    return res


def enable(package: str, device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    return _res(["shell", "pm", "enable", package], serial)


def disable(package: str, device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    return _res(["shell", "pm", "disable-user", "--user", "0", package], serial)


# ---------------------------------------------------------------- 显示

def dpi(value: str = "", device: str = "") -> dict:
    """查看 / 修改屏幕密度。不传值则只读。"""
    serial = adb_mod.pick_device(device)
    if not value:
        return _res(["shell", "wm", "density"], serial)
    if value == "reset":
        return _res(["shell", "wm", "density", "reset"], serial)
    return _res(["shell", "wm", "density", value], serial)


def screen_size(value: str = "", device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    if not value:
        return _res(["shell", "wm", "size"], serial)
    if value == "reset":
        return _res(["shell", "wm", "size", "reset"], serial)
    return _res(["shell", "wm", "size", value], serial)


def rotate(mode: str = "auto", device: str = "") -> dict:
    """开启/关闭自动旋转，或强制横竖屏。"""
    serial = adb_mod.pick_device(device)
    if mode in ("auto", "on", "off"):
        val = {"auto": "1", "on": "1", "off": "0"}[mode]
        return _res(["shell", "settings", "put", "system", "accelerometer_rotation", val], serial)
    # 强制方向：0=自然 1=横 2=竖 3=反向横
    mapping = {"portrait": "0", "landscape": "1", "reverse-portrait": "2", "reverse-landscape": "3"}
    v = mapping.get(mode, "0")
    a = _res(["shell", "settings", "put", "system", "accelerometer_rotation", "0"], serial)
    b = _res(["shell", "settings", "put", "system", "user_rotation", v], serial)
    return {"ok": a["ok"] and b["ok"], "detail": "已设为 %s" % mode,
            "commands": a["commands"] + b["commands"]}


def airplane(on: bool = True, device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    val = "1" if on else "0"
    return _res(["shell", "settings", "put", "global", "airplane_mode_on", val], serial)


def wake(device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    return _res(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], serial)


def sleep(device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    return _res(["shell", "input", "keyevent", "KEYCODE_SLEEP"], serial)


def input_text(text: str, device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    safe = text.replace(" ", "%s")
    return _res(["shell", "input", "text", safe], serial)


def input_key(code: str, device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    return _res(["shell", "input", "keyevent", str(code)], serial)


def reboot(mode: str = "", device: str = "") -> dict:
    """重启（可进 recovery/bootloader）。"""
    serial = adb_mod.pick_device(device)
    args = ["reboot"] + ([mode] if mode else [])
    r = adb_mod.run(args, serial=serial, timeout=20)
    cmd = adb_mod.command_preview(args, serial)
    return {"ok": r.rc == 0, "detail": "已发送重启指令（%s）" % (mode or "正常重启"),
            "commands": [cmd]}


def screen_record_hint(device: str = "") -> dict:
    return {"ok": True, "detail": "录屏请用 capture record", "commands": []}


# ---------------------------------------------------------------- 无线 adb

def wifi_connect(target: str, device: str = "", port: int = 5555,
                 pair_code: str = "") -> dict:
    """无线连接。

    两种模式：
      · Android 11+ 无线调试 → 传 pair_code，走 `adb pair`
      · 传统 tcpip      → 先 `adb tcpip <port>`，再 `adb connect ip:port`
    """
    if not target:
        return {"ok": False, "detail": "请提供 ip:port", "commands": []}

    commands: List[str] = []

    if pair_code:
        args = ["pair", target, pair_code]
        r = adb_mod.run(args, timeout=60)
        commands.append(adb_mod.command_preview(args))
        ok = "Successfully paired" in (r.out or "") or r.rc == 0
        # 配对成功后 Android 会另给一个连接端口，用同一 host:port 兜底连一次
        if ok:
            args2 = ["connect", target]
            r2 = adb_mod.run(args2, timeout=60)
            commands.append(adb_mod.command_preview(args2))
            ok = "connected" in (r2.out or "").lower() or r2.rc == 0
        return {"ok": ok, "detail": (r.out or r.err or "").strip().splitlines()[-1]
                if (r.out or r.err) else ("配对成功" if ok else "配对失败"),
                "commands": commands}

    # 传统方式：若给了 USB 设备就先切 tcpip
    if device:
        try:
            serial = adb_mod.pick_device(device)
            args0 = ["tcpip", str(port)]
            r0 = adb_mod.run(args0, serial=serial, timeout=30)
            commands.append(adb_mod.command_preview(args0, serial))
            if "restarting in TCP mode" not in (r0.out or "") and r0.rc != 0:
                return {"ok": False, "detail": (r0.err or r0.out or "切换 TCP 模式失败").strip(),
                        "commands": commands}
            time.sleep(1.5)
        except adb_mod.AdbError:
            pass

    host = target if ":" in target else "%s:%d" % (target, port)
    args = ["connect", host]
    r = adb_mod.run(args, timeout=30)
    commands.append(adb_mod.command_preview(args))
    out = (r.out or "").strip()
    ok = "connected" in out.lower() and "cannot" not in out.lower()
    return {"ok": bool(ok), "detail": out or (r.err or "").strip() or "连接失败",
            "commands": commands}


def wifi_disconnect(device: str = "", host: str = "") -> dict:
    args = ["disconnect"] + ([host] if host else [])
    r = adb_mod.run(args, timeout=20)
    return {"ok": r.rc == 0, "detail": (r.out or r.err or "已断开").strip(),
            "commands": [adb_mod.command_preview(args)]}


def wifi_status(device: str = "") -> dict:
    """看设备当前的 IP（判断能否走无线）。"""
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "detail": str(e), "commands": []}
    args = ["shell", "ip", "-f", "inet", "addr", "show", "wlan0"]
    r = adb_mod.run(args, serial=serial, timeout=15)
    ips = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", r.out or "")
    return {"ok": bool(ips), "detail": "、".join(ips) or "没有取到 wlan0 的 IP",
            "ips": ips, "commands": [adb_mod.command_preview(args, serial)]}


# ---------------------------------------------------------------- 存储 / 网络

def storage(device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    return _res(["shell", "df", "-h", "/data"], serial)


def forward_list(device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    return _res(["forward", "--list"], serial)


def forward_add(local: str, remote: str, device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    return _res(["forward", local, remote], serial)


def forward_remove(local: str, device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    return _res(["forward", "--remove", local], serial)


def kill_server() -> dict:
    return adb_mod.adb_server_restart()
