# -*- coding: utf-8 -*-
"""HTTP API 路由。

约定：
  · handler 返回 dict → 200 + JSON
  · 返回 (status, payload) → 指定状态码
  · 返回 SSE(...) → 服务端事件流
  · 所有写操作在响应里回显"将要执行的命令"（`commands` 字段），可审计
"""

import json
import re
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config, registry
from ..core import files as files_mod
from ..core.apk import parse_apk, size_report
from ..core.capture import gallery as gallery_mod
from ..core.capture import logcat as logcat_mod
from ..core.capture import record as rec_mod
from ..core.capture import screen as screen_mod
from ..core.device import adb as adb_mod
from ..core.device import apps as apps_mod
from ..core.device import labels as labels_mod
from ..core.device import signature as sign_mod
from ..core.device import stack as stack_mod
from ..core.device import tools as dev_tools
from ..core.install import errors as err_mod
from ..core.install import installer
from ..core.install import precheck as precheck_mod
from ..core.launch import components as comp_mod
from ..core import scripts as scripts_mod
from ..core.launch import history as hist_mod
from ..core.launch.intent import COMMON_ACTIONS, COMMON_FLAGS, Intent, flag_catalog
from ..platform import filedialog as filedialog_mod
from ..platform import finders, local_fs, menu, paths
from . import tasks as tasks_mod


# ---------------------------------------------------------------- 框架

class SSE:
    """服务端事件流响应。gen 每 yield 一个 (event, data)。"""

    def __init__(self, gen):
        self.gen = gen


class RawBody:
    """**未解析**的请求体，交给 handler 自己流式消费。

    存在的理由：上传 APK 可能几百 MB，而常规请求体路径有 `MAX_BODY`(64MB)
    上限，且会先按 JSON / 表单解析一遍 —— 二进制走那儿必然失败。
    """

    CHUNK = 1 << 20            # 1MB
    DRAIN_CAP = 16 << 20       # 清理残留时最多读这么多

    def __init__(self, rfile, length: int):
        self.rfile = rfile
        self.length = max(0, int(length or 0))
        self.read_bytes = 0
        self.truncated = False   # 客户端提前断开

    @property
    def remaining(self) -> int:
        return max(0, self.length - self.read_bytes)

    def iter_chunks(self, chunk: int = 0):
        size = int(chunk or self.CHUNK)
        while self.remaining > 0:
            try:
                b = self.rfile.read(min(size, self.remaining))
            except OSError:
                self.truncated = True
                return
            if not b:
                self.truncated = True
                return
            self.read_bytes += len(b)
            yield b

    def drain(self, max_bytes: int = 0) -> bool:
        """读掉剩余字节以保持连接可复用。返回是否已读干净。"""
        cap = int(max_bytes or self.DRAIN_CAP)
        n = 0
        for b in self.iter_chunks():
            n += len(b)
            if n >= cap and self.remaining > 0:
                return False
        return self.remaining == 0


class Ctx:
    """一次请求的上下文。"""

    def __init__(self, method: str, path: str, query: Dict[str, List[str]],
                 body: Any, headers: Dict[str, str], params: Dict[str, str]):
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.headers = headers
        self.params = params

    # -- 取值辅助 ----------------------------------------------------

    def q(self, name: str, default: str = "") -> str:
        v = self.query.get(name)
        return (v[0] if v else default) or default

    def qb(self, name: str, default: bool = False) -> bool:
        v = self.q(name, "").lower()
        if not v:
            return default
        return v in ("1", "true", "yes", "on")

    def qi(self, name: str, default: int = 0) -> int:
        try:
            return int(self.q(name, "") or default)
        except ValueError:
            return default

    def field(self, name: str, default=None):
        if isinstance(self.body, dict):
            v = self.body.get(name, default)
            return default if v is None else v
        return default

    @property
    def pkg(self) -> str:
        return self.params.get("pkg", "")


ROUTES: List[Tuple[str, "re.Pattern", Callable]] = []


ROUTES: List[Tuple[str, Any, Any]] = []
RAW_ROUTES: List[Tuple[str, Any]] = []


def route(method: str, pattern: str, raw: bool = False):
    """注册路由。

    `raw=True` 表示这个 handler 要自己消费请求体（上传大文件用）——
    server 端会跳过预读，把 `RawBody` 放进 `ctx.body`。
    """
    def deco(fn):
        rx = re.compile("^%s$" % pattern)
        ROUTES.append((method.upper(), rx, fn))
        if raw:
            RAW_ROUTES.append((method.upper(), rx))
        return fn
    return deco


def wants_raw_body(method: str, path: str) -> bool:
    for m, rx in RAW_ROUTES:
        if m == method.upper() and rx.match(path):
            return True
    return False


def dispatch(method: str, path: str, query, body, headers) -> Tuple[int, Any, str]:
    """返回 (status, payload, content_type)。"""
    for m, rx, fn in ROUTES:
        if m != method.upper():
            continue
        mt = rx.match(path)
        if not mt:
            continue
        ctx = Ctx(method, path, query, body, headers, mt.groupdict())
        try:
            out = fn(ctx)
        except Exception as e:
            traceback.print_exc()
            return 500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, "application/json"
        if isinstance(out, SSE):
            return 200, out, "text/event-stream"
        if isinstance(out, tuple):
            status, payload = out[0], out[1]
            ctype = out[2] if len(out) > 2 else "application/json"
            return status, payload, ctype
        return 200, out, "application/json"


# ---------------------------------------------------------------- 健康 / 注册表

@route("GET", r"/api/health")
def h_health(ctx):
    from .. import __version__
    # 多带一个 uiAgo：主控台**页面**距今多少秒没来取过卡片（从没取过 → None）。
    # 右键脚本靠它判断"卡片推过去到底有没有人看得见"。见 `_ui_ago()`。
    # 这里只读一个模块级浮点数，不碰磁盘、不起子进程，仍然是毫秒级返回。
    return {"ok": True, "version": __version__, "pid": _pid(), "time": time.time(),
            "uiAgo": _ui_ago()}


@route("GET", r"/api/capabilities")
def h_capabilities(ctx):
    return registry.registry()


@route("GET", r"/api/sys/doctor")
def h_doctor(ctx):
    return finders.doctor()


@route("GET", r"/api/sys/config")
def h_config_get(ctx):
    return {"ok": True, "path": config.path(), "values": config.all_values()}


@route("POST", r"/api/sys/config")
def h_config_set(ctx):
    if not isinstance(ctx.body, dict):
        return 400, {"ok": False, "error": "body 必须是 JSON 对象"}
    patch = ctx.body.get("values") or ctx.body
    if not isinstance(patch, dict):
        return 400, {"ok": False, "error": "values 必须是对象"}
    ok = config.update(patch)
    finders.clear_cache()
    return {"ok": ok, "values": config.all_values()}


# ---------------------------------------------------------------- 设备

