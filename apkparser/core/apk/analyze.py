# -*- coding: utf-8 -*-
"""APK 分析：体积构成、dex 方法数、双包对比。

都基于已经解析好的 `ApkInfo` 或原始字节，不做重复 IO。
"""

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .info import ApkInfo


# ---------------------------------------------------------------- dex

def dex_method_count(reader) -> Dict[str, int]:
    """统计每个 dex 的方法数（dex header offset 88 = method_ids_size）。"""
    out: Dict[str, int] = {}
    for name in reader.dex_names():
        blob = reader.read(name)
        if not blob or len(blob) < 112:
            continue
        if not blob.startswith(b"dex\n"):
            out[name] = 0
            continue
        try:
            out[name] = struct.unpack_from("<I", blob, 88)[0]
        except struct.error:
            out[name] = 0
    return out


def dex_limit_risk(total_methods: int) -> str:
    """方法数是否逼近 64K 限制。"""
    if total_methods > 65536:
        return "over"      # 已超（必然是 multidex 或用了其他方案）
    if total_methods > 60000:
        return "near"      # 逼近，新增方法要小心
    return "ok"


# ---------------------------------------------------------------- 体积

def size_report(info: ApkInfo) -> dict:
    """体积构成报告（含"可优化项"提示）。"""
    total = sum(info.size_breakdown.values()) or 1
    items = []
    for name, size in info.size_breakdown.items():
        items.append({
            "name": name,
            "bytes": size,
            "ratio": round(size / total, 4),
            "percent": "%.1f%%" % (size * 100.0 / total),
        })

    hints: List[str] = []
    bd = info.size_breakdown
    if bd.get("lib", 0) and len(info.abis) > 1:
        hints.append("包含 %d 个 ABI（%s），只留 arm64-v8a 通常能省一半 native 体积"
                     % (len(info.abis), "、".join(info.abis)))
    if bd.get("assets", 0) > 5 * 1024 * 1024:
        hints.append("assets 超过 5MB，检查是否有可外置的资源")
    if bd.get("dex", 0) > 8 * 1024 * 1024:
        hints.append("dex 超过 8MB，考虑 R8 全量优化 / 移除未用代码")
    if len(info.dex_names) > 1:
        hints.append("有 %d 个 dex（multidex），确认 5.x 设备兼容性" % len(info.dex_names))
    res = bd.get("res", 0)
    if res > 10 * 1024 * 1024:
        hints.append("res 超过 10MB，考虑开启资源混淆与无用资源移除")

    return {
        "fileSize": info.file_size,
        "rawSize": total,
        "items": items,
        "hints": hints,
    }


# ---------------------------------------------------------------- 双包对比

FIELD_LABELS = [
    ("package", "包名"),
    ("app_label", "应用名"),
    ("version_name", "版本名"),
    ("version_code", "版本号"),
    ("min_sdk", "minSdk"),
    ("target_sdk", "targetSdk"),
    ("file_size", "文件大小"),
    ("main_activity", "主 Activity"),
    ("channel", "渠道"),
    ("debuggable", "debuggable"),
]


@dataclass
class DiffRow:
    key: str
    label: str
    a: str
    b: str
    same: bool

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "a": self.a, "b": self.b, "same": self.same}


def diff_apks(a: ApkInfo, b: ApkInfo) -> dict:
    """对比两个 APK，返回字段差异 + 集合差异 + 签名结论。"""
    from .cert import compare as compare_sig

    rows: List[DiffRow] = []
    for key, label in FIELD_LABELS:
        va, vb = getattr(a, key, ""), getattr(b, key, "")
        if key == "file_size":
            fmt = lambda n: _human(n)
            sa, sb = fmt(va), fmt(vb)
        else:
            sa, sb = str(va), str(vb)
        rows.append(DiffRow(key, label, sa, sb, va == vb))

    perms = _set_diff(a.permissions, b.permissions)
    comps = {
        ctype: _set_diff(
            [c.name for c in a.components(ctype)],
            [c.name for c in b.components(ctype)],
        )
        for ctype in ("activity", "service", "receiver", "provider")
    }

    sig_verdict = None
    if a.signature and b.signature:
        cmp_res = compare_sig(a.signature, b.signature)
        sig_verdict = {"verdict": cmp_res["verdict"], "reason": cmp_res["reason"]}

    size_delta = {
        "bytes": b.file_size - a.file_size,
        "text": _signed(b.file_size - a.file_size),
    }

    return {
        "a": {"path": a.path, "package": a.package, "versionName": a.version_name,
              "versionCode": a.version_code, "fileName": a.file_name},
        "b": {"path": b.path, "package": b.package, "versionName": b.version_name,
              "versionCode": b.version_code, "fileName": b.file_name},
        "fields": [r.to_dict() for r in rows],
        "permissions": perms,
        "components": comps,
        "signature": sig_verdict,
        "sizeDelta": size_delta,
        "abis": {"a": a.abis, "b": b.abis},
        "dex": {"a": a.dex_names, "b": b.dex_names},
    }


def _set_diff(xs: List[str], ys: List[str]) -> dict:
    sx, sy = set(xs), set(ys)
    return {
        "onlyA": sorted(sx - sy),
        "onlyB": sorted(sy - sx),
        "common": sorted(sx & sy),
        "countA": len(sx),
        "countB": len(sy),
    }


def _human(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % int(n))
        n /= 1024.0
    return "%d B" % int(n)


def _signed(n: int) -> str:
    sign = "+" if n >= 0 else "-"
    return "%s%s" % (sign, _human(abs(n)))
