# -*- coding: utf-8 -*-
"""已安装应用的签名 —— 免 root 方案。

设计依据来自真机实测（Z21 / Android 13）：
  · `dumpsys package` 在多数 ROM 上**不输出证书**（只有方案号 + 签名标识哈希）
  · `adb pull /data/app/.../base.apk` **免 root 可用且很快**（实测 35 MB/s）

所以定成两条路，一快一全：

  ① 快路径  `dumpsys package`            ~0.3s  → 签名方案 + 签名标识（够判同）
  ② 完整路径 `pm path` → `adb pull` → 本地解析 1~3s → 证书 DN / SHA-256 / 有效期 / 算法

**关键取舍**：两条路都不通时**明确停手** —— 不申请 root、不显示占位值，
因为"拉不到"和"包本来就没签名"在界面上必须区分得开。
"""

import json
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ...platform import paths
from . import adb as adb_mod
from .model import sanitize_serial

# dumpsys 里的签名字段
_RE_SIGNING_VERSION = re.compile(r"apkSigningVersion=(\d+)")
_RE_SIGNATURES = re.compile(r"signatures\s*=\s*PackageSignatures\{[^[]*\[([^\]]*)\]")
_RE_PAST_SIGS = re.compile(r"past signatures:\s*\[([^\]]*)\]")
_RE_SIGNER_DN = re.compile(r"Signer #(\d+) certificate DN:\s*(.+)")
_RE_SIGNER_SHA256 = re.compile(r"Signer #(\d+) certificate SHA-256 digest:\s*([0-9a-fA-F]+)")

Progress = Optional[Callable[[str, str], None]]


# ---------------------------------------------------------------- 缓存

def _cache_path(serial: str, package: str, version_code: int) -> Path:
    name = "%s__%s__%s.json" % (sanitize_serial(serial), _safe(package), version_code or 0)
    return paths.signature_cache_dir() / name


def _safe(s: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in s)


def _load_cache(serial: str, package: str, version_code: int) -> Optional[dict]:
    p = _cache_path(serial, package, version_code)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data["source"] = "cache"
        data["cachedAt"] = p.stat().st_mtime
        data["cachedAgo"] = "%.0f 分钟前" % ((time.time() - p.stat().st_mtime) / 60.0)
        return data
    except Exception:
        return None


def _save_cache(serial: str, package: str, version_code: int, data: dict) -> None:
    try:
        p = _cache_path(serial, package, version_code)
        payload = {k: v for k, v in data.items()
                   if k not in ("source", "cachedAt", "cachedAgo")}
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def invalidate(package: str = "") -> int:
    """让缓存失效（安装操作后调用）。返回清掉的条数。"""
    n = 0
    root = paths.signature_cache_dir()
    if not root.is_dir():
        return 0
    for f in root.glob("*.json"):
        if not package or ("__%s__" % _safe(package)) in f.name:
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


# ---------------------------------------------------------------- 快路径

def fast_signature(package: str, serial: str) -> dict:
    """`dumpsys package` 抽签名摘要。只有方案 + 标识，不含证书详情。"""
    args = ["shell", "dumpsys", "package", package]
    r = adb_mod.run(args, serial=serial, timeout=40)
    commands = [adb_mod.command_preview(args, serial)]
    text = r.out or ""

    out = {
        "ok": False,
        "package": package,
        "schemes": [],
        "identity": "",
        "pastSignatures": [],
        "certificates": [],
        "full": False,
        "commands": commands,
    }

    if not text.strip():
        out["reason"] = "dumpsys 没有返回内容（设备可能未授权或该 ROM 限制 dumpsys）"
        return out

    m = _RE_SIGNING_VERSION.search(text)
    if m:
        v = int(m.group(1))
        if 1 <= v <= 4:
            out["schemes"].append("v%d" % min(v, 3))
        out["signingVersion"] = v

    m = _RE_SIGNATURES.search(text)
    if m and m.group(1).strip():
        first = m.group(1).split(",")[0].strip()
        out["identity"] = first[:8].lower() if first else ""
        out["signatureHashes"] = [s.strip() for s in m.group(1).split(",") if s.strip()]

    m = _RE_PAST_SIGS.search(text)
    if m and m.group(1).strip():
        out["pastSignatures"] = [s.strip() for s in m.group(1).split(",") if s.strip()]

    # 少数 ROM（如部分 AOSP 定制）会直接吐 Signer 块 —— 有就直接用，省一次 pull
    dns = dict(_RE_SIGNER_DN.findall(text))
    fps = dict(_RE_SIGNER_SHA256.findall(text))
    if fps:
        for idx in sorted(fps.keys(), key=lambda x: int(x)):
            out["certificates"].append({
                "index": int(idx),
                "subject": dns.get(idx, ""),
                "sha256": _fmt_fp(fps[idx]),
                "source": "dumpsys",
            })
        out["full"] = True

    if out["identity"] or out["certificates"] or out["schemes"]:
        out["ok"] = True
    else:
        out["reason"] = "该 ROM 的 dumpsys 输出里没有签名字段"
    return out


