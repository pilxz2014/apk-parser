# -*- coding: utf-8 -*-
"""把 reader / axml / arsc / cert 的结果汇总成 `ApkInfo`。

这是本机域的"唯一出口"：CLI、弹窗、Web API 都消费这里的 dataclass。
"""

import base64
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import arsc as arsc_mod
from .axml import Node, parse_manifest
from .cert import SignatureInfo, analyze as analyze_signature
from .reader import ApkError, ApkReader

# 危险权限（需要在界面上高亮）
DANGEROUS_PERMISSIONS = {
    "android.permission.CAMERA",
    "android.permission.RECORD_AUDIO",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.ACCESS_BACKGROUND_LOCATION",
    "android.permission.READ_CONTACTS",
    "android.permission.WRITE_CONTACTS",
    "android.permission.READ_CALL_LOG",
    "android.permission.WRITE_CALL_LOG",
    "android.permission.READ_SMS",
    "android.permission.SEND_SMS",
    "android.permission.RECEIVE_SMS",
    "android.permission.CALL_PHONE",
    "android.permission.READ_PHONE_STATE",
    "android.permission.READ_PHONE_NUMBERS",
    "android.permission.BODY_SENSORS",
    "android.permission.ACTIVITY_RECOGNITION",
    "android.permission.READ_EXTERNAL_STORAGE",
    "android.permission.WRITE_EXTERNAL_STORAGE",
    "android.permission.MANAGE_EXTERNAL_STORAGE",
    "android.permission.ACCESS_MEDIA_LOCATION",
    "android.permission.READ_MEDIA_IMAGES",
    "android.permission.READ_MEDIA_VIDEO",
    "android.permission.READ_MEDIA_AUDIO",
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.BLUETOOTH_SCAN",
    "android.permission.BLUETOOTH_CONNECT",
    "android.permission.BLUETOOTH_ADVERTISE",
    "android.permission.NEARBY_WIFI_DEVICES",
    "android.permission.SYSTEM_ALERT_WINDOW",
    "android.permission.REQUEST_INSTALL_PACKAGES",
    "android.permission.PACKAGE_USAGE_STATS",
    "android.permission.QUERY_ALL_PACKAGES",
}

# 图标兜底：直接按文件名找（arsc 解析不到时）
_ICON_HINTS = ("ic_launcher", "app_icon", "icon", "logo")

# 密度优先级（越大越优先取）
_DENSITY_HINTS = [
    ("xxxhdpi", 640), ("xxhdpi", 480), ("xhdpi", 320),
    ("hdpi", 240), ("mdpi", 160), ("ldpi", 120),
]


# ---------------------------------------------------------------- 数据结构

@dataclass
class IntentFilter:
    actions: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    schemes: List[str] = field(default_factory=list)
    hosts: List[str] = field(default_factory=list)
    paths: List[str] = field(default_factory=list)
    mime_types: List[str] = field(default_factory=list)
    priority: int = 0
    auto_verify: bool = False

    @property
    def is_launcher(self) -> bool:
        return ("android.intent.action.MAIN" in self.actions
                and "android.intent.category.LAUNCHER" in self.categories)

    @property
    def is_deeplink(self) -> bool:
        return bool(self.schemes) and "android.intent.action.VIEW" in self.actions

    def summary(self) -> str:
        bits = []
        if self.actions:
            bits.append(" ".join(a.rsplit(".", 1)[-1] for a in self.actions))
        if self.schemes:
            bits.append("://".join([self.schemes[0], (self.hosts or [""])[0]]))
        if self.mime_types:
            bits.append(self.mime_types[0])
        return " · ".join(bits) if bits else "（空 filter）"

    def to_dict(self) -> dict:
        return {
            "actions": self.actions, "categories": self.categories,
            "schemes": self.schemes, "hosts": self.hosts, "paths": self.paths,
            "mimeTypes": self.mime_types, "priority": self.priority,
            "isLauncher": self.is_launcher, "isDeeplink": self.is_deeplink,
            "summary": self.summary(),
        }


