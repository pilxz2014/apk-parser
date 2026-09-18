# -*- coding: utf-8 -*-
"""二进制 AndroidManifest.xml（AXML）解析 —— 零依赖。

AXML 是 AAPT 编译后的二进制 XML：
    RES_XML_TYPE 头 → 字符串池 → 资源映射表 → 一串 node chunk
    （START_NAMESPACE / START_ELEMENT / END_ELEMENT / CDATA）

本模块把它还原成一棵轻量 DOM，属性值按 dataType 解释成 Python 值。
遇到资源引用（TYPE_REFERENCE，例如 `android:label="@string/app_name"`）
不在这里查表 —— 只保留 `ref_id`，由 `arsc.py` 负责还原字符串。
"""

import struct
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

# ---------------------------------------------------------------- chunk 常量

RES_STRING_POOL_TYPE = 0x0001
RES_XML_TYPE = 0x0003
RES_XML_START_NAMESPACE_TYPE = 0x0100
RES_XML_END_NAMESPACE_TYPE = 0x0101
RES_XML_START_ELEMENT_TYPE = 0x0102
RES_XML_END_ELEMENT_TYPE = 0x0103
RES_XML_CDATA_TYPE = 0x0104
RES_XML_RESOURCE_MAP_TYPE = 0x0180

# dataType
TYPE_NULL = 0x00
TYPE_REFERENCE = 0x01
TYPE_ATTRIBUTE = 0x02
TYPE_STRING = 0x03
TYPE_FLOAT = 0x04
TYPE_DIMENSION = 0x05
TYPE_FRACTION = 0x06
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_INT_BOOLEAN = 0x12
TYPE_INT_COLOR_ARGB8 = 0x1C
TYPE_INT_COLOR_RGB8 = 0x1D
TYPE_INT_COLOR_ARGB4 = 0x1E
TYPE_INT_COLOR_RGB4 = 0x1F

# 常见命名空间 → 前缀
NS_PREFIX = {
    "http://schemas.android.com/apk/res/android": "android",
    "http://schemas.android.com/apk/res-auto": "app",
    "http://schemas.android.com/tools": "tools",
}

# 兜底：属性名在字符串池里为空时，用 resource map 里的 attr id 反查
# （只收录 manifest 场景真正会用到的）
ATTR_ID_NAMES = {
    0x01010000: "theme",
    0x01010001: "label",
    0x01010002: "icon",
    0x01010003: "name",
    0x01010004: "permission",
    0x01010005: "readPermission",
    0x01010006: "writePermission",
    0x01010010: "authorities",
    0x01010011: "value",
    0x01010018: "description",
    0x01010020: "process",
    0x01010021: "exported",
    0x01010022: "enabled",
    0x01010023: "debuggable",
    0x01010024: "allowClearUserData",
    0x01010025: "screenOrientation",
    0x01010026: "configChanges",
    0x01010027: "launchMode",
    0x01010028: "priority",
    0x01010029: "taskAffinity",
    0x0101002A: "allowTaskReparenting",
    0x0101002B: "hasCode",  # 实为 allowBackup 附近，命名仅作兜底
    0x01010030: "action",
    0x01010031: "data",
    0x01010032: "mimeType",
    0x01010033: "scheme",
    0x01010034: "host",
    0x01010035: "port",
    0x01010036: "path",
    0x01010037: "pathPrefix",
    0x01010038: "pathPattern",
    0x0101003A: "resource",
    0x0101020C: "minSdkVersion",
    0x0101021B: "versionCode",
    0x0101021C: "versionName",
    0x01010270: "targetSdkVersion",
    0x01010271: "maxSdkVersion",
    0x0101026C: "installLocation",
    0x010102B0: "largeHeap",
    0x0101039E: "networkSecurityConfig",
    0x01010572: "compileSdkVersion",
    0x01010573: "compileSdkVersionCodename",
    0x0101056F: "extractNativeLibs",
    0x0101055B: "roundIcon",
    0x0101F000: "requiredFeature",  # 占位，避免误名
    0x01010019: "requiredFeature",
    0x01010281: "hardwareAccelerated",
    0x0101001E: "grantUriPermissions",
    0x0101001F: "multiprocess",
    0x0101001D: "handleProfiling",
    0x0101001C: "functionalTest",
    0x0101001B: "targetPackage",
    0x0101001A: "targetActivity",
    0x0101053F: "usesCleartextTraffic",
    0x010104EA: "resizeableActivity",
    0x0101035A: "supportsRtl",
}