def _fmt_fp(hexstr: str) -> str:
    h = re.sub(r"[^0-9a-fA-F]", "", hexstr).upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


# ---------------------------------------------------------------- 完整路径

def pull_base_apk(package: str, serial: str, version_code: int = 0,
                  progress: Progress = None) -> dict:
    """把设备上的 base.apk 拉到本地缓存，并返回本地路径。

    只拉 base.apk：签名只存在于 base，split 不参与签名判定。
    """
    def say(stage, msg):
        if progress:
            try:
                progress(stage, msg)
            except Exception:
                pass

    from . import apps as apps_mod

    say("locate", "定位 APK 路径…")
    remote_paths, commands = apps_mod.package_paths(package, serial)
    if not remote_paths:
        return {"ok": False, "reason": "取不到 APK 路径（应用可能已卸载或受系统保护）",
                "commands": commands}

    remote = remote_paths[0]
    local_dir = paths.apk_cache_dir() / sanitize_serial(serial)
    local_dir.mkdir(parents=True, exist_ok=True)
    local = local_dir / ("%s__%s.apk" % (_safe(package), version_code or 0))
    commands.append("# 目标：%s" % local)

    # 已有同版本缓存 → 直接用，不重复下载
    if local.is_file() and local.stat().st_size > 0:
        say("cached", "本地已有该版本的包，跳过下载")
        return {"ok": True, "path": str(local), "remote": remote, "cached": True,
                "commands": commands}

    say("pull", "从设备拉取 base.apk…")
    t0 = time.time()
    args = ["pull", remote, str(local)]
    r = adb_mod.run(args, serial=serial, timeout=600)
    commands.append(adb_mod.command_preview(args, serial))

    if not r.ok or not local.is_file() or local.stat().st_size == 0:
        detail = (r.err or r.out or "").strip().splitlines()
        return {"ok": False,
                "reason": "拉取 base.apk 失败：%s" % (detail[-1] if detail else "未知原因"),
                "commands": commands}

    size = local.stat().st_size
    return {
        "ok": True, "path": str(local), "remote": remote, "cached": False,
        "bytes": size, "ms": int((time.time() - t0) * 1000),
        "speed": "%.1f MB/s" % (size / 1048576.0 / max(0.001, time.time() - t0)),
        "commands": commands,
    }


def full_signature(package: str, serial: str, version_code: int = 0,
                   progress: Progress = None) -> dict:
    """完整路径：拉包 + 本地解析（与本机 APK 用同一份 cert 代码）。"""
    pulled = pull_base_apk(package, serial, version_code, progress)
    if not pulled.get("ok"):
        return {"ok": False, "full": False, "reason": pulled.get("reason", "拉取失败"),
                "commands": pulled.get("commands", [])}

    def say(stage, msg):
        if progress:
            try:
                progress(stage, msg)
            except Exception:
                pass

    say("parse", "本地解析证书…")
    from ..apk.cert import analyze as analyze_cert
    from ..apk.reader import ApkReader, ApkError

    try:
        with ApkReader(pulled["path"]) as reader:
            sig = analyze_cert(reader, verify_with_apksigner=False)
    except ApkError as e:
        return {"ok": False, "full": False, "reason": "解析拉回的包失败：%s" % e,
                "commands": pulled.get("commands", [])}

    if not sig.ok:
        return {"ok": False, "full": False,
                "reason": sig.error or "解析出的包没有任何签名",
                "commands": pulled.get("commands", [])}

    return {
        "ok": True,
        "full": True,
        "schemes": sig.schemes,
        "identity": sig.digest_identity,
        "certificates": [c.to_dict() for c in sig.certificates],
        "isDebug": sig.is_debug,
        "warnings": sig.warnings,
        "apkPath": pulled["path"],
        "apkBytes": pulled.get("bytes", 0),
        "cached": pulled.get("cached", False),
        "pullMs": pulled.get("ms"),
        "commands": pulled.get("commands", []),
    }


# ---------------------------------------------------------------- 统一入口

