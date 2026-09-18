# -*- coding: utf-8 -*-
"""极简 DER / X.509 解析（零依赖）。

只需要解出证书里"人想看"的那几项：
  主体 / 颁发者 / 序列号 / 有效期 / 公钥算法与位长 / 签名算法
不校验签名、不处理任何加密 —— 完整性与有效性交给 `apksigner`。

参考 RFC 5280 的 ASN.1 结构，长度解析遵循 DER 的 definite-length 规则。
"""

from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- ASN.1 标签

TAG_BOOLEAN = 0x01
TAG_INTEGER = 0x02
TAG_BIT_STRING = 0x03
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_UTF8_STRING = 0x0C
TAG_PRINTABLE_STRING = 0x13
TAG_T61_STRING = 0x14
TAG_IA5_STRING = 0x16
TAG_UTC_TIME = 0x17
TAG_GENERALIZED_TIME = 0x18
TAG_SEQUENCE = 0x30
TAG_SET = 0x31

# 字符串类（用于 RDN 取值）
_STRING_TAGS = {
    TAG_UTF8_STRING, TAG_PRINTABLE_STRING, TAG_T61_STRING, TAG_IA5_STRING,
    0x12,  # NumericString
    0x1A,  # VisibleString
    0x1B,  # GeneralString
}


class DerError(Exception):
    pass


# ---------------------------------------------------------------- OID 名称表

OID_NAMES = {
    # 属性类型
    "2.5.4.3": "CN",
    "2.5.4.4": "SN",
    "2.5.4.5": "serialNumber",
    "2.5.4.6": "C",
    "2.5.4.7": "L",
    "2.5.4.8": "ST",
    "2.5.4.9": "STREET",
    "2.5.4.10": "O",
    "2.5.4.11": "OU",
    "2.5.4.12": "title",
    "2.5.4.42": "GN",
    "1.2.840.113549.1.9.1": "E",
    "0.9.2342.19200300.100.1.25": "DC",
    "0.9.2342.19200300.100.1.1": "UID",
    # 公钥算法
    "1.2.840.113549.1.1.1": "RSA",
    "1.2.840.113549.1.1.10": "RSASSA-PSS",
    "1.2.840.10045.2.1": "EC",
    "1.2.840.10040.4.1": "DSA",
    "1.3.101.112": "Ed25519",
    "1.3.101.113": "Ed448",
    # 签名算法
    "1.2.840.113549.1.1.2": "MD2withRSA",
    "1.2.840.113549.1.1.4": "MD5withRSA",
    "1.2.840.113549.1.1.5": "SHA1withRSA",
    "1.2.840.113549.1.1.11": "SHA256withRSA",
    "1.2.840.113549.1.1.12": "SHA384withRSA",
    "1.2.840.113549.1.1.13": "SHA512withRSA",
    "1.2.840.10045.4.1": "SHA1withECDSA",
    "1.2.840.10045.4.3.1": "SHA224withECDSA",
    "1.2.840.10045.4.3.2": "SHA256withECDSA",
    "1.2.840.10045.4.3.3": "SHA384withECDSA",
    "1.2.840.10045.4.3.4": "SHA512withECDSA",
    "1.2.840.10040.4.3": "SHA1withDSA",
    "2.16.840.1.101.3.4.3.2": "SHA256withDSA",
    # 曲线
    "1.2.840.10045.3.1.7": "prime256v1",
    "1.3.132.0.34": "secp384r1",
    "1.3.132.0.35": "secp521r1",
    "1.3.132.0.10": "secp256k1",
    # PKCS#7
    "1.2.840.113549.1.7.1": "data",
    "1.2.840.113549.1.7.2": "signedData",
    # 摘要
    "1.3.14.3.2.26": "SHA1",
    "2.16.840.1.101.3.4.2.1": "SHA256",
    "2.16.840.1.101.3.4.2.2": "SHA384",
    "2.16.840.1.101.3.4.2.3": "SHA512",
}


def oid_name(oid: str) -> str:
    """OID → 人类可读名；未知则原样返回。"""
    return OID_NAMES.get(oid, oid)


# 常见曲线的密钥位长
_CURVE_BITS = {
    "secp192r1": 192, "prime192v1": 192,
    "secp224r1": 224,
    "prime256v1": 256, "secp256r1": 256, "secp256k1": 256,
    "secp384r1": 384, "secp521r1": 521,
}

_OID_RSA = "1.2.840.113549.1.1.1"
_OID_EC = "1.2.840.10045.2.1"


