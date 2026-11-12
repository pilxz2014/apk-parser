# -*- coding: utf-8 -*-
"""渠道识别（多渠道打包排查用）。

"渠道标识写在哪"各家做法完全不同，本模块按**可信度从高到低**逐层尝试，
并把依据（`source`）一并带出 —— 界面上只给一个来路不明的值等于没给。

层次：

  1. `AndroidManifest.xml` 的 `<meta-data>`（友盟 / 极光 / TalkingData 等 SDK 约定）
  2. `META-INF/` 下的渠道标记文件（早期批量渠道包工具，空文件当标记）
  3. APK 签名块里的自定义 ID-value（美团 Walle，固定 id `0x71777777`）
  4. `assets/` 下的渠道清单文件
  5. dex 里 `BuildConfig` 的静态字符串字段（`buildConfigField("String","apkv",...)`
     这类注入；未混淆时能直接读到字段值）
  6. APK 文件名 + dex 字符串交叉校验（R8 已把 BuildConfig 内联时的兜底，
     只能算推断，标 `low`）

第 5 层读不到的原因值得记住：R8 会把 `BuildConfig` 类整个内联/删掉，
只剩值字符串孤零零留在字符串池里 —— 那时**没有任何确定性线索**能把它
和字段名对上（这也是 Walle 为什么要另存一份到签名块的原因）。
"""

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 渠道相关 meta-data 名（比"含 channel"更宽，避免漏掉项目自造的 apkv/registFlag）
_META_KEY_RE = re.compile(
    r"(channel|渠道|umeng_channel|market|flavor|apkv|regist_?flag|td_channel|"
    r"jpush_channel|xiaomi_channel|huawei_channel|oppo_channel|vivo_channel)", re.I)

# 形似渠道、实则无关（推送通知渠道 id、音频通道等）
_META_FALSE_RE = re.compile(r"(notification|firebase|fcm|push_?channel_?id|sound|audio|ringtone|alarm)", re.I)

# BuildConfig 里可能承载渠道的字段名
_BC_KEY_RE = re.compile(
    r"^(channel|channel_?name|umeng_?channel|td_?channel_?id|market|apkv|apk_?v|"
    r"regist_?flag|flavor|flavor_?name|gradle_?flavor|build_?channel)$", re.I)
_BC_LOOSE_RE = re.compile(r"(channel|flavor|apkv|regist)", re.I)

# 文件名里的通用词，绝不能当渠道
_GENERIC_TOKENS = {
    "apk", "apks", "aab", "app", "release", "debug", "beta", "alpha", "preview",
    "signed", "unsigned", "final", "test", "prod", "product", "official",
    "arm", "arm64", "x86", "x64", "x32", "universal", "google", "play",
    "store", "v1", "v2", "v3", "copy", "new", "old", "build", "output",
}

# 签名块里的标准 ID（这些不是渠道，跳过）
_STD_BLOCK_IDS = {
    0x7109871A,   # APK Signature Scheme v2
    0xF05368C0,   # APK Signature Scheme v3
    0x1B93AD61,   # APK Signature Scheme v3.1
    0x504B4453,   # Source stamp ("SDKP")
    0x42726577,   # Source stamp v2
}
WALLE_ID = 0x71777777


# ---------------------------------------------------------------- 对外入口

