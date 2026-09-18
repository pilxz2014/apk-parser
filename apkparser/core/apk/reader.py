# -*- coding: utf-8 -*-
"""APK / ZIP 低层读取。

只做"把字节拿出来"这件事：条目枚举、读取、体积聚合、ABI 与 dex 探测。
不做任何语义解析（那属于 axml / arsc / cert）。
"""

import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MANIFEST = "AndroidManifest.xml"
ARSC = "resources.arsc"


class ApkError(Exception):
    """APK 读取/解析失败。消息面向用户，可直接展示。"""


class ApkReader:
    """APK 文件读取器（上下文管理器）。

        with ApkReader("app.apk") as r:
            manifest = r.read_manifest()

    `fileobj` 传了就**不用** `path` 指向真实文件 —— 这是给"远程 APK"
    （见 `remote.py`：用 adb dd 按需读设备上的 APK）留的口子。
    `path` 那时只作展示名。
    """

    def __init__(self, path=None, fileobj=None):
        self.fileobj = fileobj
        if fileobj is None:
            self.path = Path(path)
            if not self.path.is_file():
                raise ApkError("文件不存在：%s" % self.path)
            target = str(self.path)
        else:
            # 远程模式：path 只是标签，可能根本不是本机路径
            self.path = Path(str(path)) if path else Path("(remote)")
            target = fileobj
        try:
            self.zf = zipfile.ZipFile(target, "r")
        except zipfile.BadZipFile as e:
            raise ApkError("不是有效的 APK/ZIP 文件：%s" % e)
        except OSError as e:
            raise ApkError("无法打开文件：%s" % e)
        self._names: Optional[List[str]] = None
        self._infos: Optional[List[zipfile.ZipInfo]] = None

    # ------------------------------------------------------------ 生命周期

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        try:
            self.zf.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 条目

    @property
    def names(self) -> List[str]:
        if self._names is None:
            try:
                self._names = self.zf.namelist()
            except Exception:
                self._names = []
        return self._names

    @property
    def infos(self) -> List[zipfile.ZipInfo]:
        if self._infos is None:
            try:
                self._infos = self.zf.infolist()
            except Exception:
                self._infos = []
        return self._infos

    def has(self, name: str) -> bool:
        return name in self.names

    def read(self, name: str, default: Optional[bytes] = None) -> Optional[bytes]:
        """读一个条目。读失败返回 default（单个坏条目不该让整个解析失败）。"""
        try:
            return self.zf.read(name)
        except Exception:
            return default

    def read_required(self, name: str) -> bytes:
        data = self.read(name)
        if data is None:
            raise ApkError("APK 内缺少必需文件：%s" % name)
        return data

    # ------------------------------------------------------------ 语义 shortcut

    def read_manifest(self) -> bytes:
        return self.read_required(MANIFEST)

    def read_arsc(self) -> Optional[bytes]:
        return self.read(ARSC)

    def find(self, pattern: str) -> List[str]:
        """按 glob 找条目名。"""
        import fnmatch
        return [n for n in self.names if fnmatch.fnmatch(n, pattern)]

    def find_suffix(self, suffix: str) -> List[str]:
        s = suffix.lower()
        return [n for n in self.names if n.lower().endswith(s)]

    # ------------------------------------------------------------ 属性探测

    def abis(self) -> List[str]:
        """APK 支持的 ABI 列表（按 lib/<abi>/ 探测）。"""
        abis = []
        for n in self.names:
            if n.startswith("lib/") and n.count("/") >= 2:
                abi = n.split("/")[1]
                if abi and abi not in abis:
                    abis.append(abi)
        return abis

    def dex_names(self) -> List[str]:
        """classes.dex / classes2.dex ... 按序号排序。"""
        def key(n: str) -> int:
            stem = n.rsplit("/", 1)[-1][:-4]
            if stem == "classes":
                return 1
            try:
                return int(stem.replace("classes", ""))
            except ValueError:
                return 999

        return sorted([n for n in self.names if n.endswith(".dex") and "/" not in n], key=key)

    def native_lib_names(self) -> List[str]:
        return [n for n in self.names if n.startswith("lib/") and n.endswith(".so")]

    # ------------------------------------------------------------ 体积

    def entry_sizes(self) -> List[Tuple[str, int, int]]:
        """[(条目名, 压缩后大小, 原始大小)]"""
        out = []
        for i in self.infos:
            if i.filename.endswith("/"):
                continue
            out.append((i.filename, i.compress_size, i.file_size))
        return out

    def size_breakdown(self) -> Dict[str, int]:
        """按顶层目录/文件类型聚合"占用体积"（用原始大小，更接近安装后）。

        规则：`lib/xxx/` 归到 `lib/xxx`；`res/`、`assets/` 归到自身；
        `classes*.dex` 统一归 `dex`；`META-INF/` 归 `META-INF`；其余按顶层名。
        """
        buckets: Dict[str, int] = {}
        for name, _csize, usize in self.entry_sizes():
            if name.startswith("classes") and name.endswith(".dex"):
                key = "dex"
            elif name.startswith("lib/"):
                parts = name.split("/")
                key = "lib/%s" % parts[1] if len(parts) > 2 else "lib"
            elif "/" in name:
                key = name.split("/")[0]
            else:
                key = name
            buckets[key] = buckets.get(key, 0) + usize
        return dict(sorted(buckets.items(), key=lambda kv: -kv[1]))

    def compressed_size(self) -> int:
        return sum(i.compress_size for i in self.infos if not i.filename.endswith("/"))

    def raw_size(self) -> int:
        return sum(i.file_size for i in self.infos if not i.filename.endswith("/"))

    def file_size(self) -> int:
        if self.fileobj is not None:
            return int(getattr(self.fileobj, "size", 0) or 0)
        try:
            return self.path.stat().st_size
        except OSError:
            return 0
