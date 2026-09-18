# -*- coding: utf-8 -*-
"""已装应用的「应用名」缓存与后台补齐。

## 问题

`pm list packages` **不给应用名** —— 那是 APK 里 `resources.arsc` 字符串池里的
东西（`android:label="@string/app_name"`）。Android 侧也没有便宜的 API 能查
（`dumpsys package` 只给 `labelRes=0x7f...` 这个数字，不给字符串）。

## 办法

配合 `core/apk/remote.py` 的定点读取：只从设备拿 `AndroidManifest.xml` +
`resources.arsc`，本地用现成的 axml/arsc 解析器还原出名字。实测约 1 秒 / 包，
7 次 adb 调用 —— 对比 `adb pull` 整个 APK（30~70MB）省了 99% 的搬运。

## 缓存

结果按 `包名|versionCode` 落盘到 `cache/labels.json`。应用升级（versionCode
变了）才会重取；平时列表页只是查一次字典，零额外开销。

## 并发

几百个包串行要几分钟，所以用线程池（默认 6 路）。`adb` 本身能并发，
每路各自 `exec-out`，互不干扰。
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

from ...platform import paths
from ..apk.info import read_app_label
from ..apk.reader import ApkError
from ..apk.remote import RemoteError, open_remote_apk
from . import adb as adb_mod

CACHE_VERSION = 2
DEFAULT_WORKERS = 6

_lock = threading.RLock()
_cache: Optional[Dict[str, dict]] = None
_dirty = False
_stamp = None                  # 上次读文件时的 (mtime_ns, size)
_last_check = 0.0              # 上次检查文件是否被外部改过的时间


# ---------------------------------------------------------------- 缓存读写

def cache_file() -> Path:
    return paths.cache_dir() / "labels.json"


def _stamp_of() -> Optional[tuple]:
    try:
        st = cache_file().stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def refresh_if_changed(min_interval: float = 1.0) -> bool:
    """别的进程改过缓存文件就重读。

    主控台是常驻进程，而 `apktool.py app labels` 是短命进程 —— 两边共用同一个
    `cache/labels.json`。不做这层检查的话，CLI 补好的名字在网页上要等到重启
    才看得见（或者反过来：网页进程抱着旧表把已缓存的全当成"没补过"）。
    """
    global _cache, _stamp, _last_check
    now = time.time()
    with _lock:
        if now - _last_check < min_interval:
            return False
        _last_check = now
        cur = _stamp_of()
        if _cache is None:
            _load()
            return bool(_cache)
        if cur == _stamp:
            return False
        _cache = None
        _stamp = None
    _load()
    return True


def _load() -> Dict[str, dict]:
    global _cache, _stamp
    with _lock:
        if _cache is not None:
            return _cache
        data: Dict[str, dict] = {}
        f = cache_file()
        try:
            if f.is_file():
                raw = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("version") == CACHE_VERSION:
                    entries = raw.get("entries")
                    if isinstance(entries, dict):
                        data = {str(k): v for k, v in entries.items()
                                if isinstance(v, dict) and v.get("label")}
        except Exception:
            data = {}
        _cache = data
        _stamp = _stamp_of()
        return _cache


def save() -> None:
    """原子落盘。"""
    global _dirty, _stamp
    with _lock:
        if _cache is None or not _dirty:
            return
        f = cache_file()
        payload = {"version": CACHE_VERSION,
                   "savedAt": int(time.time()),
                   "entries": _cache}
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(f))
            _dirty = False
            _stamp = _stamp_of()
        except Exception:
            pass


def key_of(package: str, version_code: int = 0) -> str:
    return "%s|%d" % (package or "", int(version_code or 0))


def get(package: str, version_code: int = 0) -> str:
    """查缓存（不打设备）。"""
    if not package:
        return ""
    refresh_if_changed()
    ent = _load().get(key_of(package, version_code))
    if ent:
        return str(ent.get("label") or "")
    # versionCode 拿不到时（老设备 / pm 不支持 --show-versioncode），
    # 退化为"这个包任意版本的 label"——总比没有强
    if not version_code:
        prefix = "%s|" % package
        for k, v in _load().items():
            if k.startswith(prefix) and v.get("label"):
                return str(v["label"])
    return ""


def put(package: str, version_code: int, label: str, source: str = "device") -> None:
    global _dirty
    if not package or not label:
        return
    with _lock:
        c = _load()
        c[key_of(package, version_code)] = {
            "label": str(label)[:120], "ts": int(time.time()), "src": source,
        }
        _dirty = True


def stats() -> dict:
    refresh_if_changed()
    with _lock:
        c = _load()
        return {"cached": len(c), "file": str(cache_file())}


def clear() -> int:
    global _cache, _dirty, _stamp
    with _lock:
        n = len(_cache or {})
        _cache = {}
        _dirty = True
        save()
        return n


# ---------------------------------------------------------------- 取一个

def fetch_label(serial: str, apk_path: str, timeout: float = 90.0) -> dict:
    """从设备上的一个 APK 里取应用名。**不抛异常**，失败放在返回值里。"""
    t0 = time.time()
    out = {"ok": False, "label": "", "apkPath": apk_path, "ms": 0, "error": "", "adbCalls": 0}
    reader = None
    try:
        reader = open_remote_apk(serial, apk_path)
        out["label"] = read_app_label(reader)
        out["ok"] = bool(out["label"])
        if not out["ok"]:
            out["error"] = "APK 里没有可用的 label"
    except (RemoteError, ApkError) as e:
        out["error"] = str(e)
    except Exception as e:                       # pragma: no cover - 兜底
        out["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if reader is not None:
            try:
                rf = getattr(reader, "fileobj", None)
                if rf is not None:
                    out["adbCalls"] = getattr(rf, "calls", 0)
                    out["bytesFetched"] = getattr(rf, "bytes_fetched", 0)
                    out["fileSize"] = getattr(rf, "size", 0)
            except Exception:
                pass
            reader.close()
    out["ms"] = int((time.time() - t0) * 1000)
    return out


# ---------------------------------------------------------------- 后台补齐

class SyncJob:
    """一次"补齐应用名"的进度状态（同一时刻只允许一个）。"""

    def __init__(self, packages: List[dict], device: str = "",
                 workers: int = DEFAULT_WORKERS):
        self.packages = packages
        self.device = device
        self.workers = max(1, min(16, int(workers)))
        self.total = len(packages)
        self.done = 0
        # 叫 succeeded 不叫 ok：接口信封已经用 ok 表示"这次请求本身成功了"，
        # 两者同名会被 routes 的 st["ok"]=True 覆盖，前端就显示"成功 true"。
        self.succeeded = 0
        self.failed = 0
        self.skipped = 0
        self.current = ""
        self.started_at = time.time()
        self.finished_at = 0.0
        self.running = False
        self.errors: List[dict] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 控制 ----------------------------------------------------

    def start(self) -> None:
        self.running = True
        self._thread = threading.Thread(target=self._run, name="label-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- 执行 ----------------------------------------------------

    def _run(self) -> None:
        try:
            serial = adb_mod.pick_device(self.device)
        except adb_mod.AdbError as e:
            self.running = False
            self.errors.append({"error": str(e)})
            self.finished_at = time.time()
            return

        # 别人（CLI）可能刚补过一批 —— 先把文件现状拿进来，免得重复干
        refresh_if_changed(min_interval=0.0)
        todo = []
        for p in self.packages:
            pkg = str(p.get("package") or "")
            vc = int(p.get("versionCode") or 0)
            if not pkg or not p.get("apkPath"):
                self.skipped += 1
                self.done += 1
                continue
            if get(pkg, vc):
                self.skipped += 1
                self.done += 1
                continue
            todo.append((pkg, vc, str(p["apkPath"])))

        self.total = len(todo) + self.skipped
        if not todo:
            self.running = False
            self.finished_at = time.time()
            save()
            return

        def work(item):
            pkg, vc, path = item
            if self._stop.is_set():
                return None
            self.current = pkg
            res = fetch_label(serial, path)
            if res.get("ok"):
                put(pkg, vc, res["label"], source="device")
                return True
            return {"package": pkg, "error": res.get("error") or "取不到"}

        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for r in pool.map(work, todo):
                    self.done += 1
                    if r is True:
                        self.succeeded += 1
                    elif isinstance(r, dict):
                        self.failed += 1
                        if len(self.errors) < 30:
                            self.errors.append(r)
                    if self.done % 10 == 0:
                        save()
                    if self._stop.is_set():
                        break
        finally:
            save()
            self.running = False
            self.finished_at = time.time()

    # -- 状态 ----------------------------------------------------

    def snapshot(self) -> dict:
        took = (self.finished_at or time.time()) - self.started_at
        rate = (self.done / took) if took > 0 and self.done else 0.0
        left = (self.total - self.done) / rate if rate > 0 else 0.0
        return {
            "running": self.running,
            "done": self.done, "total": self.total,
            "succeeded": self.succeeded, "failed": self.failed, "skipped": self.skipped,
            "current": self.current,
            "elapsedMs": int(took * 1000),
            "etaSec": int(left) if self.running else 0,
            "errors": self.errors[:30],
        }


_job_lock = threading.Lock()
_job: Optional[SyncJob] = None


def current_job() -> Optional[SyncJob]:
    with _job_lock:
        return _job


def start_sync(packages: List[dict], device: str = "",
               workers: int = DEFAULT_WORKERS) -> SyncJob:
    """启动后台补齐（已在跑就返回那一个，不重复起）。"""
    global _job
    with _job_lock:
        if _job is not None and _job.running:
            return _job
        _job = SyncJob(packages, device=device, workers=workers)
        _job.start()
        return _job


def stop_sync() -> bool:
    with _job_lock:
        if _job is not None and _job.running:
            _job.stop()
            return True
    return False


def sync_state() -> dict:
    with _job_lock:
        job = _job
    if job is None:
        return {"running": False, "done": 0, "total": 0, "succeeded": 0, "failed": 0,
                "skipped": 0, "current": "", "everRan": False}
    d = job.snapshot()
    d["everRan"] = True
    return d


def enrich(apps: List, refresh_cache: bool = True) -> int:
    """把缓存里的应用名填进 `AppInfo.label`（只在 label 为空时填）。

    返回填了几个。**纯内存查表**，不打设备 —— 列表接口可以随便调。
    """
    if refresh_cache:
        refresh_if_changed()
    n = 0
    for a in apps:
        if getattr(a, "label", ""):
            continue
        lb = get(a.package, getattr(a, "version_code", 0))
        if lb:
            a.label = lb
            n += 1
    return n
