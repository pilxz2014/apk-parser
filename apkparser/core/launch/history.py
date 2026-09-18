# -*- coding: utf-8 -*-
"""启动历史与「我的入口」。

存在用户配置目录（`~/.apk-parser/`），不污染项目目录，也不会因为
清 cache 而丢掉常用入口。
"""

import json
import random
import threading
import time
from typing import Dict, List, Optional

from ...platform import paths

_lock = threading.RLock()
MAX_HISTORY = 200


def _file(name: str):
    return paths.config_dir() / name


def _read(name: str, default):
    try:
        f = _file(name)
        if f.is_file():
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, type(default)):
                return data
    except Exception:
        pass
    return default


def _write(name: str, data) -> bool:
    try:
        f = _file(name)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 历史

def record(intent, result: dict, device: str = "") -> None:
    """记一条启动记录（失败也记 —— 排查时最需要）。"""
    with _lock:
        items = _read("history.json", [])
        items.insert(0, {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts": time.time(),
            "device": device,
            "component": intent.normalized_target or intent.action or "(隐式 Intent)",
            "kind": intent.kind,
            "ok": bool(result.get("ok")),
            "result": (result.get("detail") or "")[:200],
            "intent": intent.to_dict(),
            "totalTime": result.get("totalTime", 0),
        })
        _write("history.json", items[:MAX_HISTORY])


def list_entries(limit: int = 20, only_ok: bool = False) -> List[dict]:
    with _lock:
        items = _read("history.json", [])
    if only_ok:
        items = [i for i in items if i.get("ok")]
    out = []
    for i in items[:limit]:
        out.append({
            "time": i.get("time", ""),
            "component": i.get("component", ""),
            "result": ("✔ " if i.get("ok") else "✖ ") + (i.get("result") or ""),
            "ok": i.get("ok", False),
            "device": i.get("device", ""),
            "kind": i.get("kind", "activity"),
            "intent": i.get("intent", {}),
        })
    return out


def clear() -> dict:
    with _lock:
        _write("history.json", [])
    return {"ok": True, "detail": "启动历史已清空"}


# ---------------------------------------------------------------- 我的入口
#
# 为什么每条要有**稳定 id**：
# 老实现拿 `name` 当主键，"重命名"就只能删旧的加新的 —— 一旦改名那一刻
# 刚好失败，条目就凭空少了。id 与显示名解耦后，改名只是改一个字段。
#
# 为什么不叫 `fav`：`shortcut` 这个名字已经在 CLI 子命令、API 路由、
# 老书签里用着了，改名的收益为零、风险是满地找引用。

def _shortcut_id() -> str:
    return "sc_%d_%04x" % (int(time.time() * 1000) & 0xFFFFFFFFFFFF,
                           random.randint(0, 0xFFFF))


def list_shortcuts() -> List[dict]:
    """入口列表。**读的时候补 id** —— 老数据里没有，不能要求用户重存一遍。"""
    with _lock:
        items = _read("shortcuts.json", [])
        dirty = False
        for i in items:
            if not i.get("id"):
                i["id"] = _shortcut_id()
                dirty = True
        if dirty:
            _write("shortcuts.json", items)
        return items


def add_shortcut(name: str, component: str, args: str = "",
                 intent: dict = None, note: str = "") -> dict:
    """存一个入口。

    `intent` 是**整条** Intent（含 action/data/categories/flags/extras）——
    只存 component 的话，一条带 extras 的启动命令收藏起来就残废了。
    """
    name = (name or "").strip()
    component = (component or "").strip()
    if not name:
        return {"ok": False, "detail": "请给这个入口起个名字"}
    if not component and not (intent or {}).get("action"):
        return {"ok": False, "detail": "这条没有可启动的目标（既没有组件也没有 Action）"}
    with _lock:
        items = list_shortcuts()
        if any(i.get("name") == name for i in items):
            return {"ok": False, "detail": "已经有一个叫「%s」的入口了，换个名字" % name}
        items.insert(0, {
            "id": _shortcut_id(),
            "name": name,
            "component": component,
            "args": args or "",
            "note": note or "",
            "intent": intent or {},
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        _write("shortcuts.json", items[:80])
    return {"ok": True, "detail": "已收藏「%s」" % name}


def edit_shortcut(sid: str, patch: dict) -> dict:
    """改正/重命名一个入口。

    `sid` 同时接受 id 和旧数据里的 name —— 老书签传的是 name，
    为它单独留一条兼容路径比让调用方去查 id 便宜。
    """
    sid = str(sid or "").strip()
    if not sid:
        return {"ok": False, "detail": "缺少入口标识（id 或 name）"}
    with _lock:
        items = list_shortcuts()
        hit = None
        for i in items:
            if i.get("id") == sid or i.get("name") == sid:
                hit = i
                break
        if hit is None:
            return {"ok": False, "detail": "找不到这个入口：%s" % sid}

        new_name = str(patch.get("name", hit.get("name") or "")).strip()
        if not new_name:
            return {"ok": False, "detail": "名字不能为空"}
        # 改名撞车要拦住：列表上两行同名，用户没法分辨哪个是哪个
        for i in items:
            if i is not hit and i.get("name") == new_name:
                return {"ok": False, "detail": "已经有一个叫「%s」的入口了" % new_name}

        hit["name"] = new_name
        if "component" in patch:
            hit["component"] = str(patch.get("component") or "").strip()
        if "args" in patch:
            hit["args"] = str(patch.get("args") or "")
        if "note" in patch:
            hit["note"] = str(patch.get("note") or "")
        if isinstance(patch.get("intent"), dict) and patch["intent"]:
            hit["intent"] = patch["intent"]
        hit["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write("shortcuts.json", items)
    return {"ok": True, "detail": "已更新「%s」" % new_name, "shortcut": hit}


def del_shortcut(sid: str) -> dict:
    """删除。`sid` 同样接受 id 或 name。"""
    sid = str(sid or "").strip()
    if not sid:
        return {"ok": False, "detail": "缺少入口标识（id 或 name）"}
    with _lock:
        items = list_shortcuts()
        keep, gone = [], None
        for i in items:
            if i.get("id") == sid or i.get("name") == sid:
                gone = i
            else:
                keep.append(i)
        if gone is None:
            return {"ok": False, "detail": "找不到这个入口：%s" % sid}
        _write("shortcuts.json", keep)
    return {"ok": True, "detail": "已删除「%s」" % gone.get("name", sid)}
