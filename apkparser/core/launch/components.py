# -*- coding: utf-8 -*-
"""组件列表与启动。

**重要实测结论（Z21 / Android 13）**：这台 ROM 的 `dumpsys package <pkg>`
只输出 `Receiver Resolver Table`，**没有** Activity / Service 段 ——
所以"靠 dumpsys 列组件"在真机上不可行。

于是定成两条路，APK 解析为主：

  ① 主路径：拉回 base.apk（免 root，已验证 35 MB/s）→ 解析 manifest
     → 拿到**全部**组件 + exported + intent-filter 详情（比 dumpsys 更全）
  ② 回退：dumpsys / `cmd package` 的 Resolver Table（部分 ROM 有，快但常缺）

拉包结果与签名功能共用同一份缓存，所以"看签名"之后再看组件列表是秒开的。
"""

import re
from typing import Dict, List, Optional

from ...platform import paths
from ..device import adb as adb_mod
from . import intent as intent_mod

# dumpsys Resolver Table 里的组件行：
#   cfd65bd com.jxw.student.xueqing/androidx.profileinstaller.ProfileInstallReceiver filter a2ce280
_RESOLVER_ROW = re.compile(
    r"^\s+[0-9a-f]{6,}\s+(?P<comp>[\w.]+/[\w.$]+)\s+filter\s+[0-9a-f]+", re.M)
# 直接以 pkg/Class: 形式出现的 provider 行
_PROVIDER_ROW = re.compile(r"^\s+(?P<comp>[\w.]+/[\w.$]+):\s*$", re.M)
_TABLE_RE = re.compile(
    r"^\s*(?P<kind>Activity|Service|Receiver|Provider)\s+Resolver Table:\s*$", re.M)


# ---------------------------------------------------------------- dumpsys 回退

def _components_from_dumpsys(package: str, serial: str) -> Dict[str, List[str]]:
    args = ["shell", "dumpsys", "package", package]
    r = adb_mod.run(args, serial=serial, timeout=45)
    text = r.out or ""
    found: Dict[str, List[str]] = {"activity": [], "service": [], "receiver": [], "provider": []}
    if not text:
        return found

    # 按 "XXX Resolver Table:" 分段
    marks = list(_TABLE_RE.finditer(text))
    for i, m in enumerate(marks):
        kind = m.group("kind").lower()
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        seg = text[start:end]
        for row in _RESOLVER_ROW.finditer(seg):
            comp = row.group("comp")
            if comp.startswith(package) and comp not in found[kind]:
                found[kind].append(comp)

    # ContentProvider 单独有一段 "Registered ContentProviders:"
    seg = re.search(r"Registered ContentProviders:(.*?)(?:\n\s*\n|\Z)", text, re.S)
    if seg:
        for row in _PROVIDER_ROW.finditer(seg.group(1)):
            comp = row.group("comp")
            if comp.startswith(package) and comp not in found["provider"]:
                found["provider"].append(comp)
    return found


# ---------------------------------------------------------------- 主入口

def list_components(package: str, ctype: str = "", device: str = "",
                    source: str = "auto", refresh: bool = False,
                    progress=None) -> List[dict]:
    """列出组件的可启动清单。

    :param source: auto（先 APK 后 dumpsys）/ apk / dumpsys
    """
    serial = adb_mod.pick_device(device)

    if source in ("auto", "apk"):
        got = _from_apk(package, serial, refresh=refresh, progress=progress)
        if got is not None:
            return _filter(got, ctype)
        if source == "apk":
            return []

    return _filter(_from_dumpsys_as_dict(package, serial), ctype)


def _filter(items: List[dict], ctype: str) -> List[dict]:
    if ctype:
        items = [c for c in items if c["type"] == ctype]
    # 可启动的排前面，其次 launcher，最后按名字
    items.sort(key=lambda c: (not c.get("launchable"), not c.get("hasLauncher"), c["name"]))
    return items


