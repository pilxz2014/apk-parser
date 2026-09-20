# -*- coding: utf-8 -*-
"""Windows 右键菜单 / 打开方式注入（HKCU，全程不需要管理员）。

菜单结构（级联二级菜单）：
    APK 工具
      ├ 解析 APK 信息
      ├ 安装到设备
      ├ 提取图标
      ├ 复制包名
      ├ 与设备已装版本对比
      ├ ──────────
      └ 打开主控台

另加「打开方式候选」：`HKCU\\Software\\Classes\\.apk\\OpenWithProgids`
**不劫持默认程序** —— Windows 的 UserChoice 有保护，且替用户改默认是越权的。
"""

import subprocess
from pathlib import Path
from typing import Dict, List

try:
    import winreg
except ImportError:  # 非 Windows
    winreg = None

from . import paths

MENU_ROOT = r"Software\Classes\SystemFileAssociations\.apk\shell\APKParser"
# ExtendedSubCommandsKey 的取值 = **相对 HKEY_CLASSES_ROOT 的键路径**，
# 该键的 shell 子键里装着级联的各子项。这里指向菜单根自己 ——
# 官方文档："If you do not need to reuse this set of verbs, you can list
# the verbs under the parent. In this case, make sure the default value of
# the parent is empty."
SELF_EXT_KEY = r"SystemFileAssociations\.apk\shell\APKParser"
PROGID = "APKParser.apk"
PROGID_ROOT = r"Software\Classes\%s" % PROGID
OPENWITH_ROOT = r"Software\Classes\.apk\OpenWithProgids"

# ---- 两种菜单样式 ----------------------------------------------------------
# cascade（默认）：一个「APK 工具」级联菜单，走 ExtendedSubCommandsKey
# flat（保底）  ：各项直接平铺在 .apk 的 shell 容器下，加统一前缀
#
# 为什么要有 flat：级联菜单能否渲染**没法在无界面环境里自动验证**（Explorer
# 是悬停时才解析子菜单，程序化查不到）。顶级静态 verb 的显示则是必然的
# （本工具已在真实系统上实测到「APK 工具」这个顶级项）。所以给一个
# 100% 可见的保底选项，级联万一在某个系统上不灵，一条命令就能切过去。
STYLE_CASCADE = "cascade"
STYLE_FLAT = "flat"

FLAT_ROOT = r"Software\Classes\SystemFileAssociations\.apk\shell"
# 子键统一加前缀，方便**精确**清理 —— FLAT_ROOT 是共享容器，
# 里面还有别的软件（Bandizip/WPS 等）写的 verb，绝不能整棵删。
FLAT_KEY_PREFIX = "APKTool."
FLAT_PREFIX = "APK 工具 · "

# 打开主控台这一项（两种样式共用）
CONSOLE_ITEM = ("91console", "打开主控台", "apk-console.cmd")


def all_items():
    """级联/平铺共用的完整条目清单。"""
    return list(ITEMS) + [CONSOLE_ITEM]

# (子键名, 显示名, 脚本文件名)
ITEMS = [
    ("01info", "解析 APK 信息", "apk-info.cmd"),
    ("02install", "安装到设备", "apk-install.cmd"),
    ("03icon", "提取图标", "apk-icon.cmd"),
    ("04copyname", "复制包名", "apk-copyname.cmd"),
    ("05cmp", "与设备已装版本对比", "apk-compare.cmd"),
]
MENU_LABEL = "APK 工具"


def available() -> bool:
    return winreg is not None and paths.IS_WINDOWS


def _set(root: str, sub: str, name: str, value: str, regtype=None) -> None:
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "%s\\%s" % (root, sub) if sub else root,
                             0, winreg.KEY_WRITE)
    try:
        winreg.SetValueEx(key, name, 0, regtype or winreg.REG_SZ, value)
    finally:
        winreg.CloseKey(key)


def _key_exists(root: str) -> bool:
    """只探测键在不在，不做任何修改。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root):
            return True
    except OSError:
        return False


def _del_value(root: str, sub: str, name: str) -> bool:
    """删掉一个值（不存在就当成功）。用于清除历史版本留下的错误键值。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            "%s\\%s" % (root, sub) if sub else root,
                            0, winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, name)
            return True
    except OSError:
        return False