class AxmlError(Exception):
    """AXML 解析失败。"""


# ---------------------------------------------------------------- 数据结构

@dataclass
class Attr:
    """一个 XML 属性。

    `value` 是按 dataType 解释后的 Python 值；
    如果是资源引用，`ref_id` 有值而 `value` 是 `?0x7f0d0001` 这样的占位。
    """

    ns: str = ""
    name: str = ""
    raw: Optional[str] = None
    type: int = TYPE_NULL
    data: int = 0
    value: Any = None
    ref_id: Optional[int] = None

    @property
    def key(self) -> str:
        prefix = NS_PREFIX.get(self.ns, "")
        return "%s:%s" % (prefix, self.name) if prefix else self.name


@dataclass
class Node:
    """XML 元素。`attrs` 的 key 形如 `android:name` / `package`。"""

    tag: str = ""
    attrs: Dict[str, Attr] = field(default_factory=dict)
    children: List["Node"] = field(default_factory=list)
    parent: Optional["Node"] = None
    text: str = ""
    line: int = 0

    # -- 取值 --------------------------------------------------------

    def attr(self, name: str, default: Any = None) -> Any:
        """取解释后的值。name 可带前缀（`android:label`）或不带（`label`）。"""
        a = self.attrs.get(name)
        if a is None and ":" not in name:
            for k, v in self.attrs.items():
                if k.rsplit(":", 1)[-1] == name:
                    a = v
                    break
        if a is None:
            return default
        return default if a.value is None and default is not None else a.value

    def attr_obj(self, name: str) -> Optional[Attr]:
        a = self.attrs.get(name)
        if a is None and ":" not in name:
            for k, v in self.attrs.items():
                if k.rsplit(":", 1)[-1] == name:
                    return v
        return a

    def raw(self, name: str, default: Optional[str] = None) -> Optional[str]:
        a = self.attr_obj(name)
        if a is None:
            return default
        return a.raw if a.raw is not None else default

    def bool_attr(self, name: str, default: bool = False) -> bool:
        v = self.attr(name)
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v != 0
        return str(v).strip().lower() in ("true", "1", "yes")

    def int_attr(self, name: str, default: int = 0) -> int:
        v = self.attr(name)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    # -- 遍历 --------------------------------------------------------

    def find(self, tag: str) -> List["Node"]:
        return [c for c in self.children if c.tag == tag]

    def first(self, tag: str) -> Optional["Node"]:
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def walk(self) -> Iterator["Node"]:
        yield self
        for c in self.children:
            yield from c.walk()


# ---------------------------------------------------------------- 字符串池

class StringPool:
    """RES_STRING_POOL_TYPE 解析（UTF-8 与 UTF-16 两种编码）。"""

    def __init__(self, data: bytes, offset: int):
        self.ok = False
        self.strings: List[str] = []
        self.offsets: List[int] = []
        try:
            _type, header_size, size = struct.unpack_from("<HHI", data, offset)
            (str_count, style_count, flags,
             strings_start, _styles_start) = struct.unpack_from("<IIIII", data, offset + 8)
            self.utf8 = bool(flags & (1 << 8))
            self.strings_start = offset + strings_start
            self.offsets = list(struct.unpack_from("<%dI" % str_count, data, offset + header_size))
            self._data = data
            for off in self.offsets:
                self.strings.append(self._read_string(self.strings_start + off))
            self.ok = True
        except (struct.error, IndexError):
            self.ok = False

    def _read_string(self, pos: int) -> str:
        """读一个字符串。

        UTF-8 池里长度是**两段**：先字符数，再字节数 ——
        必须用字节数切片，用字符数会在多字节字符上截断（ASCII 时两者恰好相等，
        所以这个 bug 只在中文等非 ASCII 文本上暴露）。
        """
        data = self._data
        try:
            if self.utf8:
                _char_len, pos = self._utf8_len(data, pos)
                byte_len, pos = self._utf8_len(data, pos)
                return data[pos:pos + byte_len].decode("utf-8", errors="replace")
            char_len, pos = self._utf16_len(data, pos)
            return data[pos:pos + char_len * 2].decode("utf-16-le", errors="replace")
        except (IndexError, struct.error, UnicodeDecodeError):
            return ""

    @staticmethod
    def _utf8_len(data: bytes, pos: int) -> Tuple[int, int]:
        first = data[pos]
        if first & 0x80:
            return ((first & 0x7F) << 8) | data[pos + 1], pos + 2
        return first, pos + 1

    @staticmethod
    def _utf16_len(data: bytes, pos: int) -> Tuple[int, int]:
        first = struct.unpack_from("<H", data, pos)[0]
        if first & 0x8000:
            second = struct.unpack_from("<H", data, pos + 2)[0]
            return ((first & 0x7FFF) << 16) | second, pos + 4
        return first, pos + 2