def detect(reader, root, arsc_t=None, file_name: str = "",
           resolve=None, allow_dex: bool = True) -> Dict[str, Any]:
    """识别渠道。

    `resolve(node, attr, arsc)` 由调用方注入（info._resolve），避免本模块
    反过来依赖 info 造成循环导入。

    `allow_dex=False` 用于**设备上的 APK**：那条路径每次读条目都要走 adb
    按需搬运，读三个 dex（几十 MB）代价不可接受；manifest / META-INF /
    签名块 / assets 这几层仍然是便宜的，照常尝试。

    返回值恒有这四个键（取不到时 value 为空串、items 为空表）：
      value / source / confidence / items[{key,value,source,confidence}]
    """
    items: List[Dict[str, str]] = []

    hits = _from_manifest_meta(root, arsc_t, resolve)
    if hits:
        items.extend(hits)
        return _pack(items)

    hit = _from_meta_inf(reader)
    if hit:
        return _pack([hit])

    hit = _from_signing_block(reader)
    if hit:
        return _pack([hit])

    hit = _from_assets(reader)
    if hit:
        return _pack([hit])

    pkg = ""
    if root is not None:
        try:
            pkg = str(root.attr("package", "") or "")
        except Exception:
            pkg = ""

    # 5、6 两层都要读 dex，读一次共用
    if allow_dex:
        dex_blobs = _read_dex_blobs(reader)
        hits = _from_build_config(dex_blobs)
        if hits:
            return _pack(hits)
        hit = _from_file_name(file_name, dex_blobs, pkg)
        if hit:
            return _pack([hit])

    return _pack([])


def _pack(items: List[Dict[str, str]]) -> Dict[str, Any]:
    items = items[:8]        # 一个包写了十几个 meta-data 也别把界面撑爆
    first = items[0] if items else {}
    return {
        "value": first.get("value", ""),
        "source": first.get("source", ""),
        "confidence": first.get("confidence", ""),
        "items": items,
    }


def _hit(key: str, value: str, source: str, confidence: str = "high") -> Dict[str, str]:
    return {"key": key, "value": value, "source": source, "confidence": confidence}


# ---------------------------------------------------------------- 1. manifest

def _from_manifest_meta(root, arsc_t, resolve) -> List[Dict[str, str]]:
    if root is None or resolve is None:
        return []
    app = root.first("application")
    if app is None:
        return []
    out: List[Dict[str, str]] = []
    for meta in app.find("meta-data"):
        name = str(meta.raw("name") or meta.attr("name") or "")
        if not name or not _META_KEY_RE.search(name):
            continue
        if _META_FALSE_RE.search(name):
            continue
        val = resolve(meta, "value", arsc_t)
        if not val:
            continue
        text = str(val).strip()
        if not text or len(text) > 64:
            continue
        short = name.rsplit(".", 1)[-1]
        out.append(_hit(short, text, "manifest meta-data 的 %s" % short))
    return out


# ---------------------------------------------------------------- 2. META-INF

def _from_meta_inf(reader) -> Optional[Dict[str, str]]:
    """传统批量渠道包：META-INF/ 下放一个 `channel_xxx` 空文件当标记。"""
    for name in reader.names:
        low = name.lower()
        if not low.startswith("meta-inf/"):
            continue
        if low.startswith(("meta-inf/services/", "meta-inf/versions/", "meta-inf/com/")):
            continue
        tail = low.rsplit("/", 1)[-1]
        if "channel" not in tail:
            continue
        if tail.endswith((".rsa", ".dsa", ".ec", ".sf", ".mf", ".version", ".kotlin_module")):
            continue
        if not re.match(r"^channel[_-]?[\w.-]*$", tail):
            continue
        stem = tail
        for ext in (".txt", ".json", ".properties", ".config"):
            if stem.endswith(ext):
                stem = stem[:-len(ext)]
        tag = re.sub(r"^channel[_-]?", "", stem, flags=re.I)
        if not tag:
            continue
        return _hit("channel", tag, "META-INF/%s（标记文件）" % name.rsplit("/", 1)[-1])
    return None


# ---------------------------------------------------------------- 3. Walle

def _from_signing_block(reader) -> Optional[Dict[str, str]]:
    """美团 Walle 把渠道写进 APK 签名块的自定义 ID-value 里。

    这条路是"重签名不改包内容"的，所以 META-INF 里干干净净 ——
    不解签名块就永远找不到。
    """
    for pid, val in _signing_block_pairs(reader):
        if pid in _STD_BLOCK_IDS:
            continue
        got = _decode_block_value(val)
        if not got:
            continue
        key, text = got
        if pid == WALLE_ID:
            return _hit(key, text, "APK 签名块（Walle 渠道信息）")
        return _hit(key, text, "APK 签名块（自定义 ID-value 0x%08x）" % pid)
    return None