def _del_tree(root: str) -> bool:
    """删除一棵注册表树，返回是否有东西被删掉。"""
    try:
        # access 传 0 = 跟随进程默认视图，必须和 CreateKeyEx（也没传 WOW64 标志）
        # 保持一致；否则 32 位 Python 下写进了 WOW6432Node，这里却去删 64 位视图，
        # 结果"删了个寂寞"。
        winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, root, 0, 0)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        pass

    # 非空 → 递归删子键
    deleted = False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, root, 0,
                             winreg.KEY_READ | winreg.KEY_WRITE)
    except FileNotFoundError:
        return False
    try:
        subs = []
        i = 0
        while True:
            try:
                subs.append(winreg.EnumKey(key, i))
                i += 1
            except OSError:
                break
        for s in subs:
            if _del_tree("%s\\%s" % (root, s)):
                deleted = True
    finally:
        winreg.CloseKey(key)
    try:
        winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, root, 0, 0)
        deleted = True
    except OSError:
        pass
    return deleted


def _script(name: str) -> Path:
    # 平台脚本统一住在 scripts/ 下（根目录只留 start/stop 这类主入口）
    return paths.project_root() / paths.SCRIPTS_DIRNAME / name


# ---------------------------------------------------------------- 安装

def install(project_root: Path = None, style: str = STYLE_CASCADE) -> Dict:
    """写入右键菜单 + 打开方式候选。返回操作明细（可审计）。

    style = "cascade"（默认，一个「APK 工具」级联菜单）
          = "flat"  （保底，各项平铺 + 「APK 工具 · 」前缀）
    """
    if not available():
        return {"ok": False, "error": "此平台不支持注册表菜单注入", "steps": []}

    style = (style or STYLE_CASCADE).strip().lower()
    if style not in (STYLE_CASCADE, STYLE_FLAT):
        return {"ok": False, "steps": [],
                "error": "未知的菜单样式：%r（可选 cascade / flat）" % style}

    root = Path(project_root) if project_root else paths.project_root()
    steps: List[str] = []
    missing: List[str] = []

    # 0) 先把**两种样式**的历史注册全清掉再重建。
    #    既保证幂等，也**必须**清 —— 旧版本留下的 SubCommands / 90sep / 平铺键
    #    如果不删，会和新机制打架（SubCommands 优先级更高，会让子菜单永远空着，
    #    正是这个 bug 反复复发的根源）。
    if _del_tree(MENU_ROOT):
        steps.append("清理旧级联菜单树：HKCU\\%s" % MENU_ROOT)
    n_flat = _purge_flat()
    if n_flat:
        steps.append("清理旧平铺菜单项：%d 个" % n_flat)

    if style == STYLE_FLAT:
        _install_flat(root, steps, missing)
    else:
        _install_cascade(root, steps, missing)

    # 打开方式候选（不设默认）
    _set(PROGID_ROOT + r"\shell\open\command", "", "", '"%s" "%%1"' % (root / paths.SCRIPTS_DIRNAME / "apk-install.cmd"))
    _set(PROGID_ROOT, "", "", "APK 安装到设备")
    # REG_NONE 只接受 bytes —— 传 str 同样抛 ValueError
    # 注意：CPython 的 winreg **根本不支持写 REG_NONE**（Py2Reg 无此分支，
    # 传什么都抛 ValueError）。OpenWithProgids 的值只要存在即可，Explorer
    # 不挑类型，所以用 REG_SZ 空串（这也是多数安装器的写法）。
    _set(OPENWITH_ROOT, "", PROGID, "")
    steps.append("注册打开方式候选：HKCU\\%s（未改默认程序）" % OPENWITH_ROOT)

    result = {"ok": not missing, "steps": steps, "missingScripts": missing,
              "style": style}
    if missing:
        result["warning"] = "以下脚本缺失，菜单项点了会没反应：%s" % "、".join(missing)
    return result