def _key_bits(alg_oid: str, key_raw: bytes, curve: str = "") -> int:
    """从 SubjectPublicKeyInfo 的 BIT STRING 里算出真实密钥位长。

    RSA 要解一层 RSAPublicKey 取 modulus（否则会把指数也算进位长），
    EC 直接查曲线表（点坐标长度只是它的两倍多）。
    """
    if not key_raw:
        return 0
    unused = key_raw[0]
    body = key_raw[1:]

    if alg_oid == _OID_RSA:
        try:
            t, _l, vstart, nxt = read_tlv(body, 0)
            if t == TAG_SEQUENCE:
                for t2, _l2, v2, n2 in iter_children(body, vstart, nxt):
                    if t2 == TAG_INTEGER:
                        m = body[v2:n2].lstrip(b"\x00")
                        if m:
                            return len(m) * 8 - (8 - m[0].bit_length())
        except (DerError, IndexError, ValueError):
            pass
        return max(0, len(key_raw) - 1) * 8 - unused

    if alg_oid == _OID_EC:
        if curve in _CURVE_BITS:
            return _CURVE_BITS[curve]
        if len(body) > 1:
            return (len(body) - 1) * 4   # 未压缩点 = 1 + X + Y，两轴各半
        return 0

    if alg_oid == "1.3.101.112":
        return 256   # Ed25519
    if alg_oid == "1.3.101.113":
        return 456   # Ed448

    return max(0, len(key_raw) - 1) * 8 - unused


# ---------------------------------------------------------------- TLV 读取

def read_tlv(data: bytes, pos: int, end: Optional[int] = None) -> Tuple[int, int, int, int]:
    """读一个 TLV。

    :returns: (tag, length, value_start, next_pos)
    """
    if end is None:
        end = len(data)
    if pos >= end:
        raise DerError("TLV 越界：pos=%d end=%d" % (pos, end))

    tag = data[pos]
    pos += 1

    # 多字节 tag（不常见，但规范允许：低 5 位全 1 表示还有后续）
    if (tag & 0x1F) == 0x1F:
        while pos < end and (data[pos] & 0x80):
            pos += 1
        pos += 1

    if pos >= end:
        raise DerError("长度字段越界")

    first = data[pos]
    pos += 1
    if first & 0x80:
        n = first & 0x7F
        if n == 0 or n > 4:
            raise DerError("不支持的长度编码（%d 字节）" % n)
        if pos + n > end:
            raise DerError("长度字段越界")
        length = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    else:
        length = first

    if pos + length > end:
        raise DerError("内容越界（需要 %d，剩 %d）" % (length, end - pos))
    return tag, length, pos, pos + length


def iter_children(data: bytes, start: int, end: int):
    """遍历某个容器下的所有 TLV。"""
    pos = start
    while pos < end:
        tag, length, vstart, nxt = read_tlv(data, pos, end)
        yield tag, length, vstart, nxt
        pos = nxt