def _from_apk(package: str, serial: str, refresh: bool = False, progress=None) -> Optional[List[dict]]:
    """拉包 + 解析 manifest。失败返回 None（交给回退路径）。"""
    def say(stage, msg):
        if progress:
            try:
                progress(stage, msg)
            except Exception:
                pass

    try:
        from ..device import signature as sig_mod
        detail_version = 0
        try:
            from ..device import apps as apps_mod
            d = apps_mod.app_detail(package, serial)
            detail_version = int(d.get("versionCode") or 0)
        except Exception:
            pass

        if refresh:
            pass
        pulled = sig_mod.pull_base_apk(package, serial, detail_version, progress)
        if not pulled.get("ok"):
            say("fallback", "拉包失败，改用 dumpsys：%s" % pulled.get("reason", ""))
            return None

        say("parse", "解析组件…")
        from ..apk.info import parse_apk
        info = parse_apk(pulled["path"], with_signature=False, with_icon=False)
        if info.error or not info.package:
            return None

        out = []
        for c in info.components():
            d = c.to_dict()
            d["source"] = "apk"
            # `Component.name` 是**全限定类名**（com.jxw.xwsf.ui.WifiListActivity），
            # 不带斜杠。界面拿它当 target 传给 Intent 时定不出组件 ——
            # 历史上就是这么拼出了裸命令 `am start`。这里直接给出 am 能吃的
            # `pkg/.Class`，调用方不需要再自己拼。
            d["target"] = intent_mod.as_component(info.package, d.get("name", ""))
            # `am` 是从 shell 用户发起的：未导出的组件会直接 Permission Denial。
            # 与其让用户点了按钮才收到报错，不如在这里就标成"不可启动"。
            d["launchable"] = bool(d.get("exported")) and d.get("type") in (
                "activity", "service", "receiver")
            out.append(d)

        # 如果 manifest 里没有 launcher，补一个从 resolver 查到的（个别壳应用）
        if not any(x["hasLauncher"] for x in out):
            launcher = _resolve_launcher(package, serial)
            if launcher:
                for x in out:
                    if x["name"] == launcher:
                        x["hasLauncher"] = True
                        break
                else:
                    out.insert(0, {
                        "type": "activity", "name": launcher, "exported": True,
                        "enabled": True, "hasLauncher": True, "launchable": True,
                        "summary": "启动入口（运行时解析）", "source": "resolve",
                        "target": launcher, "intentFilters": [],
                    })
        return out
    except Exception:
        # 主路径失败要回退到 dumpsys，所以不能把异常抛出去；
        # 但**也不能默默咽掉** —— 否则"APK 解析永远失败、永远在走回退"这种问题
        # 在界面上了无痕迹。打到 stderr，用 APKPARSER_HTTP_LOG 之类的地方能看到。
        import os as _os
        import traceback as _tb
        if _os.environ.get("APKPARSER_DEBUG"):
            _tb.print_exc()
        else:
            import sys as _sys
            _sys.stderr.write("[components] 拉包解析组件失败，回退 dumpsys: %s\n"
                              % _tb.format_exc().strip().splitlines()[-1])
        return None


def _from_dumpsys_as_dict(package: str, serial: str) -> List[dict]:
    found = _components_from_dumpsys(package, serial)
    out = []
    for kind, comps in found.items():
        for comp in comps:
            out.append({
                "type": kind,
                "name": comp,
                "target": intent_mod.as_component(*comp.split("/", 1)) if "/" in comp else "",
                "exported": None,          # dumpsys 给不出，界面标"未知"
                "enabled": True,
                "hasLauncher": kind == "activity" and comp.endswith("MainActivity"),
                "launchable": kind == "activity",
                "summary": "（来自 dumpsys，缺少导出状态）",
                "intentFilters": [],
                "source": "dumpsys",
            })
    return out


def _resolve_launcher(package: str, serial: str) -> str:
    """`cmd package resolve-activity` 查启动入口（兜底）。"""
    return intent_mod.launcher_component(package, serial)


# ---------------------------------------------------------------- 启动

_STATUS_RE = re.compile(r"^(?P<k>Status|Error|Exception|Activity|Warning|TotalTime|WaitTime|ThisTime|Complete):?\s*(?P<v>.*)$")

# am 报错 → 中文解释 + 下一步
_AM_HINTS = [
    (r"no intent specified|intent has no component|no intent supplied",
     "命令里既没有组件也没有 Action。检查「目标组件」是不是只写了包名或裸类名；"
     "正确写法是 包名/.类名（com.foo/.ui.MainActivity），或者填 Action + Data 走 URI 方式。"),
    (r"Permission Denial.*not exported",
     "该组件未导出（manifest 里 android:exported=\"false\"），第三方应用无法启动它。"
     "如果是你自己的 App，把 exported 改成 true 或加一个 exported 的入口 Activity。"),
    (r"unable to resolve Intent|Error type 3",
     "系统找不到这个组件。可能类名写错了，或该组件在当前设备上不存在"
     "（检查是否装的是同一个包/版本）。"),
    (r"does not exist",
     "组件不存在。确认包名与类名都正确；也可以先用「列出组件」看真实名字。"),
    (r"requires the permission",
     "目标组件要求调用方持有某个权限，普通 adb shell 没有该权限。"),
    (r"not have permission",
     "权限不足（通常是组件 protected-broadcast 或 signature 级权限）。"),
    (r"Error: Activity not started",
     "Activity 没被拉起，具体原因见上面的 Status/Error 行。"),
    (r"SecurityException",
     "被系统安全策略拦下，通常是导出设置或权限问题。"),
    (r"java.lang.RuntimeException",
     "组件自身在启动过程中抛了异常 —— 用「启动后抓日志」看堆栈。"),
]