def _install_cascade(root: Path, steps: List[str], missing: List[str]) -> None:
    r"""级联样式：一个「APK 工具」菜单，子项在它的 shell 子键下。

    ⚠ 这里是最容易写错的地方，官方文档（Creating Static Cascading Menus）
      给了三条硬性要求：

      (a) 自定义子项**不能**靠 SubCommands 列名字。
          SubCommands 里的名字是在
              HKLM\Software\Microsoft\Windows\CurrentVersion\Explorer\CommandStore\shell
          下解析的（内置 Windows.xxx 谓词，或 HKLM 下注册的静态谓词）。
          把子项挂在 <菜单根>\shell\<名字> 下，它根本不去那儿找 ——
          症状就是"有级联箭头，子菜单却是空的"。
          自定义子项的正道是 ExtendedSubCommandsKey。

      (b) ExtendedSubCommandsKey 是一个**值**（不是子键），内容是一个
          相对 HKEY_CLASSES_ROOT 的键路径；该键的 shell 子键里装着子项。
          这里直接指向菜单根自己。
          （HKCU\Software\Classes 会并入 HKCR，所以不需要管理员权限。）

      (c) 级联菜单根的 (Default) 值必须**留空（未设置）**，设了会让级联解析失败。
    """
    _set(MENU_ROOT, "", "MUIVerb", MENU_LABEL)
    _set(MENU_ROOT, "", "ExtendedSubCommandsKey", SELF_EXT_KEY)
    _del_value(MENU_ROOT, "", "")             # 防御性：确保不残留历史的 (Default)
    _del_value(MENU_ROOT, "", "SubCommands")  # 防御性：确保旧的错误机制不残留
    steps.append("创建级联菜单根：HKCU\\%s" % MENU_ROOT)
    steps.append("  └ ExtendedSubCommandsKey = %s" % SELF_EXT_KEY)

    # Explorer 按**键名的字典序**枚举 shell 下的子键，所以用 01/02/… 定顺序
    for sub, label, script in ITEMS:
        sfile = root / paths.SCRIPTS_DIRNAME / script
        if not sfile.is_file():
            missing.append(script)
        subkey = "%s\\shell\\%s" % (MENU_ROOT, sub)
        _set(subkey, "", "MUIVerb", label)
        _set_icon(subkey)
        _set(subkey + "\\command", "", "", '"%s" "%%1"' % sfile)
        steps.append("  └ %s → %s" % (label, script))

    # 打开主控台 —— 分隔线用 CommandFlags=0x20（ECF_SEPARATORBEFORE）直接加在
    # 这一项上。官方说明该标志**只对顶层菜单项生效**，所以级联内部这条分隔线
    # 可能不显示，属于可接受的降级。
    # （之前用一个独立的 90sep 空标题项做分隔线，在级联里是无效且有害的写法。）
    sub, label, script = CONSOLE_ITEM
    console_key = "%s\\shell\\%s" % (MENU_ROOT, sub)
    _set(console_key, "", "MUIVerb", label)
    _set(console_key, "", "CommandFlags", 0x20, winreg.REG_DWORD)
    _set(console_key + "\\command", "", "", '"%s" "%%1"' % (root / paths.SCRIPTS_DIRNAME / script))
    steps.append("  └ %s（含分隔线标志）" % label)


def _install_flat(root: Path, steps: List[str], missing: List[str]) -> None:
    """平铺样式：各项直接挂在 .apk 的 shell 容器下，加统一前缀。

    不依赖任何级联解析机制，顶级静态 verb 的可见性是确定的。
    """
    for sub, label, script in all_items():
        sfile = root / paths.SCRIPTS_DIRNAME / script
        if not sfile.is_file():
            missing.append(script)
        key = "%s\\%s%s" % (FLAT_ROOT, FLAT_KEY_PREFIX, sub)
        _set(key, "", "MUIVerb", FLAT_PREFIX + label)
        _set_icon(key)
        _set(key + "\\command", "", "", '"%s" "%%1"' % sfile)
        steps.append("  └ %s%s → %s" % (FLAT_PREFIX, label, script))
    steps.append("平铺模式：HKCU\\%s（%s 前缀的子键）"
                 % (FLAT_ROOT, FLAT_KEY_PREFIX))


def _set_icon(key: str) -> None:
    """%SystemRoot% 要展开就必须用 REG_EXPAND_SZ，REG_SZ 会让它原样显示。"""
    _set(key, "", "Icon", r"%SystemRoot%\System32\shell32.dll,13",
         winreg.REG_EXPAND_SZ)


def _purge_flat() -> int:
    """删掉本工具写过的平铺子键，返回删掉几个。只动 APKTool. 前缀的键。"""
    n = 0
    for sub, _label, _script in all_items():
        if _del_tree("%s\\%s%s" % (FLAT_ROOT, FLAT_KEY_PREFIX, sub)):
            n += 1
    return n


