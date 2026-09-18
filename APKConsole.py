#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打包版入口（PyInstaller 的 analyze 起点）。

放根目录、用绝对导入的原因：PyInstaller 分析脚本时只把**脚本所在目录**
塞进 sys.path —— 入口要是放在 apkparser/ 里，`import apkparser` 就断了。
托盘逻辑本体在 `apkparser/tray.py`，这里只做转发。
"""

import sys

from apkparser.tray import main

if __name__ == "__main__":
    sys.exit(main())