@dataclass
class Component:
    """Activity / Service / Receiver / Provider 的统一描述。"""

    type: str = ""              # activity | service | receiver | provider
    name: str = ""              # 全限定类名
    exported: bool = False
    has_exported_attr: bool = False
    enabled: bool = True
    permission: str = ""
    process: str = ""
    launch_mode: str = ""
    intent_filters: List[IntentFilter] = field(default_factory=list)
    authorities: str = ""

    @property
    def full(self) -> str:
        return self.name

    @property
    def summary(self) -> str:
        """一行摘要：优先 launcher/deeplink，否则第一个 filter 的简写。"""
        for f in self.intent_filters:
            if f.is_launcher:
                return "启动入口（LAUNCHER）"
        for f in self.intent_filters:
            if f.is_deeplink:
                return "DeepLink · %s" % f.summary()
        return self.intent_filters[0].summary() if self.intent_filters else ""

    @property
    def short_name(self) -> str:
        """类名末段，列表里用。"""
        if "/" in self.name:
            return self.name.rsplit("/", 1)[-1]
        return self.name.rsplit(".", 1)[-1]

    @property
    def launchable(self) -> bool:
        """能不能被 `am start` 直接拉起。"""
        if self.type != "activity":
            return False
        if not self.enabled:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "type": self.type, "name": self.name,
            "exported": self.exported, "enabled": self.enabled,
            "permission": self.permission, "process": self.process,
            "launchMode": self.launch_mode, "authorities": self.authorities,
            "intentFilters": [f.to_dict() for f in self.intent_filters],
            "hasLauncher": any(f.is_launcher for f in self.intent_filters),
            "hasDeeplink": any(f.is_deeplink for f in self.intent_filters),
            "summary": (self.intent_filters[0].summary() if self.intent_filters else ""),
        }


def component_target(package: str, name: str) -> str:
    """把 包名 + 组件类名 拼成 `am -n` 认的写法（`_comp_dict` 用）。

    组件清单里的 `name` 是**全限定类名**，拿它直接当 target 是定不出组件的。
    两种合法写法：
      - 类名以包名开头 → `pkg/.tail`（最 idiomatic）
      - 否则 → `pkg/全限定类名`（同样合法，且是**唯一**能表达
        "安装包名 ≠ 代码包名" 的写法；真机实测 com.jxw.aizx.teacher
        的类全在 com.jxw.teacher.* 下）

    真正的拼接规则只在 `launch.intent.as_component` 里维护一份，这里复用，
    不另起一套（历史上就是因为两处各拼各的，才出现了裸命令 `am start`）。
    """
    pkg = (package or "").strip()
    cls = (name or "").strip()
    if not pkg or not cls:
        return ""
    if "/" in cls:
        return cls
    try:
        from ..launch.intent import as_component
    except Exception:
        return ""
    return as_component(pkg, cls)


