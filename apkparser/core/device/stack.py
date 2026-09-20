# -*- coding: utf-8 -*-
"""设备 Activity 堆栈 —— 把 `dumpsys activity activities` 拆成结构化数据。

为什么要单独一个模块：那条命令一个人读不动 —— 真机上一次输出 480+ 行、
36KB，有用的信息（当前在前台的是谁、栈里有几个任务、每个 Activity 什么状态）
全埋在 `Task{...}` / `Hist #0: ActivityRecord{...}` 这类行里，而且**同一份内容
在输出里会出现两次**（先是 "activities from top to bottom"，后面
"Task display areas in top down Z order" 又来一遍）。直接把原文丢给用户，
等于让他自己在 480 行里找那个 `state=RESUMED`。

所以这里做两件事：
  1. `parse(text)` —— 纯函数，把原文拆成 display → task → activity 三层
  2. `snapshot(device)` —— 跑一次 adb，把 1 的结果包成接口响应

`parse` 是纯函数（不碰 adb、不读盘），所以拿一份真机样本就能直接测 ——
格式是 ROM 给的，各 Android 版本长得还不一样，能脱离设备测是硬需求。
"""

import re
import time
from typing import Dict, List, Optional

from . import adb as adb_mod

DUMP_CMD = "dumpsys activity activities"

# 解析到这里为止：后面是同一批任务的**第二次**罗列（Z order 视角），
# 再往下会把每个任务算两遍。
_STOP_MARKERS = (
    "Task display areas in top down Z order",
    "Display areas in top down Z order",
)

_TASK_RE = re.compile(
    r"^(?P<ind>\s*)\*\s*Task\{(?P<hash>[0-9a-f]+)\s+#(?P<id>\d+)(?P<rest>[^}]*)\}")

_HIST_RE = re.compile(
    r"^\s*\*\s*Hist\s*#(?P<idx>\d+):\s*ActivityRecord\{(?P<hash>[0-9a-f]+)"
    r"\s+u(?P<user>\d+)\s+(?P<comp>\S+)\}\s*t(?P<task>\d+)"
    r"(?:\s+d(?P<display>\d+))?")

# `topResumedActivity=ActivityRecord{9d6af95 u0 com.x/.A} t3831 d0}`
# `mFocusedApp=ActivityRecord{...}`、`mLastPausedActivity: ActivityRecord{...}`
_AR_RE = re.compile(
    r"ActivityRecord\{(?P<hash>[0-9a-f]+)\s+u(?P<user>\d+)\s+(?P<comp>[^\s}]+)\}")

_WINDOW_RE = re.compile(r"Window\{[0-9a-f]+\s+u\d+\s+(?P<comp>[^\s}]+)")

_KV_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)=([^\s,]+)")

# ---------------------------------------------------------------- 状态文案

STATE_TEXT = {
    "RESUMED": "前台运行中",
    "PAUSED": "已暂停",
    "STOPPED": "已停止",
    "DESTROYED": "已销毁",
    "DESTROYING": "销毁中",
    "INITIALIZING": "启动中",
    "RESUMED_ON_TOP": "前台运行中",
}

MODE_TEXT = {
    "fullscreen": "全屏",
    "multi-window": "多窗口",
    "split-screen-primary": "分屏（主）",
    "split-screen-secondary": "分屏（副）",
    "freeform": "自由窗口",
    "pip": "画中画",
    "undefined": "未定义",
}

