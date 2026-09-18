# -*- coding: utf-8 -*-
"""按「文件名 + 体积」在常见目录里把文件找回来（Web 拖拽用）。

**为什么要这么绕**：浏览器出于安全**不把拖入文件的真实路径**给网页
（`File.path` 是 Electron 才有的私货），只能拿到 `name / size / lastModified`。
但本工具的服务端就在同一台机器上 —— 拿这三个特征去常见目录里搜一遍，
命中率足够高（体积按字节精确比对，几乎不会错判）。

搜索是**有边界**的：只扫 `paths.known_dirs()` + 配置追加的目录，
限制深度、条目数与耗时，绝不"全盘扫描"。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from ...platform import paths

# 明显不该进去翻的目录（既慢又不可能放 APK）
SKIP_NAMES = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".idea", ".gradle",
    ".android", ".ccache", "$RECYCLE.BIN", "System Volume Information",
    "Windows", "WindowsApps", "Program Files", "Program Files (x86)",
    "ProgramData", "AppData", "WSL", "Temp", "temp", "tmp",
    # 本工具自己的产物目录，扫了纯浪费时间
    "cache", "output", "logs",
}

APK_SUFFIXES = (".apk", ".apks", ".xapk", ".aab")

MAX_DEPTH = 5           # 相对各搜根的最大深度
MAX_ENTRIES = 60000     # 最多看多少个目录项（防呆）
DEFAULT_BUDGET = 4.0    # 秒


def search_roots(extra: Optional[List[str]] = None) -> List[Path]:
    """搜索起点：常见用户目录 + 配置追加目录。"""
    out: List[Path] = []
    seen = set()

    def add(p):
        try:
            p = Path(os.path.expandvars(os.path.expanduser(str(p))))
        except Exception:
            return
        key = str(p).lower()
        if key in seen or not p.is_dir():
            return
        seen.add(key)
        out.append(p)

    for p in paths.known_dirs():
        add(p)
    for p in (extra or []):
        add(p)
    return out


def roots_info(extra: Optional[List[str]] = None) -> List[dict]:
    """搜索起点的**可读说明**（哪几个目录、各自是什么来头）。

    拖拽定位命中后要把"为什么是这条路径"讲清楚 —— 起点清单就是证据。
    """
    out = []
    for f in paths.known_folders():
        if not f["exists"]:
            continue
        out.append({
            "path": f["path"], "title": f["title"],
            "redirected": f.get("redirected", False),
            "defaultPath": f.get("defaultPath", ""),
        })
    out.append({"path": str(paths.project_root()), "title": "工具目录", "redirected": False})
    for p in (extra or []):
        try:
            p = Path(os.path.expandvars(os.path.expanduser(str(p))))
        except Exception:
            continue
        if p.is_dir():
            out.append({"path": str(p), "title": "自定义目录", "redirected": False})
    return out


def _score(cand: Path, want_name: str, want_size: int, want_mtime: int) -> int:
    """越像分数越低。"""
    s = 0
    if cand.name.lower() != want_name.lower():
        s += 100
    try:
        st = cand.stat()
    except OSError:
        return s + 50
    if want_size and st.st_size != want_size:
        s += 40
    if want_mtime:
        s += min(30, int(abs(st.st_mtime - want_mtime)))
    return s


def locate(name: str, size: int = 0, mtime: int = 0,
           extra_dirs: Optional[List[str]] = None,
           roots: Optional[List[Path]] = None,
           budget: float = DEFAULT_BUDGET) -> Dict:
    """在常见目录里找 `name`。

    :param size: 期望字节数（浏览器给的 `File.size`，精确值，用于排除同名异文件）
    :param mtime: 期望修改时间（秒；只用于排序，不做硬过滤 —— 时区/精度都可能差）
    :returns: `{ok, path, candidates, roots, scanned, elapsedMs, hint}`
    """
    t0 = time.time()
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "缺少文件名", "candidates": []}

    use_roots = roots if roots is not None else search_roots(extra_dirs)
    found: List[Dict] = []
    scanned = 0
    truncated = False

    for root in use_roots:
        if time.time() - t0 > budget:
            truncated = True
            break
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(str(root), onerror=lambda _e: None):
            if time.time() - t0 > budget:
                truncated = True
                break
            # 剪枝：跳过系统/缓存目录，控制深度
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_NAMES and not d.startswith(".")
                           and not d.startswith("$")]
            depth = len(Path(dirpath).parts) - base_depth
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            scanned += len(filenames) + len(dirnames)
            if scanned > MAX_ENTRIES:
                truncated = True
                break

            lower = name.lower()
            for fn in filenames:
                if fn.lower() != lower:
                    continue
                p = Path(dirpath) / fn
                found.append({
                    "path": str(p),
                    "size": _safe_size(p),
                    "mtime": _safe_mtime(p),
                    "score": _score(p, name, size, mtime),
                })

    found.sort(key=lambda c: (c["score"], -c["mtime"]))
    hit = found[0] if found and found[0]["score"] < 40 else None
    # 给每个候选附上"它落在哪个已知文件夹里"——用户对着一串 D 盘路径才不会懵。
    # 不认识的目录也要把三个 key 摆上（空值）：前端直接读 c.folder，缺 key 会拿到
    # undefined 并渲染出 "undefined" 三个字。
    for c in found[:8]:
        try:
            f = paths.folder_of(c["path"])
        except Exception:
            f = {}
        c["folder"] = f.get("title", "")
        c["folderRoot"] = f.get("path", "")
        c["folderRedirected"] = bool(f.get("redirected"))
    out = {
        "ok": bool(hit),
        "path": hit["path"] if hit else "",
        "candidates": found[:8],
        "roots": [str(r) for r in use_roots],
        "rootsInfo": roots_info(extra_dirs),
        "scanned": scanned,
        "elapsedMs": int((time.time() - t0) * 1000),
        "truncated": truncated,
    }
    if hit:
        # 命中路径的"归属说明"：重定向过的目录要额外解释一句
        try:
            f = paths.folder_of(hit["path"])
        except Exception:
            f = {}
        if f:
            out["folder"] = f.get("title", "")
            out["folderRoot"] = f.get("path", "")
            out["folderNote"] = f.get("note", "")
            out["folderRedirected"] = bool(f.get("redirected"))
            out["folderDefault"] = f.get("defaultPath", "")
    if not hit:
        out["hint"] = ("在 %d 个常见目录里没找到叫「%s」的文件；"
                       "浏览器不提供拖入文件的真实路径，请用「浏览…」按钮直接选。"
                       % (len(use_roots), name))
    elif len(found) > 1:
        out["ambiguous"] = True
        out["hint"] = "找到 %d 个同名文件，已按体积/时间取最像的一个" % len(found)
    return out


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return -1


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0