def decode_oid(raw: bytes) -> str:
    """DER OID 字节 → 点分字符串。"""
    if not raw:
        return ""
    first = raw[0]
    parts = [str(first // 40), str(first % 40)]
    val = 0
    for b in raw[1:]:
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            parts.append(str(val))
            val = 0
    return ".".join(parts)


def decode_der_string(raw: bytes, tag: int) -> str:
    """按 tag 解出字符串。优先 UTF-8，失败退 latin-1。"""
    if tag == 0x1E:  # BMPString
        try:
            return raw.decode("utf-16-be")
        except Exception:
            return raw.hex()
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def decode_time(raw: bytes, tag: int) -> str:
    """UTCTime / GeneralizedTime → ISO8601（本地不做时区换算，按 UTC 标记）。"""
    s = raw.decode("ascii", errors="replace").strip()
    try:
        if tag == TAG_UTC_TIME and len(s) >= 12:
            # YYMMDDHHMMSSZ
            yy = int(s[0:2])
            year = 1900 + yy if yy >= 50 else 2000 + yy
            return "%04d-%s-%sT%s:%s:%sZ" % (
                year, s[2:4], s[4:6], s[6:8], s[8:10], s[10:12])
        if tag == TAG_GENERALIZED_TIME and len(s) >= 14:
            return "%s-%s-%sT%s:%s:%sZ" % (
                s[0:4], s[4:6], s[6:8], s[8:10], s[10:12], s[12:14])
    except Exception:
        pass
    return s


# ---------------------------------------------------------------- X.509

def _parse_name(data: bytes, start: int, end: int) -> Tuple[str, Dict[str, str]]:
    """解析 Name（RDNSequence），返回 (字符串形式, {短名: 值})。

    字符串形式按 X.509 惯例用 RFC4514 风格：`CN=a, O=b, C=cn`。
    """
    parts: List[str] = []
    kv: Dict[str, str] = {}
    for tag, _l, vstart, nxt in iter_children(data, start, end):
        if tag != TAG_SET:
            continue
        # SET OF AttributeTypeAndValue
        for t2, _l2, v2, n2 in iter_children(data, vstart, nxt):
            if t2 != TAG_SEQUENCE:
                continue
            oid = ""
            value = ""
            for t3, _l3, v3, n3 in iter_children(data, v2, n2):
                if t3 == TAG_OID:
                    oid = decode_oid(data[v3:n3])
                elif t3 in _STRING_TAGS:
                    value = decode_der_string(data[v3:n3], t3)
                elif t3 == TAG_OCTET_STRING and not value:
                    # X.509 里有的 DN 用 OCTET STRING 包 UTF-8（罕见）
                    value = decode_der_string(data[v3:n3], TAG_UTF8_STRING)
            if oid:
                short = oid_name(oid)
                parts.append("%s=%s" % (short, value))
                if short not in kv:
                    kv[short] = value
    return ", ".join(parts), kv


def parse_x509(der: bytes) -> Dict[str, Any]:
    """解析 X.509 证书，返回可展示的字典。

    不抛异常：解析不了的字段留空，`_error` 里带原因。
    """
    info: Dict[str, Any] = {
        "subject": "", "subject_parts": {}, "issuer": "", "issuer_parts": {},
        "serial": "", "not_before": "", "not_after": "",
        "public_key_algorithm": "", "public_key_bits": 0,
        "signature_algorithm": "", "version": "", "is_ca": False,
        "_error": "",
    }
    try:
        tag, _l, tbs_start, cert_end = read_tlv(der, 0)
        if tag != TAG_SEQUENCE:
            raise DerError("证书根节点不是 SEQUENCE")

        # Certificate ::= SEQUENCE { tbsCertificate, signatureAlgorithm, signatureValue }
        children = list(iter_children(der, tbs_start, cert_end))
        if len(children) < 2:
            raise DerError("证书结构不完整")

        # 签名算法（外层）
        _t, _l, alg_v, alg_n = children[1]
        for t2, _l2, v2, n2 in iter_children(der, alg_v, alg_n):
            if t2 == TAG_OID:
                info["signature_algorithm"] = oid_name(decode_oid(der[v2:n2]))
                break

        # tbsCertificate
        _t, _l, tbs_v, tbs_n = children[0]
        idx = 0
        elems = list(iter_children(der, tbs_v, tbs_n))

        # [0] EXPLICIT version（可选）
        if elems and elems[0][0] == 0xA0:
            for t2, _l2, v2, n2 in iter_children(der, elems[0][2], elems[0][3]):
                if t2 == TAG_INTEGER:
                    info["version"] = "v%d" % (int.from_bytes(der[v2:n2], "big") + 1)
            idx = 1

        if idx < len(elems) and elems[idx][0] == TAG_INTEGER:
            raw = der[elems[idx][2]:elems[idx][3]]
            info["serial"] = raw.hex().upper() if len(raw) <= 20 else raw.hex().upper()[:40] + "…"
            idx += 1

        if idx < len(elems) and elems[idx][0] == TAG_SEQUENCE:
            idx += 1  # 内层 signature 算法，外层已取

        if idx < len(elems) and elems[idx][0] == TAG_SEQUENCE:
            i_str, i_kv = _parse_name(der, elems[idx][2], elems[idx][3])
            info["issuer"], info["issuer_parts"] = i_str, i_kv
            idx += 1

        if idx < len(elems) and elems[idx][0] == TAG_SEQUENCE:
            for t2, _l2, v2, n2 in iter_children(der, elems[idx][2], elems[idx][3]):
                if t2 in (TAG_UTC_TIME, TAG_GENERALIZED_TIME):
                    if not info["not_before"]:
                        info["not_before"] = decode_time(der[v2:n2], t2)
                    else:
                        info["not_after"] = decode_time(der[v2:n2], t2)
            idx += 1

        if idx < len(elems) and elems[idx][0] == TAG_SEQUENCE:
            s_str, s_kv = _parse_name(der, elems[idx][2], elems[idx][3])
            info["subject"], info["subject_parts"] = s_str, s_kv
            idx += 1

        if idx < len(elems):  # subjectPublicKeyInfo ::= SEQUENCE { algorithm, subjectPublicKey }
            alg_oid = ""
            key_raw = b""
            for t2, _l2, v2, n2 in iter_children(der, elems[idx][2], elems[idx][3]):
                if t2 == TAG_SEQUENCE and not alg_oid:
                    first = True
                    for t3, _l3, v3, n3 in iter_children(der, v2, n2):
                        if t3 == TAG_OID:
                            oid = decode_oid(der[v3:n3])
                            if first:
                                alg_oid = oid
                                first = False
                            else:
                                # EC 的曲线名在算法参数的 OID 里
                                info["curve"] = oid_name(oid)
                elif t2 == TAG_BIT_STRING and not key_raw:
                    key_raw = der[v2:n2]

            if alg_oid:
                info["public_key_algorithm"] = oid_name(alg_oid)
            info["public_key_bits"] = _key_bits(alg_oid, key_raw, info.get("curve", ""))

        # 是否 CA（BasicConstraints）
        for t2, _l2, v2, n2 in elems[idx if idx < len(elems) else -1:]:
            if t2 == TAG_SEQUENCE:
                for t3, _l3, v3, n3 in iter_children(der, v2, n2):
                    if t3 == TAG_OID and decode_oid(der[v3:n3]) == "2.5.29.19":
                        info["is_ca"] = True
    except (DerError, IndexError, ValueError) as e:
        info["_error"] = "证书解析不完整：%s" % e

    return info


# ---------------------------------------------------------------- PKCS#7

def certs_from_pkcs7(der: bytes) -> List[bytes]:
    """从 PKCS#7 SignedData 里抽出证书 DER 列表（v1 签名用）。

    ContentInfo ::= SEQUENCE { contentType OID, content [0] EXPLICIT SignedData }
    SignedData  ::= SEQUENCE { version, digestAlgorithms, contentInfo,
                               [0] IMPLICIT certificates, ... }
    """
    out: List[bytes] = []
    try:
        tag, _l, ci_start, ci_end = read_tlv(der, 0)
        if tag != TAG_SEQUENCE:
            return out
        kids = list(iter_children(der, ci_start, ci_end))
        if len(kids) < 2 or kids[1][0] != 0xA0:
            return out
        # [0] EXPLICIT → 内层 SignedData SEQUENCE
        inner = list(iter_children(der, kids[1][2], kids[1][3]))
        if not inner or inner[0][0] != TAG_SEQUENCE:
            return out
        sd = inner[0]
        for t, _l2, v, n in iter_children(der, sd[2], sd[3]):
            if t == 0xA0:  # [0] IMPLICIT certificates SET OF Certificate
                # 用显式游标遍历，才能把整段 TLV（含 tag/length 头）切出来 ——
                # 只取内容会让下游 parse_x509 拿到一段不是证书开头的字节。
                pos = v
                while pos < n:
                    tag_pos = pos
                    try:
                        t2, _l3, _v2, nxt = read_tlv(der, pos, n)
                    except (DerError, IndexError, ValueError):
                        break
                    pos = nxt
                    if t2 == TAG_SEQUENCE:
                        out.append(der[tag_pos:nxt])
                break
    except (DerError, IndexError, ValueError):
        pass
    return out


def peek_certificates(der: bytes) -> List[bytes]:
    """从任意"可能包含证书"的 DER 里粗暴地找证书。

    兜底用：遍历所有 SEQUENCE，把看起来像 X.509 的挑出来
    （以 SEQUENCE 开头、内含另一个 SEQUENCE 且能被 parse_x509 解出主题）。
    """
    out: List[bytes] = []

    def walk(start: int, end: int, depth: int = 0):
        if depth > 6:
            return
        pos = start
        while pos < end:
            tag_pos = pos
            try:
                tag, _length, vstart, nxt = read_tlv(der, pos, end)
            except (DerError, IndexError, ValueError):
                return
            pos = nxt
            if tag != TAG_SEQUENCE:
                continue
            cand = der[tag_pos:nxt]
            if len(cand) > 64:
                info = parse_x509(cand)
                if info.get("subject") and not info.get("_error"):
                    out.append(cand)
                    continue
            walk(vstart, nxt, depth + 1)

    walk(0, len(der))
    return out