@route("GET", r"/api/devices")
def h_devices(ctx):
    deep = ctx.qb("deep", True)
    devs = adb_mod.list_devices(deep=deep)
    return {
        "ok": True,
        "adbAvailable": adb_mod.available(),
        "adbPath": str(finders.find_adb() or ""),
        "devices": [d.to_dict() for d in devs],
        "defaultDevice": config.get("default_device", ""),
    }


@route("GET", r"/api/devices/(?P<serial>[^/]+)/info")
def h_device_info(ctx):
    return adb_mod.device_info(ctx.params.get("serial", ""))


@route("POST", r"/api/devices/restart-adb")
def h_restart_adb(ctx):
    return adb_mod.adb_server_restart()


# ---------------------------------------------------------------- Activity 堆栈

@route("GET", r"/api/device/stack")
@route("POST", r"/api/device/stack")
def h_device_stack(ctx):
    """当前设备的 Activity 堆栈（结构化）。

    原始 `dumpsys activity activities` 在真机上是 480 行 / 36KB，人读不动；
    这里返回拆好的 display → task → activity 三层，字段名见响应里的
    `fields`（中文标签 + 说明），前端直接拿它渲染，不用再翻译一遍。

    `refresh=1` 强制重新 adb（默认 3 秒内的结果复用，防重复点击刷出一串调用）。
    """
    device = ctx.field("device", None) or ctx.q("device")
    refresh = ctx.qb("refresh", False) or bool(ctx.field("refresh", False))
    return stack_mod.snapshot(device=device or "", refresh=refresh)


# ---------------------------------------------------------------- 已装应用

_APP_CACHE = {"ts": 0.0, "serial": "", "system": False, "data": []}
_APP_TTL = 20.0


def _cached_apps(serial: str, include_system: bool, refresh: bool = False):
    now = time.time()
    if (not refresh and _APP_CACHE["data"]
            and _APP_CACHE["serial"] == serial
            and _APP_CACHE["system"] == include_system
            and now - _APP_CACHE["ts"] < _APP_TTL):
        return _APP_CACHE["data"], True
    items = apps_mod.list_apps("", include_system=include_system, device=serial)
    _APP_CACHE.update({"ts": now, "serial": serial, "system": include_system, "data": items})
    return items, False


@route("GET", r"/api/apps")
def h_apps(ctx):
    kw = ctx.q("filter")
    include_system = ctx.qb("system", False)
    refresh = ctx.qb("refresh", False)
    try:
        serial = adb_mod.pick_device(ctx.q("device"))
    except adb_mod.AdbError as e:
        return 200, {"ok": False, "error": str(e), "apps": [], "count": 0}

    items, cached = _cached_apps(serial, include_system, refresh)
    # 缓存里存的是 AppInfo 对象：应用名缓存后来才补齐的话，
    # 这里要再查一次表（纯内存字典，"补齐"跑完刷新列表就能看到名字）
    labels_mod.enrich(items)
    if kw:
        items = [a for a in items if a.matches(kw)]
    named = sum(1 for a in items if a.label)
    return {
        "ok": True,
        "device": serial,
        "cached": cached,
        "count": len(items),
        "labeled": named,
        "labelsCached": labels_mod.stats().get("cached", 0),
        "apps": [a.to_dict() for a in items],
    }


# ---------------------------------------------------------------- 应用名补齐

@route("POST", r"/api/apps/labels/sync")
def h_labels_sync(ctx):
    """后台补齐应用名（只读 APK 的两个条目，不 pull 整包）。

    不传包列表就用当前设备的应用清单。已在跑就直接返回那一个任务。
    """
    body = ctx.body or {}
    device = str(body.get("device", "") or ctx.q("device") or "")
    try:
        workers = int(body.get("workers", 0) or 0)
    except (TypeError, ValueError):
        workers = 0

    pkgs = body.get("packages")
    if not isinstance(pkgs, list) or not pkgs:
        include_system = bool(body.get("system"))
        try:
            serial = adb_mod.pick_device(device)
        except adb_mod.AdbError as e:
            return 200, {"ok": False, "error": str(e)}
        items, _cached = _cached_apps(serial, include_system, False)
        pkgs = [{"package": a.package, "versionCode": a.version_code,
                 "apkPath": a.apk_path} for a in items]

    kw = dict(device=device)
    if workers:
        kw["workers"] = workers
    labels_mod.start_sync(pkgs, **kw)
    st = labels_mod.sync_state()
    st["ok"] = True
    return st


@route("GET", r"/api/apps/labels/state")
def h_labels_state(ctx):
    st = labels_mod.sync_state()
    st["ok"] = True
    st["cached"] = labels_mod.stats().get("cached", 0)
    return st


@route("POST", r"/api/apps/labels/stop")
def h_labels_stop(ctx):
    stopped = labels_mod.stop_sync()
    st = labels_mod.sync_state()
    st["ok"] = True
    st["stopped"] = stopped
    return st


@route("GET", r"/api/apps/(?P<pkg>[^/]+)")
def h_app_detail(ctx):
    serial = ""
    try:
        serial = adb_mod.pick_device(ctx.q("device"))
    except adb_mod.AdbError:
        pass
    info = apps_mod.app_detail(ctx.pkg, serial) if serial else {"error": "没有可用设备"}
    return info


@route("GET", r"/api/apps/(?P<pkg>[^/]+)/signature")
def h_app_signature(ctx):
    fast = ctx.qb("fast", False)
    refresh = ctx.qb("refresh", False)
    res = sign_mod.get_signature(ctx.pkg, device=ctx.q("device"),
                                 fast=fast, full=not fast, refresh=refresh)
    return res


@route("POST", r"/api/apps/(?P<pkg>[^/]+)/signature/refresh")
def h_app_signature_refresh(ctx):
    res = sign_mod.get_signature(ctx.pkg, device=ctx.field("device", ""),
                                 fast=False, full=True, refresh=True)
    return res


@route("POST", r"/api/apps/(?P<pkg>[^/]+)/action")
def h_app_action(ctx):
    op = str(ctx.field("op", "") or ctx.field("action", ""))
    if op not in ("stop", "clear", "uninstall", "enable", "disable", "restart"):
        return 400, {"ok": False, "error": "不支持的操作：%s" % op}
    if op == "clear":
        # 清数据不可恢复：要求前端把包名原样传回来做二次确认
        # （工具界面不能只靠一个按钮）
        confirm = str(ctx.field("confirm", ""))
        if confirm != ctx.pkg:
            return 400, {
                "ok": False,
                "error": "清除数据需要二次确认：请把包名原样传入 confirm 字段",
                "needConfirm": ctx.pkg,
                "risk": "dangerous",
            }
    elif op == "uninstall":
        # 卸载：前端弹一句确认就够，不要求敲包名；但仍要求调用方显式带上
        # confirm 标记 —— 免得有脚本或别的界面误调这条接口。
        if not str(ctx.field("confirm", "")):
            return 400, {
                "ok": False,
                "error": "卸载需要确认：请带上 confirm 字段（页面上的确认框会自动带）",
                "needConfirm": "yes",
                "risk": "dangerous",
            }
    res = dev_tools.uninstall(ctx.pkg, ctx.field("device", "")) if op == "uninstall" else \
        apps_mod.app_action(ctx.pkg, op, ctx.field("device", ""))
    _APP_CACHE["ts"] = 0
    sign_mod.invalidate(ctx.pkg)
    return res


