# -*- coding: utf-8 -*-
"""核心能力层：apk / device / launch / capture / install / files。

`core/` 的约束：
  · 不 import web / ui
  · 不出现 `if sys.platform == ...`（平台差异走 `apkparser.platform`）
  · 不直接 subprocess（外部命令走 `apkparser.platform.shell.run`）
  · 函数尽量纯、IO 边界清晰、返回 dataclass，便于单测
"""

from . import apk, capture, device, files, install, launch  # noqa: F401

__all__ = ["apk", "device", "launch", "capture", "install", "files"]
