# -*- coding: utf-8 -*-
"""本机域：APK 解析与分析（与 adb 完全解耦，无设备也能用）。"""

from .analyze import dex_limit_risk, dex_method_count, diff_apks, size_report
from .cert import Certificate, SignatureInfo, analyze as analyze_signature, compare as compare_signature
from .info import ApkInfo, Component, IntentFilter, parse_apk, parse_apk_reader
from .locate import locate as locate_file, search_roots
from .reader import ApkError, ApkReader

__all__ = [
    "parse_apk", "parse_apk_reader", "ApkInfo", "Component", "IntentFilter",
    "ApkReader", "ApkError",
    "analyze_signature", "compare_signature", "SignatureInfo", "Certificate",
    "diff_apks", "size_report", "dex_method_count", "dex_limit_risk",
    "locate_file", "search_roots",
]