def get_signature(package: str, device: str = "", fast: bool = False,
                  full: bool = True, refresh: bool = False,
                  progress: Progress = None) -> dict:
    """已安装应用签名的统一入口（渐进式：先快后全）。

    :param fast: 只走快路径（约 0.3s），返回方案 + 标识
    :param full: 是否继续走完整路径补全证书
    :param refresh: 忽略缓存
    """
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "package": package, "reason": str(e), "commands": []}

    # 先拿版本号，缓存键要靠它（版本变了缓存自然失效）
    version_code = 0
    try:
        from . import apps as apps_mod
        detail = apps_mod.app_detail(package, serial)
        version_code = int(detail.get("versionCode") or 0)
    except Exception:
        pass

    if not refresh:
        cached = _load_cache(serial, package, version_code)
        if cached and (not full or cached.get("full") or fast):
            return cached

    result: Dict = {"package": package, "serial": serial,
                    "versionCode": version_code, "fast": None, "full": False}

    # ---- ① 快路径 ----
    f = fast_signature(package, serial)
    result["fast"] = f
    result["commands"] = list(f.get("commands", []))
    result["ok"] = bool(f.get("ok"))
    result["schemes"] = f.get("schemes", [])
    result["identity"] = f.get("identity", "")
    result["certificates"] = f.get("certificates", [])
    result["source"] = "dumpsys"
    result["full"] = bool(f.get("full"))
    if f.get("reason"):
        result["fastReason"] = f["reason"]

    if fast and result["ok"]:
        _save_cache(serial, package, version_code, result)
        return result

    # ---- ② 完整路径 ----
    if full and not result["full"]:
        got = full_signature(package, serial, version_code, progress)
        result["commands"] += got.get("commands", [])
        if got.get("ok"):
            result.update({
                "ok": True, "full": True, "source": "device",
                "schemes": got.get("schemes", []),
                "identity": got.get("identity", result["identity"]),
                "certificates": got.get("certificates", []),
                "isDebug": got.get("isDebug", False),
                "warnings": got.get("warnings", []),
                "apkPath": got.get("apkPath"),
                "apkBytes": got.get("apkBytes", 0),
                "pullMs": got.get("pullMs"),
                "fromCacheApk": got.get("cached", False),
            })
            _save_cache(serial, package, version_code, result)
            return result
        result["fullReason"] = got.get("reason", "完整路径失败")

    if result["ok"]:
        _save_cache(serial, package, version_code, result)
        return result

    # ---- 两条路都不通：明确停手 ----
    # 这里刻意不申请 root、不填占位值 —— 让界面能区分"拿不到"与"本来没有签名"
    result["ok"] = False
    result["reason"] = "该设备不支持免 root 读取已安装应用签名"
    result["detail"] = "；".join(x for x in (result.get("fastReason"), result.get("fullReason")) if x)
    result["hint"] = ("可以改用「导出 APK」把包装拉下来本地看签名，"
                      "或对本机 APK 文件直接用右键「解析 APK 信息」。")
    return result


# ---------------------------------------------------------------- 比对

def compare_with_apk(package: str, apk_path: str, device: str = "") -> dict:
    """设备上已安装应用的签名 ⟷ 本机 APK 文件的签名。"""
    from ..apk.cert import analyze as analyze_cert, compare as compare_sig
    from ..apk.reader import ApkError, ApkReader

    dev_sig = get_signature(package, device=device, fast=False, full=True)
    if not dev_sig.get("ok") or not dev_sig.get("certificates"):
        return {
            "verdict": "undetermined",
            "reason": dev_sig.get("reason") or "设备侧签名取不到，无法比对",
            "device": dev_sig,
            "apk": None,
        }

    try:
        with ApkReader(apk_path) as reader:
            local_sig = analyze_cert(reader)
    except ApkError as e:
        return {"verdict": "undetermined", "reason": "本机 APK 解析失败：%s" % e,
                "device": dev_sig, "apk": None}

    # 用同一套比对逻辑：构造两个 SignatureInfo
    from ..apk.cert import Certificate, SignatureInfo

    dev_info = SignatureInfo(schemes=dev_sig.get("schemes", []))
    for c in dev_sig.get("certificates", []):
        dev_info.certificates.append(Certificate(
            sha256=c.get("sha256", ""), sha1=c.get("sha1", ""),
            subject=c.get("subject", ""), subject_parts=c.get("subject_parts", {}),
            not_before=c.get("notBefore", ""), not_after=c.get("notAfter", ""),
        ))

    res = compare_sig(dev_info, local_sig)
    res["device"] = {"package": package, "identity": dev_sig.get("identity"),
                     "schemes": dev_sig.get("schemes")}
    res["apk"] = {"path": apk_path, "package": local_sig.to_dict().get("package", ""),
                  "identity": local_sig.digest_identity}
    return res
