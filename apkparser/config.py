# -*- coding: utf-8 -*-
"""配置读写（`~/.apk-parser/config.json`）。

线程安全、写盘原子（先写 .tmp 再 replace），损坏时回退默认值而不是崩。
"""

import json
import os
import threading
from typing import Any, Dict

from .platform import paths

DEFAULTS: Dict[str, Any] = {
    # 外部工具路径：空 = 自动探测
    "adb_path": "",
    "aapt2_path": "",
    "scrcpy_path": "",
    # 主控台
    "bind": "127.0.0.1",
    "port": 5170,
    "open_browser_on_start": True,
    # 目录覆盖：空 = 项目内默认
    "output_dir": "",
    "logs_dir": "",
    # 行为
    "default_device": "",
    "theme": "light",
    "history_limit": 50,
    "signature_auto_full": True,   # 签名先在快路径出摘要后，是否自动走完整路径
    "signature_pull_max_mb": 200,  # 超过此体积不自动 pull（手动可触发）
    "confirm_risk": True,          # 危险操作二次确认
    # 本机 APK：记住最后一次选文件的目录；拖拽定位的额外搜索目录（分号分隔）
    "last_apk_dir": "",
    "locate_dirs": "",
    # 记住最后一次"导出到哪儿 / 扫哪个目录"—— 下次开目录对话框直接落在这儿
    "last_out_dir": "",
    "upload_max_mb": 4096,         # 浏览器上传兜底的单文件上限（MB）
}

_lock = threading.RLock()
_data: Dict[str, Any] = {}
_loaded = False


def _coerce(default: Any, value: Any) -> Any:
    """按默认值类型做温和转换，避免手改配置写错类型导致崩。"""
    if value is None:
        return default
    try:
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "y")
            return bool(value)
        if isinstance(default, int) and not isinstance(value, bool):
            return int(value)
    except (TypeError, ValueError):
        return default
    return value


def load(force: bool = False) -> Dict[str, Any]:
    """读配置（带缓存）。文件不存在 / 损坏都返回默认值。"""
    global _data, _loaded
    with _lock:
        if _loaded and not force:
            return _data

        data = dict(DEFAULTS)
        f = paths.config_file()
        try:
            if f.is_file():
                raw = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        data[k] = _coerce(DEFAULTS.get(k, v), v)
        except Exception:
            # 配置坏了不影响使用：用默认值继续，并把坏文件挪走
            try:
                f.replace(f.with_suffix(".json.bak"))
            except Exception:
                pass

        _data = data
        _loaded = True
        return _data


def save(data: Dict[str, Any]) -> bool:
    """原子写盘。"""
    global _data
    with _lock:
        f = paths.config_file()
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(f))
            _data = dict(data)
            return True
        except Exception:
            return False


def get(key: str, default: Any = None) -> Any:
    with _lock:
        d = load()
        if key in d:
            return d[key]
        return DEFAULTS.get(key, default)


def set_value(key: str, value: Any) -> bool:
    """设一个键并落盘。值会按默认类型转换。"""
    with _lock:
        d = dict(load())
        d[key] = _coerce(DEFAULTS.get(key, value), value)
        return save(d)


def update(patch: Dict[str, Any]) -> bool:
    with _lock:
        d = dict(load())
        for k, v in patch.items():
            d[k] = _coerce(DEFAULTS.get(k, v), v)
        return save(d)


def all_values() -> Dict[str, Any]:
    with _lock:
        return dict(load())


def path() -> str:
    return str(paths.config_file())
