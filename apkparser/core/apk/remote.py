# -*- coding: utf-8 -*-
"""把**设备上**的文件当成本地文件随机读。

## 为什么需要它

想知道设备上某个已装应用的「应用名」，就得读它 APK 里的
`AndroidManifest.xml` + `resources.arsc`。传统做法是 `adb pull` 整个 APK ——
可一个 APK 动辄 30~70MB，几百个应用就是几十 GB 的搬运，
而真正需要的数据只有几百 KB。

APK 是 zip：目录信息在文件尾部，条目数据散在中间。与其整文件搬运，
不如**按需读那几个字节**：

    adb exec-out dd if=<apk> bs=65536 skip=<块号> count=<块数>

这里把这条能力包装成一个 `file-like`，直接喂给标准库 `zipfile.ZipFile` ——
于是 `axml` / `arsc` / `cert` 这些现成的解析器**一行都不用改**。

## 代价

实测（真机，35MB 与 71MB 的 APK）：读 manifest + arsc 共 **7 次 adb 调用、
约 1.0 秒**，其中绝大部分时间是传 arsc（3~4MB，必须整块拿到才能查字符串池）。

## 注意

`dd` 的 `skip` / `count` 单位都是 `bs` 的块数，不是字节。所以取任意偏移时
要向下对齐到块边界，再把多出来的前半截切掉。
"""

from __future__ import annotations

import io
import threading
from typing import Dict, List, Optional

from ...platform import shell
from ..device import adb as adb_mod
from .reader import ApkError, ApkReader

# 一次取多少字节（越大 adb 调用越少，但单次内存占用越大）。
# zipfile 内部按 1MB 粒度向我们要数据，所以窗口别小于 1MB，否则白取。
DEFAULT_WINDOW = 1 << 20        # 1 MiB
BLOCK = 65536                   # dd 的 bs
MAX_WINDOW_CACHE = 6            # 最多缓存几个窗口（约 6MB）
MAX_SINGLE_FETCH = 8 << 20      # 单次 dd 最多取 8MB，防 zipfile 要一个超大 read


class RemoteError(Exception):
    """远程读取失败（消息面向用户）。"""


class AdbRangeFile(io.RawIOBase):
    """用 `adb exec-out dd` 实现随机读的文件对象。

    只实现 `readinto` / `seek` / `tell` —— `zipfile` 需要的就这些。
    内部按 WINDOW 大小的"窗口"抓取并缓存，避免 zipfile 的小块读放大成
    几十次 adb 调用。
    """

    def __init__(self, serial: str, remote: str, window: int = DEFAULT_WINDOW):
        self.serial = serial
        self.remote = remote
        self.window = max(BLOCK, int(window))
        self.pos = 0
        # 诊断用计数（验证脚本要靠它证明"真的没有整包搬运"）。
        # 必须在 _stat() 之前初始化 —— _stat 自己就会 +1。
        self.calls = 0
        self.bytes_fetched = 0
        self._order: List[int] = []
        self._wins: Dict[int, bytes] = {}
        self._lock = threading.Lock()
        self.size = self._stat()
        if self.size <= 0:
            raise RemoteError("读不到文件大小：%s" % remote)

    # ------------------------------------------------------------ 底层

    def _stat(self) -> int:
        args = ["shell", "stat", "-c", "%s", self.remote]
        r = adb_mod.run(args, serial=self.serial, timeout=20)
        self.calls += 1
        if not r.ok:
            return 0
        for line in reversed(r.lines()):
            line = line.strip()
            if line.isdigit():
                return int(line)
        return 0

    def _fetch(self, offset: int, length: int) -> bytes:
        """从 offset 起取 length 字节（一次 dd 调用）。"""
        length = min(length, MAX_SINGLE_FETCH)
        blk = offset // BLOCK
        inner = offset % BLOCK
        need = inner + length
        count = (need + BLOCK - 1) // BLOCK

        argv = [str(adb_mod.adb_bin())]
        if self.serial:
            argv += ["-s", self.serial]
        argv += ["exec-out", "dd", "if=%s" % self.remote,
                 "bs=%d" % BLOCK, "skip=%d" % blk, "count=%d" % count]
        rc, raw, err = shell.run_bytes(argv, timeout=90)
        self.calls += 1
        self.bytes_fetched += len(raw or b"")
        if rc != 0 and not raw:
            raise RemoteError("adb dd 失败：%s" % (err or "rc=%s" % rc).strip()[:200])
        return (raw or b"")[inner:inner + length]

    def _window_at(self, offset: int) -> bytes:
        with self._lock:
            cached = self._wins.get(offset)
            if cached is not None:
                return cached
            data = self._fetch(offset, self.window)
            self._wins[offset] = data
            if offset not in self._order:
                self._order.append(offset)
            while len(self._order) > MAX_WINDOW_CACHE:
                self._wins.pop(self._order.pop(0), None)
            return data

    def _serve(self, offset: int, length: int) -> bytes:
        """按窗口拼出 [offset, offset+length)。"""
        if offset >= self.size or length <= 0:
            return b""
        out = bytearray()
        cur = offset
        want = length
        while want > 0 and cur < self.size:
            start = (cur // self.window) * self.window
            win = self._window_at(start)
            if not win:
                break
            begin = cur - start
            piece = win[begin:begin + want]
            if not piece:
                break
            out += piece
            cur += len(piece)
            want -= len(piece)
        return bytes(out)

    # ------------------------------------------------------------ io 接口

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, off: int, whence: int = 0) -> int:
        if whence == 0:
            self.pos = int(off)
        elif whence == 1:
            self.pos += int(off)
        else:
            self.pos = max(0, self.size + int(off))
        return self.pos

    def readinto(self, b) -> int:      # type: ignore[override]
        n = len(b)
        if n == 0:
            return 0
        data = self._serve(self.pos, n)
        b[:len(data)] = data
        if len(data) < n:
            b[len(data):] = b"\x00" * (n - len(data))
        self.pos += n
        return n

    def stat_summary(self) -> dict:
        """本次读取花了多少代价（给验证/日志用）。"""
        return {"remote": self.remote, "size": self.size,
                "adbCalls": self.calls, "fetched": self.bytes_fetched}


