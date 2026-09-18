# -*- coding: utf-8 -*-
"""启动域：组件发现、Intent 构造、启动历史。"""

from . import components, history, intent
from .intent import COMMON_ACTIONS, COMMON_FLAGS, FLAGS, Extra, Intent, flag_catalog

__all__ = [
    "Intent", "Extra", "FLAGS", "COMMON_FLAGS", "COMMON_ACTIONS", "flag_catalog",
    "components", "history", "intent",
]