@route("POST", r"/api/apps/(?P<pkg>[^/]+)/export")
def h_app_export(ctx):
    out = str(ctx.field("out", "") or "")
    res = apps_mod.export_apk(ctx.pkg, out, ctx.field("device", ""))
    if res.get("ok") and out:
        # 记住这次导到了哪儿：下次开对话框/目录浏览器直接落在这儿，
        # 而不是又从盘符根开始翻
        try:
            if Path(out).is_dir():
                config.set_value("last_out_dir", str(Path(out)))
        except Exception:
            pass
    return (200 if res.get("ok") else 500), res


# ---------------------------------------------------------------- 本机 APK

def _path_arg(ctx, *names) -> str:
    """取路径参数：body 优先，其次 query（同一个 handler 两种调用都能用）。"""
    for n in names:
        v = ctx.field(n, None)
        if v not in (None, ""):
            return v
    for n in names:
        v = ctx.q(n, "")
        if v:
            return v
    return ""


def _valid_apk_path(raw) -> Path:
    """校验并归一化 APK 路径。

    **刻意对"类型不对"单独报错**：前端把 DOM 对象/空对象塞进 path 时，
    `str({})` 会变成 `"{}"`，老代码会报「文件不存在：{}」这种看不懂的错。
    现在直接说清楚"收到的是 X，不是字符串"。
    """
    if isinstance(raw, (dict, list)):
        raise ValueError("路径参数类型不对：收到的是 %s（应为字符串），请检查调用方是否传错参数"
                         % type(raw).__name__)
    s = str(raw or "").strip().strip('"').strip("'")
    if not s or s in ("{}", "null", "undefined"):
        raise ValueError("路径为空：请先用「浏览…」选择 APK，或把完整路径粘进输入框")
    # 容忍从浏览器地址栏复制的 file:///D:/... 形式
    if s.lower().startswith("file://"):
        import urllib.parse
        s = urllib.parse.unquote(s[7:])
        if s.startswith("/") and len(s) > 2 and s[2] == ":":
            s = s[1:]
    p = Path(s)
    if not p.is_file():
        hint = ""
        if p.parent.is_dir():
            like = [q.name for q in list(p.parent.iterdir())[:200]
                    if q.name.lower() == p.name.lower()]
            if like:
                hint = "（同目录下有同名文件，注意大小写：%s）" % like[0]
        raise ValueError("文件不存在：%s%s" % (s, hint))
    if p.suffix.lower() not in (".apk", ".apks", ".xapk", ".aab"):
        raise ValueError("不是 APK 文件：%s" % s)
    return p


def _remember_apk_dir(p: Path) -> None:
    """记住最后一个 APK 所在目录 —— 下次弹文件框直接落在那里。"""
    try:
        if p.parent.is_dir():
            config.set_value("last_apk_dir", str(p.parent))
    except Exception:
        pass


@route("POST", r"/api/apk/parse")
def h_apk_parse(ctx):
    raw = _path_arg(ctx, "path", "file", "apk")
    try:
        p = _valid_apk_path(raw)
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    info = parse_apk(
        p,
        with_signature=bool(ctx.field("signature", True)),
        with_icon=bool(ctx.field("icon", True)),
        with_components=bool(ctx.field("components", True)),
        verify_signature=bool(ctx.field("verify", False)),
    )
    _remember_apk_dir(p)
    return info.to_dict()


