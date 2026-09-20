# -*- coding: utf-8 -*-
"""能力注册表 —— 可扩展性的核心。

每个能力是一条**声明**：它属于哪个功能组、要不要设备、风险多高、
对应哪个 CLI 子命令、对应哪个 API、前端用哪种渲染策略。

前端拿这张表自动生成导航与入口，所以**加功能通常不用改前端**：
只要新功能落在预置的 5 种渲染策略里，前端零改动。
只有出现全新交互形态（例如"时间轴回放"）才需要新增 renderer。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# 前端渲染策略（预置 5 种，覆盖绝大多数新增需求）
RENDERERS = {
    "table": "表格 + 过滤（列表类）",
    "form+command-preview": "表单 + 实时命令预览（参数编辑类）",
    "log-stream": "实时日志流（长任务类）",
    "gallery": "产物画廊（图片/视频类）",
    "wizard": "分步向导（复杂流程类）",
    "detail": "详情展示（只读信息类）",
}

RISK_LEVELS = {"low": "安全", "confirm": "需要确认", "dangerous": "危险（需输入包名确认）"}


@dataclass
class Capability:
    """一条能力声明。

    ## 合并（`hidden` / `tabOf` / `tabKey`）

    同一个功能被拆成七零八落的子页面，用户找东西要翻半天 —— 所以能力之间
    可以有**父子**关系：子能力不再单独出现在侧栏，而是变成父页面里的一个
    **tab**。三个字段各司其职：

      * `tab_of`  —— 父能力的 id；非空即"我已被合并进某个页面"
      * `tab_key` —— 我在父页面里那一格的键（前端 `q.tab` 用的就是它）
      * `alias`   —— 纯别名：我和父页**自己那一格**是同一个东西
                     （`local.popup` 的信息卡片就是 `local.parse` 本身）
      * `hidden`  —— 由 `tab_of` 推导，别手写（`__post_init__` 里兜）

    **能力 ID 全部保留**，因为 CLI 子命令 / API / 老书签都还指着它 ——
    前端拿到 `tab_of` 会把 `#/cap/local.sign` 重定向到
    `#/cap/local.parse?tab=sign`。合并的是**入口**，不是功能。
    """

    id: str
    group: str
    title: str
    description: str = ""
    needs_device: bool = False
    risk: str = "low"
    cli: str = ""
    api: str = ""
    renderer: str = "table"
    icon: str = ""
    tags: List[str] = field(default_factory=list)
    tab_of: str = ""
    tab_key: str = ""
    tab_label: str = ""
    alias: bool = False
    hidden: bool = False

    def __post_init__(self):
        # 只要声明了父页面，就必然不该出现在侧栏 —— 让人手写 hidden 一定会漏
        if self.tab_of:
            self.hidden = True

    def to_dict(self) -> dict:
        return {
            "id": self.id, "group": self.group, "title": self.title,
            "description": self.description, "needsDevice": self.needs_device,
            "risk": self.risk, "riskText": RISK_LEVELS.get(self.risk, self.risk),
            "cli": self.cli, "api": self.api, "renderer": self.renderer,
            "icon": self.icon, "tags": self.tags,
            "tabOf": self.tab_of, "tabKey": self.tab_key,
            "tabLabel": self.tab_label, "alias": self.alias, "hidden": self.hidden,
        }


@dataclass
class Group:
    id: str
    title: str
    domain: str          # local | device | system
    description: str = ""
    icon: str = ""
    order: int = 0

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "domain": self.domain,
                "description": self.description, "icon": self.icon, "order": self.order}


GROUPS = [
    Group("local", "本机 · APK", "local", "解析、分析本机 APK 文件，不需要设备", "📦", 10),
    Group("device.apps", "设备 · 应用", "device", "已装应用检索、详情、签名、快捷操作", "📱", 20),
    Group("device.launcher", "设备 · 启动器", "device", "启动任意 Activity / Service / 广播", "🚀", 30),
    Group("device.runtime", "设备 · 运行时", "device", "正在跑的界面、进程、堆栈快照", "🧭", 35),
    Group("device.capture", "设备 · 捕获", "device", "截图、录屏、实时镜像、日志", "📷", 40),
    Group("device.install", "设备 · 安装", "device", "安装前预检 + 安装执行 + 失败诊断", "⬇", 50),
    Group("device.files", "设备 · 文件", "device", "push / pull / 浏览设备文件", "📁", 60),
    Group("system", "系统", "system", "菜单注入、环境自检、配置、主控台", "⚙", 70),
]


CAPABILITIES: List[Capability] = [
    # ================================================================
    # 本机域 —— 解析 APK 是一个页面 + 4 个 tab
    #
    # 「信息卡片 / 签名解析 / 提取图标 / 体积构成」原本是 4 个并列入口，
    # 但它们**看的是同一个文件**：用户先在「解析 APK」里看到签名，再想单独
    # 看体积，得重新选一次文件。合并成一个页面后，选定文件一次、tab 里随便翻。
    # ================================================================
    Capability("local.parse", "local", "解析 APK", "概览/信息/签名/图标/体积，一次选文件随便翻",
               False, "low", "apk info", "GET /api/apk/parse", "detail", "🔍",
               tab_key="info", tab_label="信息"),
    Capability("local.popup", "local", "信息卡片", "卡片式展示，右键菜单用的就是这个",
               False, "low", "apk popup", "POST /api/popup/info", "detail", "🪟",
               tab_of="local.parse", tab_key="info", tab_label="信息", alias=True),
    Capability("local.sign", "local", "签名解析", "方案 v1/v2/v3、证书 DN、SHA-256、有效期",
               False, "low", "apk sign", "GET /api/apk/signature", "detail", "🔏",
               tab_of="local.parse", tab_key="sign", tab_label="签名"),
    Capability("local.icon", "local", "提取图标", "导出最高密度图标文件",
               False, "low", "apk icon", "POST /api/apk/icon", "detail", "🖼",
               tab_of="local.parse", tab_key="icon", tab_label="图标"),
    Capability("local.slim", "local", "体积构成", "各目录占比 + 可优化提示",
               False, "low", "apk slim", "GET /api/apk/analyze", "table", "📊",
               tab_of="local.parse", tab_key="slim", tab_label="体积"),
    # 这两个**不合并**：它们要么吃两个文件（对比），要么吃一整目录（批量），
    # 跟"一次看一个 APK"不是同一种动作，塞进 tab 反而别扭。
    Capability("local.diff", "local", "双包对比", "两个 APK 逐项比对；也可与设备已装应用比",
               False, "low", "apk diff", "POST /api/apk/diff", "table", "⚖"),
    Capability("local.scan", "local", "批量解析", "扫一个目录，导出 CSV",
               False, "low", "apk scan", "POST /api/apk/scan", "table", "🗂"),

    # ================================================================
    # 设备 · 应用 —— 设备列表 / 应用列表 是"找"；应用详情是"看这一个"
    # 详情页里原本并列着 详情/签名/签名比对/导出/停止/清除/卸载 7 个入口，
    # 它们**全部针对同一个已选包**，合并成 4 个 tab。
    # ================================================================
    Capability("device.devices", "device.apps", "设备列表", "多设备、无线连接、设备工具",
               True, "low", "device list", "GET /api/devices", "table", "🔌"),
    Capability("device.apps", "device.apps", "应用列表", "包名/应用名模糊检索",
               True, "low", "app list", "GET /api/apps", "table", "📋"),
    Capability("device.app.detail", "device.apps", "应用详情", "详情/签名/导出/操作，选定包后随便翻",
               True, "low", "app info", "GET /api/apps/<pkg>", "detail", "ℹ",
               tab_key="info", tab_label="详情"),
    Capability("device.app.sign", "device.apps", "应用签名（免 root）", "两级加载：快路径摘要 → 完整证书",
               True, "low", "app sign", "GET /api/apps/<pkg>/signature", "detail", "🔏",
               tab_of="device.app.detail", tab_key="sign", tab_label="签名"),
    Capability("device.app.signcmp", "device.apps", "签名比对", "已装应用 ⟷ 本机 APK",
               True, "low", "app sign-cmp", "POST /api/apk/signature/compare", "detail", "⚖",
               tab_of="device.app.detail", tab_key="cmp", tab_label="签名比对"),
    Capability("device.app.export", "device.apps", "导出已装 APK", "拉回设备上的包（与签名共用缓存）",
               True, "low", "app export", "POST /api/apps/<pkg>/export", "detail", "💾",
               tab_of="device.app.detail", tab_key="export", tab_label="导出"),
    # 三个危险动作共用「操作」这一格 —— tab 按 key 去重，第一个定义标签
    Capability("device.app.stop", "device.apps", "强制停止", "force-stop",
               True, "confirm", "app action stop", "POST /api/apps/<pkg>/action", "detail", "⏹",
               tab_of="device.app.detail", tab_key="actions", tab_label="操作"),
    Capability("device.app.clear", "device.apps", "清除数据", "pm clear（会丢数据）",
               True, "dangerous", "app action clear", "POST /api/apps/<pkg>/action", "detail", "🧹",
               tab_of="device.app.detail", tab_key="actions"),
    Capability("device.app.uninstall", "device.apps", "卸载", "pm uninstall",
               True, "dangerous", "app action uninstall", "POST /api/apps/<pkg>/action", "detail", "🗑",
               tab_of="device.app.detail", tab_key="actions"),

    # ================================================================
    # 设备 · 启动器 —— 启动 / 组件 / 广播 / 历史 / 我的入口 都是"围绕一条 Intent"
    # ================================================================
    Capability("device.launch.start", "device.launcher", "启动组件", "Intent 全参数编辑 + 命令预览 + 收藏常用入口",
               True, "low", "launch start", "POST /api/launch/start", "form+command-preview", "🚀",
               tab_key="start", tab_label="启动"),
    Capability("device.launch.list", "device.launcher", "组件列表", "Activity/Service/Receiver/Provider",
               True, "low", "launch list", "GET /api/launch/components", "table", "🧩",
               tab_of="device.launch.start", tab_key="comps", tab_label="组件列表"),
    Capability("device.launch.broadcast", "device.launcher", "发送广播", "am broadcast",
               True, "confirm", "launch broadcast", "POST /api/launch/broadcast", "form+command-preview", "📡",
               tab_of="device.launch.start", tab_key="broadcast", tab_label="广播"),
    Capability("device.launch.history", "device.launcher", "启动历史", "记录每次启动与结果",
               True, "low", "launch history", "GET /api/launch/history", "table", "🕘",
               tab_of="device.launch.start", tab_key="history", tab_label="历史"),
    Capability("device.launch.shortcut", "device.launcher", "我的入口", "保存常用启动为快捷入口",
               True, "low", "launch shortcut", "GET /api/launch/shortcut", "table", "⭐",
               tab_of="device.launch.start", tab_key="fav", tab_label="我的入口"),

    # ================================================================
    # 设备 · 运行时 —— 看的是"此刻设备上正在跑什么"
    # ================================================================
    Capability("device.stack", "device.runtime", "Activity 堆栈",
               "dumpsys activity activities 结构化：当前界面 / 任务栈 / 每个 Activity 的状态",
               True, "low", "device stack", "GET /api/device/stack", "detail", "🧭"),

    # ================================================================
    # 设备 · 捕获 —— 截图/录屏/镜像 都盯着屏幕，合成一格；日志与产物另放
    # ================================================================
    Capability("device.shot", "device.capture", "屏幕捕获", "截图 / 录屏 / 实时镜像",
               True, "low", "capture screenshot", "POST /api/capture/screenshot", "gallery", "📸",
               tab_key="shot", tab_label="截图"),
    Capability("device.record", "device.capture", "录屏", "screenrecord，可启停",
               True, "low", "capture record", "POST /api/capture/record", "log-stream", "🎬",
               tab_of="device.shot", tab_key="record", tab_label="录屏"),
    Capability("device.mirror", "device.capture", "实时镜像", "scrcpy（可选依赖）",
               True, "low", "capture mirror", "POST /api/capture/mirror", "log-stream", "🖥",
               tab_of="device.shot", tab_key="mirror", tab_label="实时镜像"),
    Capability("device.logcat", "device.capture", "日志", "按包过滤、崩溃自动捕获",
               True, "low", "capture logcat", "GET /api/capture/logcat", "log-stream", "📜"),
    Capability("device.artifacts", "device.capture", "产物管理", "截图/录屏归档与清理",
               False, "low", "capture artifacts", "GET /api/capture/artifacts", "gallery", "🗃"),

    # ================================================================
    # 设备 · 安装 —— 预检 → 安装 → 失败诊断 本来就是一条流水线
    # ================================================================
    Capability("device.install.one", "device.install", "安装", "预检 / 安装 / 失败诊断一条流水线",
               True, "confirm", "install one", "POST /api/install", "log-stream", "⬇",
               tab_key="install", tab_label="安装"),
    Capability("device.install.precheck", "device.install", "安装前预检", "minSdk/ABI/签名/版本升降",
               True, "low", "install one --check-only", "POST /api/install/precheck", "table", "🩺",
               tab_of="device.install.one", tab_key="precheck", tab_label="预检"),
    Capability("device.install.errors", "device.install", "失败诊断", "INSTALL_FAILED_* 中文词表",
               False, "low", "install errors", "GET /api/install/errors", "table", "🩹",
               tab_of="device.install.one", tab_key="errors", tab_label="失败诊断"),

    # ---------------- 设备 · 文件 ----------------
    Capability("device.files", "device.files", "文件浏览", "列目录 / push / pull",
               True, "confirm", "file ls", "GET /api/files/ls", "table", "📂"),

    # ---------------- 系统 ----------------
    Capability("system.menu", "system", "菜单注入", "右键菜单 / 打开方式（工具界面里执行）",
               False, "confirm", "sys menu install", "POST /api/menu/apply", "table", "🖱"),
    Capability("system.doctor", "system", "环境自检", "adb/aapt2/scrcpy/ffmpeg 探测",
               False, "low", "sys doctor", "GET /api/sys/doctor", "table", "🩺"),
    Capability("system.config", "system", "设置", "adb 路径、端口、产物目录",
               False, "low", "sys config", "GET /api/sys/config", "form+command-preview", "⚙"),
    Capability("system.scripts", "system", "脚本存档", "bat / sh 脚本分类存档、备注、点一下就跑",
               False, "confirm", "script list", "GET /api/scripts", "table", "📜"),
    Capability("system.server", "system", "主控台", "启停与状态",
               False, "low", "web status", "GET /api/health", "detail", "🌐"),
]


def tabs_of(cap_id: str) -> List[dict]:
    """一个页面的 tab 定义（父自己排第一，然后是宣告了 `tab_of` 的子能力）。

    两条去重规则，都是有意的：

      * `alias=True` 的子能力**不占格** —— 它和父页自己那一格是同一个东西，
        只是名字不同（老书签还指着它）
      * 同一个 `tab_key` 出现多次时只留第一个 —— 「停止/清除/卸载」三个
        能力共用 `actions` 这一格，标签取第一个的
    """
    me = None
    for c in CAPABILITIES:
        if c.id == cap_id:
            me = c
            break
    if me is None:
        return []
    out = [{"key": me.tab_key or "main", "label": me.tab_label or me.title,
            "cap": me.id, "own": True}]
    seen = {out[0]["key"]}
    for c in CAPABILITIES:
        if c.tab_of != cap_id or c.alias or c.tab_key in seen:
            continue
        seen.add(c.tab_key)
        out.append({"key": c.tab_key, "label": c.tab_label or c.title,
                    "cap": c.id, "own": False})
    return out


def groups() -> List[dict]:
    return [g.to_dict() for g in sorted(GROUPS, key=lambda g: g.order)]


def all_capabilities() -> List[dict]:
    return [c.to_dict() for c in CAPABILITIES]


def get(cap_id: str) -> Optional[dict]:
    for c in CAPABILITIES:
        if c.id == cap_id:
            return c.to_dict()
    return None


def by_group(group_id: str) -> List[dict]:
    return [c.to_dict() for c in CAPABILITIES if c.group == group_id]


def registry() -> dict:
    """整张表（前端启动时拉一次）。"""
    return {
        "version": 1,
        "renderers": RENDERERS,
        "riskLevels": RISK_LEVELS,
        "groups": groups(),
        "capabilities": all_capabilities(),
    }
