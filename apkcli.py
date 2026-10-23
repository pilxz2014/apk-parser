#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打包版 CLI 入口（PyInstaller 的另一个分析起点，console 子系统）。

为什么要有它：发行包里的右键菜单脚本（`scripts/apk-*.cmd`）不能再靠
`python apktool.py ...` —— 绿色包既没有 Python，也没有源码树。
所以打包时把 `apk.cmd` 改成调用 exe 同级的 `apkcli.exe`，这个 exe 就是
CLI 本体，和托盘宿主 `APKConsole.exe` 共用同一份 `_internal`。

只做转发，逻辑都在 `apkparser/cli.py`。
"""

import sys

from apkparser.cli import main

if __name__ == "__main__":
    sys.exit(main())