@route("POST", r"/api/apk/locate")
def h_apk_locate(ctx):
    """拖拽进浏览器后，按「文件名 + 体积」把真实路径找回来。

    浏览器不肯给拖入文件的路径（`File.path` 只有 Electron 有），
    但服务端就在本机 —— 用 name/size/mtime 去常见目录搜一遍即可。
    """
    from ..core.apk import locate as locate_mod

    name = str(ctx.field("name", "") or "").strip()
    if not name:
        return 400, {"ok": False, "error": "缺少文件名（name）"}
    try:
        size = int(ctx.field("size", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    try:
        mtime = int(ctx.field("mtime", 0) or 0)   # 前端给的是秒
    except (TypeError, ValueError):
        mtime = 0

    extra = []
    last = str(config.get("last_apk_dir", "") or "")
    if last:
        extra.append(last)
    cfg_extra = str(config.get("locate_dirs", "") or "")
    for part in re.split(r"[;\n]", cfg_extra):
        if part.strip():
            extra.append(part.strip())

    res = locate_mod.locate(name, size=size, mtime=mtime, extra_dirs=extra)
    if res.get("ok"):
        _remember_apk_dir(Path(res["path"]))
    return res


@route("GET", r"/api/apk/analyze")
@route("POST", r"/api/apk/analyze")
def h_apk_analyze(ctx):
    try:
        p = _valid_apk_path(_path_arg(ctx, "path", "file"))
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    info = parse_apk(p, with_signature=False, with_icon=False)
    if info.error:
        return 500, {"ok": False, "error": info.error}
    rep = size_report(info)
    rep["ok"] = True
    rep["package"] = info.package
    rep["fileName"] = info.file_name
    return rep


@route("GET", r"/api/apk/signature")
@route("POST", r"/api/apk/signature")
def h_apk_signature(ctx):
    try:
        p = _valid_apk_path(_path_arg(ctx, "path", "file"))
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    info = parse_apk(p, with_signature=True, with_icon=False, with_components=False,
                     verify_signature=ctx.qb("verify", False) or bool(ctx.field("verify", False)))
    if not info.signature:
        return 500, {"ok": False, "error": "未能解析签名"}
    _remember_apk_dir(p)
    return info.signature.to_dict()


@route("POST", r"/api/apk/diff")
def h_apk_diff(ctx):
    """双包对比。每一侧可以是**本机 APK 文件**，也可以是**设备上的已装应用**。

    每一侧的三态：
      · 给 `a`（路径）           → 这一侧是本机文件
      · 给 `aPackage: "com.x"`   → 这一侧是设备上的 com.x
      · 给 `aPackage: ""`        → 这一侧是设备，**包名用另一侧推出来**
                                   （也就是"和另一侧同名的那个已装应用比"）
      · 都没给                   → 400

    老调用 `{a, b}`（两个路径）走原路径，响应一字不变。
    """
    from ..core.apk.analyze import diff_apks
    from ..core.device.compare import SideSpec, diff_sides

    device = str(ctx.field("device", "") or ctx.q("device") or "")
    given = {}
    for side in ("a", "b"):
        # 注意这里用 None 而不是 "" 取默认值：**"键不存在"和"键存在但为空"**
        # 是两个不同的意思（没声明来源 / 声明了设备但包名待定），不能混成一个。
        pkg = ctx.field(side + "Package", None)
        raw = _path_arg(ctx, side)
        if raw and pkg not in (None, ""):
            return 400, {"ok": False,
                         "error": "%s 侧同时给了 APK 路径和包名，只能二选一"
                                  % side.upper()}
        given[side] = (pkg, raw)

    # ---- 两侧都没声明设备 → 老行为（响应结构一字不变）----
    if given["a"][0] is None and given["b"][0] is None:
        # 两侧都缺的时候要**指名道姓**说缺的是哪一侧。老代码在这里直接去
        # `_valid_apk_path("")`，报出来的是「路径为空：请先用「浏览…」选择 APK」——
        # 调用方（脚本 / 另一个界面）看到这句话根本不知道是自己漏了 B。
        for side in ("a", "b"):
            if not given[side][1]:
                return 400, {"ok": False,
                             "error": "%s 侧没填：写 APK 路径，或给 %sPackage 表示"
                                      "这一侧用设备上已装的应用" % (side.upper(), side)}
        try:
            a = _valid_apk_path(given["a"][1])
            b = _valid_apk_path(given["b"][1])
        except ValueError as e:
            return 400, {"ok": False, "error": str(e)}
        ia = parse_apk(a, with_icon=False)
        ib = parse_apk(b, with_icon=False)
        if ia.error or ib.error:
            return 500, {"ok": False, "error": ia.error or ib.error}
        return diff_apks(ia, ib)

    # ---- 至少一侧来自设备 ----
    specs = {}
    for side in ("a", "b"):
        pkg, raw = given[side]
        if raw:
            try:
                specs[side] = SideSpec.file(str(_valid_apk_path(raw)))
            except ValueError as e:
                return 400, {"ok": False, "error": "%s 侧：%s" % (side.upper(), e)}
        elif pkg is not None:
            specs[side] = SideSpec.device(pkg)
        else:
            return 400, {"ok": False,
                         "error": "%s 侧没填：写 APK 路径，或填设备上的包名"
                                  % side.upper()}

    res = diff_sides(specs["a"], specs["b"], device=device)
    if not res.get("ok"):
        return 500, res
    return res


@route("POST", r"/api/apk/signature/compare")
def h_sign_compare(ctx):
    """签名比对：{apk: 路径} 或 {apk, device, pkg}。"""
    apk = str(ctx.field("apk", ""))
    pkg = str(ctx.field("pkg", ""))
    device = ctx.field("device", "")
    try:
        _valid_apk_path(apk)
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}

    if pkg:
        return sign_mod.compare_with_apk(pkg, apk, device=device)

    # 只给 apk → 与同包名的已装应用比
    info = parse_apk(apk, with_signature=False, with_icon=False, with_components=False)
    if not info.package:
        return 400, {"ok": False, "error": "解析不出包名，无法确定要跟哪个已装应用比对"}
    return sign_mod.compare_with_apk(info.package, apk, device=device)


@route("POST", r"/api/apk/icon")
def h_apk_icon(ctx):
    import base64
    try:
        p = _valid_apk_path(_path_arg(ctx, "path", "file"))
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    info = parse_apk(p, with_signature=False)
    if not info.icon_data_uri:
        return 404, {"ok": False, "error": "该 APK 没有可提取的位图图标"}
    data = base64.b64decode(info.icon_data_uri.split(",", 1)[1])
    name = "%s-icon.%s" % (info.package or p.stem, info.icon_mime.split("/")[-1])
    out = paths.output_dir() / name
    out.write_bytes(data)
    _remember_apk_dir(p)
    return {"ok": True, "path": str(out), "url": "/files/output/%s" % name,
            "mime": info.icon_mime, "bytes": len(data), "source": info.icon_path}


@route("POST", r"/api/apk/scan")
def h_apk_scan(ctx):
    raw = str(ctx.field("dir", ""))
    d = Path(raw)
    if not d.is_dir():
        return 400, {"ok": False, "error": "目录不存在：%s" % raw}
    files = sorted(p for p in d.rglob("*.apk") if p.is_file())
    limit = int(ctx.field("limit", 200) or 200)
    items = []
    for f in files[:limit]:
        info = parse_apk(f, with_signature=False, with_icon=False, with_components=False)
        items.append({"path": str(f), "package": info.package, "appLabel": info.app_label,
                      "versionName": info.version_name, "versionCode": info.version_code,
                      "bytes": info.file_size, "error": info.error})
    return {"ok": True, "count": len(items), "total": len(files), "items": items}


# ---------------------------------------------------------------- 本机文件选择

# 「浏览…」的候选过滤器。tkinter 用空格分隔多个扩展名。
APK_FILETYPES = [
    ("Android 包", "*.apk *.apks *.xapk *.aab"),
    ("APK", "*.apk"),
    ("所有文件", "*.*"),
]


@route("POST", r"/api/apk/pick")
def h_apk_pick(ctx):
    """弹本机原生文件对话框，把**真实路径**回给页面。

    浏览器出于安全永远拿不到 `<input type=file>` 里文件的真实路径，但本工具的
    服务端就在本机 —— 由它弹对话框最省事，也免了"为了解析一个 APK 先复制一份"。

    实际弹窗逻辑在 `platform/filedialog.py`（有 tkinter 的 Python 优先，
    Windows 上退化到 PowerShell + WinForms）。后端全不可用时这里返回
    `fallback: "browser"`，前端改用浏览器选择 + 上传兜底。
    """
    r = filedialog_mod.pick_file(
        title="选择 APK",
        initial_dir=str(config.get("last_apk_dir", "") or ""),
        filetypes=APK_FILETYPES,
    )

    if not r.get("ok"):
        return {"ok": False, "fallback": "browser", "backend": None,
                "tried": r.get("tried") or [],
                "error": r.get("error") or "无法弹出文件对话框",
                "hint": "可以点「浏览…」用浏览器选择文件，或直接把 APK 拖进页面"}

    out = str(r.get("path") or "").strip()
    if not out:
        return {"ok": False, "cancelled": True, "error": "未选择文件"}

    # 各后端给的路径写法不同（tkinter 给正斜杠）—— 归一化，
    # 免得用户以为路径被改过
    if paths.IS_WINDOWS:
        out = out.replace("/", "\\")
    p = Path(out)
    if not p.is_file():
        return {"ok": False, "error": "文件不存在：%s" % out}
    _remember_apk_dir(p)
    res = {"ok": True, "path": str(p), "name": p.name, "bytes": p.stat().st_size,
           "mtime": int(p.stat().st_mtime), "backend": r.get("backend")}
    # 顺手说清"这条路径属于哪个已知文件夹"——下载/文档可能被重定向到别的盘，
    # 不解释一句，用户会以为工具把路径改写了
    try:
        f = paths.folder_of(p)
    except Exception:
        f = {}
    if f:
        res["folder"] = f.get("title", "")
        res["folderRoot"] = f.get("path", "")
        res["folderNote"] = f.get("note", "")
        res["folderRedirected"] = bool(f.get("redirected"))
    return res


# ---------------------------------------------------------------- 上传兜底

def _upload_dir() -> Path:
    """浏览器上传的副本落在这儿。放系统临时目录 —— 这些是副本，
    不该污染项目目录，也方便系统清理。"""
    d = paths.system_tmp_dir() / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune_uploads(d: Path, max_age_hours: int = 24) -> None:
    """顺手清理过期的上传副本，别让磁盘悄悄涨起来。"""
    cutoff = time.time() - max_age_hours * 3600
    try:
        for f in d.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _safe_upload_name(raw: str) -> str:
    """把浏览器给的文件名收敛成一个安全的纯文件名。

    `File.name` 理论上就是文件名，但它来自客户端 —— 一律当不可信输入处理，
    不能让它带出路径（`..\\..\\x.apk` 这种）。
    """
    name = str(raw or "").replace("\\", "/").split("/")[-1]      # 去掉任何路径成分
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)              # Windows 非法字符
    name = name.strip(". ")                                      # 结尾的点/空格 Windows 不接受
    if not name:
        return "upload.apk"
    if len(name) > 180:                                          # 别撞路径长度上限
        stem, dot, ext = name.rpartition(".")
        name = (stem[:150] + dot + ext[:20]) if dot else name[:160]
    return name