def open_remote_apk(serial: str, remote_path: str,
                    window: int = DEFAULT_WINDOW) -> ApkReader:
    """打开设备上的一个 APK，返回和本地 `ApkReader` 完全同构的读取器。

    用法与本机一致：

        with open_remote_apk(serial, "/data/app/.../base.apk") as r:
            label = read_app_label(r)
    """
    rf = AdbRangeFile(serial, remote_path, window=window)
    try:
        return ApkReader(remote_path, fileobj=rf)
    except ApkError:
        raise
    except Exception as e:                      # pragma: no cover - 兜底
        raise RemoteError("打开远程 APK 失败：%s: %s" % (type(e).__name__, e))


def parse_remote_apk(serial: str, remote_path: str, with_signature: bool = True,
                     with_icon: bool = False, verify_signature: bool = False,
                     window: int = DEFAULT_WINDOW) -> "ApkInfo":
    """把**设备上**的 APK 解析成 `ApkInfo`（不整包 pull）。

    返回的对象与 `parse_apk(本机文件)` 出来的完全同构 —— 这是"本机 APK ⟷
    设备已装应用"整体对比能成立的前提：两边同一个解析器，差异只剩包本身的差异。

    代价记在 `info.remote_stats` 里（adb 调用次数 / 实际搬运字节），
    用来证明"真的没有整包搬运"，而不是嘴上说说。

    不抛异常：失败信息放在 `info.error` 里（与 `parse_apk` 一致）。
    """
    from .info import ApkInfo, parse_apk_reader

    name = remote_path.replace("\\", "/").rsplit("/", 1)[-1] or "base.apk"
    rf = None
    reader = None
    try:
        rf = AdbRangeFile(serial, remote_path, window=window)
        reader = ApkReader(remote_path, fileobj=rf)
    except (RemoteError, ApkError, OSError) as e:
        info = ApkInfo(path=remote_path, file_name=name)
        info.error = "读取设备上的 APK 失败：%s" % e
        return info
    except Exception as e:                      # 兜底
        info = ApkInfo(path=remote_path, file_name=name)
        info.error = "读取设备上的 APK 失败：%s: %s" % (type(e).__name__, e)
        return info

    try:
        info = parse_apk_reader(reader, path=remote_path, file_name=name,
                                file_size=rf.size,
                                with_signature=with_signature, with_icon=with_icon,
                                verify_signature=verify_signature)
        info.remote_stats = rf.stat_summary()
        info.source_device = serial
        return info
    finally:
        reader.close()
