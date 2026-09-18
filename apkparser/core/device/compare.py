# -*- coding: utf-8 -*-
"""对比的两侧：本机 APK 文件 / 设备已装应用 —— 任意组合。

## 与 `signature.compare_with_apk` 的分工

`signature.compare_with_apk` 只比签名（`dumpsys` 拿签名标识 + 必要时 pull 算证书），
回答的是"**装得上吗**"。这里是**整包**逐项比较（版本 / 权限 / 组件 / 签名 / 体积 / ABI），
回答的是"**这两个包差在哪**"。两个都留着 —— 前者快（0.3s）且够判同，后者信息全。

## 设备侧的包从哪儿来

三条路，按代价从低到高，能用低的不上高的：

  ① 远程按需读取  `pm path` 拿 base.apk → `adb exec-out dd` 只取要用的字节
                 （实测：41.5MB 的包只用 6 次 adb 调用、搬 4.8MB），**默认走这条**
  ② 拉整包到本地  `adb pull` → 本地 `parse_apk`（几十 MB 搬运），**兜底**
  ③ 用 dumpsys 拼一个"半个 ApkInfo" —— **刻意不做**

③ 为什么不做：dumpsys 给的权限是**已授予**的、组件是系统筛过的子集、没有体积构成。
   拿它跟本机 APK 比，差出来的全是"来源不同"而不是"包不同" —— 结论会骗人，
   而且骗得很像真的。宁可多花 1 秒，也要两边同源（同一个解析器、同一套字段）。

## 为什么不吃 apk_cache 里已拉过的包

`cache/apk/<serial>/<pkg>__<versionCode>.apk` 的键里只有 versionCode。
开发期最常见的一幕恰恰是"**同一个 versionCode 重新打一个包**"（换签名 / 换渠道），
命中缓存会拿旧二进制去比，给出"完全一样"这种最危险的错误结论。
所以这里不用缓存，只认设备上此刻那一份。
"""

from typing import List

from ...platform import paths
from ..apk import parse_apk
from ..apk.analyze import diff_apks
from ..apk.remote import parse_remote_apk
from . import adb as adb_mod
from . import apps as apps_mod

# 每一侧的来源标识（会回显给界面：用户有权知道这份数据是怎么来的）
SRC_FILE = "file"                    # 本机文件
SRC_DEVICE_REMOTE = "device-remote"  # 设备上按需读取（没把整包搬过来）
SRC_DEVICE_PULLED = "device-pulled"  # 拉回本地再解析（兜底）

SOURCE_LABELS = {
    SRC_FILE: "本机文件",
    SRC_DEVICE_REMOTE: "设备（按需读取）",
    SRC_DEVICE_PULLED: "设备（拉回本地）",
}


# ---------------------------------------------------------------- 设备取包

def device_apk_paths(package: str, serial: str) -> dict:
    """设备上这个包的 APK 路径列表（base 在最前）。永不抛异常。"""
    try:
        found, commands = apps_mod.package_paths(package, serial)
    except Exception as e:
        return {"ok": False, "paths": [], "commands": [],
                "detail": "取 APK 路径失败：%s" % e}
    if not found:
        return {"ok": False, "paths": [], "commands": commands,
                "detail": "设备上没有这个包（或它被系统保护、读不到路径）"}
    return {"ok": True, "paths": found, "commands": commands, "detail": ""}


def _pull_to_tmp(serial: str, remote: str) -> dict:
    """把设备上一个文件拉到临时目录。返回 {ok, path, commands, detail}。"""
    name = remote.replace("\\", "/").rsplit("/", 1)[-1] or "base.apk"
    try:
        dest_dir = paths.tmp_dir() / "device-apk"
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"ok": False, "path": "", "commands": [], "detail": "建临时目录失败：%s" % e}
    local = dest_dir / ("%s__%s" % (serial or "dev", name))
    args = ["pull", remote, str(local)]
    r = adb_mod.run(args, serial=serial, timeout=300)
    cmds = [adb_mod.command_preview(args, serial)]
    if r.ok and local.is_file():
        return {"ok": True, "path": str(local), "commands": cmds, "detail": ""}
    return {"ok": False, "path": "", "commands": cmds,
            "detail": "adb pull 失败：%s" % (r.err or r.out or "").strip()[:160]}