@dataclass
class ApkInfo:
    """一个 APK 的全部可展示信息。"""

    path: str = ""
    file_name: str = ""
    file_size: int = 0
    file_mtime: float = 0.0

    package: str = ""
    version_name: str = ""
    version_code: int = 0
    app_label: str = ""
    app_label_raw: str = ""
    main_activity: str = ""
    application_class: str = ""

    min_sdk: int = 0
    target_sdk: int = 0
    compile_sdk: int = 0

    debuggable: bool = False
    allow_backup: bool = True
    uses_cleartext: bool = False
    extract_native_libs: bool = False
    network_security_config: str = ""

    permissions: List[str] = field(default_factory=list)
    declared_permissions: List[str] = field(default_factory=list)
    features: List[str] = field(default_factory=list)

    activities: List[Component] = field(default_factory=list)
    services: List[Component] = field(default_factory=list)
    receivers: List[Component] = field(default_factory=list)
    providers: List[Component] = field(default_factory=list)

    abis: List[str] = field(default_factory=list)
    dex_names: List[str] = field(default_factory=list)
    native_lib_count: int = 0
    size_breakdown: Dict[str, int] = field(default_factory=dict)

    signature: Optional[SignatureInfo] = None
    icon_path: str = ""
    icon_mime: str = ""
    icon_data_uri: str = ""

    channel: str = ""
    parse_ms: int = 0
    warnings: List[str] = field(default_factory=list)
    error: str = ""

    # 来源。空 = 本机文件；填了序列号 = 这个包是**从设备上按需读出来**的
    # （见 remote.py），此时 `path` 是设备上的路径，本机并不存在这个文件。
    source_device: str = ""
    # 远程读取的代价（adb 调用次数 / 搬运字节），用来证明没整包搬运
    remote_stats: Dict[str, Any] = field(default_factory=dict)

    # -- 派生属性 ----------------------------------------------------

    @property
    def ok(self) -> bool:
        return bool(self.package) and not self.error

    @property
    def dangerous_permissions(self) -> List[str]:
        return [p for p in self.permissions if p in DANGEROUS_PERMISSIONS]

    @property
    def launchable_activities(self) -> List[Component]:
        return [a for a in self.activities if a.launchable]

    @property
    def launcher_activities(self) -> List[Component]:
        return [a for a in self.activities if any(f.is_launcher for f in a.intent_filters)]

    @property
    def deeplinks(self) -> List[Component]:
        return [a for a in self.activities if any(f.is_deeplink for f in a.intent_filters)]

    @property
    def is_split(self) -> bool:
        return False  # 单文件 APK；split 判断在安装模块做

    def component_dicts(self, ctype: Optional[str] = None) -> List[dict]:
        """组件清单（dict 形态），每项都带 `am` 能直接吃的 `target`。"""
        return [self._comp_dict(c) for c in self.components(ctype)]

    def _comp_dict(self, c: "Component") -> dict:
        d = c.to_dict()
        d["target"] = component_target(self.package, c.name)
        return d

    def components(self, ctype: Optional[str] = None) -> List[Component]:
        if ctype == "activity":
            return self.activities
        if ctype == "service":
            return self.services
        if ctype == "receiver":
            return self.receivers
        if ctype == "provider":
            return self.providers
        return self.activities + self.services + self.receivers + self.providers

    # -- 序列化 ------------------------------------------------------

    def to_dict(self, with_signature: bool = True, with_components: bool = True,
                with_icon: bool = True) -> dict:
        d: Dict[str, Any] = {
            "path": self.path,
            "fileName": self.file_name,
            "fileSize": self.file_size,
            "mtime": self.file_mtime,
            "package": self.package,
            "versionName": self.version_name,
            "versionCode": self.version_code,
            "appLabel": self.app_label,
            "mainActivity": self.main_activity,
            "applicationClass": self.application_class,
            "minSdk": self.min_sdk,
            "targetSdk": self.target_sdk,
            "compileSdk": self.compile_sdk,
            "debuggable": self.debuggable,
            "allowBackup": self.allow_backup,
            "usesCleartext": self.uses_cleartext,
            "networkSecurityConfig": self.network_security_config,
            "permissions": self.permissions,
            "dangerousPermissions": self.dangerous_permissions,
            "declaredPermissions": self.declared_permissions,
            "features": self.features,
            "abis": self.abis,
            "dexNames": self.dex_names,
            "nativeLibCount": self.native_lib_count,
            "sizeBreakdown": self.size_breakdown,
            "channel": self.channel,
            "parseMs": self.parse_ms,
            "warnings": self.warnings,
            "error": self.error,
            "componentCounts": {
                "activity": len(self.activities),
                "service": len(self.services),
                "receiver": len(self.receivers),
                "provider": len(self.providers),
            },
        }
        if with_signature and self.signature:
            d["signature"] = self.signature.to_dict()
        if with_components:
            d["components"] = {
                "activity": self.component_dicts("activity"),
                "service": self.component_dicts("service"),
                "receiver": self.component_dicts("receiver"),
                "provider": self.component_dicts("provider"),
            }
        if with_icon:
            d["icon"] = self.icon_data_uri
            d["iconPath"] = self.icon_path
        return d


# ---------------------------------------------------------------- 解析

_MIME_BY_EXT = {
    ".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".xml": "image/xml",
}


def parse_apk(
    path,
    with_signature: bool = True,
    with_icon: bool = True,
    with_components: bool = True,
    verify_signature: bool = False,
) -> ApkInfo:
    """解析一个本机 APK 文件。

    不抛异常：失败信息放在 `info.error` 里（右键弹窗必须能优雅报错）。
    """
    t0 = time.time()
    p = Path(path)
    info = ApkInfo(path=str(p.resolve()) if p.exists() else str(p), file_name=p.name)

    try:
        st = p.stat()
        info.file_size = st.st_size
        info.file_mtime = st.st_mtime
    except OSError:
        pass

    reader = None
    try:
        reader = ApkReader(p)
        _read_into(info, reader,
                   with_signature=with_signature,
                   with_icon=with_icon,
                   verify_signature=verify_signature)
    except ApkError as e:
        info.error = str(e)
    except Exception as e:  # 兜底：任何解析异常都要变成可读信息
        info.error = "解析失败：%s: %s" % (type(e).__name__, e)
    finally:
        if reader:
            reader.close()

    info.parse_ms = int((time.time() - t0) * 1000)
    return info


