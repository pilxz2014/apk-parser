# -*- coding: utf-8 -*-
"""adb 封装：设备解析、超时、错误翻译。

原则：
  · 所有外部调用统一走 `platform.shell.run`（一处加超时与日志）
  · 每次调用都带超时；超时/失败不抛异常，而是返回可读的 Result
  · 命令本身可以被上层回显（"不猜命令"）
"""

import re
from typing import Dict, List, Optional

from ...platform import finders, paths, shell
from ...platform.shell import Result
from .model import Device

# `adb devices -l` 的一行：
#   AQLCCP5A23403810       device product:xxx model:YYY device:zzz transport_id:1
_DEV_RE = re.compile(
    r"^(?P<serial>\S+)\s+(?P<state>device|offline|unauthorized|no permissions|connecting|recovery|sideload|bootloader)"
    r"(?P<rest>.*)$")


class AdbError(Exception):
    """adb 相关错误（消息面向用户）。"""


# ---------------------------------------------------------------- 基础

def adb_bin():
    p = finders.find_adb()
    if not p:
        raise AdbError(
            "找不到 adb。请安装 Android SDK Platform-Tools，"
            "或在 `sys config set adb_path <路径>` 里指定。")
    return p


def available() -> bool:
    return finders.find_adb() is not None


def run(args: List[str], serial: str = "", timeout: float = 30.0,
        binary: bool = False) -> Result:
    """执行一条 adb 命令（自动加 -s）。

    不动 `adb start-server`，让它按需自启。
    """
    try:
        exe = adb_bin()
    except AdbError as e:
        return Result(["adb"] + list(args), 127, "", str(e), 0.0, exception=str(e))

    argv = [str(exe)]
    if serial:
        argv += ["-s", serial]
    argv += [str(a) for a in args]
    return shell.run(argv, timeout=timeout)


def shell_cmd(command: str, serial: str = "", timeout: float = 30.0) -> Result:
    """`adb shell <command>`。"""
    return run(["shell", command], serial=serial, timeout=timeout)


def command_preview(args: List[str], serial: str = "") -> str:
    """人类可读的命令预览（回显给用户 / 抄进脚本）。"""
    exe = "adb"
    try:
        exe = str(finders.find_adb() or "adb")
    except Exception:
        pass
    parts = [exe]
    if serial:
        parts += ["-s", serial]
    parts += [str(a) for a in args]
    return " ".join(shell.quote_for_display(p) for p in parts)


# ---------------------------------------------------------------- 设备

def list_devices(deep: bool = False, timeout: float = 15.0) -> List[Device]:
    """列出设备。deep=True 时额外查一次 getprop 补全版本/ABI/分辨率。"""
    r = run(["devices", "-l"], timeout=timeout)
    devices: List[Device] = []
    for line in r.lines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        m = _DEV_RE.match(line)
        if not m:
            continue
        d = Device(serial=m.group("serial"), state=m.group("state"))
        rest = m.group("rest") or ""
        for key, attr in (("model", "model"), ("product", "product"),
                          ("device", "device"), ("transport_id", "transport_id")):
            mm = re.search(r"%s:(\S+)" % key, rest)
            if mm:
                setattr(d, attr, mm.group(1))
        d.is_wireless = ":" in d.serial
        devices.append(d)

    if deep:
        for d in devices:
            if d.online:
                _fill_props(d)
    return devices


_PROP_KEYS = {
    "ro.build.version.release": "android_version",
    "ro.build.version.sdk": "sdk",
    "ro.product.cpu.abi": "abi",
    "ro.product.brand": "brand",
    "ro.product.model": "model",
    "ro.product.device": "device",
}


def _fill_props(d: Device) -> None:
    """一次 getprop 拿全部属性（比多次调用快得多）。"""
    r = shell_cmd("getprop", serial=d.serial, timeout=20)
    if not r.ok:
        return
    for line in r.lines():
        m = re.match(r"\[(?P<k>[^\]]+)\]:\s*\[(?P<v>[^\]]*)\]", line.strip())
        if not m:
            continue
        k, v = m.group("k"), m.group("v")
        attr = _PROP_KEYS.get(k)
        if attr and v:
            if attr == "sdk":
                try:
                    d.sdk = int(v)
                except ValueError:
                    pass
            else:
                setattr(d, attr, v)

    # 分辨率
    r2 = shell_cmd("wm size", serial=d.serial, timeout=10)
    m = re.search(r"(\d+x\d+)", r2.text)
    if m:
        d.resolution = m.group(1)
    r3 = shell_cmd("wm density", serial=d.serial, timeout=10)
    m = re.search(r"(\d+)", r3.text.split(":")[-1] if ":" in r3.text else r3.text)
    if m:
        d.density = m.group(1)


def pick_device(serial: str = "") -> str:
    """决定用哪台设备。传了就校验；没传就要求"唯一在线"。"""
    devices = list_devices()
    online = [d for d in devices if d.online]

    if serial:
        for d in devices:
            if d.serial == serial:
                if not d.online:
                    raise AdbError("设备 %s 状态为「%s」，无法操作" % (serial, d.state_text))
                return serial
        raise AdbError("找不到设备 %s。当前：%s" % (
            serial, "、".join(d.serial for d in devices) or "（无）"))

    if not online:
        if devices:
            raise AdbError("设备未就绪：%s" % "；".join(
                "%s → %s" % (d.serial, d.state_text) for d in devices))
        raise AdbError("没有连接任何设备。请插上手机并打开 USB 调试。")

    if len(online) > 1:
        from ... import config
        default = config.get("default_device", "")
        if default and any(d.serial == default for d in online):
            return default
        raise AdbError("检测到 %d 台设备，请用 --device <序列号> 指定：%s" % (
            len(online), "、".join(d.serial for d in online)))

    return online[0].serial


def device_info(serial: str = "") -> dict:
    """设备的详细信息（`device info`）。"""
    try:
        serial = pick_device(serial)
    except AdbError as e:
        return {"error": str(e)}

    d = Device(serial=serial)
    d.state = "device"
    _fill_props(d)

    extra = {}
    for label, cmd in (
        ("uptime", "uptime"),
        ("battery", "dumpsys battery"),
        ("storage", "df /data"),
    ):
        r = shell_cmd(cmd, serial=serial, timeout=12)
        if r.ok:
            extra[label] = r.out.strip()[:400]

    batt = 0
    if "battery" in extra:
        m = re.search(r"level:\s*(\d+)", extra["battery"])
        if m:
            batt = int(m.group(1))

    info = d.to_dict()
    info.update({
        "uptime": extra.get("uptime", "").split("\n")[0],
        "batteryLevel": batt,
        "storage": extra.get("storage", ""),
    })
    return info


def adb_server_restart() -> dict:
    """adb server 卡死时的救命操作。"""
    r1 = run(["kill-server"], timeout=15)
    r2 = run(["start-server"], timeout=25)
    return {
        "ok": r2.rc == 0,
        "detail": "adb server 已重启" if r2.rc == 0 else (r2.err or "重启失败"),
        "commands": [command_preview(["kill-server"]), command_preview(["start-server"])],
    }