def parse_device_package(package: str, device: str = "",
                         with_signature: bool = True,
                         allow_pull: bool = True) -> dict:
    """把设备上已装的包解析成 `ApkInfo`（含来源信息）。

    返回 {ok, info, source, remotePath, serial, splits, commands, notes, detail}
    —— `info` 是 `ApkInfo`，失败时为 None。
    """
    serial = ""
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "info": None, "source": "", "remotePath": "",
                "serial": "", "splits": [], "commands": [], "notes": [],
                "detail": str(e)}

    got = device_apk_paths(package, serial)
    commands = list(got["commands"])
    if not got["ok"]:
        return {"ok": False, "info": None, "source": "", "remotePath": "",
                "serial": serial, "splits": [], "commands": commands,
                "notes": [], "detail": got["detail"]}

    remote_all: List[str] = got["paths"]
    base = remote_all[0]
    notes: List[str] = []
    if len(remote_all) > 1:
        notes.append("设备上这个应用是 split 安装（base + %d 个拆分包，如 %s）—— "
                     "这里只对比了 base.apk"
                     % (len(remote_all) - 1,
                        ", ".join(p.rsplit("/", 1)[-1] for p in remote_all[1:4])))

    # ---- ① 远程按需读取（不搬整包）----
    info = parse_remote_apk(serial, base, with_signature=with_signature, with_icon=False)
    commands = commands + ["# 按需读取 %s（adb exec-out dd，未整包 pull）" % base]
    if info.ok:
        return {"ok": True, "info": info, "source": SRC_DEVICE_REMOTE,
                "remotePath": base, "serial": serial, "splits": remote_all,
                "commands": commands, "notes": notes, "detail": ""}

    remote_err = info.error or "未知原因"

    # ---- ② 兜底：拉回本地再解析 ----
    if not allow_pull:
        return {"ok": False, "info": None, "source": "", "remotePath": base,
                "serial": serial, "splits": remote_all, "commands": commands,
                "notes": notes,
                "detail": "按需读取失败：%s（已禁用 pull 兜底）" % remote_err}
    pulled = _pull_to_tmp(serial, base)
    commands = commands + list(pulled["commands"])
    if not pulled["ok"]:
        return {"ok": False, "info": None, "source": "", "remotePath": base,
                "serial": serial, "splits": remote_all, "commands": commands,
                "notes": notes,
                "detail": "按需读取失败（%s）；%s" % (remote_err, pulled["detail"])}

    info2 = parse_apk(pulled["path"], with_signature=with_signature, with_icon=False)
    if not info2.ok:
        return {"ok": False, "info": None, "source": "", "remotePath": base,
                "serial": serial, "splits": remote_all, "commands": commands,
                "notes": notes,
                "detail": "按需读取失败（%s）；拉回本地后解析也失败（%s）"
                          % (remote_err, info2.error)}
    notes.append("设备侧这份是拉回本地解析的（按需读取失败：%s）" % remote_err)
    info2.source_device = serial
    return {"ok": True, "info": info2, "source": SRC_DEVICE_PULLED,
            "remotePath": base, "serial": serial, "splits": remote_all,
            "commands": commands, "notes": notes, "detail": ""}


def device_facts(package: str, serial: str) -> dict:
    """设备侧的安装事实（安装器 / 是否系统应用 / 安装时间）。取不到就空着。"""
    try:
        d = apps_mod.app_detail(package, serial)
    except Exception:
        return {}
    if not isinstance(d, dict) or d.get("error"):
        return {}
    keep = ("package", "versionCode", "versionName", "label", "system",
            "installer", "firstInstall", "lastUpdate", "enabled", "uid",
            "apkPath", "abi", "splits")
    return {k: d[k] for k in keep if k in d}


# ---------------------------------------------------------------- 统一入口

class SideSpec:
    """一侧的输入：本机文件路径，或设备上的包名。

    `SideSpec.device("")` 是合法的 —— 含义是"设备上、包名待定"，
    由另一侧（本机文件解析出来的包名，或另一侧的包名）补齐。
    也就是界面上那句"留空 = 跟设备上的同名应用比"。
    """

    def __init__(self, kind: str, value: str = ""):
        self.kind = kind              # "file" | "device" | "" (完全没给)
        self.value = (value or "").strip()

    @property
    def empty(self) -> bool:
        """完全没给（既不是文件也不是设备包）。`device("")` 不算空 —— 它待补齐。"""
        return not self.kind

    @property
    def unresolved(self) -> bool:
        """是"设备上"但包名还是空的（另一侧也没能补齐）。"""
        return self.kind == "device" and not self.value

    @classmethod
    def file(cls, path) -> "SideSpec":
        return cls("file", str(path or ""))

    @classmethod
    def device(cls, package) -> "SideSpec":
        return cls("device", str(package or ""))

    def label(self) -> str:
        if self.kind == "file":
            return self.value
        if self.kind == "device":
            return self.value or "（设备上的同名应用）"
        return "（空）"


def _pkg_of_local(path: str) -> str:
    """只读出本机 APK 的包名（跳过签名/图标，代价小）。"""
    try:
        probe = parse_apk(path, with_signature=False, with_icon=False,
                          with_components=False)
    except Exception:
        return ""
    return probe.package or ""


