#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打包版 CLI 入口（PyInstaller 的另一个分析起点，console 子系统）。

为什么要有它：发行包里的右键菜单脚本（`scripts/apk-*.cmd`）不能再靠
`python apktool.py ...` —— 绿色包既没有 Python，也没有源码树。
所以打包时把 `apk.cmd` 改成调用 exe 同级的 `apkcli.exe`，这个 exe 就是
CLI 本体，和托盘宿主 `APKConsole.exe` 共用同一份 `_internal`。

只做转发，逻辑都在 `apkparser/cli.py`。唯一自己干的事是 `apktool.py`
里那段 `_bootstrap()` 的等价物：Windows 控制台默认 GBK，`console.ok`
的 `✔` / 中文输出会当场 UnicodeEncodeError（实测 `web stop` 在 GBK
窗口里停是停掉了、报错也是真报）。调用方脚本（apk-*.cmd / stop.bat）
配合 `chcp 65001` 使用，显示才不乱码。
"""

import os
import sys


def _bootstrap() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # py3.7+
        except Exception:
            pass
    os.environ.setdefault("PYTHONUTF8", "1")


_bootstrap()

from apkparser.cli import main  # noqa: E402

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