# ---------------------------------------------------------------- 解析器

class AxmlParser:
    """把 AXML 字节流解析成 `Node` 树。"""

    def __init__(self, data: bytes):
        self.data = data
        self.pool: Optional[StringPool] = None
        self.res_map: List[int] = []
        self.ns_map: Dict[str, str] = {}   # uri → prefix
        self.root: Optional[Node] = None

    # -- 入口 --------------------------------------------------------

    def parse(self) -> Node:
        data = self.data
        if len(data) < 8:
            raise AxmlError("文件太小，不是 AXML")

        _type, header_size, total = struct.unpack_from("<HHI", data, 0)
        if _type != RES_XML_TYPE:
            raise AxmlError("不是 AXML（chunk type=0x%04x，期望 0x0003）" % _type)
        if total > len(data) or total < header_size:
            total = len(data)

        pos = header_size
        stack: List[Node] = []

        while pos + 8 <= total:
            ctype, cheader, csize = struct.unpack_from("<HHI", data, pos)
            if csize < 8 or pos + csize > total:
                break

            if ctype == RES_STRING_POOL_TYPE:
                self.pool = StringPool(data, pos)

            elif ctype == RES_XML_RESOURCE_MAP_TYPE:
                n = (csize - cheader) // 4
                if n > 0:
                    try:
                        self.res_map = list(struct.unpack_from("<%dI" % n, data, pos + cheader))
                    except struct.error:
                        self.res_map = []

            elif ctype == RES_XML_START_NAMESPACE_TYPE:
                self._read_namespace(pos, cheader)

            elif ctype == RES_XML_START_ELEMENT_TYPE:
                node = self._read_element(pos, cheader)
                if stack:
                    node.parent = stack[-1]
                    stack[-1].children.append(node)
                else:
                    if self.root is None:
                        self.root = node
                stack.append(node)

            elif ctype == RES_XML_END_ELEMENT_TYPE:
                if stack:
                    stack.pop()

            elif ctype == RES_XML_CDATA_TYPE:
                if stack:
                    stack[-1].text = self._read_cdata(pos, cheader)

            pos += csize

        if self.root is None:
            raise AxmlError("未找到根元素（可能是非标准 AXML）")
        return self.root

    # -- chunk 读取 --------------------------------------------------

    def _str(self, idx: int) -> str:
        if self.pool and 0 <= idx < len(self.pool.strings):
            return self.pool.strings[idx]
        return ""

    def _read_namespace(self, pos: int, header_size: int) -> None:
        base = pos + header_size
        try:
            prefix_idx, uri_idx = struct.unpack_from("<II", self.data, base)
        except struct.error:
            return
        uri = self._str(uri_idx)
        prefix = self._str(prefix_idx)
        if uri:
            self.ns_map[uri] = prefix or NS_PREFIX.get(uri, "")

    def _read_element(self, pos: int, header_size: int) -> Node:
        data = self.data
        base = pos + header_size  # 跳过 ResXMLTree_node（含 lineNumber/comment）
        node = Node()
        try:
            line = struct.unpack_from("<I", data, pos + 8)[0]
            node.line = line
        except struct.error:
            pass

        try:
            ns_idx, name_idx = struct.unpack_from("<II", data, base)
            (attr_start, attr_size, attr_count,
             _id_idx, _class_idx, _style_idx) = struct.unpack_from("<HHHHHH", data, base + 8)
        except struct.error:
            return node

        node.tag = self._str(name_idx)
        attrs_base = base + attr_start

        for i in range(attr_count):
            off = attrs_base + i * attr_size
            a = self._read_attr(off)
            if a is not None:
                node.attrs[a.key] = a

        return node

    def _read_attr(self, off: int) -> Optional[Attr]:
        data = self.data
        try:
            ns_idx, name_idx, raw_idx = struct.unpack_from("<III", data, off)
            _vsize, _res0, dtype, dval = struct.unpack_from("<HBBI", data, off + 12)
        except struct.error:
            return None

        a = Attr()
        a.ns = self._str(ns_idx)
        a.name = self._str(name_idx)
        a.raw = self._str(raw_idx) or None
        a.type = dtype
        a.data = dval

        # 属性名缺失时用 resource map 反查
        if not a.name and name_idx < len(self.res_map):
            a.name = ATTR_ID_NAMES.get(self.res_map[name_idx], "attr_0x%08x" % self.res_map[name_idx])

        a.value = self._interpret(a)
        return a

    def _interpret(self, a: Attr) -> Any:
        t, d = a.type, a.data
        if t == TYPE_NULL:
            return None
        if t == TYPE_STRING:
            return self._str(d)
        if t in (TYPE_INT_DEC, TYPE_INT_HEX):
            return d
        if t == TYPE_INT_BOOLEAN:
            return bool(d)
        if t == TYPE_FLOAT:
            try:
                return struct.unpack("<f", struct.pack("<I", d))[0]
            except struct.error:
                return 0.0
        if t in (TYPE_INT_COLOR_ARGB8, TYPE_INT_COLOR_RGB8, TYPE_INT_COLOR_ARGB4, TYPE_INT_COLOR_RGB4):
            return "#%08X" % d
        if t in (TYPE_REFERENCE, TYPE_ATTRIBUTE):
            a.ref_id = d
            # 资源引用先给可读占位，arsc 查表后由上层替换
            if a.raw:
                return a.raw
            return "?0x%08x" % d
        if t == TYPE_DIMENSION:
            return _decode_complex(d, ("dp", "dip", "sp", "pt", "in", "mm", "px"))
        if t == TYPE_FRACTION:
            return _decode_fraction(d)
        return d

    def _read_cdata(self, pos: int, header_size: int) -> str:
        base = pos + header_size
        try:
            _idx, = struct.unpack_from("<I", self.data, base)
            _vsize, _res0, dtype, dval = struct.unpack_from("<HBBI", self.data, base + 4)
            if dtype == TYPE_STRING:
                return self._str(dval)
        except struct.error:
            pass
        return ""


