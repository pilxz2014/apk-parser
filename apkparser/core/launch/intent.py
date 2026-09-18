# -*- coding: utf-8 -*-
"""Intent 模型 → `am` 命令行参数。**唯一的真相来源**。

界面上的命令预览、CLI 的实际执行、以及"复制命令"按钮，全都走这里，
所以不存在"预览和实际执行不一致"的可能。

一个容易忽略的细节：`am` 的 `-f` 只接受一个整数，多个 Flag 必须按位或，
所以这里先把名字翻译成位值再合并，而不是拼成多个 `-f`。
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------- 组件写法

def as_component(pkg: str, cls: str) -> str:
    """把 包名 + 类名 组成 `am` 真正能吃的 `pkg/Class`。

    **关键细节**：`ComponentName.unflattenFromString` 只对**以 `.` 开头**的类名
    补包名前缀。所以写成 `com.foo/WifiListActivity`（类名不带包名）会被当成
    "类名就叫 WifiListActivity" 的无包名组件，直接
    `Unable to find explicit activity class`。

    正确写法只有两种：
      - 类名以包名开头 → 用缩写形式 `pkg/.tail`（最idiomatic，也是 am 文档写法）
      - 否则 → 类名必须是全限定名 `pkg/com.foo.bar.Cls`
    """
    pkg = (pkg or "").strip()
    cls = (cls or "").strip()
    if not pkg or not cls:
        return ""
    if cls.startswith("."):
        return "%s/%s" % (pkg, cls)
    if cls.startswith(pkg + "."):
        return "%s/.%s" % (pkg, cls[len(pkg) + 1:])
    return "%s/%s" % (pkg, cls)


def as_component_parts(pkg: str, cls: str) -> Tuple[str, str]:
    """`as_component` 的分裂版：返回 (包名, 类名)，类名补成全限定名。

    界面要分列显示"包名 / 类名"时用这个，保证和拼出来的 `-n` 一致。
    """
    pkg = (pkg or "").strip()
    cls = (cls or "").strip()
    if not pkg or not cls:
        return "", ""
    if cls.startswith("."):
        return pkg, pkg + cls
    return pkg, cls


# 设备已装包名集合缓存：serial -> (时间戳, 集合)
_PKG_CACHE: Dict[str, Tuple[float, Set[str]]] = {}
# 失败负缓存：serial -> 失败时间戳。
# 没有它的话，adb 一挂 / 设备一拔，**每一次**命令预览都要实打实等一次
# 30 秒超时（`pm list packages` 的 timeout），界面直接卡成幻灯片。
_PKG_FAIL: Dict[str, float] = {}
_PKG_FAIL_TTL = 20.0


def installed_packages(device: str = "", ttl: float = 120.0) -> Optional[Set[str]]:
    """设备上的包名集合（带缓存）。**拿不到就返回 None**，绝不猜。

    有这套集合，"裸全限定类名 → pkg/Class" 才是**确定**的：
    从最长的包名前缀往回试，第一个命中的就是真包名。
    没有它就只能靠字符串猜，而 `com.android.settings.homepage.SettingsHomepageActivity`
    这类壳式类名一猜就错（真包名是 com.android.settings）。
    """
    adb_mod = None
    serial = device or ""
    try:
        from ..device import adb as adb_mod
        serial = adb_mod.pick_device(device or "")
        now = time.time()
        if (now - _PKG_FAIL.get(serial, 0.0)) < _PKG_FAIL_TTL:
            return None                    # 刚失败过，别重复撞墙
        hit = _PKG_CACHE.get(serial)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
        r = adb_mod.run(["shell", "pm", "list", "packages"], serial=serial, timeout=30)
        if not r.ok:
            _PKG_FAIL[serial] = now
            return None
        pkgs: Set[str] = set()
        for line in (r.out or "").splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkgs.add(line[len("package:"):].strip())
        if not pkgs:
            _PKG_FAIL[serial] = now
            return None
        _PKG_CACHE[serial] = (now, pkgs)
        _PKG_FAIL.pop(serial, None)
        return pkgs
    except Exception:
        if adb_mod is not None:
            try:
                _PKG_FAIL[adb_mod.pick_device(device or "")] = time.time()
            except Exception:
                pass
        else:
            _PKG_FAIL[serial] = time.time()
        return None


def forget_packages(device: str = "") -> None:
    """装/卸载之后清一下缓存（不然新装的包会被判成"不存在"）。"""
    if not device:
        _PKG_CACHE.clear()
        _PKG_FAIL.clear()
        return
    for k in list(_PKG_CACHE):
        if k == device or k.startswith(device):
            _PKG_CACHE.pop(k, None)
    for k in list(_PKG_FAIL):
        if k == device or k.startswith(device):
            _PKG_FAIL.pop(k, None)


def launcher_component(package: str, device: str = "") -> str:
    """向设备问这个包的启动入口，返回 `pkg/.MainActivity`（查不到返回空串）。

    只给了一个包名时必须走这里 —— `am start` 没有"启动某包首页"这种语法，
    必须落到具体组件上（或走 monkey，那条路会另开进程、拿不到 TotalTime）。
    """
    package = (package or "").strip()
    if not package:
        return ""
    try:
        from ..device import adb as adb_mod
        serial = adb_mod.pick_device(device or "")
        args = ["shell", "cmd", "package", "resolve-activity", "--brief",
                "-c", "android.intent.category.LAUNCHER", package]
        r = adb_mod.run(args, serial=serial, timeout=20)
        for line in (r.out or "").splitlines():
            line = line.strip()
            if "/" not in line:
                continue
            if line.startswith(("priority", "No activity", "Activity", "Resolving", "package:")):
                continue
            comp = line.split()[0]
            if "/" in comp:
                return comp
    except Exception:
        pass
    return ""


def _code_package_segments(cls: str) -> List[str]:
    """从全限定类名取出「代码包名」的段（去掉最后一段类名）。

    `com.jxw.teacher.ui.activity.LoginActivity`
        → ['com', 'jxw', 'teacher', 'ui', 'activity']
    """
    segs = [s for s in (cls or "").strip().split(".") if s]
    return segs[:-1] if len(segs) > 1 else []


def _subsequence_score(code_segs: List[str], pkg: str) -> int:
    """包名 `pkg` 的各段**按序**能匹配上 `code_segs` 的段数。

    `com.jxw.teacher.ui.activity` 对 `com.jxw.aizx.teacher` → 3（com, jxw, teacher）
    `com.jxw.teacher.ui.activity` 对 `com.jxw.aikyls`       → 2（com, jxw）
    """
    i = 0
    for s in (pkg or "").split("."):
        if i < len(code_segs) and s == code_segs[i]:
            i += 1
    return i


def guess_packages_for_class(cls: str, pkgs: Optional[Set[str]],
                             limit: int = 6) -> List[str]:
    """给一个裸全限定类名，按可能性排出它可能属于的设备包。

    **为什么需要**：applicationId 和代码包名经常不一致。真机实测
    `com.jxw.aizx.teacher` 里所有类都在 `com.jxw.teacher.ui.activity.*` 下 ——
    这时"最长前缀匹配"是**永远解不出来**的（`com.jxw.teacher` 根本不是已装包名），
    只能反过来查：哪个已装包的段，按序覆盖了类名的代码包名段。

    这仍然是启发式，所以只用于两处：唯一高分时自动采用，否则当候选丢给用户挑。
    """
    code = _code_package_segments(cls)
    # 门槛：代码包名至少 3 段，且首个包名段必须命中。
    # 少了这道闸，`com.example.NotInstalledActivity` 这种"两段式"代码包名
    # 会去匹配 `com.android.partnerbrowsercustomizations.example`（com…example
    # 按序命中 2 段）—— 那是**纯粹的噪音**，只会拼出错误的包名。
    if len(code) < 3 or not pkgs:
        return []
    scored: List[Tuple[int, int, str]] = []
    for p in pkgs:
        if not p.startswith(code[0] + "."):
            continue
        n = _subsequence_score(code, p)
        # 至少要命中「顶级域 + 组织域 + 一个业务段」，否则纯属噪音
        if n >= 3:
            scored.append((n, len(p.split(".")), p))
    if not scored:
        return []
    # 命中段数优先，其次段数少的（更贴合）
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [p for _, _, p in scored[:limit]]


def guess_package_for_class(cls: str, pkgs: Optional[Set[str]]) -> str:
    """唯一高分时返回那个包名；并列或高分不够 → 空串（宁可让用户选，也不猜错）。

    门槛：命中段数 >= 3。`com.jxw.aizx.teacher` 对
    `com.jxw.teacher.ui.activity.LoginActivity` 命中 3 段（com/jxw/teacher）算够；
    只命中 `com.jxw` 两段的（`com.jxw.aikyls` 之类）不算。
    """
    cands = guess_packages_for_class(cls, pkgs, limit=3)
    if not cands:
        return ""
    code = _code_package_segments(cls)
    best, runner = cands[0], (cands[1] if len(cands) > 1 else None)
    if _subsequence_score(code, best) < 3:
        return ""
    if runner and _subsequence_score(code, runner) == _subsequence_score(code, best):
        return ""                      # 并列最高分 → 不猜
    return best


def suggest_packages_for_class(cls: str, device: str = "", limit: int = 6) -> List[str]:
    """给界面用的候选包名（拿不到设备就返回空列表，不编）。"""
    return guess_packages_for_class(cls, installed_packages(device), limit=limit)


def split_pkg_class_ex(target: str, pkg_hint: str = "",
                       device: str = "") -> Tuple[str, str, bool]:
    """把「没有斜杠的 target」拆成 (包名, 全限定类名或 "", 是否启发式猜的)。

    拆解顺序 —— **先确定、后启发**：
      ⓪ `pkg_hint` 够不够格被直接信任：设备信息拿不到，或它确实是已装包
      ① hint 命中（`t == hint` 或 `t` 以 `hint + "."` 开头）→ 直接用
      ② 设备已装包名集合做**最长前缀匹配**（确定，不猜）
      ③ 都不行 → **反向反查**：哪个已装包的段覆盖了类名的代码包名段
      ④ 反查无果但调用方给了包名 → 信它（拼出来的形式是对的）
      ⑤ 都没有 → ("", "")，交给上层报错，而不是瞎拼一条跑不通的命令

    ③ 存在的理由是一个真实案例：`com.jxw.aizx.teacher` 的组件类名都在
    `com.jxw.teacher.ui.activity.*` 下 —— 前缀匹配对这类应用**完全无解**，
    而 `pkg/全限定类名` 本身是 `am` 的合法写法，拼出来能跑。

    第三个返回值只有走 ③ 的反查才是 True。界面拿它决定要不要提示
    "这个包名是推断的，不对就改"。
    """
    t = (target or "").strip()
    if not t:
        return "", "", False
    segs = t.split(".")
    if len(segs) <= 1:
        return "", "", False
    pkgs = installed_packages(device)

    # `hint_ok` 是这道闸的核心。设备上**查不到**的 hint 往往是"代码包名"
    # 而不是 applicationId —— 用户照着类名填「所属包名 = com.jxw.teacher」时
    # 若直接采信，就会拼出 `com.jxw.teacher/...` 这条必死命令；设备上真正
    # 能跑的是 `com.jxw.aizx.teacher/com.jxw.teacher.ui.activity.LoginActivity`。
    hint_ok = bool(pkg_hint) and (pkgs is None or pkg_hint in pkgs)

    if hint_ok:
        if t == pkg_hint:
            return pkg_hint, "", False
        if t.startswith(pkg_hint + "."):
            return pkg_hint, t, False

    if pkgs:
        if t in pkgs:
            return t, "", False
        for i in range(len(segs) - 1, 0, -1):
            cand = ".".join(segs[:i])
            if cand in pkgs:
                return cand, t, False
        # hint 是设备上真实存在的包 → 它比任何启发式都可靠，直接采信。
        # （真机实例：`com.jxw.aizx.teacher` + `com.jxw.teacher.ui.activity.LoginActivity`
        #   必须拼成 `com.jxw.aizx.teacher/com.jxw.teacher.ui.activity.LoginActivity`。）
        if hint_ok and "." in t:
            return pkg_hint, t, False
        guess = guess_package_for_class(t, pkgs)
        if guess:
            return guess, t, True
        # 兜底：设备上查不到 hint，但调用方明确给了 → 用它的（形式是对的，
        # 真跑不通时 am 会报 unable to resolve，配 _AM_HINTS 的中文解释）。
        if pkg_hint and t == pkg_hint:
            return pkg_hint, "", False
        if pkg_hint and "." in t:
            return pkg_hint, t, False
        return "", "", False

    # 设备信息拿不到（adb 不可用）：只能信调用方给的包名，至少预览是对的
    if pkg_hint and t == pkg_hint:
        return pkg_hint, "", False
    if pkg_hint and "." in t:
        return pkg_hint, t, False
    return "", "", False


def split_pkg_class(target: str, pkg_hint: str = "", device: str = "") -> Tuple[str, str]:
    """兼容壳：只要 (包名, 类名)。细节见 `split_pkg_class_ex`。"""
    pkg, cls, _ = split_pkg_class_ex(target, pkg_hint, device)
    return pkg, cls

# ---------------------------------------------------------------- Flag 位值

FLAGS = {
    "GRANT_READ_URI_PERMISSION": 0x00000001,
    "GRANT_WRITE_URI_PERMISSION": 0x00000002,
    "FROM_BACKGROUND": 0x00000004,
    "DEBUG_LOG_RESOLUTION": 0x00000008,
    "EXCLUDE_STOPPED_PACKAGES": 0x00000010,
    "INCLUDE_STOPPED_PACKAGES": 0x00000020,
    "ACTIVITY_NO_HISTORY": 0x40000000,
    "ACTIVITY_SINGLE_TOP": 0x20000000,
    "ACTIVITY_NEW_TASK": 0x10000000,
    "ACTIVITY_MULTIPLE_TASK": 0x08000000,
    "ACTIVITY_CLEAR_TOP": 0x04000000,
    "ACTIVITY_FORWARD_RESULT": 0x02000000,
    "ACTIVITY_PREVIOUS_IS_TOP": 0x01000000,
    "ACTIVITY_EXCLUDE_FROM_RECENTS": 0x00800000,
    "ACTIVITY_BROUGHT_TO_FRONT": 0x00400000,
    "ACTIVITY_RESET_TASK_IF_NEEDED": 0x00200000,
    "ACTIVITY_LAUNCHED_FROM_HISTORY": 0x00100000,
    "ACTIVITY_NEW_DOCUMENT": 0x00080000,
    "ACTIVITY_NO_USER_ACTION": 0x00040000,
    "ACTIVITY_REORDER_TO_FRONT": 0x00020000,
    "ACTIVITY_NO_ANIMATION": 0x00010000,
    "ACTIVITY_CLEAR_TASK": 0x00008000,
    "ACTIVITY_TASK_ON_HOME": 0x00004000,
    "ACTIVITY_RETAIN_IN_RECENTS": 0x00002000,
    "ACTIVITY_LAUNCH_ADJACENT": 0x00001000,
    "ACTIVITY_MATCH_EXTERNAL": 0x00000800,
    "ACTIVITY_REQUIRE_NON_BROWSER": 0x00000400,
}

# 界面上优先展示的"常用 Flag"（其余放"更多"里）
COMMON_FLAGS = [
    "ACTIVITY_NEW_TASK",
    "ACTIVITY_CLEAR_TOP",
    "ACTIVITY_SINGLE_TOP",
    "ACTIVITY_CLEAR_TASK",
    "ACTIVITY_NO_ANIMATION",
    "GRANT_READ_URI_PERMISSION",
    "GRANT_WRITE_URI_PERMISSION",
    "INCLUDE_STOPPED_PACKAGES",
]

# 常用 Action（给下拉框）
COMMON_ACTIONS = [
    "android.intent.action.MAIN",
    "android.intent.action.VIEW",
    "android.intent.action.SEND",
    "android.intent.action.SENDTO",
    "android.intent.action.EDIT",
    "android.intent.action.DIAL",
    "android.intent.action.CALL",
    "android.intent.action.WEB_SEARCH",
    "android.intent.action.SEARCH",
    "android.intent.action.SYNC",
    "android.settings.SETTINGS",
    "android.settings.APPLICATION_DETAILS_SETTINGS",
]

EXTRAS_TYPES = ["string", "int", "bool", "float", "long", "uri", "string-array", "int-array", "null"]

# 类型 → am 参数前缀
_EXTRA_PREFIX = {
    "string": "--es",
    "int": "--ei",
    "bool": "--ez",
    "float": "--ef",
    "long": "--el",
    "uri": "--eu",
    "string-array": "--esa",
    "int-array": "--eia",
    "null": "--esn",
}


# ---------------------------------------------------------------- Extra

@dataclass
class Extra:
    """一个 Intent extra。`type` 决定用哪个 `--e*` 参数。"""

    key: str = ""
    value: str = ""
    type: str = "string"

    def to_args(self) -> List[str]:
        prefix = _EXTRA_PREFIX.get(self.type, "--es")
        if self.type == "null":
            return [prefix, self.key]
        if self.type == "bool":
            v = str(self.value).strip().lower()
            return [prefix, self.key, "true" if v in ("1", "true", "yes", "on") else "false"]
        if self.type == "string-array":
            return [prefix, self.key, ",".join(self.value.split(","))]
        if self.type == "int-array":
            return [prefix, self.key, ",".join(x.strip() for x in self.value.split(","))]
        return [prefix, self.key, str(self.value)]

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "type": self.type}

    @classmethod
    def from_dict(cls, d: dict) -> "Extra":
        return cls(key=d.get("key", ""), value=d.get("value", ""),
                   type=d.get("type", "string"))


# ---------------------------------------------------------------- Intent

@dataclass
class Intent:
    """一次组件调用（也用于广播）。"""

    kind: str = "activity"          # activity | service | broadcast
    target: str = ""                # pkg/Component 或 pkg/.Cls
    action: str = ""
    data: str = ""
    mime_type: str = ""
    categories: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    extras: List[Extra] = field(default_factory=list)
    user: str = ""
    wait: bool = False
    pkg: str = ""                   # 仅广播时用于 -p 限定包
    # 解析不出来时，设备上"可能属于这个类名"的包名候选（给用户点，不自动用）
    resolve_candidates: List[str] = field(default_factory=list)
    # 包名是从候选里**推断**出来的（不是用户/调用方明确给的），界面要提示
    pkg_inferred: bool = False

    # -- 组件规范化 --------------------------------------------------

    @property
    def normalized_target(self) -> str:
        """把各种写法统一成 `pkg/Class`。**纯字符串运算，不碰设备。**

        `pkg/.ui.MainActivity`  → 原样
        `pkg/com.pkg.ui.MainActivity` → `pkg/.ui.MainActivity`
        `com.pkg.ui.MainActivity`（裸全限定类名，且已知包名）→ `com.pkg/.ui.MainActivity`
        只有包名 / 裸类名且包名未知 → **空串**（说明还得过一遍 `resolve()`）
        """
        t = (self.target or "").strip()
        if not t:
            return ""
        if "/" in t:
            pkg, comp = t.split("/", 1)
            return as_component(pkg, comp)
        if self.pkg and t.startswith(self.pkg + "."):
            return as_component(self.pkg, t)
        return ""

    @property
    def needs_resolve(self) -> bool:
        """target 填了东西、但光靠字符串定不下来组件（裸类名 / 纯包名）。"""
        t = (self.target or "").strip()
        return bool(t) and not self.normalized_target

    def pkg_suspect(self, device: str = "") -> bool:
        """`self.pkg` 不像设备上的真实包名 → 它是"代码包名"，字符串规则会骗人。

        为什么需要：`normalized_target` 里有一条**纯字符串**规则 ——
        「类名以 pkg 开头就当它属于该包」。真机实例：

            pkg    = com.jxw.teacher            ← 代码包名（用户照着类名填的）
            target = com.jxw.teacher.ui.activity.LoginActivity
            → normalized_target = com.jxw.teacher/.ui.activity.LoginActivity

        字符串看着毫无破绽，但设备上根本没有 `com.jxw.teacher` 这个包
        （真包名是 `com.jxw.aizx.teacher`），这条命令**必然失败**。

        所以只要设备信息能拿到、且 pkg 不在里面，就必须推翻字符串结论、
        老老实实走 `split_pkg_class_ex` 重新拆（那里会反查出真包名）。
        """
        if not self.pkg or self.needs_resolve:
            return False                    # 没给 pkg，或本来就要 resolve
        t = (self.target or "").strip()
        if "/" in t:
            # target 自己带了包名（用户显式写的 pkg/Class），那是他的明确意图，
            # 不质疑。**这条保护不能省** —— 否则带斜杠的 target 会被丢进
            # split_pkg_class_ex，拼出 `pkg/pkg/Class` 这种垃圾。
            return False
        pkgs = installed_packages(device)
        if not pkgs:
            return False                    # 拿不到设备 → 不质疑，保持旧行为
        return self.pkg not in pkgs

    def resolve(self, device: str = "") -> "Intent":
        """就地补全 target：裸类名 → `pkg/.Class`；纯包名 → 设备上的 launcher。

        返回 self，便于链式调用。**预览和执行都必须先过这一步**，
        否则同一条输入在"命令预览"里是空的、执行时也是空的。
        """
        t = (self.target or "").strip()
        if not t:
            return self
        # 两条进这里的路：① 字符串上定不下来（裸类名/纯包名）
        #                 ② 定下来了，但给的包名在设备上不存在（代码包名）
        if not self.needs_resolve and not self.pkg_suspect(device):
            return self
        pkg, cls, guessed = split_pkg_class_ex(t, self.pkg, device)
        if not pkg:
            # 拆不出来的时候别只说一句"请写成 包名/.类名" —— 用户手里那串
            # **本身就是合法的完整类名**，问题出在"设备上没有这个包名"。
            # 把设备上可能的包名捞出来，validate() 才能给出可操作的下一步。
            self.resolve_candidates = suggest_packages_for_class(t, device)
            return self
        # 包名是反查猜出来的（或替换掉了调用方给的）→ 留痕，界面提示"这是推断的"
        self.pkg_inferred = guessed or (bool(self.pkg) and pkg != self.pkg)
        self.pkg = self.pkg or pkg
        if cls:
            self.target = as_component(pkg, cls)
        elif self.kind == "activity":
            # 只有「启动 Activity」才存在"这个包的首页"这个概念；
            # service / broadcast 只给包名是无解的，交给 validate() 报错，
            # 别擅自替换成 launcher Activity（那会启错东西）。
            comp = launcher_component(pkg, device)
            if comp:
                self.target = comp
        return self

    def validate(self) -> str:
        """返回一句中文错误说明；没问题返回空串。

        存在的意义：以前 target 解析不出来时 `to_am_args()` 会老老实实
        返回空列表，于是真的就执行了 `adb shell am start`（什么都不带），
        am 回一句 "Error: no intent specified" 然后被当成"启动成功"。
        """
        component = self.normalized_target
        if self.kind == "broadcast":
            if not self.action and not component:
                return "广播至少要给 Action（-a）或接收器组件（pkg/.Receiver）"
            return ""
        if self.kind == "service":
            if not component:
                return ("服务必须写成 包名/类名，例如 com.foo/.svc.SyncService；"
                        "只给包名无法确定启动哪个服务")
            return ""
        if component:
            return ""
        if self.action or self.data or self.mime_type:
            return ""                      # 隐式 Intent（URI 方式），合法
        t = (self.target or "").strip()
        if t:
            cands = [c for c in (self.resolve_candidates or []) if c]
            if cands:
                # 用户手里那串**本身就是合法的完整类名**，卡点在于
                # "安装包名 ≠ 代码包名"（com.jxw.aizx.teacher 的类全在
                # com.jxw.teacher.* 下）。这时给出设备上的真实包名才可操作，
                # 一味说"请写成 包名/.类名"等于让用户去猜。
                return ("没能确定「%s」属于哪个包 —— 设备上没有以它开头的应用"
                        "（安装包名和代码包名经常不一样）。\n"
                        "设备上最相关的是：%s\n"
                        "→ 把「所属包名」填成 %s，或直接写成 %s/%s"
                        % (t, "、".join(cands), cands[0], cands[0], t))
            return ("没能把「%s」解析成组件。请写成 包名/.类名（如 "
                    "com.jxw.xwsf/.ui.WifiListActivity），"
                    "或者只填 Action/Data 走 URI 方式"
                    "（-a android.intent.action.VIEW -d https://example.com）" % t)
        return ("至少要给组件（pkg/.Class）或 Action/Data（URI 方式）；例如 "
                "launch start com.foo/.ui.MainActivity，或 "
                "launch start -a android.intent.action.VIEW -d https://example.com")

    @property
    def package(self) -> str:
        t = (self.target or "").strip()
        return t.split("/", 1)[0] if "/" in t else t

    @property
    def component_class(self) -> str:
        t = self.normalized_target
        if not t or "/" not in t:
            return ""
        comp = t.split("/", 1)[1]
        if comp.startswith("."):
            return t.split("/", 1)[0] + comp
        return comp

    # -- 命令行 ------------------------------------------------------

    @property
    def am_command(self) -> str:
        return {"activity": "start", "service": "startservice", "broadcast": "broadcast"}[self.kind]

    def flag_value(self) -> Optional[int]:
        """把 Flag 名单合并成一个位值。"""
        if not self.flags:
            return None
        total = 0
        for name in self.flags:
            key = name.upper().replace("FLAG_", "")
            if key in FLAGS:
                total |= FLAGS[key]
            else:
                try:
                    total |= int(name, 0)
                except ValueError:
                    continue
        return total

    def to_am_args(self) -> List[str]:
        """传给 `am` 的参数列表（不含 `am` 自身）。

        `adb shell am start <这些参数>`
        """
        args: List[str] = []
        if self.wait and self.kind == "activity":
            args.append("-W")

        target = self.normalized_target
        if target:
            args += ["-n", target]

        if self.action:
            args += ["-a", self.action]
        if self.data:
            args += ["-d", self.data]
        if self.mime_type:
            args += ["-t", self.mime_type]
        for c in self.categories:
            if c:
                args += ["-c", c]
        fv = self.flag_value()
        if fv is not None:
            args += ["-f", "0x%08X" % fv]

        if self.kind == "broadcast" and self.pkg:
            args += ["-p", self.pkg]

        for e in self.extras:
            if e.key:
                args += e.to_args()

        if self.user:
            args += ["--user", str(self.user)]

        return args

    def to_am_command(self) -> List[str]:
        """完整的 `am start ...` 命令（不含 adb）。"""
        return ["am", self.am_command] + self.to_am_args()

    def to_display(self) -> str:
        """人类可读的一行命令（多行续行，便于贴进脚本）。"""
        from ...platform.shell import quote_for_display

        parts = ["am", self.am_command] + [quote_for_display(a) for a in self.to_am_args()]
        if len(parts) <= 6:
            return " ".join(parts)
        out = " ".join(parts[:6]).replace(" -", " \\\n  -")
        # 简化：按参数对折行
        chunks, i = [], 0
        while i < len(parts):
            if parts[i].startswith("-") and not parts[i].startswith("--e") and i + 1 < len(parts):
                chunks.append("%s %s" % (parts[i], parts[i + 1]))
                i += 2
            elif parts[i].startswith("--e") and i + 2 < len(parts):
                chunks.append("%s %s %s" % (parts[i], parts[i + 1], parts[i + 2]))
                i += 3
            else:
                chunks.append(parts[i])
                i += 1
        return " \\\n  ".join(chunks)

    # -- 构造 / 序列化 -----------------------------------------------

    @classmethod
    def from_cli(cls, ns) -> "Intent":
        intent = cls()
        intent.target = getattr(ns, "component", "") or getattr(ns, "target", "") or ""
        intent.action = getattr(ns, "action", "") or ""
        intent.data = getattr(ns, "data", "") or ""
        intent.categories = [c for c in (getattr(ns, "cat", None) or []) if c]
        intent.flags = [f for f in (getattr(ns, "flag", None) or []) if f]
        intent.wait = bool(getattr(ns, "wait", False))
        intent.user = getattr(ns, "user", "") or ""
        intent.pkg = getattr(ns, "package", "") or ""
        for kind, attr in (("string", "es"), ("int", "ei"), ("bool", "ez"),
                           ("float", "ef"), ("long", "el"), ("uri", "eu")):
            for pair in (getattr(ns, attr, None) or []):
                if len(pair) == 2:
                    intent.extras.append(Extra(key=pair[0], value=pair[1], type=kind))
        intent.kind = "broadcast" if getattr(ns, "cmd", "") == "broadcast" else "activity"
        return intent

    @classmethod
    def from_dict(cls, d: dict) -> "Intent":
        it = cls(
            kind=d.get("kind", "activity"),
            target=d.get("target", "") or d.get("component", ""),
            action=d.get("action", ""),
            data=d.get("data", ""),
            mime_type=d.get("mimeType", ""),
            categories=list(d.get("categories") or []),
            flags=list(d.get("flags") or []),
            user=str(d.get("user") or ""),
            wait=bool(d.get("wait")),
            pkg=d.get("package", "") or d.get("pkg", ""),
        )
        it.extras = [Extra.from_dict(x) for x in (d.get("extras") or [])]
        return it

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "normalizedTarget": self.normalized_target,
            "action": self.action,
            "data": self.data,
            "mimeType": self.mime_type,
            "categories": self.categories,
            "flags": self.flags,
            "flagValue": ("0x%08X" % self.flag_value()) if self.flag_value() is not None else "",
            "extras": [e.to_dict() for e in self.extras],
            "user": self.user,
            "wait": self.wait,
            "package": self.pkg,
            # 解析不出来时给用户挑的候选；界面把它渲染成可点的标签
            "resolveCandidates": list(self.resolve_candidates or []),
            # 包名是从候选里推断的（不是调用方给的）→ 界面提示一句"这是猜的"
            "pkgInferred": bool(self.pkg_inferred),
            "amArgs": self.to_am_args(),
            "amCommand": " ".join(self.to_am_command()),
            "display": self.to_display(),
        }


def flag_catalog() -> List[dict]:
    """给界面用的 Flag 目录。"""
    return [
        {"name": n, "value": "0x%08X" % v, "common": n in COMMON_FLAGS}
        for n, v in sorted(FLAGS.items(), key=lambda kv: -kv[1])
    ]


# ---------------------------------------------------------------- 兼容包装
#
# cli.py 里有 `from .core.launch.intent import Intent, to_am_args, to_display`
# 这样的历史调用（名字取自 Intent 方法）。保留薄包装以免 ImportError。

def to_am_args(intent: "Intent") -> List[str]:
    """`ada shell am start` 后面的参数列表。"""
    return intent.to_am_args()


def to_display(intent: "Intent") -> str:
    """人类可读的一行命令（多行续行）。"""
    return intent.to_display()
