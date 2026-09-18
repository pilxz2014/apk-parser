# -*- coding: utf-8 -*-
"""设备与应用的 dataclass。"""

from dataclasses import dataclass, field
from typing import List, Optional


def sanitize_serial(serial: str) -> str:
    """序列号 → 安全文件名（TCP 设备带冒号，Windows 上不能做文件名）。"""
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in (serial or "nodevice"))


@dataclass
class Device:
    """一台 adb 设备。"""

    serial: str = ""
    state: str = "unknown"          # device / offline / unauthorized / no permissions
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""
    android_version: str = ""
    sdk: int = 0
    abi: str = ""
    resolution: str = ""
    density: str = ""
    brand: str = ""
    is_wireless: bool = False

    @property
    def online(self) -> bool:
        return self.state == "device"

    @property
    def title(self) -> str:
        name = self.model or self.device or self.serial
        return "%s (%s)" % (name, self.serial) if self.model else name

    @property
    def state_text(self) -> str:
        return {
            "device": "在线",
            "offline": "离线",
            "unauthorized": "未授权（请在手机上点「允许」）",
            "no permissions": "无权限（检查 udev / 驱动）",
            "connecting": "连接中",
        }.get(self.state, self.state)

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "state": self.state,
            "stateText": self.state_text,
            "online": self.online,
            "model": self.model,
            "product": self.product,
            "device": self.device,
            "brand": self.brand,
            "androidVersion": self.android_version,
            "sdk": self.sdk,
            "abi": self.abi,
            "resolution": self.resolution,
            "density": self.density,
            "wireless": self.is_wireless,
            "title": self.title,
        }


@dataclass
class AppInfo:
    """一台设备上的一个已安装应用。"""

    package: str = ""
    version_code: int = 0
    version_name: str = ""
    apk_path: str = ""
    is_system: bool = False
    uid: int = 0
    label: str = ""                 # 应用名，需解析 APK 才有（懒加载）
    abi: str = ""
    min_sdk: int = 0
    target_sdk: int = 0
    first_install: str = ""
    last_update: str = ""
    installer: str = ""
    data_dir: str = ""
    enabled: bool = True
    signature_identity: str = ""    # 快路径签名标识（可选）

    @property
    def short_name(self) -> str:
        """没有 label 时的显示名（包名末段）。"""
        return self.label or (self.package.rsplit(".", 1)[-1] if self.package else "")

    def matches(self, kw: str) -> bool:
        """模糊匹配：包名或应用名包含关键字（空格分词 AND）。"""
        if not kw:
            return True
        hay = ("%s %s" % (self.package, self.label)).lower()
        return all(tok in hay for tok in kw.lower().split())

    def to_dict(self) -> dict:
        return {
            "package": self.package,
            "versionCode": self.version_code,
            "versionName": self.version_name,
            "apkPath": self.apk_path,
            "system": self.is_system,
            "uid": self.uid,
            "label": self.label,
            "shortName": self.short_name,
            "abi": self.abi,
            "minSdk": self.min_sdk,
            "targetSdk": self.target_sdk,
            "firstInstall": self.first_install,
            "lastUpdate": self.last_update,
            "installer": self.installer,
            "dataDir": self.data_dir,
            "enabled": self.enabled,
            "signatureIdentity": self.signature_identity,
        }