# ---------------------------------------------------------------- 卸载

def uninstall() -> Dict:
    if not available():
        return {"ok": False, "error": "此平台不支持注册表菜单注入", "steps": []}

    steps: List[str] = []
    if _del_tree(MENU_ROOT):
        steps.append("已删除 HKCU\\%s（含级联子菜单）" % MENU_ROOT)
    n_flat = _purge_flat()
    if n_flat:
        steps.append("已删除平铺菜单项：%d 个（HKCU\\%s\\%s*）"
                     % (n_flat, FLAT_ROOT, FLAT_KEY_PREFIX))
    if _del_tree(PROGID_ROOT):
        steps.append("已删除 HKCU\\%s" % PROGID_ROOT)

    # OpenWithProgids 是共享键，只删自己写的那个值
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, OPENWITH_ROOT, 0, winreg.KEY_WRITE)
        try:
            winreg.DeleteValue(key, PROGID)
            steps.append("已移除打开方式候选值：%s" % PROGID)
        except FileNotFoundError:
            pass
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        pass

    return {"ok": True, "steps": steps or ["没有发现需要清理的注册表项"]}


# ---------------------------------------------------------------- 状态

def status() -> Dict:
    if not available():
        return {"supported": False, "installed": False, "items": []}

    items = []
    for sub, label, script in all_items():
        # 级联样式查 MENU_ROOT\shell\<sub>，平铺样式查 FLAT_ROOT\APKTool.<sub>
        candidates = [
            "%s\\shell\\%s\\command" % (MENU_ROOT, sub),
            "%s\\%s%s\\command" % (FLAT_ROOT, FLAT_KEY_PREFIX, sub),
        ]
        val = ""
        for key in candidates:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                    val = winreg.QueryValueEx(k, "")[0]
                break
            except OSError:
                continue
        items.append({"key": sub, "label": label, "registered": bool(val),
                      "command": val})

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MENU_ROOT):
            installed = True
    except FileNotFoundError:
        installed = False

    # 平铺样式：只要有一个 APKTool. 子键在，就算装了
    flat_count = 0
    for sub, _label, _script in all_items():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                "%s\\%s%s" % (FLAT_ROOT, FLAT_KEY_PREFIX, sub)):
                flat_count += 1
        except OSError:
            pass
    if not installed and flat_count:
        installed = True

    style = (STYLE_FLAT if (flat_count and not _key_exists(MENU_ROOT))
             else (STYLE_CASCADE if installed else ""))

    # 级联机制自检：必须"有 ExtendedSubCommandsKey、没有 (Default)、没有 SubCommands"
    ext_key = ""
    has_default = False
    has_subcommands = False
    if installed:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MENU_ROOT) as k:
                try:
                    ext_key = winreg.QueryValueEx(k, "ExtendedSubCommandsKey")[0]
                except OSError:
                    ext_key = ""
                for name in ("", "SubCommands"):
                    try:
                        winreg.QueryValueEx(k, name)
                        if name == "":
                            has_default = True
                        else:
                            has_subcommands = True
                    except OSError:
                        pass
        except OSError:
            pass

    # ExtendedSubCommandsKey 指向的键是否真的存在、且带 shell 子键
    ext_ok = False
    if ext_key:
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "%s\\shell" % ext_key) as k:
                ext_ok = winreg.QueryInfoKey(k)[0] > 0
        except OSError:
            ext_ok = False

    cascade_ok = bool(ext_key) and not has_default and not has_subcommands and ext_ok

    openwith = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, OPENWITH_ROOT) as k:
            winreg.QueryValueEx(k, PROGID)
            openwith = True
    except FileNotFoundError:
        pass

    return {
        "supported": True,
        "installed": installed,
        "style": style,                       # cascade / flat / ""
        "menuRoot": "HKCU\\%s" % (
            MENU_ROOT if style == STYLE_CASCADE else FLAT_ROOT),
        "openWithRegistered": openwith,
        "items": items,
        "flatCount": flat_count,
        "cascade": {
            "ok": cascade_ok,
            "extendedSubCommandsKey": ext_key,
            "extKeyResolved": ext_ok,
            "hasDefaultValue": has_default,        # 必须 False
            "hasSubCommands": has_subcommands,     # 必须 False
        },
    }
