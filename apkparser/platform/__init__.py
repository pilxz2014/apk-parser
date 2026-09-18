# -*- coding: utf-8 -*-
"""平台层 —— 唯一允许出现平台差异的地方。

`core/` 通过本包拿路径、跑命令、探测工具、做桌面集成。
"""

from . import finders, paths, shell  # noqa: F401

__all__ = ["paths", "shell", "finders"]