_MANTISSA_MULT = 1.0 / (1 << 8)
# 对应 AOSP Res_value::complexToFloat 的 RADIX_MULTS
_RADIX_MULTS = [
    _MANTISSA_MULT,
    _MANTISSA_MULT / (1 << 7),
    _MANTISSA_MULT / (1 << 15),
    _MANTISSA_MULT / (1 << 23),
]


def complex_to_float(value: int) -> float:
    """解 TYPE_DIMENSION / TYPE_FRACTION 的复合编码（与 AOSP 一致）。"""
    return (value & ~(0x80000000 | (0x03 << 4))) * _RADIX_MULTS[(value >> 4) & 0x03]


def _decode_complex(data: int, units: Tuple[str, ...]) -> str:
    unit_idx = data & 0x0F
    value = complex_to_float(data)
    unit = units[unit_idx] if unit_idx < len(units) else "?"
    if value == int(value):
        return "%d%s" % (int(value), unit)
    return "%.3f%s" % (value, unit)


def _decode_fraction(data: int) -> str:
    unit = "%" if (data & 0x0F) == 0 else "%p"
    return "%.2f%s" % (complex_to_float(data) * 100, unit)


# ---------------------------------------------------------------- 便捷入口

def parse_manifest(data: bytes) -> Node:
    """解析 AndroidManifest.xml 字节流。"""
    return AxmlParser(data).parse()


def manifest_to_dict(root: Node, max_depth: int = 3) -> dict:
    """调试用：把 manifest 树转成可 JSON 化的 dict。"""

    def conv(n: Node, depth: int) -> dict:
        d = {"tag": n.tag, "attrs": {k: v.value for k, v in n.attrs.items()}}
        if depth < max_depth:
            kids = [conv(c, depth + 1) for c in n.children]
            if kids:
                d["children"] = kids
        return d

    return conv(root, 0)