def parse_apk_reader(
    reader: ApkReader,
    path: str = "",
    file_name: str = "",
    file_size: int = 0,
    file_mtime: float = 0.0,
    with_signature: bool = True,
    with_icon: bool = True,
    verify_signature: bool = False,
) -> ApkInfo:
    """用**已经打开**的 reader 解析成 `ApkInfo` —— 设备上的 APK 走这条。

    为什么不直接复用 `parse_apk`：那个函数第一件事是 `Path(path).stat()`，
    它默认"要解析的东西就在本机磁盘上"。设备上的 APK 没有本机路径，但
    `remote.py` 已经把它包装成了与本地**同构**的 reader。

    这里**必须**和本机走同一段解析代码（`_read_into`）—— 否则两边会差出
    解析器本身的差异，而"本机 APK vs 设备应用"的对比正是要排除这种差异，
    差异里混进噪音就等于结论不可信。

    reader 的生命周期归调用方（本函数不关它）。
    不抛异常：失败信息放在 `info.error` 里。
    """
    t0 = time.time()
    name = file_name or (path.replace("\\", "/").rsplit("/", 1)[-1] if path else "")
    info = ApkInfo(path=path, file_name=name)
    info.file_size = int(file_size or 0)
    info.file_mtime = float(file_mtime or 0.0)
    if not info.file_size:
        try:
            info.file_size = reader.file_size()   # 远程模式下读的是 fileobj.size
        except Exception:
            pass

    try:
        _read_into(info, reader,
                   with_signature=with_signature,
                   with_icon=with_icon,
                   verify_signature=verify_signature)
    except ApkError as e:
        info.error = str(e)
    except Exception as e:
        info.error = "解析失败：%s: %s" % (type(e).__name__, e)

    info.parse_ms = int((time.time() - t0) * 1000)
    return info


def _read_into(info: ApkInfo, reader: ApkReader, with_signature: bool = True,
               with_icon: bool = True, verify_signature: bool = False) -> None:
    """`reader → info` 的**唯一**实现（本机与设备共用）。"""
    arsc_table = None
    raw_arsc = reader.read_arsc()
    if raw_arsc:
        try:
            arsc_table = arsc_mod.ResourceTable(raw_arsc)
        except Exception as e:
            info.warnings.append("resources.arsc 解析失败：%s" % e)

    root = parse_manifest(reader.read_manifest())
    _fill_from_manifest(info, root, arsc_table)

    info.abis = reader.abis()
    info.dex_names = reader.dex_names()
    info.native_lib_count = len(reader.native_lib_names())
    info.size_breakdown = reader.size_breakdown()
    info.channel = _detect_channel(reader, root, arsc_table)

    if with_icon:
        _fill_icon(info, reader, root, arsc_table)
    if with_signature:
        info.signature = analyze_signature(reader, verify_with_apksigner=verify_signature)


def read_app_label(reader: ApkReader) -> str:
    """**只**解应用名：AndroidManifest.xml + resources.arsc，别的都不碰。

    设备端动辄几百个应用，每个都走完整 `parse_apk`（图标 / 签名 / 体积 /
    组件）代价太大 —— 配合 `remote.py` 的远程定点读取，这条路径只读两个
    条目，约 1 秒一个包。

    取不到时返回空串（调用方自己决定要不要退回包名末段）。
    """
    arsc_t = None
    raw = reader.read_arsc()
    if raw:
        try:
            arsc_t = arsc_mod.ResourceTable(raw)
        except Exception:
            arsc_t = None

    root = parse_manifest(reader.read_manifest())
    app = root.first("application")
    if app is None:
        return ""
    got = _resolve(app, "label", arsc_t)
    if got is None:
        return ""
    text = str(got).strip()
    # 有些包把 label 写成空串/纯空格，等于没有
    return text


