# -*- coding: utf-8 -*-
"""APK 签名解析 —— 零依赖。

**本机 APK 与"从设备拉回的 base.apk"走的是同一份代码**，这是刻意的设计：
免 root 读已安装应用签名的原理就是"把包装拉回本地自己解析"。

覆盖三种签名方案：
  · v1（JAR signing）：`META-INF/*.RSA|DSA|EC` 里的 PKCS#7 SignedData
  · v2（APK Signing Block, id=0x7109871a）
  · v3（APK Signing Block, id=0xf05368c0；含 SDK 区间与密钥轮换）

只做"提取 + 展示"，**不做有效性校验** —— 校验交给 `apksigner`，
有它时把权威结论并列展示，没它时明确说"未校验"，绝不把自家解析说成"已验证"。
"""

import hashlib
import os
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .der import certs_from_pkcs7, parse_x509, peek_certificates
from .reader import ApkReader

# APK Signing Block
SIG_BLOCK_MAGIC = b"APK Sig Block 42"
EOCD_SIG = b"PK\x05\x06"
V2_BLOCK_ID = 0x7109871A
V3_BLOCK_ID = 0xF05368C0
V31_BLOCK_ID = 0x1B93AD61
SOURCE_STAMP_ID = 0x6DFF800D

V1_SUFFIXES = (".rsa", ".dsa", ".ec", ".rsa.p7s")


@dataclass
class Certificate:
    """一张证书的可展示信息。"""

    sha256: str = ""
    sha1: str = ""
    md5: str = ""
    subject: str = ""
    subject_parts: Dict[str, str] = field(default_factory=dict)
    issuer: str = ""
    serial: str = ""
    not_before: str = ""
    not_after: str = ""
    public_key_algorithm: str = ""
    public_key_bits: int = 0
    signature_algorithm: str = ""
    is_ca: bool = False
    der_size: int = 0
    schemes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sha256": self.sha256,
            "sha1": self.sha1,
            "md5": self.md5,
            "subject": self.subject,
            "subject_parts": self.subject_parts,
            "issuer": self.issuer,
            "serial": self.serial,
            "notBefore": self.not_before,
            "notAfter": self.not_after,
            "publicKeyAlgorithm": self.public_key_algorithm,
            "publicKeyBits": self.public_key_bits,
            "signatureAlgorithm": self.signature_algorithm,
            "isCa": self.is_ca,
            "derSize": self.der_size,
            "schemes": self.schemes,
            "cn": self.subject_parts.get("CN", ""),
            "o": self.subject_parts.get("O", ""),
            "c": self.subject_parts.get("C", ""),
        }


@dataclass
class SignatureInfo:
    """一个 APK（或设备上已安装包）的签名全貌。"""

    schemes: List[str] = field(default_factory=list)
    certificates: List[Certificate] = field(default_factory=list)
    is_debug: bool = False
    min_sdk_for_v2: int = 0
    max_sdk_for_v3: int = 0
    error: str = ""
    warnings: List[str] = field(default_factory=list)
    verified: str = ""          # apksigner 的结论（未运行时为空）
    source: str = "apk"
    digest_identity: str = ""   # 首签名的 sha256 短标识，用于快速判同

    @property
    def ok(self) -> bool:
        return bool(self.certificates)

    def to_dict(self) -> dict:
        return {
            "schemes": self.schemes,
            "certificates": [c.to_dict() for c in self.certificates],
            "isDebug": self.is_debug,
            "minSdkForV2": self.min_sdk_for_v2,
            "maxSdkForV3": self.max_sdk_for_v3,
            "error": self.error,
            "warnings": self.warnings,
            "verified": self.verified,
            "source": self.source,
            "identity": self.digest_identity,
            "ok": self.ok,
        }


# ---------------------------------------------------------------- 指纹

def _fmt_fp(digest: bytes) -> str:
    """字节 → `AB:CD:EF:...`（与 keytool 同风格，大写、冒号分隔）。"""
    return ":".join("%02X" % b for b in digest)