# 字段的**中文标签与说明**。前端直接拿它渲染 —— 用户看的是"包名"不是
# "packageName"；说明解决"这玩意儿到底是啥"。加字段只改这一处。
FIELDS: Dict[str, dict] = {
    "package": {"label": "包名", "hint": "应用的唯一标识（applicationId），一台机器上不会重复"},
    "appLabel": {"label": "应用名", "hint": "resources.arsc 里的应用名，来自本机缓存；没补过就不显示"},
    "className": {"label": "类名", "hint": "Activity 的类名（清单里怎么写就怎么显示）"},
    "fullClass": {"label": "全限定类名", "hint": "包名 + 类名的完整写法，代码里找这个类用这个"},
    "component": {"label": "组件", "hint": "「包名/类名」，am start -n 要的就是这个写法"},
    "state": {"label": "状态", "hint": "RESUMED = 正在前台跑；PAUSED = 被别的界面盖住但还在栈里"},
    "visible": {"label": "可见", "hint": "窗口当前是否真的在屏幕上（不可见 ≠ 被销毁）"},
    "processName": {"label": "进程", "hint": "承载这个 Activity 的进程名（可能与包名不同）"},
    "pid": {"label": "进程 PID", "hint": "进程在设备上的 ID，杀进程 / 看日志用得上"},
    "userId": {"label": "用户", "hint": "Android 多用户 / 工作资料的 userId，主用户是 0"},
    "taskId": {"label": "任务 ID", "hint": "Activity 所属任务（就是最近任务列表里那一格）"},
    "displayId": {"label": "屏幕", "hint": "所在屏幕编号（外接屏 / 投屏会是 1、2…）"},
    "intent": {"label": "Intent", "hint": "启动这个 Activity 时带的 Intent（Action / flags / extras）"},
    "launchedFromPackage": {"label": "启动来源", "hint": "是谁把它拉起来的（另一个应用的包名）"},
    "launchedFromUid": {"label": "来源 UID", "hint": "启动来源在设备上的 Linux 用户 ID"},
    "launchCount": {"label": "启动次数", "hint": "这个实例被复用/重建的次数"},
    "lastLaunchTime": {"label": "最近启动", "hint": "距离现在多久之前启动（负数表示已经过去多久）"},
    "bounds": {"label": "窗口区域", "hint": "任务在屏幕上的矩形范围 [左上][右下]"},
    "mode": {"label": "窗口模式", "hint": "全屏 / 分屏 / 画中画 / 自由窗口"},
    "affinity": {"label": "任务归属", "hint": "taskAffinity：这个任务默认属于哪个应用"},
    "type": {"label": "任务类型", "hint": "standard / home / recent 等系统分类"},
    "size": {"label": "Activity 数", "hint": "这个任务栈里压了几个 Activity"},
}


def field_label(key: str) -> str:
    return (FIELDS.get(key) or {}).get("label", key)


def field_hint(key: str) -> str:
    return (FIELDS.get(key) or {}).get("hint", "")


# ---------------------------------------------------------------- 小工具

def _kv(line: str) -> Dict[str, str]:
    """一行里所有 `k=v`（`Intent {...}` 这种整段的不走这儿）。"""
    return {m.group(1): m.group(2) for m in _KV_RE.finditer(line)}


def split_component(comp: str, package: str = "") -> Dict[str, str]:
    """拆 `com.x/.A`、`com.x/com.y.A`、`com.x.A` 三种写法。

    dumpsys 给得并不统一：有的是 `pkg/.Class`（相对类名），有的是
    `pkg/com.other.Class`（**全限定**类名，跟 applicationId 不同源 —— 本机
    Z21 上 `com.jxw.aizx.teacher` 的类全在 `com.jxw.teacher.*` 下，就是这个）。
    拆错会把启动命令拼坏，所以三种写法都要认。
    """
    comp = (comp or "").strip()
    if "/" in comp:
        pkg, _, cls = comp.partition("/")
    elif package:
        pkg, cls = package, comp
    else:
        return {"component": comp, "package": "", "className": comp,
                "fullClass": comp}
    full = (pkg + cls) if cls.startswith(".") else cls
    return {"component": "%s/%s" % (pkg, cls), "package": pkg,
            "className": cls, "fullClass": full}


def _strip_uid(affinity: str) -> str:
    """`A=10127:com.jxw.student.ctb` → `com.jxw.student.ctb`（老格式没有 uid 前缀）。"""
    a = (affinity or "").strip()
    if ":" in a:
        head, _, tail = a.partition(":")
        if head.isdigit():
            return tail
    return a


def _mode_text(mode: str) -> str:
    return MODE_TEXT.get((mode or "").strip(), mode or "")


def _state_text(state: str) -> str:
    return STATE_TEXT.get((state or "").strip().upper(), state or "")


# ---------------------------------------------------------------- 解析