def _fill_from_manifest(info: ApkInfo, root: Node, arsc_t) -> None:
    """从 manifest 树里抽字段。"""
    info.package = str(root.attr("package", "") or "")
    info.version_code = root.int_attr("versionCode", 0)
    info.version_name = _resolve(root, "versionName", arsc_t) or ""
    if isinstance(info.version_name, int):
        info.version_name = str(info.version_name)

    uses_sdk = root.first("uses-sdk")
    if uses_sdk:
        info.min_sdk = uses_sdk.int_attr("minSdkVersion", 0)
        info.target_sdk = uses_sdk.int_attr("targetSdkVersion", 0)

    info.compile_sdk = root.int_attr("compileSdkVersion", 0)

    for node in root.find("uses-permission"):
        n = node.raw("name") or node.attr("name")
        if n:
            info.permissions.append(str(n))
    for node in root.find("uses-permission-sdk-23"):
        n = node.raw("name") or node.attr("name")
        if n and str(n) not in info.permissions:
            info.permissions.append(str(n))
    for node in root.find("permission"):
        n = node.raw("name") or node.attr("name")
        if n:
            info.declared_permissions.append(str(n))
    for node in root.find("uses-feature"):
        n = node.raw("name") or node.attr("name")
        if n:
            info.features.append(str(n))

    app = root.first("application")
    if app is None:
        info.warnings.append("AndroidManifest 里没有 <application> 节点")
        return

    info.app_label_raw = str(app.attr("label") or "")
    info.app_label = _resolve(app, "label", arsc_t) or info.package.rsplit(".", 1)[-1]
    info.debuggable = app.bool_attr("debuggable", False)
    info.allow_backup = app.bool_attr("allowBackup", True)
    info.uses_cleartext = app.bool_attr("usesCleartextTraffic", False)
    info.extract_native_libs = app.bool_attr("extractNativeLibs", False)
    info.application_class = str(app.attr("name") or "")
    info.network_security_config = str(app.attr("networkSecurityConfig") or "")

    for ctype, bucket in (("activity", info.activities), ("service", info.services),
                          ("receiver", info.receivers), ("provider", info.providers)):
        for el in app.find(ctype):
            comp = _parse_component(el, ctype)
            if comp.name:
                bucket.append(comp)

    # 别名 activity 的 targetActivity 也能启动，保留原名即可（am 用别名）
    for el in app.find("activity-alias"):
        comp = _parse_component(el, "activity")
        if comp.name:
            info.activities.append(comp)

    # 相对类名补全为全限定名（manifest 里写 ".ui.MainActivity" 是常见写法）
    for comp in info.components():
        if comp.name.startswith("."):
            comp.name = info.package + comp.name

    launchers = info.launcher_activities
    info.main_activity = launchers[0].name if launchers else (
        info.activities[0].name if info.activities else "")


def _parse_component(el: Node, ctype: str) -> Component:
    c = Component(type=ctype)
    name = el.raw("name") or el.attr("name") or ""
    c.name = str(name)
    c.permission = str(el.raw("permission") or el.attr("permission") or "")
    c.process = str(el.raw("process") or el.attr("process") or "")
    c.authorities = str(el.raw("authorities") or el.attr("authorities") or "")

    exported_attr = el.attr_obj("exported")
    c.has_exported_attr = exported_attr is not None
    c.exported = el.bool_attr("exported", False)
    c.enabled = el.bool_attr("enabled", True)

    mode = el.attr("launchMode")
    if isinstance(mode, int):
        c.launch_mode = {0: "standard", 1: "singleTop", 2: "singleTask", 3: "singleInstance"}.get(mode, str(mode))
    elif mode:
        c.launch_mode = str(mode)

    for f in el.find("intent-filter"):
        flt = IntentFilter()
        flt.priority = f.int_attr("priority", 0)
        flt.auto_verify = f.bool_attr("autoVerify", False)
        for a in f.find("action"):
            v = a.raw("name") or a.attr("name")
            if v:
                flt.actions.append(str(v))
        for cat in f.find("category"):
            v = cat.raw("name") or cat.attr("name")
            if v:
                flt.categories.append(str(v))
        for d in f.find("data"):
            for key, bucket in (("scheme", flt.schemes), ("host", flt.hosts),
                                ("path", flt.paths), ("pathPrefix", flt.paths),
                                ("pathPattern", flt.paths), ("mimeType", flt.mime_types)):
                v = d.raw(key) or d.attr(key)
                if v:
                    bucket.append(str(v))
        c.intent_filters.append(flt)

    return c


_REF_PLACEHOLDER = re.compile(r"^[?@]0x[0-9a-fA-F]+$")