def fingerprint(der: bytes) -> Tuple[str, str, str]:
    return (
        _fmt_fp(hashlib.sha256(der).digest()),
        _fmt_fp(hashlib.sha1(der).digest()),
        _fmt_fp(hashlib.md5(der).digest()),
    )


# ---------------------------------------------------------------- 证书包装

def _make_cert(der: bytes, schemes: List[str]) -> Certificate:
    info = parse_x509(der)
    s256, s1, m5 = fingerprint(der)
    c = Certificate(
        sha256=s256, sha1=s1, md5=m5,
        subject=info.get("subject", ""),
        subject_parts=info.get("subject_parts", {}) or {},
        issuer=info.get("issuer", ""),
        serial=info.get("serial", ""),
        not_before=info.get("not_before", ""),
        not_after=info.get("not_after", ""),
        public_key_algorithm=info.get("public_key_algorithm", ""),
        public_key_bits=info.get("public_key_bits", 0),
        signature_algorithm=info.get("signature_algorithm", ""),
        is_ca=bool(info.get("is_ca")),
        der_size=len(der),
    )
    if info.get("curve"):
        c.public_key_algorithm = "%s (%s)" % (c.public_key_algorithm, info["curve"])
    c.schemes = list(schemes)
    return c


def _dedupe(certs: List[Certificate]) -> List[Certificate]:
    """同一张证书可能同时出现在 v1/v2/v3 里 → 合并 schemes，只留一条。"""
    by_fp: Dict[str, Certificate] = {}
    order: List[str] = []
    for c in certs:
        key = c.sha256
        if key in by_fp:
            for s in c.schemes:
                if s not in by_fp[key].schemes:
                    by_fp[key].schemes.append(s)
        else:
            by_fp[key] = c
            order.append(key)
    return [by_fp[k] for k in order]


# ---------------------------------------------------------------- Signing Block

