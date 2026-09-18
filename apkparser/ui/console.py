# -*- coding: utf-8 -*-
"""终端输出渲染（ANSI 彩色，三平台兼容）。

控制台窗口是"双击安装"的载体，所以这里的重点是：
  · 颜色要能正常显示（Windows 需先开 VT）
  · 中文不炸（入口已把 stdout 改成 UTF-8）
  · 关键信息（成功/失败/下一步）要一眼看到
"""

import os
import sys
from typing import Iterable, List, Sequence

from ..platform import paths

_USE_COLOR = True


def _enable_vt() -> None:
    """Windows 10+ 打开虚拟终端处理，让 ANSI 转义生效。"""
    if not paths.IS_WINDOWS:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def init(force: bool = False) -> None:
    """初始化（入口处调一次）。"""
    global _USE_COLOR
    if os.environ.get("NO_COLOR"):
        _USE_COLOR = False
    if not sys.stdout.isatty() and not force:
        _USE_COLOR = False
    _enable_vt()


def style(text: str, *codes: str) -> str:
    if not _USE_COLOR or not codes:
        return text
    seq = ";".join(codes)
    return "\x1b[%sm%s\x1b[0m" % (seq, text)


def bold(t: str) -> str:
    return style(t, "1")


def dim(t: str) -> str:
    return style(t, "2")


def red(t: str) -> str:
    return style(t, "31")


def green(t: str) -> str:
    return style(t, "32")


def yellow(t: str) -> str:
    return style(t, "33")


def blue(t: str) -> str:
    return style(t, "34")


def cyan(t: str) -> str:
    return style(t, "36")


def grey(t: str) -> str:
    return style(t, "90")


# ---------------------------------------------------------------- 语义化输出

def info(msg: str) -> None:
    print("%s %s" % (blue("ℹ"), msg))


def ok(msg: str) -> None:
    print("%s %s" % (green("✔"), msg))


def warn(msg: str) -> None:
    print("%s %s" % (yellow("⚠"), msg))


def error(msg: str) -> None:
    print("%s %s" % (red("✖"), msg), file=sys.stderr)


def step(msg: str) -> None:
    print("%s %s" % (cyan("▸"), msg))


def hint(msg: str) -> None:
    """给"下一步怎么办"的提示。"""
    print("  %s" % grey(msg))


def cmd_line(argv: Sequence[str]) -> str:
    """回显将要执行的命令（"不猜命令"原则）。"""
    from ..platform.shell import quote_for_display
    return " ".join(quote_for_display(str(a)) for a in argv)


def kv(rows: Iterable) -> None:
    """对齐的 key-value 列表。"""
    rows = [(str(k), str(v)) for k, v in rows]
    if not rows:
        return
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        print("  %s  %s" % (grey(k.ljust(width)), v))


def table(headers: Sequence[str], rows: Sequence[Sequence[str]],
          max_widths: Sequence[int] = None) -> None:
    """极简表格（自动列宽，超宽截断）。"""
    rows = [[str(c) for c in r] for r in rows]
    if not rows:
        return
    ncol = len(headers)
    widths = [len(str(h)) for h in headers]
    for r in rows:
        for i in range(min(ncol, len(r))):
            w = _disp_len(r[i])
            if max_widths and i < len(max_widths):
                w = min(w, max_widths[i])
            widths[i] = max(widths[i], w)

    head = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(grey(head))
    print(grey("─" * _disp_len(head)))
    for r in rows:
        cells = []
        for i in range(ncol):
            v = r[i] if i < len(r) else ""
            limit = max_widths[i] if max_widths and i < len(max_widths) else None
            if limit and len(v) > limit:
                v = v[:limit - 1] + "…"
            cells.append(_pad(v, widths[i]))
        print("  ".join(cells))


def _disp_len(s: str) -> int:
    """粗略的显示宽度（CJK 算 2）。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_len(s))


def banner(title: str, subtitle: str = "") -> None:
    print()
    print(bold("  " + title))
    if subtitle:
        print("  " + grey(subtitle))
    print(grey("  " + "─" * 56))