@route("POST", r"/api/apk/upload", raw=True)
def h_apk_upload(ctx):
    """浏览器没有原生对话框时的兜底：把文件收到本机临时目录再解析。

    **只是兜底** —— 能拿到真实路径就不该复制文件。所以这条路径仅在
    `/api/apk/pick` 返回 `fallback` 时才会被前端用到（或用户主动选择）。
    """
    body = ctx.body
    if not isinstance(body, RawBody) or body.length <= 0:
        return 400, {"ok": False, "error": "没有收到文件内容（缺少 Content-Length）"}

    limit = int(config.get("upload_max_mb", 4096) or 4096) * 1024 * 1024
    if body.length > limit:
        return 413, {"ok": False,
                     "error": "文件太大（%.1f MB），当前上限 %d MB"
                              % (body.length / 1048576.0, limit // 1048576),
                     "hint": "改 config.json 的 upload_max_mb，或把 APK 拖进页面"
                             "（走本机定位，不复制文件）"}

    name = _safe_upload_name(ctx.q("name", "") or ctx.headers.get("X-Filename", ""))
    dest_dir = _upload_dir()
    _prune_uploads(dest_dir)

    # 同名同尺寸直接复用：反复选同一个文件时不必重复传输
    dest = dest_dir / name
    try:
        if dest.is_file() and dest.stat().st_size == body.length:
            body.drain()
            _remember_apk_dir(dest)
            return {"ok": True, "path": str(dest), "name": name, "bytes": body.length,
                    "uploaded": True, "reused": True}
    except OSError:
        pass

    tmp = dest.with_name(dest.name + ".part")
    written = 0
    try:
        with open(tmp, "wb") as f:
            for chunk in body.iter_chunks():
                f.write(chunk)
                written += len(chunk)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return 500, {"ok": False, "error": "写入失败：%s" % e}

    if body.truncated or written != body.length:
        try:
            tmp.unlink()
        except OSError:
            pass
        return 400, {"ok": False,
                     "error": "上传中断：只收到 %d / %d 字节" % (written, body.length)}
    try:
        tmp.replace(dest)
    except OSError as e:
        return 500, {"ok": False, "error": "落盘失败：%s" % e}

    _remember_apk_dir(dest)
    return {"ok": True, "path": str(dest), "name": name, "bytes": written, "uploaded": True}


@route("GET", r"/api/sys/pick")
def h_sys_pick(ctx):
    """原生对话框能力探测（给前端决定「浏览…」怎么走）。

    **不要**塞进 `/api/health` —— 那个接口被单实例探活高频调用，必须毫秒级返回；
    这里的首次调用会起子进程探测解释器（几百毫秒）。
    """
    return filedialog_mod.backend_info()


@route("POST", r"/api/sys/pickdir")
def h_sys_pickdir(ctx):
    """弹原生**目录**对话框，回一个真实存在的本机目录。

    为什么要有这条：导出已装 APK、批量扫目录这两件事，以前都得让用户
    **手打一个绝对路径**。而"我想放哪儿"本质上就是"点一下我要的目录"——
    能弹对话框就别让人默写自己的磁盘结构。

    与 `/api/apk/pick` 的差别只有一个：这里是目录，不校验扩展名。
    """
    start = str(ctx.field("start", "") or config.get("last_out_dir", "") or "")
    if start and not Path(start).is_dir():
        start = ""
    r = filedialog_mod.pick_dir(title=str(ctx.field("title", "") or "选择文件夹"),
                               initial_dir=start)
    if not r.get("ok"):
        # 没有原生后端 → 告诉前端去用页面内的目录浏览器，而不是干瞪眼
        return {"ok": False, "fallback": "inpage", "backend": None,
                "tried": r.get("tried") or [],
                "error": r.get("error") or "无法弹出目录对话框"}
    p = str(r.get("path") or "").strip()
    if not p:
        return {"ok": False, "cancelled": True, "error": "未选择目录"}
    if paths.IS_WINDOWS:
        p = p.replace("/", "\\")
    if not Path(p).is_dir():
        return {"ok": False, "error": "目录不存在：%s" % p}
    try:
        config.set_value("last_out_dir", p)
    except Exception:
        pass
    return {"ok": True, "path": p, "name": Path(p).name, "backend": r.get("backend")}


@route("GET", r"/api/fs/ls")
def h_fs_ls(ctx):
    """列**本机**目录（设备目录走 `/api/files/ls`，两者别搞混）。

    `dirsOnly=1` 是给"选一个目录"用的：只列目录，列表短、不用在文件里翻。
    """
    return local_fs.ls_dir(
        ctx.q("path", ""),
        dirs_only=ctx.qb("dirsOnly", False),
        show_hidden=ctx.qb("hidden", False),
    )


@route("GET", r"/api/fs/roots")
def h_fs_roots(ctx):
    """本机盘符 + 常用文件夹（目录浏览器的起点）。"""
    return {"ok": True, **local_fs.roots()}


# ---------------------------------------------------------------- 启动器

@route("GET", r"/api/launch/components")
def h_components(ctx):
    pkg = ctx.q("pkg")
    if not pkg:
        return 400, {"ok": False, "error": "缺少 pkg 参数"}
    items = comp_mod.list_components(
        pkg, ctype=ctx.q("type"), device=ctx.q("device"),
        source=ctx.q("source", "auto"), refresh=ctx.qb("refresh", False))
    return {"ok": True, "package": pkg, "count": len(items), "components": items}


@route("POST", r"/api/launch/preview")
def h_launch_preview(ctx):
    """只回命令预览（前端实时预览走这里）。"""
    intent = Intent.from_dict(ctx.body or {})
    serial = ctx.field("device", "") or ""
    if not serial:
        try:
            serial = adb_mod.pick_device("")
        except adb_mod.AdbError:
            serial = ""
    # 预览和执行必须做同样的补全，否则会出现"预览是空命令、执行却报错"的割裂。
    # 补全失败也不回 4xx —— 用户还在打字，预览条给个警告就够了。
    intent.resolve(serial)
    problem = intent.validate()
    return {
        "ok": True,
        "intent": intent.to_dict(),
        "amArgs": intent.to_am_args(),
        "command": adb_mod.command_preview(["shell"] + intent.to_am_command(), serial),
        "problem": problem,
        # 解析不出来时设备上"可能属于这个类名"的包名。前端拿它渲染成可点标签，
        # 点一下就把「所属包名」填上 —— 比让用户自己猜 applicationId 强。
        "candidates": list(intent.resolve_candidates or []),
        "available": True,
    }


@route("POST", r"/api/launch/start")
def h_launch_start(ctx):
    body = ctx.body or {}
    intent = Intent.from_dict(body)
    dry = bool(body.get("dryRun"))
    if not intent.target and not intent.action:
        return 400, {"ok": False, "error": "至少要指定组件或 Action"}
    # 只给包名 / 裸类名时，这里把它落成 pkg/.Class（包名 → 查设备 launcher）
    try:
        serial = adb_mod.pick_device(body.get("device", ""))
    except adb_mod.AdbError:
        serial = ""
    intent.resolve(serial)
    problem = intent.validate()
    if problem:
        return 400, {"ok": False, "error": problem, "detail": problem,
                     "candidates": list(intent.resolve_candidates or []),
                     "amArgs": intent.to_am_args(), "intent": intent.to_dict()}
    if dry:
        return {"ok": True, "dryRun": True, "command":
                adb_mod.command_preview(["shell"] + intent.to_am_command(), serial),
                "amArgs": intent.to_am_args()}

    res = comp_mod.start_component(intent, device=body.get("device", ""),
                                   wait=bool(body.get("wait")))
    hist_mod.record(intent, res, device=body.get("device", ""))
    return res


@route("POST", r"/api/launch/broadcast")
def h_launch_broadcast(ctx):
    intent = Intent.from_dict(ctx.body or {})
    res = comp_mod.send_broadcast(intent, device=ctx.field("device", ""))
    hist_mod.record(intent, res, device=ctx.field("device", ""))
    return res


@route("GET", r"/api/launch/history")
def h_launch_history(ctx):
    return {"ok": True, "history": hist_mod.list_entries(limit=ctx.qi("limit", 30))}


@route("POST", r"/api/launch/history/clear")
def h_launch_history_clear(ctx):
    return hist_mod.clear()


@route("GET", r"/api/launch/shortcut")
def h_shortcut_list(ctx):
    return {"ok": True, "shortcuts": hist_mod.list_shortcuts()}


@route("POST", r"/api/launch/shortcut")
def h_shortcut_save(ctx):
    """我的入口：增 / 改 / 删。

    改和删都接受 `id`，也兼容老前端传的 `name` —— 详见
    `history.edit_shortcut` 的说明（老数据没有 id）。
    """
    op = str(ctx.field("op", "add") or "add")
    sid = str(ctx.field("id", "") or ctx.field("name", ""))
    if op == "del":
        return hist_mod.del_shortcut(sid)
    if op in ("edit", "rename", "update"):
        patch = {
            "name": ctx.field("name", None),
            "component": ctx.field("component", None),
            "args": ctx.field("args", None),
            "note": ctx.field("note", None),
            "intent": ctx.field("intent", None),
        }
        # 只把**真的传了**的字段交给下层，别用 None 把没打算改的字段抹掉
        patch = {k: v for k, v in patch.items() if v is not None}
        return hist_mod.edit_shortcut(sid, patch)
    return hist_mod.add_shortcut(str(ctx.field("name", "")),
                                 str(ctx.field("component", "")),
                                 str(ctx.field("args", "")),
                                 ctx.field("intent", {}) or {},
                                 str(ctx.field("note", "")))


@route("GET", r"/api/launch/catalog")
def h_launch_catalog(ctx):
    return {
        "ok": True,
        "flags": flag_catalog(),
        "commonFlags": COMMON_FLAGS,
        "commonActions": COMMON_ACTIONS,
        "extrasTypes": ["string", "int", "bool", "float", "long", "uri", "string-array", "int-array", "null"],
    }


# ---------------------------------------------------------------- 捕获

@route("POST", r"/api/capture/screenshot")
def h_screenshot(ctx):
    res = screen_mod.screenshot(out=ctx.field("out", ""), device=ctx.field("device", ""))
    if res.get("ok"):
        res["url"] = "/files/captures/%s" % Path(res["path"]).name
    return (200 if res.get("ok") else 500), res


@route("POST", r"/api/capture/screenshot/raw")
def h_screenshot_raw(ctx):
    data = screen_mod.screenshot_bytes(device=ctx.field("device", ""))
    if not data:
        return 500, {"ok": False, "error": "截图失败"}, "application/json"
    return 200, data, "image/png"


@route("POST", r"/api/capture/record")
def h_record(ctx):
    op = str(ctx.field("op", "start"))
    if op == "start":
        res = rec_mod.start(bitrate=str(ctx.field("bitrate", "8M")),
                            size=str(ctx.field("size", "")),
                            time_limit=int(ctx.field("timeLimit", 180) or 180),
                            device=ctx.field("device", ""))
    elif op == "stop":
        res = rec_mod.stop(device=ctx.field("device", ""))
    else:
        return 400, {"ok": False, "error": "op 只能是 start / stop"}
    return (200 if res.get("ok") else 500), res


@route("GET", r"/api/capture/record/state")
def h_record_state(ctx):
    return rec_mod.session_state(ctx.q("device"))


@route("POST", r"/api/capture/mirror")
def h_mirror(ctx):
    res = rec_mod.mirror(record_to=str(ctx.field("record", "")),
                         device=ctx.field("device", ""),
                         args=str(ctx.field("args", "")))
    return (200 if res.get("ok") else 500), res


@route("GET", r"/api/capture/logcat")
def h_logcat(ctx):
    return logcat_mod.snapshot(package=ctx.q("pkg"), level=ctx.q("level", "V"),
                               filter_kw=ctx.q("filter"), device=ctx.q("device"),
                               lines=ctx.qi("lines", 300))


@route("GET", r"/api/capture/logcat/stream")
def h_logcat_stream(ctx):
    pkg = ctx.q("pkg")
    level = ctx.q("level", "V")
    kw = ctx.q("filter")
    device = ctx.q("device")
    duration = float(ctx.q("duration", "0") or 0)

    def gen():
        yield "open", {"ok": True}
        n = 0
        for line in logcat_mod.stream(package=pkg, level=level, filter_kw=kw,
                                      device=device, duration=duration):
            n += 1
            yield "log", {"line": line, "n": n}

    return SSE(gen())


@route("POST", r"/api/capture/logcat/clear")
def h_logcat_clear(ctx):
    return logcat_mod.clear(ctx.field("device", ""))


@route("GET", r"/api/capture/crash")
def h_crash(ctx):
    return logcat_mod.crash_watch(package=ctx.q("pkg"), device=ctx.q("device"),
                                  seconds=ctx.qi("seconds", 12))


@route("GET", r"/api/capture/artifacts")
def h_artifacts(ctx):
    items = gallery_mod.list_artifacts(limit=ctx.qi("limit", 60), kind=ctx.q("kind"))
    return {"ok": True, "artifacts": items, "usage": gallery_mod.disk_usage()}


@route("POST", r"/api/capture/artifacts/delete")
def h_artifact_delete(ctx):
    return gallery_mod.delete(str(ctx.field("path", "")))


@route("POST", r"/api/capture/artifacts/cleanup")
def h_artifact_cleanup(ctx):
    return gallery_mod.cleanup(days=int(ctx.field("days", 7) or 7))


# ---------------------------------------------------------------- 安装

@route("POST", r"/api/install/precheck")
def h_install_precheck(ctx):
    apk = str(ctx.field("apk", ctx.field("path", "")))
    try:
        p = _valid_apk_path(apk)
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    serial = ctx.field("device", "")
    if not serial:
        try:
            serial = adb_mod.pick_device("")
        except adb_mod.AdbError:
            serial = ""
    items = precheck_mod.precheck(p, serial, deep_sign=bool(ctx.field("deepSign")))
    return {"ok": True, "items": items, "summary": precheck_mod.summary(items),
            "device": serial}


@route("POST", r"/api/install")
def h_install(ctx):
    apks = ctx.field("apks") or ([ctx.field("apk")] if ctx.field("apk") else [])
    if not apks:
        return 400, {"ok": False, "error": "没有指定要安装的 APK"}
    if isinstance(apks, str):
        apks = [apks]
    for a in apks:
        try:
            _valid_apk_path(a)
        except ValueError as e:
            return 400, {"ok": False, "error": str(e)}

    opts = {
        "reinstall": not bool(ctx.field("noReinstall")),
        "downgrade": bool(ctx.field("downgrade")),
        "test": bool(ctx.field("test")),
        "grant": bool(ctx.field("grant")),
        "check_only": bool(ctx.field("checkOnly")),
    }
    device = ctx.field("device", "")

    task = tasks_mod.tasks.create("install", "安装 %d 个 APK" % len(apks))

    def work():
        task.progress("start", "开始安装")
        res = installer.install(
            list(apks), device=device,
            progress=lambda stage, msg: task.progress(stage, msg),
            **opts)
        for r in res.get("results", []):
            for line in (r.get("log") or [])[-8:]:
                task.log("[%s] %s" % (r.get("file", ""), line))
        _APP_CACHE["ts"] = 0
        task.finish(res, ok=bool(res.get("ok")))

    tasks_mod.run_in_thread(work)
    return {"ok": True, "taskId": task.id, "title": task.title,
            "apks": apks, "options": opts}


@route("GET", r"/api/install/errors")
def h_install_errors(ctx):
    code = ctx.q("code")
    if code:
        return err_mod.explain(code)
    return {"ok": True, "errors": err_mod.all_codes()}


# ---------------------------------------------------------------- 文件

@route("GET", r"/api/files/ls")
def h_files_ls(ctx):
    return files_mod.ls(ctx.q("path", "/sdcard"), ctx.q("device"))


@route("POST", r"/api/files/push")
def h_files_push(ctx):
    return files_mod.push(str(ctx.field("src", "")), str(ctx.field("dst", "/sdcard/")),
                          ctx.field("device", ""))


@route("POST", r"/api/files/pull")
def h_files_pull(ctx):
    return files_mod.pull(str(ctx.field("src", "")), str(ctx.field("dst", "")),
                          ctx.field("device", ""))


# ---------------------------------------------------------------- 脚本存档库

@route("GET", r"/api/scripts")
def h_scripts_list(ctx):
    """列脚本库的一个目录（文件夹 + 脚本，带备注与子信息）。"""
    return scripts_mod.list_dir(ctx.q("folder", ""))


@route("GET", r"/api/scripts/folders")
def h_scripts_folders(ctx):
    """所有分类目录（「移到…」下拉用，扁平列表）。"""
    return {"ok": True, "root": str(scripts_mod.library_root()),
            "folders": scripts_mod.folder_choices()}


@route("GET", r"/api/scripts/tree")
def h_scripts_tree(ctx):
    """整棵目录树（侧栏多级层级，一次给全 → 展开不再发请求）。"""
    return scripts_mod.folder_tree()


@route("GET", r"/api/scripts/read")
def h_scripts_read(ctx):
    return scripts_mod.read_script(ctx.q("path", ""))


@route("POST", r"/api/scripts/folder")
def h_scripts_mkdir(ctx):
    return scripts_mod.make_folder(str(ctx.field("folder", "")),
                                   str(ctx.field("name", "")))


@route("POST", r"/api/scripts/new")
def h_scripts_new(ctx):
    return scripts_mod.create(str(ctx.field("folder", "")), str(ctx.field("name", "")),
                              str(ctx.field("kind", "bat") or "bat"),
                              str(ctx.field("text", "")))


@route("POST", r"/api/scripts/add")
def h_scripts_add(ctx):
    """把本机已有脚本**复制**进库（原文件不动）。"""
    return scripts_mod.add_existing(str(ctx.field("src", "")),
                                    str(ctx.field("folder", "")),
                                    str(ctx.field("name", "")))


@route("POST", r"/api/scripts/save")
def h_scripts_save(ctx):
    return scripts_mod.write_script(str(ctx.field("path", "")),
                                    str(ctx.field("text", "")))


@route("POST", r"/api/scripts/rename")
def h_scripts_rename(ctx):
    return scripts_mod.rename(str(ctx.field("path", "")), str(ctx.field("name", "")))


@route("POST", r"/api/scripts/move")
def h_scripts_move(ctx):
    return scripts_mod.move(str(ctx.field("path", "")), str(ctx.field("folder", "")))


@route("POST", r"/api/scripts/note")
def h_scripts_note(ctx):
    return scripts_mod.set_note(str(ctx.field("path", "")), str(ctx.field("note", "")))


@route("POST", r"/api/scripts/alias")
def h_scripts_alias(ctx):
    """改**展示名**（别名），不动磁盘上的文件名。"""
    return scripts_mod.set_alias(str(ctx.field("path", "")), str(ctx.field("alias", "")))


@route("POST", r"/api/scripts/delete")
def h_scripts_delete(ctx):
    return scripts_mod.delete(str(ctx.field("path", "")))


@route("POST", r"/api/scripts/run")
def h_scripts_run(ctx):
    """跑一个脚本，把输出带回来（同步等待，默认 120s 上限）。"""
    return scripts_mod.run(str(ctx.field("path", "")), str(ctx.field("args", "")),
                           float(ctx.field("timeout", 0) or 0) or 120.0)


@route("POST", r"/api/sys/pickfile")
def h_sys_pickfile(ctx):
    """弹原生**文件**对话框（添加现有脚本时用）。

    和 `/api/apk/pick` 的区别只有默认过滤器：那里只认 APK，这里按调用方
    给的 `types` 走（不给就是所有文件）。
    """
    types = ctx.field("types", None)
    ft = None
    if isinstance(types, list) and types:
        ft = [(str(t[0]), str(t[1])) for t in types if isinstance(t, (list, tuple)) and len(t) >= 2]
    r = filedialog_mod.pick_file(
        title=str(ctx.field("title", "") or "选择文件"),
        initial_dir=str(ctx.field("start", "") or ""),
        filetypes=ft or scripts_mod.SCRIPT_TYPES)
    if not r.get("ok"):
        return {"ok": False, "fallback": "inpage", "backend": None,
                "tried": r.get("tried") or [],
                "error": r.get("error") or "无法弹出文件对话框"}
    p = str(r.get("path") or "").strip()
    if not p:
        return {"ok": False, "cancelled": True, "error": "未选择文件"}
    return {"ok": True, "path": p, "name": Path(p).name, "backend": r.get("backend")}


# ---------------------------------------------------------------- 菜单注入

@route("GET", r"/api/menu/state")
def h_menu_state(ctx):
    st = menu.status()
    st["supported"] = menu.supported()
    st["scripts"] = _script_state()
    return st


@route("POST", r"/api/menu/apply")
def h_menu_apply(ctx):
    op = str(ctx.field("op", "install"))
    if op == "install":
        res = menu.install()
    elif op == "uninstall":
        res = menu.uninstall()
    else:
        return 400, {"ok": False, "error": "op 只能是 install / uninstall"}
    res["state"] = menu.status()
    return (200 if res.get("ok") else 500), res


@route("POST", r"/api/sys/shutdown")
def h_sys_shutdown(ctx):
    from . import server as web_server
    return web_server.request_shutdown()


# ---------------------------------------------------------------- 弹窗推送

@route("POST", r"/api/popup/info")
def h_popup_info(ctx):
    """右键脚本把卡片推给正在运行的主控台。"""
    token = str(ctx.field("token", ""))
    if token != _token():
        return 403, {"ok": False, "error": "token 不匹配"}
    data = ctx.field("data", {})
    _PUSHED.append({"kind": ctx.field("kind", "apk-info"), "data": data,
                    "ts": time.time()})
    del _PUSHED[:-20]
    return {"ok": True, "detail": "已推送到主控台"}


@route("GET", r"/api/popup/pending")
def h_popup_pending(ctx):
    """前端轮询拿新推送的卡片。since 是上次拿到的时间戳。

    这个接口**同时是心跳**：右键脚本没有别的办法知道"卡片推过去有没有人看"，
    只能问服务端"最近还有没有页面来取过"。所以每次调用都要记一笔时间 ——
    少了这一笔，`apk popup` 就没法区分"主控台开着并且我正看着"和
    "进程活着但页面早关了"，后者会把卡片推给空气。

    ⚠ 但"在轮询"**不等于**"看得见"。实测过：一个被切走（隐藏）的标签页会被
    浏览器节流到 30~60 秒才轮询一次，偶尔也会落进判定窗口里 —— 于是卡片又被
    推进了一个没人看的页面，老问题换了个马甲。所以这里还要看前端报的
    `visible`：那就是 `document.visibilityState === 'visible'`，
    是浏览器对"这一页现在显示在屏幕上吗"的权威回答
    （切走的标签页、最小化的窗口都是 `hidden`）。

    隐藏时**照样要回 items**（不然切回来时会漏卡片），只是不刷新心跳。
    """
    global _UI_LAST_POLL
    if str(ctx.q("visible", "0")).strip().lower() not in ("0", "", "false", "no"):
        _UI_LAST_POLL = time.time()
    since = float(ctx.q("since", "0") or 0)
    items = [p for p in _PUSHED if p["ts"] > since]
    return {"ok": True, "items": items, "now": time.time()}


_PUSHED: List[dict] = []

# 最后一次有**页面**来取卡片的时间戳（0 = 从来没取过）。
# 注意它记录的是"页面在轮询"，不是"服务在跑" —— 两者差别正是
# 「右键点了没反应」那个 bug 的全部根源。
_UI_LAST_POLL: float = 0.0


def _ui_ago() -> Optional[float]:
    """主控台页面距今多少秒没来取卡片（从没取过 → None）。

    返回 `None` 而不是 `inf`：JSON 没有 Infinity，`json.dumps` 会写出裸
    `Infinity`，浏览器 `JSON.parse` 当场抛 —— "当前没有页面在看"是个完全
    正常的中间状态，不该让它把整条探活链路炸掉。
    """
    return (time.time() - _UI_LAST_POLL) if _UI_LAST_POLL else None


def _token() -> str:
    try:
        return paths.token_file().read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _pid() -> int:
    import os
    return os.getpid()


def _script_state() -> List[dict]:
    root = paths.project_root()
    # 根目录只留一键启停这几个主入口，其余平台脚本都收在 scripts/ 下
    root_only = {"start.bat", "stop.bat", "run-web.cmd", "stop-web.cmd"}
    out = []
    for name, desc in (
        ("apk-info.cmd", "解析信息（右键）"),
        ("apk-install.cmd", "安装到设备（双击）"),
        ("apk-icon.cmd", "提取图标"),
        ("apk-copyname.cmd", "复制包名"),
        ("apk-compare.cmd", "签名/版本对比"),
        ("apk-console.cmd", "打开主控台"),
        ("run-web.cmd", "启动主控台（先停旧实例）"),
        ("stop-web.cmd", "停止主控台"),
        ("start.bat", "一键启动（restart 语义）"),
        ("stop.bat", "一键停止"),
    ):
        f = root / name if name in root_only else root / paths.SCRIPTS_DIRNAME / name
        out.append({"name": name, "desc": desc, "exists": f.is_file(), "path": str(f)})
    return out


# ---------------------------------------------------------------- 任务与事件

@route("GET", r"/api/tasks")
def h_tasks(ctx):
    return {"ok": True, "tasks": tasks_mod.tasks.list(ctx.qi("limit", 20))}


@route("GET", r"/api/tasks/(?P<tid>[^/]+)")
def h_task(ctx):
    t = tasks_mod.tasks.get(ctx.params.get("tid", ""))
    if not t:
        return 404, {"ok": False, "error": "任务不存在"}
    return t.to_dict()


@route("GET", r"/api/tasks/(?P<tid>[^/]+)/events")
def h_task_events(ctx):
    t = tasks_mod.tasks.get(ctx.params.get("tid", ""))
    if not t:
        return 404, {"ok": False, "error": "任务不存在"}, "application/json"

    def gen():
        q = t.subscribe(replay=True)
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                except Exception:
                    yield "ping", {"t": time.time()}
                    continue
                yield ev["event"], ev["data"]
                if ev["event"] == "end":
                    break
        finally:
            t.unsubscribe(q)

    return SSE(gen())
