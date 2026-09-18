# -*- coding: utf-8 -*-
"""安装域：预检、执行、失败中文化。"""

from . import errors, installer, precheck
from .errors import explain as explain_error
from .installer import install, install_multi, install_one
from .precheck import precheck as run_precheck

__all__ = ["installer", "precheck", "errors", "install", "install_one",
           "install_multi", "run_precheck", "explain_error"]
