# -*- coding: utf-8 -*-
"""**本机**目录浏览（设备那边是 `core/files.py`，两者不要混）。

给"导出到哪儿 / 扫哪个目录"这类**本机路径**的场景用。存在的理由是：
让人手打自己磁盘的绝对路径是没道理的 —— 服务端就在本机，列一下目录并不难。

## 设计取舍

* **只列目录和文件名/大小/时间，绝不读内容** —— 页面要的是"点进去选一个"，不是文件管理器。
* 默认**隐藏**以 `.` 开头的项（`.git`/`.workbuddy` 之类刷屏），但可以打开。
* 不为安全做白名单：这是用户自己机器上的本机工具，服务端只监听回环地址。
  做白名单只会让"我 APK 放在 D 盘某个奇怪目录"变成不可用。
"""

from __future__ import annotations

import os
import string
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import paths

__all__ = ["ls_dir", "roots"]


def _crumbs(p: Path) -> List[Dict[str, str]]:
    """面包屑。Windows 上从盘符开始（`D:\\` → `a` → `b`），不列空的根。"""
    parts: List[Dict[str, str]] = []
    p = p.resolve()
    if paths.IS_WINDOWS:
        anchor = p.anchor or "\\"
        parts.append({"name": anchor, "path": anchor})
        rel = str(p)[len(anchor):]
    else:
        parts.append({"name": "/", "path": "/"})
        rel = str(p)[1:]
    cur = parts[0]["path"]
    for seg in [s for s in rel.replace("\\", "/").split("/") if s]:
        cur = os.path.join(cur, seg) if cur.endswith(os.sep) or cur.endswith("/") \
            else os.path.join(cur, seg)
        parts.append({"name": seg, "path": str(Path(cur))})
    return parts


def drives() -> List[Dict[str, str]]:
    """本机可用盘符（非 Windows 上就是 `/`）。"""
    if not paths.IS_WINDOWS:
        return [{"name": "/", "path": "/"}]
    out = []
    for ch in string.ascii_uppercase:
        root = "%s:\\" % ch
        try:
            if Path(root).exists():
                out.append({"name": ch + ":", "path": root})
        except OSError:
            continue
    return out


def roots() -> Dict[str, object]:
    """页面进来看见的"起点"：常用文件夹 + 盘符。"""
    try:
        known = paths.known_folders()
    except Exception:
        known = []
    return {"drives": drives(), "known": known}


def ls_dir(path: str = "", dirs_only: bool = False,
           show_hidden: bool = False) -> Dict:
    """列本机目录。

    返回结构与 `core/files.py` 的 `ls()` 保持**同一个形状**（`entries` /
    `crumbs` / `parent` / `detail`），这样前端能用同一套渲染思路，也少一处
    让人记混的差异。区别只有：路径分隔符是 Windows 反斜杠，且没有 adb。
    """
    raw = (path or "").strip()
    if not raw:
        # 默认落在用户主目录 —— 比落在盘符根更可能是他想要的地方
        raw = str(Path.home())
    try:
        p = Path(raw).expanduser()
    except (OSError, ValueError) as e:
        return {"ok": False, "detail": "路径写错了：%s" % e, "entries": [],
                "path": raw, "crumbs": [], "parent": ""}

    if not p.exists():
        parent = p.parent
        return {
            "ok": False, "path": str(p), "detail": "路径不存在：%s" % p,
            "hint": ("上一级 %s %s" % (parent, "存在，可以从那里找" if parent.is_dir()
                                      else "也不存在")),
            "entries": [], "crumbs": _crumbs(parent if parent.is_dir() else Path.home()),
            "parent": str(parent),
        }
    if p.is_file():
        # 传了个文件进来：告诉用户"这是文件"，并把它的上级目录列出来
        r = ls_dir(str(p.parent), dirs_only=dirs_only, show_hidden=show_hidden)
        r["isFile"] = True
        r["pickedFile"] = str(p)
        r["detail"] = "这是一个文件：%s（下面是它所在的目录）" % p.name
        return r

    try:
        p = p.resolve()
    except OSError:
        pass

    entries: List[Dict] = []
    skipped = 0
    try:
        with os.scandir(str(p)) as it:
            for e in it:
                name = e.name
                if not show_hidden and name.startswith("."):
                    continue
                try:
                    is_dir = e.is_dir()
                    is_link = e.is_symlink()
                except OSError:
                    skipped += 1
                    continue
                if dirs_only and not is_dir:
                    continue
                size = None
                mtime = 0
                if not is_dir:
                    try:
                        st = e.stat()
                        size = st.st_size
                        mtime = int(st.st_mtime)
                    except OSError:
                        skipped += 1
                target = ""
                if is_link:
                    try:
                        target = os.readlink(e.path)
                    except OSError:
                        target = ""
                entries.append({
                    "name": name,
                    "path": str(Path(e.path)),
                    "dir": is_dir,
                    "link": is_link,
                    "linkTarget": target,
                    "size": size,
                    "mtime": mtime,
                })
    except PermissionError:
        return {"ok": False, "path": str(p),
                "detail": "没有权限读取：%s" % p,
                "hint": "系统保护目录（如 C:\\Windows\\System32\\config）普通权限读不了",
                "entries": [], "crumbs": _crumbs(p), "parent": str(p.parent)}
    except OSError as e:
        return {"ok": False, "path": str(p), "detail": "列目录失败：%s" % e,
                "entries": [], "crumbs": _crumbs(p), "parent": str(p.parent)}

    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    dirs = sum(1 for x in entries if x["dir"])
    res: Dict[str, object] = {
        "ok": True,
        "path": str(p),
        "detail": "%d 项（%d 目录 / %d 文件）" % (len(entries), dirs, len(entries) - dirs),
        "count": len(entries), "dirCount": dirs, "fileCount": len(entries) - dirs,
        "entries": entries,
        "parent": str(p.parent),
        "crumbs": _crumbs(p),
        "dirsOnly": bool(dirs_only),
        "hiddenShown": bool(show_hidden),
        "roots": roots(),
    }
    if skipped:
        res["warning"] = "有 %d 项读不到（权限或已被删除）" % skipped
    return res


if __name__ == "__main__":       # 手工看一眼（开发用）
    import json
    print(json.dumps(ls_dir(sys.argv[1] if len(sys.argv) > 1 else ""),
                     ensure_ascii=False, indent=2)[:2000])
