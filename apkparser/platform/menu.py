# -*- coding: utf-8 -*-
"""系统集成入口 —— 按平台分发。

对上层只暴露 install / uninstall / status 三个动作，
调用方完全不关心底下是注册表、Quick Action 还是 .desktop。
"""

from . import menu_linux, menu_mac, menu_win, paths

import inspect


def impl():
    """当前平台的实现模块。"""
    if paths.IS_WINDOWS:
        return menu_win
    if paths.IS_MAC:
        return menu_mac
    return menu_linux


def supported() -> bool:
    return impl().available()


def install(project_root=None, style=None) -> dict:
    """style（cascade / flat）只在 Windows 实现里有意义，其他平台忽略。

    这里按实现模块的签名决定要不要传 —— 比 try/except TypeError 稳，
    不会把实现里真正的 TypeError 一起咽掉。
    """
    fn = impl().install
    if style and "style" in inspect.signature(fn).parameters:
        return fn(project_root, style=style)
    return fn(project_root)


def uninstall() -> dict:
    return impl().uninstall()


def status() -> dict:
    return impl().status()