def parse(text: str) -> dict:
    """把 dumpsys 原文拆成结构化数据（纯函数，不碰 adb）。"""
    displays: List[dict] = []
    cur_display: Optional[dict] = None
    cur_task: Optional[dict] = None
    cur_act: Optional[dict] = None

    focused = {"component": "", "package": "", "className": "", "raw": ""}
    resumed = {"component": "", "package": "", "className": "", "raw": ""}
    focus_window = ""

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if any(m in line for m in _STOP_MARKERS):
            break

        # ---- 全局焦点信息（在哪个缩进层都有，扫到就记）----
        if "mFocusedApp=" in line:
            m = _AR_RE.search(line)
            if m:
                focused = dict(split_component(m.group("comp")),
                               raw=line.strip(), hash=m.group("hash"))
        if "topResumedActivity=" in line or "mResumedActivity" in line \
                or "mFocusedActivity" in line:
            m = _AR_RE.search(line)
            if m and not resumed.get("component"):
                resumed = dict(split_component(m.group("comp")),
                               raw=line.strip(), hash=m.group("hash"))
        if "mCurrentFocus=" in line:
            m = _WINDOW_RE.search(line)
            if m:
                focus_window = m.group("comp")

        # ---- Display ----
        m = re.match(r"^Display #(?P<id>\d+)", line)
        if m:
            cur_display = {"id": int(m.group("id")), "tasks": []}
            displays.append(cur_display)
            cur_task = cur_act = None
            continue

        # ---- Task ----
        m = _TASK_RE.match(line)
        if m:
            rest = m.group("rest")
            kv = _kv(rest)
            aff = _strip_uid(kv.get("A", "") or kv.get("I", ""))
            task = {
                "id": int(m.group("id")),
                "hash": m.group("hash"),
                "depth": len(m.group("ind")) // 2,
                "type": kv.get("type", ""),
                "affinity": aff,
                "affinityPackage": aff,
                "userId": kv.get("U", ""),
                "visible": kv.get("visible", "") == "true",
                "visibleRequested": kv.get("visibleRequested", "") == "true",
                "mode": kv.get("mode", ""),
                "modeText": _mode_text(kv.get("mode", "")),
                "translucent": kv.get("translucent", "") == "true",
                "size": int(kv.get("sz", "0") or 0),
                "rootTaskId": kv.get("rootTaskId", ""),
                "bounds": "",
                "activities": [],
                "topComponent": "",
            }
            if cur_display is None:          # 有些 ROM 不打印 Display 行
                cur_display = {"id": 0, "tasks": []}
                displays.append(cur_display)
            cur_display["tasks"].append(task)
            cur_task, cur_act = task, None
            continue

        # ---- Activity（Hist）----
        m = _HIST_RE.match(line)
        if m:
            act = {
                "index": int(m.group("idx")),
                "hash": m.group("hash"),
                "userId": m.group("user"),
                "taskId": int(m.group("task")),
                "displayId": m.group("display") or "",
                "package": "", "className": "", "component": "", "fullClass": "",
                "state": "", "stateText": "", "visible": False,
                "visibleRequested": False,
                "intent": "", "launchedFromPackage": "", "launchedFromUid": "",
                "processName": "", "pid": "",
                "launchCount": "", "lastLaunchTime": "", "lastVisibleTime": "",
                "isTop": False, "isResumed": False, "isFocused": False,
                "appLabel": "",
            }
            act.update(split_component(m.group("comp")))
            if cur_task is None:             # 兜底：Hist 出现在 Task 之前
                cur_task = {"id": act["taskId"], "hash": "", "depth": 0, "type": "",
                            "affinity": "", "affinityPackage": "", "userId": "",
                            "visible": False, "visibleRequested": False, "mode": "",
                            "modeText": "", "translucent": False, "size": 0,
                            "rootTaskId": "", "bounds": "", "activities": [],
                            "topComponent": ""}
                if cur_display is None:
                    cur_display = {"id": 0, "tasks": []}
                    displays.append(cur_display)
                cur_display["tasks"].append(cur_task)
            cur_task["activities"].append(act)
            cur_task["topComponent"] = act["component"]
            cur_act = act
            continue

        # ---- 归属到当前 task / activity 的补充行 ----
        s = line.strip()
        if cur_task is not None and s.startswith("bounds="):
            cur_task["bounds"] = s[len("bounds="):]
            continue
        if cur_act is None:
            continue

        if s.startswith("Intent {"):
            cur_act["intent"] = s
            continue
        kv = _kv(line)
        if not kv:
            continue
        if "packageName" in kv:
            cur_act["package"] = kv["packageName"]
            # 包名这会儿才确定 → 之前按 `pkg/FullClass` 拆的结果要按包名重拆一次
            if cur_act["className"] and not cur_act["className"].startswith(".") \
                    and "." in cur_act["className"]:
                cur_act["component"] = "%s/%s" % (kv["packageName"],
                                                  cur_act["className"])
        if "processName" in kv:
            cur_act["processName"] = kv["processName"]
        if "state" in kv:
            cur_act["state"] = kv["state"]
            cur_act["stateText"] = _state_text(kv["state"])
            cur_act["isResumed"] = kv["state"].upper().startswith("RESUMED")
        if "mVisible" in kv:
            cur_act["visible"] = kv["mVisible"] == "true"
        if "mVisibleRequested" in kv:
            cur_act["visibleRequested"] = kv["mVisibleRequested"] == "true"
        if "launchedFromPackage" in kv:
            cur_act["launchedFromPackage"] = kv["launchedFromPackage"]
        if "launchedFromUid" in kv:
            cur_act["launchedFromUid"] = kv["launchedFromUid"]
        if "launchCount" in kv:
            cur_act["launchCount"] = kv["launchCount"]
        if "lastLaunchTime" in kv:
            cur_act["lastLaunchTime"] = kv["lastLaunchTime"]
        if "lastVisibleTime" in kv:
            cur_act["lastVisibleTime"] = kv["lastVisibleTime"]
        if "app" in kv and "ProcessRecord" in line:
            pm = re.search(r"ProcessRecord\{\S+\s+(\d+):", line)
            if pm:
                cur_act["pid"] = pm.group(1)

    # ---- 收尾：给每个 task 标出栈顶，给全局认出"当前在前台的那个" ----
    top = {"component": "", "package": "", "className": "", "taskId": 0}
    packages: List[str] = []
    n_act = 0
    n_visible_task = 0
    for d in displays:
        for t in d["tasks"]:
            if t["visible"]:
                n_visible_task += 1
            if not t["activities"]:
                continue
            n_act += len(t["activities"])
            t["activities"][-1]["isTop"] = True
            for a in t["activities"]:
                pk = a.get("package") or ""
                if pk and pk not in packages:
                    packages.append(pk)
                if focused.get("component") and a["component"] == focused["component"]:
                    a["isFocused"] = True
            if not top["component"]:
                a = t["activities"][-1]
                top = {"component": a["component"], "package": a["package"],
                       "className": a["className"], "taskId": t["id"],
                       "fullClass": a.get("fullClass", "")}

    if not resumed.get("component"):         # 没扫到 topResumed 就用栈顶
        resumed = dict(top, raw="", hash="") if top.get("component") else resumed
    if not focused.get("component") and resumed.get("component"):
        focused = dict(resumed)

    return {
        "displays": displays,
        "focused": focused,
        "resumed": resumed,
        "top": top,
        "focusWindow": focus_window,
        "packages": packages,
        "counts": {
            "displays": len(displays),
            "tasks": sum(len(d["tasks"]) for d in displays),
            "visibleTasks": n_visible_task,
            "activities": n_act,
            "packages": len(packages),
        },
    }