def _resolve(node: Node, attr_name: str, arsc_t) -> Optional[str]:
    """取属性值；是资源引用时用 arsc 还原成字符串。

    解不开时**不要**把 `?0x7f110135` 这种占位串当成结果返回 —— 那是
    资源引用的字面量，对外显示成"应用名"纯属误导（曾经列表里就出现过
    `?0x7f110135` 这种"应用名"）。解不开就返回 None，让上层走兜底。
    """
    a = node.attr_obj(attr_name)
    if a is None:
        return None
    if a.ref_id and arsc_t is not None:
        # 先按当前配置挑最佳语言，再退一步跟随引用链
        for fn_name in ("resolve_string", "resolve_string_ref"):
            fn = getattr(arsc_t, fn_name, None)
            if fn is None:
                continue
            try:
                got = fn(a.ref_id)
            except Exception:
                got = None
            if got:
                return got
    if a.value is None:
        return None
    text = str(a.value)
    if _REF_PLACEHOLDER.match(text):
        return None
    return text


# ---------------------------------------------------------------- 图标

def _fill_icon(info: ApkInfo, reader: ApkReader, root: Node, arsc_t) -> None:
    """尽力取到一个能显示的图标（PNG/WebP），转 data URI。"""
    app = root.first("application")
    candidates: List[str] = []

    if app is not None and arsc_t is not None:
        for attr in ("icon", "roundIcon"):
            a = app.attr_obj(attr)
            if a is not None and a.ref_id:
                try:
                    path = arsc_t.icon_best_path(a.ref_id)
                    if path:
                        candidates.append(path)
                    candidates.extend(arsc_t.all_icon_paths(a.ref_id))
                except Exception:
                    pass

    # 兜底：按文件名在 zip 里找
    if not candidates:
        candidates.extend(_guess_icons_by_name(reader))

    for cand in _dedupe(candidates):
        blob = reader.read(cand)
        if not blob or len(blob) < 32:
            continue
        ext = (cand.rsplit(".", 1)[-1] or "").lower()
        mime = _MIME_BY_EXT.get("." + ext)
        if mime in (None, "image/xml"):
            continue
        info.icon_path = cand
        info.icon_mime = mime
        info.icon_data_uri = "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode("ascii"))
        return


def _guess_icons_by_name(reader: ApkReader) -> List[str]:
    scored = []
    for name in reader.names:
        low = name.lower()
        if not low.startswith("res/") or not low.endswith((".png", ".webp")):
            continue
        base = low.rsplit("/", 1)[-1]
        if not any(h in base for h in _ICON_HINTS):
            continue
        score = 0
        for hint, val in _DENSITY_HINTS:
            if hint in low:
                score = val
                break
        if "ic_launcher" in base:
            score += 1000
        scored.append((score, name))
    return [n for _s, n in sorted(scored, key=lambda kv: -kv[0])]


def _dedupe(seq: List[str]) -> List[str]:
    seen, out = set(), []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------- 渠道

_CHANNEL_META = re.compile(r"(channel|渠道)", re.IGNORECASE)


def _detect_channel(reader: ApkReader, root: Node, arsc_t) -> str:
    """识别打包渠道（面向多渠道构建的日常排查）。

    三条线索按可信度排序：
      1. manifest 里的 meta-data（name 含 channel）
      2. META-INF/ 下的渠道标记文件
      3. 内置渠道清单文件（assets/channel*、assets/*channel*）
    """
    app = root.first("application")
    if app is not None:
        for meta in app.find("meta-data"):
            name = str(meta.raw("name") or meta.attr("name") or "")
            if _CHANNEL_META.search(name):
                val = _resolve(meta, "value", arsc_t)
                if val:
                    return str(val)

    for name in reader.names:
        if name.upper().startswith("META-INF/") and "channel" in name.lower():
            tail = name.rsplit("/", 1)[-1]
            if tail.upper() in ("MANIFEST.MF", "CERT.SF"):
                continue
            if tail.lower().endswith((".rsa", ".dsa", ".ec", ".sf", ".mf")):
                continue
            tag = tail.rsplit(".", 1)[0]
            tag = re.sub(r"^channel[_-]?", "", tag, flags=re.IGNORECASE)
            return tag or tail

    for name in reader.names:
        low = name.lower()
        if low.startswith("assets/") and "channel" in low and low.endswith((".txt", ".json", ".config", ".properties")):
            blob = reader.read(name)
            if blob:
                text = blob.decode("utf-8", errors="replace").strip()
                if 0 < len(text) <= 64:
                    return text.splitlines()[0].strip()
            return name.rsplit("/", 1)[-1]
    return ""