def _decode_block_value(val: bytes) -> Optional[Tuple[str, str]]:
    """签名块里的值：Walle 存 JSON，也有工具存裸字符串。"""
    if not val or len(val) > 8192:
        return None
    try:
        text = val.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return None
        if isinstance(obj, dict):
            for k in ("channel", "CHANNEL", "Channel", "channelName", "market"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return (k, v.strip())
        return None
    # 裸字符串：只收"像标识"的短串，避免把二进制里的可打印片段当渠道
    if re.fullmatch(r"[\w.+-]{1,32}", text):
        return ("channel", text)
    return None


def _signing_block_pairs(reader) -> List[Tuple[int, bytes]]:
    """解析 APK Signing Block 的 ID-value 对。任何异常都当"没有"。"""
    fp = _file_obj(reader)
    if fp is None:
        return []
    try:
        fp.seek(0, 2)
        total = fp.tell()
        if total < 1024:
            return []
        tail_n = min(total, 66000)
        fp.seek(total - tail_n)
        tail = fp.read(tail_n)
        pos = tail.rfind(b"PK\x05\x06")
        if pos < 0 or pos + 22 > len(tail):
            return []
        cd_off = int.from_bytes(tail[pos + 16:pos + 20], "little")
        if cd_off in (0, 0xFFFFFFFF) or cd_off + 24 > total:
            return []
        fp.seek(cd_off - 24)
        hdr = fp.read(24)
        if len(hdr) < 24 or hdr[8:] != b"APK Sig Block 42":
            return []
        size = int.from_bytes(hdr[0:8], "little")
        if size < 32 or size > 8 * 1024 * 1024:
            return []
        start = cd_off - size - 8
        if start < 0:
            return []
        fp.seek(start)
        blob = fp.read(size + 8)
    except Exception:
        return []

    pairs: List[Tuple[int, bytes]] = []
    i, end = 8, len(blob) - 24
    while i + 12 <= end:
        ln = int.from_bytes(blob[i:i + 8], "little")
        if ln < 4 or i + 8 + ln > end:
            break
        pid = int.from_bytes(blob[i + 8:i + 12], "little")
        pairs.append((pid, blob[i + 12:i + 8 + ln]))
        i += 8 + ln
    return pairs


def _file_obj(reader):
    """拿一个能随机读的文件对象（本地是真实文件，设备上是 adb 按需读）。"""
    fp = getattr(reader, "fileobj", None)
    if fp is None:
        try:
            fp = reader.zf.fp
        except Exception:
            return None
    if not (hasattr(fp, "seek") and hasattr(fp, "read")):
        return None
    return fp


# ---------------------------------------------------------------- 4. assets

def _from_assets(reader) -> Optional[Dict[str, str]]:
    for name in reader.names:
        low = name.lower()
        if not low.startswith("assets/") or "channel" not in low:
            continue
        if not low.endswith((".txt", ".json", ".config", ".properties")):
            continue
        blob = reader.read(name)
        if not blob or len(blob) > 4096:
            continue
        try:
            text = blob.decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        if not text:
            continue
        # JSON 就取 channel 字段，否则取第一行
        if text.startswith("{"):
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    for k in ("channel", "CHANNEL", "channelName"):
                        v = obj.get(k)
                        if isinstance(v, str) and v.strip():
                            return _hit(k, v.strip(), "assets/%s" % name.split("/", 1)[1])
            except ValueError:
                pass
        first = text.splitlines()[0].strip()
        if 0 < len(first) <= 64:
            return _hit("channel", first, "assets/%s" % name.split("/", 1)[1])
    return None


# ---------------------------------------------------------------- 5. BuildConfig

def _read_dex_blobs(reader, budget: int = 48 * 1024 * 1024) -> List[bytes]:
    """读 dex 原文。总量超过预算就停 —— 有些包 dex 解压后上百 MB，
    为一个"可能没有"的渠道值把解析拖到好几秒不值得。"""
    out: List[bytes] = []
    total = 0
    sizes = {zi.filename: zi.file_size for zi in reader.infos}
    for name in reader.dex_names():
        size = sizes.get(name, 0)
        if total + size > budget:
            break
        blob = reader.read(name)
        if blob:
            out.append(blob)
            total += len(blob)
    return out


def _from_build_config(dex_blobs: Sequence[bytes]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for blob in dex_blobs:
        # 快速预筛：连 BuildConfig 这个名字都没有的 dex 不必解析类表
        if b"BuildConfig" not in blob:
            continue
        for desc, fields in _dex_build_config(blob):
            for fname, fval in fields:
                if not isinstance(fval, str) or not fval.strip():
                    continue
                if not (_BC_KEY_RE.match(fname) or _BC_LOOSE_RE.search(fname)):
                    continue
                val = fval.strip()
                if not val or len(val) > 64 or (fname, val) in seen:
                    continue
                seen.add((fname, val))
                out.append(_hit(fname, val, "dex 的 %s.%s" % (_short_class(desc), fname)))
    return out


def _short_class(desc: str) -> str:
    """`Lcom/jxw/student/BuildConfig;` → `com.jxw.student.BuildConfig`"""
    s = desc.strip("L;").replace("/", ".")
    return s


def _dex_build_config(blob: bytes) -> List[Tuple[str, List[Tuple[str, Optional[str]]]]]:
    """读出 dex 里所有 `*BuildConfig` 类的 static final 字段与初值。

    只做必要的结构解析：header → string_ids → type_ids → field_ids →
    class_defs → class_data → static_values。零依赖，几十行搞定。
    """
    if blob[:4] != b"dex\n" or len(blob) < 0x70:
        return []
    try:
        (string_ids_size, string_ids_off, type_ids_size, type_ids_off,
         _proto_sz, _proto_off, field_ids_size, field_ids_off,
         _method_sz, _method_off, class_defs_size, class_defs_off) = \
            _u32s(blob, 0x38, 12)
    except Exception:
        return []
    if (string_ids_size > 500000 or type_ids_size > 200000
            or field_ids_size > 500000 or class_defs_size > 200000
            or not string_ids_off or not class_defs_off):
        return []

    cache: Dict[int, str] = {}

    def u32(off: int) -> int:
        return int.from_bytes(blob[off:off + 4], "little")

    def string_at(idx: int) -> str:
        if idx >= string_ids_size:
            return ""
        off = u32(string_ids_off + idx * 4)
        return _mutf8(blob, off, cache)

    def type_name(idx: int) -> str:
        if idx >= type_ids_size:
            return ""
        return string_at(u32(type_ids_off + idx * 4))

    out = []
    for ci in range(class_defs_size):
        base = class_defs_off + ci * 32
        cls_idx = u32(base)
        desc = type_name(cls_idx)
        if not desc.endswith("BuildConfig;"):
            continue
        class_data_off = u32(base + 24)
        static_values_off = u32(base + 28)
        if not class_data_off:
            continue
        try:
            i = class_data_off
            sf, i = _uleb(blob, i)
            _ins, i = _uleb(blob, i)
            _dm, i = _uleb(blob, i)
            _vm, i = _uleb(blob, i)
            fields: List[Tuple[int, str]] = []
            idx = 0
            for _ in range(sf):
                diff, i = _uleb(blob, i)
                _flags, i = _uleb(blob, i)
                idx += diff
                if idx >= field_ids_size:
                    break
                # field_id_item 是 8 字节：class_idx(u2) type_idx(u2) name_idx(u4)
                fbase = field_ids_off + idx * 8
                fields.append((idx, string_at(u32(fbase + 4))))
            values = _dex_static_values(blob, static_values_off, string_at, string_ids_off, string_ids_size) \
                if static_values_off else []
        except Exception:
            continue
        out.append((desc, [(name, values[k] if k < len(values) else None)
                           for k, (_fi, name) in enumerate(fields)]))
    return out


def _dex_static_values(blob: bytes, off: int, string_at, string_ids_off: int,
                       string_ids_size: int) -> List[Optional[str]]:
    """encoded_array：只关心字符串型初值，其余跳过（占位 None）。"""
    n, i = _uleb(blob, off)
    out: List[Optional[str]] = []
    for _ in range(n):
        head = blob[i]
        i += 1
        vtype, varg = head & 0x1F, head >> 5
        if vtype == 0x17:                      # VALUE_STRING
            sidx = int.from_bytes(blob[i:i + varg + 1], "little")
            i += varg + 1
            out.append(string_at(sidx) if sidx < string_ids_size else None)
        else:
            if vtype == 0x00 and varg == 0:     # VALUE_BYTE
                i += 1
            else:
                i += varg + 1
            out.append(None)
    return out


def _uleb(blob: bytes, i: int) -> Tuple[int, int]:
    result = shift = 0
    while True:
        x = blob[i]
        i += 1
        result |= (x & 0x7F) << shift
        if not (x & 0x80):
            return result, i
        shift += 7
        if shift > 35:
            raise ValueError("uleb128 过长")


def _mutf8(blob: bytes, off: int, cache: Dict[int, str]) -> str:
    if off in cache:
        return cache[off]
    _n, i = _uleb(blob, off)
    end = blob.find(b"\x00", i)
    if end < 0:
        return ""
    s = blob[i:end].decode("utf-8", errors="replace")
    cache[off] = s
    return s


def _u32s(blob: bytes, off: int, n: int) -> Tuple[int, ...]:
    return tuple(int.from_bytes(blob[off + k * 4:off + k * 4 + 4], "little") for k in range(n))


# ---------------------------------------------------------------- 6. 文件名

def _from_file_name(file_name: str, dex_blobs: Sequence[bytes],
                    package: str) -> Optional[Dict[str, str]]:
    """兜底：R8 把 BuildConfig 内联后，值和字段名对不上时只能靠命名约定。

    Gradle 的输出文件名几乎总带 flavor（`app-<flavor>-release.apk`），
    所以从右往左找"像渠道"的段。为避免把 `release`、模块名当渠道，
    候选必须**在 dex 里作为独立字符串出现**（说明它是代码里真实用的标识，
    而不是随手起的文件名）。
    """
    if not file_name or not dex_blobs:
        return None
    stem = re.sub(r"\.(apk|apks|aab)$", "", file_name, flags=re.I)
    tokens = [t for t in re.split(r"[-_.\s]+", stem) if t]
    pkg_parts = set((package or "").lower().split("."))
    for tok in reversed(tokens):
        low = tok.lower()
        if len(low) < 3 or len(low) > 24:
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", tok):
            continue
        if low in _GENERIC_TOKENS or low in pkg_parts:
            continue
        if any(_standalone_count(b, tok) >= 1 for b in dex_blobs):
            return _hit("flavor", tok,
                        "APK 文件名（已与 dex 字符串交叉校验）", "low")
    return None


def _standalone_count(blob: bytes, token: str, limit: int = 3) -> int:
    """token 在 dex 里"独立出现"的次数。

    独立 = 前后都不是标识符字符，且**不紧贴 `/` 或 `.`**
    （紧贴说明它是类路径 / 域名片段，比如 com/jxw/student/home）。
    """
    tb = token.encode("ascii", "ignore")
    if not tb:
        return 0
    n, i = 0, 0
    while True:
        i = blob.find(tb, i)
        if i < 0:
            return n
        before = blob[i - 1] if i > 0 else 0
        after = blob[i + len(tb)] if i + len(tb) < len(blob) else 0
        if not _ident(before) and not _ident(after) \
                and before not in (0x2F, 0x2E) and after not in (0x2F, 0x2E):
            n += 1
            if n >= limit:
                return n
        i += len(tb)


def _ident(b: int) -> bool:
    return (0x30 <= b <= 0x39) or (0x41 <= b <= 0x5A) or (0x61 <= b <= 0x7A) or b == 0x5F