def start_component(intent, device: str = "", wait: bool = False,
                    timeout: float = 60.0) -> dict:
    """执行 `am start`（或 `am startservice`）。"""
    serial = adb_mod.pick_device(device)
    if wait and intent.kind == "activity":
        intent.wait = True

    # 先补全、再校验，最后才拼命令。
    # 补全：只有一个包名或一个裸全限定类名时，把它落成真正能用的 `pkg/.Class`。
    # 校验：没有组件也没 Action 时直接驳回 —— 否则 `to_am_args()` 会返回空列表，
    # 于是真的执行了 `adb shell am start`（什么都不带），am 嘟一句
    # "Error: no intent specified" 还被当成"已发送启动请求"，纯属自欺欺人。
    intent.resolve(serial or device)
    problem = intent.validate()
    if problem:
        return {
            "ok": False, "detail": problem, "hint": "",
            "command": "", "commandShort": "", "amArgs": [],
            "stdout": "", "stderr": "",
            "totalTime": 0, "thisTime": 0, "waitTime": 0,
            "component": intent.normalized_target, "intent": intent.to_dict(),
        }

    am_args = intent.to_am_command()
    args = ["shell"] + am_args
    r = adb_mod.run(args, serial=serial, timeout=timeout)

    out = r.out or ""
    err = r.err or ""
    cmd_short = adb_mod.command_preview(["shell"] + am_args, serial)
    cmd_full = adb_mod.command_preview(args, serial)

    parsed = {}
    for line in out.splitlines():
        m = _STATUS_RE.match(line.strip())
        if m:
            parsed[m.group("k")] = m.group("v").strip()

    ok = False
    if parsed.get("Status", "").lower().startswith("ok"):
        ok = True
    elif parsed.get("Complete"):
        ok = True
    elif r.ok and not parsed.get("Error") and not parsed.get("Exception") and out.strip():
        ok = True

    detail = "已发送启动请求"
    hint = ""
    if parsed.get("Warning"):
        detail = parsed["Warning"]

    if not ok:
        blob = out + "\n" + err
        detail = (parsed.get("Error") or parsed.get("Exception")
                  or next((l.strip() for l in blob.splitlines() if l.strip()), "启动失败"))
        for pat, h in _AM_HINTS:
            if re.search(pat, blob, re.I):
                hint = h
                break
        if not hint and not blob.strip():
            hint = "am 没有任何输出，可能是 adb 通道问题；试试 `device tools kill_server` 重启 adb。"

    res = {
        "ok": ok,
        "detail": detail,
        "hint": hint,
        # `am` 的 Warning 行（例如 "Activity not started, intent has been delivered
        # to currently running top-most instance."）前端会单独用黄条渲染 ——
        # 之前只塞进 detail，前端读的 `r.warning` 永远是空的。
        "warning": parsed.get("Warning", ""),
        "command": cmd_full,
        "commandShort": cmd_short,
        "amArgs": am_args,
        "stdout": out[:4000],
        "stderr": err[:2000],
        "totalTime": _int(parsed.get("TotalTime")),
        "thisTime": _int(parsed.get("ThisTime")),
        "waitTime": _int(parsed.get("WaitTime")),
        "component": intent.normalized_target,
        "intent": intent.to_dict(),
    }
    return res


def _int(v) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def send_broadcast(intent, device: str = "", timeout: float = 60.0) -> dict:
    intent.kind = "broadcast"
    return start_component(intent, device=device, timeout=timeout)


def force_stop(package: str, device: str = "") -> dict:
    serial = adb_mod.pick_device(device)
    args = ["shell", "am", "force-stop", package]
    r = adb_mod.run(args, serial=serial, timeout=30)
    return {"ok": r.ok, "detail": "已停止 %s" % package,
            "command": adb_mod.command_preview(args, serial)}


def component_state(package: str, component: str, device: str = "") -> dict:
    """查组件的启用状态（`pm dump` 不行时的替代）。"""
    serial = adb_mod.pick_device(device)
    target = component if "/" in component else "%s/%s" % (package, component)
    args = ["shell", "cmd", "package", "dump", package]
    r = adb_mod.run(args, serial=serial, timeout=30)
    return {"ok": r.ok, "raw": (r.out or "")[:2000],
            "command": adb_mod.command_preview(args, serial)}
