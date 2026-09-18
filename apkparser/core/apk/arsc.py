# -*- coding: utf-8 -*-
"""resources.arsc 解析 —— 零依赖。

用途只有两个，但都很关键：
  1. `android:label="@string/app_name"` → 真实应用名（且要按语言优先级挑）
  2. `android:icon="@mipmap/ic_launcher"` → 真实图标文件路径（挑最高密度）

设计上按需查表：给定一个资源 id（0xPPTTEEEE）直接定位
package → type chunk → entry，不预建全表索引（几万条目的索引反而更慢）。
"""

import struct
from typing import Any, Dict, List, Optional, Tuple

RES_STRING_POOL_TYPE = 0x0001
RES_TABLE_TYPE = 0x0002
RES_TABLE_PACKAGE_TYPE = 0x0200
RES_TABLE_TYPE_TYPE = 0x0201
RES_TABLE_TYPE_SPEC_TYPE = 0x0202

# Res_value dataType
TYPE_NULL = 0x00
TYPE_REFERENCE = 0x01
TYPE_ATTRIBUTE = 0x02
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_INT_BOOLEAN = 0x12

FLAG_COMPLEX = 0x0001
FLAG_COMPACT = 0x0008

# 语言优先级：中文场景优先 zh-CN
DEFAULT_LOCALES = ("zh-CN", "zh", "en", "")

# 密度排序用（越大越"高清"）
DENSITY_ORDER = {
    0xFFFF: -1,   # nodpi：不缩放，作为兜底
    0xFFFE: -2,   # anydpi
    120: 120, 160: 160, 213: 213, 240: 240, 320: 320, 480: 480, 640: 640,
}


class ArscError(Exception):
    pass


# ---------------------------------------------------------------- 结构

class _TypeChunk:
    """一个 ResTable_type chunk（同一 type 在某个 config 下的一组条目）。"""

    def __init__(self, data: bytes, offset: int, pkg_offset: int):
        self.ok = False
        self.offset = offset
        self.type_id = 0
        self.entry_count = 0
        self.locale = ""        # "zh-CN" / "zh" / ""
        self.density = 0
        self.compact_skipped = False
        self._values: Dict[int, Tuple[int, int]] = {}
        self._parse(data, offset)

    def _parse(self, data: bytes, offset: int) -> None:
        try:
            _type, header_size, size = struct.unpack_from("<HHI", data, offset)
            (type_id, flags, _reserved,
             entry_count, entries_start) = struct.unpack_from("<BBHII", data, offset + 8)
        except struct.error:
            return

        self.type_id = type_id
        self.entry_count = entry_count
        self.size = size
        self.sparse = bool(flags & 0x01)

        config_off = offset + 20
        config_size = header_size - 20
        if config_size > 0:
            self.locale, self.density = self._parse_config(data, config_off, config_size)

        try:
            if self.sparse:
                # 稀疏表：每项是 (uint16 idx, uint16 offset/4)
                for i in range(entry_count):
                    idx, off4 = struct.unpack_from("<HH", data, offset + header_size + i * 4)
                    self._values[idx] = (offset + entries_start + off4 * 4, 0)
            else:
                raw = struct.unpack_from("<%dI" % entry_count, data, offset + header_size)
                for i, off in enumerate(raw):
                    if off != 0xFFFFFFFF:
                        self._values[i] = (offset + entries_start + off, 0)
        except struct.error:
            pass
        self.ok = True

    @staticmethod
    def _parse_config(data: bytes, off: int, size: int) -> Tuple[str, int]:
        """从 ResTable_config 里取 (locale, density)。"""
        locale = ""
        density = 0
        try:
            if size >= 12:
                lang = data[off + 8:off + 10]
                country = data[off + 10:off + 12]
                language = lang.decode("ascii", errors="ignore").strip("\x00")
                region = country.decode("ascii", errors="ignore").strip("\x00")
                if language:
                    locale = language
                    if region:
                        locale = "%s-%s" % (language, region.upper())
            if size >= 16:
                density = struct.unpack_from("<H", data, off + 14)[0]
        except (struct.error, IndexError):
            pass
        return locale, density

    def value_at(self, data: bytes, entry_id: int) -> Optional[Tuple[int, int]]:
        """返回 (dataType, data) —— 未压缩的 Res_value。

        注意 `ResTable_entry` 是 8 字节（size u16 + flags u16 + key u32），
        `Res_value` 紧随其后；偏移写成 4 会读到 key 本身。
        """
        loc = self._values.get(entry_id)
        if loc is None:
            return None
        pos = loc[0]
        try:
            size, flags, _key = struct.unpack_from("<HHI", data, pos)
        except struct.error:
            return None
        if flags & FLAG_COMPACT:
            # 紧凑条目（Android 14+ 优化）把值塞进 size/flags 两个字段，
            # 编码未被 AOSP 文档化。宁可不给值，也不给错值 —— 上层会退化到
            # 其他线索（例如按文件名找图标 / 用包名末段当应用名）。
            self.compact_skipped = True
            return None
        try:
            _vsize, _res0, dtype, dval = struct.unpack_from("<HBBI", data, pos + 8)
        except struct.error:
            return None
        return dtype, dval

    def key_index(self, data: bytes, entry_id: int) -> int:
        loc = self._values.get(entry_id)
        if loc is None:
            return -1
        try:
            entry_size, flags, key = struct.unpack_from("<HHI", data, loc[0])
        except struct.error:
            return -1
        if flags & FLAG_COMPACT:
            return -1
        return key


