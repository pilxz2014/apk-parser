#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""APK Parser Toolkit —— 唯一 CLI 入口。

用法：
    python apktool.py sys doctor
    python apktool.py apk info app.apk --json
    python apktool.py web start

脚本化调用时请加 `--json`（稳定契约，只增字段不删）。
"""

import os
import sys


def _bootstrap():
    """把项目根塞进 sys.path，让脚本无需安装即可运行；并修好 stdout 编码。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    # Windows 控制台默认 GBK，输出非 GBK 字符会直接崩掉进程
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
