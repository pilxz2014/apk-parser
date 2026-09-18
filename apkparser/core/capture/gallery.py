# -*- coding: utf-8 -*-
"""产物管理：截图 / 录屏文件的归档、列表、清理。"""

import time
from pathlib import Path
from typing import Dict, List, Optional

from ...platform import paths

KINDS = {".png": "截图", ".jpg": "截图", ".jpeg": "截图", ".mp4": "录屏", ".gif": "动图"}


def _dir() -> Path:
    d = paths.captures_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_artifacts(limit: int = 50, kind: str = "") -> List[dict]:
    """列出捕获产物（最新在前）。"""
    out = []
    for f in _dir().iterdir():
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        k = KINDS.get(ext)
        if not k:
            continue
        if kind and k != kind:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({
            "name": f.name,
            "path": str(f),
            "kind": k,
            "bytes": st.st_size,
            "mtime": st.st_mtime,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "url": "/files/captures/%s" % f.name,
        })
    out.sort(key=lambda x: -x["mtime"])
    return out[:limit] if limit else out


def delete(path: str) -> dict:
    """删一个产物（只允许删 captures 目录内的，防路径穿越）。"""
    p = Path(path).resolve()
    root = _dir().resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return {"ok": False, "detail": "只能删除 captures 目录内的文件"}
    if not p.is_file():
        return {"ok": False, "detail": "文件不存在"}
    try:
        p.unlink()
        return {"ok": True, "detail": "已删除 %s" % p.name}
    except OSError as e:
        return {"ok": False, "detail": "删除失败：%s" % e}


def cleanup(days: int = 7) -> dict:
    """清掉 N 天前的产物。"""
    cutoff = time.time() - days * 86400
    n = 0
    freed = 0
    for f in _dir().iterdir():
        if not f.is_file() or f.suffix.lower() not in KINDS:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            try:
                freed += st.st_size
                f.unlink()
                n += 1
            except OSError:
                pass
    return {"ok": True, "detail": "清理了 %d 个文件，释放 %.1f MB" % (n, freed / 1048576.0),
            "count": n, "bytes": freed}


def disk_usage() -> dict:
    total = 0
    count = 0
    for f in _dir().iterdir():
        if f.is_file() and f.suffix.lower() in KINDS:
            try:
                total += f.stat().st_size
                count += 1
            except OSError:
                pass
    return {"count": count, "bytes": total, "dir": str(_dir())}