# ---------------------------------------------------------------- 资源表

class ResourceTable:
    """resources.arsc 的查询接口。"""

    def __init__(self, data: bytes):
        self.data = data
        self.ok = False
        self.packages: Dict[int, dict] = {}     # pkg_id → {offset, name, types, keys}
        self.global_pool: Optional[List[str]] = None
        self._type_chunks: Dict[Tuple[int, int], List[_TypeChunk]] = {}
        self._parse()

    # -- 解析 --------------------------------------------------------

    def _parse(self) -> None:
        data = self.data
        try:
            _type, header_size, total = struct.unpack_from("<HHI", data, 0)
            if _type != RES_TABLE_TYPE:
                raise ArscError("不是 resources.arsc（type=0x%04x）" % _type)
            package_count = struct.unpack_from("<I", data, 8)[0]
        except struct.error as e:
            raise ArscError("arsc 头部解析失败：%s" % e)

        total = min(total, len(data)) or len(data)
        pos = header_size

        while pos + 8 <= total:
            ctype, cheader, csize = struct.unpack_from("<HHI", data, pos)
            if csize < 8 or pos + csize > total:
                break

            if ctype == RES_STRING_POOL_TYPE and self.global_pool is None:
                self.global_pool = _read_pool(data, pos)

            elif ctype == RES_TABLE_PACKAGE_TYPE:
                self._parse_package(pos, csize)

            pos += csize

        self.ok = bool(self.packages)

    def _parse_package(self, offset: int, size: int) -> None:
        data = self.data
        try:
            _t, header_size, _s = struct.unpack_from("<HHI", data, offset)
            pkg_id = struct.unpack_from("<I", data, offset + 8)[0]
            name_raw = data[offset + 12:offset + 12 + 256]
            name = name_raw.decode("utf-16-le", errors="replace").split("\x00")[0]
            type_strings_off = struct.unpack_from("<I", data, offset + 268)[0]
            key_strings_off = struct.unpack_from("<I", data, offset + 276)[0]
        except struct.error:
            return

        entry = {"offset": offset, "name": name, "size": size,
                 "type_names": [], "key_names": []}

        if 0 < type_strings_off < size:
            entry["type_names"] = _read_pool(data, offset + type_strings_off) or []
        if 0 < key_strings_off < size:
            entry["key_names"] = _read_pool(data, offset + key_strings_off) or []

        self.packages[pkg_id] = entry

        # 遍历包内的 type chunks
        pos = offset + header_size
        end = offset + size
        while pos + 8 <= end:
            ctype, cheader, csize = struct.unpack_from("<HHI", data, pos)
            if csize < 8 or pos + csize > end:
                break
            if ctype == RES_TABLE_TYPE_TYPE:
                tc = _TypeChunk(data, pos, offset)
                if tc.ok:
                    self._type_chunks.setdefault((pkg_id, tc.type_id), []).append(tc)
            pos += csize

    # -- 查询 --------------------------------------------------------

    def package_name(self, pkg_id: int) -> str:
        return (self.packages.get(pkg_id) or {}).get("name", "")

    def type_name(self, pkg_id: int, type_id: int) -> str:
        names = (self.packages.get(pkg_id) or {}).get("type_names") or []
        if 0 <= type_id < len(names):
            return names[type_id]
        return "type%d" % type_id

    def resource_name(self, res_id: int) -> str:
        """`0x7f0d0001` → `com.pkg:string/app_name`（尽力而为）。"""
        pkg_id, type_id, entry_id = split_id(res_id)
        pkg = self.packages.get(pkg_id)
        if not pkg:
            return ""
        tname = self.type_name(pkg_id, type_id)
        keys = pkg.get("key_names") or []
        kname = ""
        for tc in self._type_chunks.get((pkg_id, type_id), []):
            ki = tc.key_index(self.data, entry_id)
            if 0 <= ki < len(keys):
                kname = keys[ki]
                break
        base = pkg.get("name") or ("0x%02x" % pkg_id)
        if kname:
            return "%s:%s/%s" % (base, tname, kname)
        return "%s:%s/0x%04x" % (base, tname, entry_id)

    def resolve_string(self, res_id: int, locales=DEFAULT_LOCALES,
                       _depth: int = 0) -> Optional[str]:
        """把一个资源 id 解析成字符串，按语言优先级挑最佳匹配。"""
        if _depth > 5 or not res_id:
            return None

        candidates: List[Tuple[str, str]] = []
        for loc, value in self.iter_values(res_id):
            if isinstance(value, str):
                candidates.append((loc, value))

        if not candidates:
            return None

        # 1) 按语言优先级
        for want in locales:
            for loc, val in candidates:
                if loc == want and val:
                    return val
        # 2) 任意非空
        for loc, val in candidates:
            if val:
                return val
        return None

    def resolve_string_ref(self, res_id: int, _depth: int = 0) -> Optional[str]:
        """解引用链（值本身又是资源引用时递归）。"""
        if _depth > 5:
            return None
        for _loc, value in self.iter_values(res_id):
            if isinstance(value, str):
                return value
            if isinstance(value, int) and 0x7F000000 <= value <= 0x7FFFFFFF:
                got = self.resolve_string_ref(value, _depth + 1)
                if got:
                    return got
        return None

    def iter_values(self, res_id: int):
        """遍历某资源在所有 config 下的值，yield (locale, value)。"""
        pkg_id, type_id, entry_id = split_id(res_id)
        for tc in self._type_chunks.get((pkg_id, type_id), []):
            got = tc.value_at(self.data, entry_id)
            if got is None:
                continue
            dtype, dval = got
            if dtype == TYPE_STRING:
                pool = self.global_pool or []
                if 0 <= dval < len(pool):
                    yield tc.locale, pool[dval]
            elif dtype == TYPE_REFERENCE:
                yield tc.locale, dval  # 上层可继续解引用
            elif dtype in (TYPE_INT_DEC, TYPE_INT_HEX):
                yield tc.locale, str(dval)
            elif dtype == TYPE_INT_BOOLEAN:
                yield tc.locale, "true" if dval else "false"

    def icon_best_path(self, res_id: int) -> Optional[str]:
        """图标资源 → 最高密度对应的文件路径（`res/mipmap-xxxhdpi-v4/ic.png`）。"""
        best: Tuple[int, str] = (-999, "")
        for tc in self._type_chunks.get((split_id(res_id)[0], split_id(res_id)[1]), []):
            got = tc.value_at(self.data, split_id(res_id)[2])
            if got is None:
                continue
            dtype, dval = got
            path = None
            if dtype == TYPE_STRING:
                pool = self.global_pool or []
                if 0 <= dval < len(pool):
                    path = pool[dval]
            if not path:
                continue
            rank = DENSITY_ORDER.get(tc.density, tc.density if tc.density else 0)
            if rank > best[0]:
                best = (rank, path)
        return best[1] or None

    def all_icon_paths(self, res_id: int) -> List[str]:
        """某图标资源名下的所有文件路径（用于在 zip 里兜底找）。"""
        out = []
        for _loc, value in self.iter_values(res_id):
            if isinstance(value, str) and value.lower().endswith((".png", ".webp", ".jpg", ".xml")):
                out.append(value)
        return out

    # -- 统计 --------------------------------------------------------

    def stats(self) -> dict:
        total_entries = sum(len(tc._values) for chunks in self._type_chunks.values() for tc in chunks)
        return {
            "packages": [{"id": "0x%02x" % k, "name": v.get("name", "")}
                         for k, v in sorted(self.packages.items())],
            "types": len(self._type_chunks),
            "entries": total_entries,
            "global_strings": len(self.global_pool or []),
        }


# ---------------------------------------------------------------- 工具

def split_id(res_id: int) -> Tuple[int, int, int]:
    """0x7f0d0001 → (0x7f, 0x0d, 0x0001)"""
    return (res_id >> 24) & 0xFF, (res_id >> 16) & 0xFF, res_id & 0xFFFF


def _read_pool(data: bytes, offset: int) -> Optional[List[str]]:
    """读一个 RES_STRING_POOL_TYPE。"""
    from .axml import StringPool
    pool = StringPool(data, offset)
    return pool.strings if pool.ok else None