def find_signing_block(path) -> Optional[bytes]:
    """定位 APK Signing Block（在中央目录之前，以魔数结尾）。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size < 32:
        return None

    with open(str(path), "rb") as f:
        # EOCD 可能带最大 64KB 注释，从尾部往前扫
        tail_len = min(size, 65557)
        f.seek(size - tail_len)
        tail = f.read()
        idx = tail.rfind(EOCD_SIG)
        if idx < 0:
            return None
        try:
            cd_offset = struct.unpack_from("<I", tail, idx + 16)[0]
        except struct.error:
            return None
        if cd_offset < 32 or cd_offset > size:
            return None

        f.seek(cd_offset - 24)
        footer = f.read(24)
        if len(footer) < 24 or footer[8:] != SIG_BLOCK_MAGIC:
            return None
        block_size = struct.unpack_from("<Q", footer, 0)[0]
        start = cd_offset - block_size - 8
        if start < 0 or block_size > size:
            return None
        f.seek(start)
        return f.read(block_size + 8)


def _split_pairs(block: bytes) -> Dict[int, bytes]:
    """Signing Block → {block_id: value}。"""
    out: Dict[int, bytes] = {}
    if len(block) < 32:
        return out
    pos = 8
    end = len(block) - 24
    while pos + 12 <= end:
        try:
            ln = struct.unpack_from("<Q", block, pos)[0]
        except struct.error:
            break
        if ln < 4 or pos + 8 + ln > len(block):
            break
        pid = struct.unpack_from("<I", block, pos + 8)[0]
        out[pid] = block[pos + 12: pos + 8 + ln]
        pos += 8 + ln
    return out


def _read_lp(data: bytes, pos: int) -> Tuple[bytes, int]:
    """读一个 u32 长度前缀的块。"""
    if pos + 4 > len(data):
        return b"", len(data)
    ln = struct.unpack_from("<I", data, pos)[0]
    start = pos + 4
    end = start + ln
    if end > len(data):
        return data[start:], len(data)
    return data[start:end], end


def _certs_from_scheme_block(value: bytes) -> Tuple[List[bytes], int, int]:
    """解 v2/v3 的 signer 块，返回 (证书 DER 列表, minSdk, maxSdk)。"""
    certs: List[bytes] = []
    min_sdk = max_sdk = 0

    signers, _ = _read_lp(value, 0)
    spos = 0
    while spos < len(signers):
        signer, spos = _read_lp(signers, spos)
        if not signer:
            continue
        signed_data, epos = _read_lp(signer, 0)
        dpos = 0
        _digests, dpos = _read_lp(signed_data, dpos)
        certs_blob, dpos = _read_lp(signed_data, dpos)
        # v3 在证书之后还有 minSdk / maxSdk（v2 没有）
        if dpos < len(signed_data):
            sdk_blob, dpos = _read_lp(signed_data, dpos)
            if len(sdk_blob) == 4:
                min_sdk = struct.unpack_from("<I", sdk_blob, 0)[0]
                sdk_blob2, dpos = _read_lp(signed_data, dpos)
                if len(sdk_blob2) == 4:
                    max_sdk = struct.unpack_from("<I", sdk_blob2, 0)[0]

        cpos = 0
        while cpos < len(certs_blob):
            der, cpos = _read_lp(certs_blob, cpos)
            if der:
                certs.append(der)
        _ = epos
    return certs, min_sdk, max_sdk


# ---------------------------------------------------------------- v1

def _v1_certs(reader: ApkReader) -> List[bytes]:
    """从 META-INF 里的签名块提取证书。"""
    out: List[bytes] = []
    for name in reader.names:
        if not name.upper().startswith("META-INF/"):
            continue
        low = name.lower()
        if not low.endswith(V1_SUFFIXES):
            continue
        blob = reader.read(name)
        if not blob:
            continue
        got = certs_from_pkcs7(blob)
        if not got:
            # 有的工具产出的是裸证书列表或非标准结构 → 兜底扫描
            got = peek_certificates(blob)
        # 只保留真能解出主体的字节（坏包 / 非证书数据不进结果）
        out.extend([c for c in got if parse_x509(c).get("subject")])
    return out


# ---------------------------------------------------------------- 主入口

def analyze(reader: ApkReader, verify_with_apksigner: bool = False) -> SignatureInfo:
    """解析 APK 签名。

    :param verify_with_apksigner: 有 apksigner 时跑一次真实校验（"已验证/未验证"）
    """
    info = SignatureInfo(source="apk")

    # --- v1 ---------------------------------------------------------
    v1: List[bytes] = []
    try:
        v1 = _v1_certs(reader)
    except Exception as e:
        info.warnings.append("v1 签名解析异常：%s" % e)
    if v1:
        info.schemes.append("v1")

    # --- v2 / v3 ----------------------------------------------------
    v2: List[bytes] = []
    v3: List[bytes] = []
    try:
        block = find_signing_block(reader.path)
        if block:
            pairs = _split_pairs(block)
            if V2_BLOCK_ID in pairs:
                v2, _mn, _mx = _certs_from_scheme_block(pairs[V2_BLOCK_ID])
                if v2:
                    info.schemes.append("v2")
            if V3_BLOCK_ID in pairs:
                v3, mn, mx = _certs_from_scheme_block(pairs[V3_BLOCK_ID])
                info.min_sdk_for_v2 = mn
                info.max_sdk_for_v3 = mx
                if v3:
                    info.schemes.append("v3")
            if V31_BLOCK_ID in pairs or SOURCE_STAMP_ID in pairs:
                info.warnings.append("检测到 v3.1 / SourceStamp 签名块（本工具只读取 v1/v2/v3）")
    except Exception as e:
        info.warnings.append("v2/v3 签名块解析异常：%s" % e)

    # --- 汇总证书 ----------------------------------------------------
    certs: List[Certificate] = []
    certs += [_make_cert(d, ["v1"]) for d in v1]
    certs += [_make_cert(d, ["v2"]) for d in v2]
    certs += [_make_cert(d, ["v3"]) for d in v3]
    info.certificates = _dedupe(certs)

    if not info.certificates:
        info.error = "未找到任何签名信息（该 APK 可能未签名）"
    else:
        info.schemes = sorted(set(info.schemes), key=lambda s: ["v1", "v2", "v3"].index(s))
        first = info.certificates[0]
        info.digest_identity = first.sha256.replace(":", "")[:8].lower()
        cn = (first.subject_parts or {}).get("CN", "")
        o = (first.subject_parts or {}).get("O", "")
        c = (first.subject_parts or {}).get("C", "")
        info.is_debug = ("Android Debug" in cn) or (
            cn.lower().startswith("androiddebugkey") or
            (o == "Android" and c == "US" and "debug" in cn.lower())
        )
        if info.is_debug:
            info.warnings.append("这是 debug 签名，不可用于正式分发")
        if "v1" not in info.schemes and info.schemes:
            info.warnings.append("缺少 v1 签名：Android 6 及以下设备可能装不上")

    if verify_with_apksigner:
        info.verified = verify_apksigner(reader.path)

    return info


def verify_apksigner(apk_path) -> str:
    """用 apksigner 做权威校验；没装则返回空串（表示"未校验"）。"""
    from ...platform import finders, shell, paths

    exe = finders.find_apksigner()
    if not exe:
        return ""

    argv = [str(exe), "verify", "--print-certs", "-v", str(apk_path)]
    # Windows 上 apksigner 是 .bat，得借 cmd 才能执行
    if paths.IS_WINDOWS and str(exe).lower().endswith((".bat", ".cmd")):
        argv = ["cmd", "/c"] + argv

    r = shell.run(argv, timeout=180)
    if r.timed_out:
        return "校验超时"
    if r.rc == 0:
        return "已验证有效（apksigner）"
    first = next((l.strip() for l in r.lines() if l.strip() and "error" in l.lower()), "")
    return "校验失败：%s" % (first or (r.err.strip().splitlines() or ["未知原因"])[0])


def apksigner_digests(apk_path) -> List[str]:
    """取 apksigner 报告的签名者 SHA-256（无冒号小写）——单测逐字节比对用。"""
    from ...platform import finders, shell, paths

    exe = finders.find_apksigner()
    if not exe:
        return []
    argv = [str(exe), "verify", "--print-certs", str(apk_path)]
    if paths.IS_WINDOWS and str(exe).lower().endswith((".bat", ".cmd")):
        argv = ["cmd", "/c"] + argv
    r = shell.run(argv, timeout=180)
    out = []
    for line in r.lines():
        if "SHA-256 digest" in line and ":" in line:
            out.append(line.rsplit(":", 1)[-1].strip().lower())
    return out


def compare(a: SignatureInfo, b: SignatureInfo) -> dict:
    """比对两份签名，给出结论。

    判定依据是**证书 SHA-256 指纹集合** —— 只要首签名者不一致，
    两个包就不可能互相覆盖安装。
    """
    a_fps = [c.sha256 for c in a.certificates]
    b_fps = [c.sha256 for c in b.certificates]

    if not a_fps or not b_fps:
        missing = []
        if not a_fps:
            missing.append("A")
        if not b_fps:
            missing.append("B")
        return {
            "verdict": "undetermined",
            "reason": "签名信息不完整（%s 侧未取到），无法判定" % "/".join(missing),
            "a": a.to_dict(), "b": b.to_dict(),
        }

    if a_fps == b_fps:
        return {
            "verdict": "same",
            "reason": "签名一致，可以互相覆盖安装",
            "a": a.to_dict(), "b": b.to_dict(),
        }

    if a_fps[0] == b_fps[0]:
        return {
            "verdict": "different",
            "reason": "首签名一致但证书数量不同（一侧多签），覆盖安装前请确认",
            "a": a.to_dict(), "b": b.to_dict(),
        }

    # 找差异原因：同一 subject 但指纹不同 = 同 key 名不同密钥
    same_subject = a.certificates[0].subject == b.certificates[0].subject
    reason = "签名不一致，无法互相覆盖安装"
    if same_subject:
        reason += "（证书主体相同但指纹不同 → 用的是不同密钥）"
    return {
        "verdict": "different",
        "reason": reason,
        "a": a.to_dict(), "b": b.to_dict(),
    }