# ---------------------------------------------------------------- 取数

_CACHE = {"serial": "", "ts": 0.0, "data": None}
_TTL = 3.0


def fetch(device: str = "") -> dict:
    """跑一次 adb，返回 {"ok", "text", "command", "error", "elapsedMs"}。"""
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "error": str(e), "text": "", "command": "", "elapsedMs": 0}
    t0 = time.time()
    r = adb_mod.shell_cmd(DUMP_CMD, serial=serial, timeout=20.0)
    elapsed = int((time.time() - t0) * 1000)
    cmd = adb_mod.command_preview(["shell"] + DUMP_CMD.split(), serial)
    if r.rc != 0 and not (r.out or "").strip():
        return {"ok": False, "error": (r.err or "adb 执行失败").strip()[:300],
                "text": "", "command": cmd, "elapsedMs": elapsed}
    return {"ok": True, "text": r.out or "", "command": cmd, "error": "",
            "elapsedMs": elapsed, "device": serial}


def snapshot(device: str = "", refresh: bool = False) -> dict:
    """当前设备堆栈（结构化）。

    `refresh=False` 且 3 秒内有结果就直接复用 —— 页面上有刷新按钮，
    但偶发的重复点击不该变成一串 adb 调用。
    """
    now = time.time()
    if not refresh and _CACHE["data"] and _CACHE["serial"] == device \
            and now - _CACHE["ts"] < _TTL:
        out = dict(_CACHE["data"])
        out["cached"] = True
        return out

    got = fetch(device)
    if not got.get("ok"):
        return {"ok": False, "error": got.get("error") or "读取失败",
                "command": got.get("command", ""), "device": device,
                "displays": [], "counts": {}, "packages": []}

    data = parse(got["text"])
    out = {
        "ok": True,
        "device": got.get("device", device),
        "command": got["command"],
        "elapsedMs": got["elapsedMs"],
        "cached": False,
        "rawBytes": len(got["text"]),
        "rawLines": got["text"].count("\n") + 1,
        "fields": FIELDS,
    }
    out.update(data)
    _enrich_labels(out)
    _CACHE.update({"serial": device, "ts": now, "data": out})
    return out


def _enrich_labels(out: dict) -> None:
    """顺手给包名补上应用名（**只用本机缓存**，不额外读设备）。

    缓存没有就留空 —— 拿不到真名硬塞一个占位串，界面上会多出一堆
    「null」「?0x7f…」，那是在骗人。
    """
    try:
        from . import labels as labels_mod
        labels_mod.refresh_if_changed()
        for d in out.get("displays", []):
            for t in d.get("tasks", []):
                for a in t.get("activities", []):
                    if a.get("package"):
                        a["appLabel"] = labels_mod.get(a["package"]) or ""
    except Exception:
        pass