def _fill_missing_package(a: SideSpec, b: SideSpec):
    """把"设备上、包名待定"的一侧补齐成明确包名。

    这是"与设备上的同名应用对比"能一键完成的关键：用户只需要给出本机那份
    APK，另一侧留空即可 —— 但**必须在这里补齐**，而不是在界面里猜：
    包名只能从 APK 里读出来，界面拿不到。
    """
    if a.kind == "device" and not a.value:
        p = b.value if b.kind == "device" else _pkg_of_local(b.value) if b.kind == "file" else ""
        if p:
            a = SideSpec.device(p)
    if b.kind == "device" and not b.value:
        p = a.value if a.kind == "device" else _pkg_of_local(a.value) if a.kind == "file" else ""
        if p:
            b = SideSpec.device(p)
    return a, b


def _side_info(spec: SideSpec, device: str, allow_pull: bool) -> dict:
    """把一侧解析成 ApkInfo（本机文件或设备包），并带上来源/命令/提示。"""
    if spec.kind == "file":
        info = parse_apk(spec.value, with_signature=True, with_icon=False)
        if info.error or not info.package:
            return {"ok": False, "info": None, "source": SRC_FILE,
                    "serial": "", "remotePath": "", "splits": [],
                    "commands": [], "notes": [],
                    "detail": "本机 APK 解析失败：%s" % (info.error or "读不出包名")}
        return {"ok": True, "info": info, "source": SRC_FILE, "serial": "",
                "remotePath": "", "splits": [], "commands": [], "notes": [],
                "detail": ""}
    got = parse_device_package(spec.value, device=device, with_signature=True,
                               allow_pull=allow_pull)
    return got


def diff_sides(a: SideSpec, b: SideSpec, device: str = "",
               allow_pull: bool = True) -> dict:
    """对比两侧（本机文件 / 设备应用，任意组合）。

    返回结构与 `diff_apks` 同构，外加 `device` / `notes` / `commands` /
    `remoteStats` / `deviceFacts` —— 所以前端两种模式共用一套渲染代码。
    失败时返回 `{"ok": False, "error": ...}`（不抛异常，界面要好报错）。
    """
    if a.empty or b.empty:
        return {"ok": False, "error": "两侧都要填：可以是本机 APK 路径，"
                                      "也可以是设备上的包名"}
    a, b = _fill_missing_package(a, b)
    for tag, spec in (("A", a), ("B", b)):
        if spec.unresolved:
            return {"ok": False,
                    "error": "%s 侧没给出包名，而且没法从另一侧推出来 —— "
                             "请直接填包名，或把另一侧换成一份本机 APK" % tag}

    ra = _side_info(a, device, allow_pull)
    if not ra["ok"]:
        return {"ok": False, "error": "A 侧取不到：%s" % ra["detail"],
                "commands": ra["commands"]}
    rb = _side_info(b, device, allow_pull)
    if not rb["ok"]:
        return {"ok": False, "error": "B 侧取不到：%s" % rb["detail"],
                "commands": ra["commands"] + rb["commands"]}

    ia, ib = ra["info"], rb["info"]
    d = diff_apks(ia, ib)

    d["a"].update({"source": ra["source"], "device": ra["serial"],
                   "deviceFile": ra["remotePath"]})
    d["b"].update({"source": rb["source"], "device": rb["serial"],
                   "deviceFile": rb["remotePath"]})

    notes = list(ra["notes"]) + list(rb["notes"])
    if ia.package and ib.package and ia.package != ib.package:
        notes.append("两侧包名不同（A 是 %s，B 是 %s）—— 这不是同一个应用的版本对比，"
                     "逐项差异仅供参考" % (ia.package, ib.package))
    for tag, r in (("A", ra), ("B", rb)):
        if r["source"] != SRC_FILE and not r["info"].signature:
            notes.append("%s 侧（设备）的签名没读出来，签名一项未参与比对" % tag)

    facts = {}
    for r in (ra, rb):
        if r["serial"]:
            facts = device_facts(r["info"].package, r["serial"])
            break

    d.update({
        "ok": True,
        "package": ib.package or ia.package,
        "notes": notes,
        "commands": ra["commands"] + rb["commands"],
        "device": ra["serial"] or rb["serial"],
        "deviceFacts": facts,
        "remoteStats": dict(getattr(ib, "remote_stats", {}) or {})
                       or dict(getattr(ia, "remote_stats", {}) or {}),
        "splits": rb["splits"] or ra["splits"],
        "sources": {"a": {"kind": a.kind, "value": a.label(), "from": ra["source"]},
                    "b": {"kind": b.kind, "value": b.label(), "from": rb["source"]}},
    })
    return d


def diff_with_device(local_apk: str, package: str = "", device: str = "",
                     allow_pull: bool = True) -> dict:
    """本机 APK ⟷ 设备已装应用（最常用的那一种；`package` 留空 = 同名应用）。"""
    return diff_sides(SideSpec.file(local_apk), SideSpec.device(package),
                      device=device, allow_pull=allow_pull)
