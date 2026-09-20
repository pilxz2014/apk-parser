# -*- coding: utf-8 -*-
"""跨平台路径计算。

本模块与 `shell.py` / `finders.py` 同属平台层 —— 是**唯一允许出现
`sys.platform` 分叉**的地方，`core/` 一律通过这里拿路径。

三类目录：
  · 配置目录（用户级，跟随系统惯例）
  · 运行时产物目录（项目内，可被 config 覆盖）
  · 临时目录（可随时清空）
"""

import os
import sys
import tempfile
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

APP_NAME = "apk-parser"

# 右键菜单 / 打开方式用的平台脚本（.cmd / .sh）统一住在 scripts/ 下，
# 根目录只留 start/stop 这类「一键启停主程序」的入口。
SCRIPTS_DIRNAME = "scripts"


# ---------------------------------------------------------------- 项目根

def project_root() -> Path:
    """项目根目录（apkparser/platform/paths.py → 上溯三层）。

    打包后（PyInstaller frozen）没有源码树，产物/缓存/日志目录改为：
      Windows : exe 同级目录（便携式布局 —— 把程序文件夹放哪，数据就在哪）
      mac/Linux: 用户级数据目录（.app 包内不可写也不该写）
    """
    if getattr(sys, "frozen", False):
        if IS_WINDOWS:
            return Path(sys.executable).resolve().parent
        return _ensure(config_dir() / "data")
    return Path(__file__).resolve().parents[2]


def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- 配置目录

def config_dir() -> Path:
    """用户级配置目录。

    Windows : %APPDATA%\\apk-parser
    macOS   : ~/Library/Application Support/apk-parser
    Linux   : $XDG_CONFIG_HOME/apk-parser 或 ~/.config/apk-parser
    """
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_NAME


def config_file() -> Path:
    return config_dir() / "config.json"


def token_file() -> Path:
    """一次性 token（右键脚本推卡片用，权限 600）。"""
    return config_dir() / "token"


def server_file() -> Path:
    """主控台运行状态（pid / port），用于单实例检测。"""
    return config_dir() / "server.json"


# ---------------------------------------------------------------- 产物目录

def _cfg_override(key: str, default: Path) -> Path:
    """允许 config.json 覆盖产物目录；读配置失败则用默认。"""
    try:
        from ..config import get as _get  # 延迟导入，避免循环
        v = _get(key, "")
        if v:
            return Path(os.path.expandvars(os.path.expanduser(str(v))))
    except Exception:
        pass
    return default


def output_dir() -> Path:
    return _ensure(_cfg_override("output_dir", project_root() / "output"))


def captures_dir() -> Path:
    return _ensure(output_dir() / "captures")


def logs_dir() -> Path:
    return _ensure(_cfg_override("logs_dir", project_root() / "logs"))


def cache_dir() -> Path:
    return _ensure(project_root() / "cache")


def apk_cache_dir() -> Path:
    """从设备拉回的 base.apk（看签名 / 导出 APK 共用同一份）。"""
    return _ensure(cache_dir() / "apk")


def signature_cache_dir() -> Path:
    """签名解析结果缓存，键 = 包名 + versionCode。"""
    return _ensure(cache_dir() / "signature")


def parse_cache_dir() -> Path:
    """本机 APK 解析结果缓存，键 = 绝对路径 + mtime + size。"""
    return _ensure(cache_dir() / "parse")


def tmp_dir() -> Path:
    """临时文件（弹窗卡片 HTML 等，可随时清）。"""
    return _ensure(project_root() / "tmp")


def system_tmp_dir() -> Path:
    """交给外部程序（浏览器 / 终端）用的临时目录。

    项目内的 tmp/ 也够用，但某些沙箱环境不允许外部程序读项目目录，
    这里保留一个系统临时目录作为回退。
    """
    return Path(tempfile.gettempdir()) / APP_NAME


# ---------------------------------------------------------------- 查找

# 已知用户文件夹。
# Windows 必须走注册表 `Shell Folders` —— 因为这些目录**可能被重定向**
# （典型：把「下载」改到 D 盘，于是 C:\Users\X\Downloads 根本不存在）。
# 这里顺便把"重定向"这件事记下来，界面才有办法跟用户解释清楚
# "为什么你看到的路径是 D:\Backup\Downloads"。
_FOLDER_SPECS = (
    # key         中文名   注册表值名（Windows）
    ("desktop", "桌面", "Desktop"),
    ("documents", "文档", "Personal"),
    ("downloads", "下载", "{374DE290-123F-4565-9164-39C4925E467B}"),
    ("pictures", "图片", "My Pictures"),
)

_SHELL_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"


def _default_folder(key: str) -> Path:
    """该文件夹在"没被改过"时的默认位置。"""
    home = Path.home()
    return {
        "desktop": home / "Desktop",
        "documents": home / "Documents",
        "downloads": home / "Downloads",
        "pictures": home / "Pictures",
    }.get(key, home)


def known_folders() -> list:
    """常见"用户文件"目录，带**来源**与**是否被重定向**。

    :returns: `[{key, title, path, exists, source, defaultPath, redirected}]`

    - `source`: `registry`（Windows 注册表给出的当前真身）/ `default`（常规位置）
    - `redirected`: 当前真身与默认位置不同 —— 例如把「下载」搬到了 D 盘。
      面板上解释路径时用得上，也是"用户以为路径被改了"的真正原因。
    """
    out = []
    seen = set()

    def add(key, title, value, source):
        try:
            p = Path(os.path.expandvars(os.path.expanduser(str(value))))
        except Exception:
            return
        if not str(p):
            return
        try:
            exists = p.is_dir()
        except OSError:
            exists = False
        rec = {
            "key": key, "title": title, "path": str(p), "exists": exists,
            "source": source, "defaultPath": str(_default_folder(key)),
        }
        try:
            rec["redirected"] = os.path.normcase(str(p)) != os.path.normcase(rec["defaultPath"])
        except Exception:
            rec["redirected"] = False
        k = os.path.normcase(str(p))
        if k in seen:
            return
        seen.add(k)
        out.append(rec)

    reg_values = {}
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SHELL_FOLDERS) as k:
                for key, title, vname in _FOLDER_SPECS:
                    try:
                        reg_values[key] = winreg.QueryValueEx(k, vname)[0]
                    except OSError:
                        continue
        except Exception:
            pass

    for key, title, _vname in _FOLDER_SPECS:
        v = reg_values.get(key)
        if v:
            add(key, title, v, "registry")
        # 注册表没有、或注册表给的目录不存在 → 退回常规位置
        rec = out[-1] if out and out[-1]["key"] == key else None
        if rec is None or not rec["exists"]:
            add(key, title, _default_folder(key), "default")

    return out


def known_dirs() -> list:
    """常见"用户文件"目录（拖拽定位文件时的搜索起点，**顺序即优先级**）。

    只在盘上真实存在的目录才算 —— 不存在的旧路径搜了也是白搜。
    """
    out = []
    for f in known_folders():
        if not f["exists"]:
            continue
        p = Path(f["path"])
        if p not in out:
            out.append(p)

    # 项目目录与进程当前目录（用户常常把 APK 放在这两处）
    for p in (project_root(),):
        if p.is_dir() and p not in out:
            out.append(p)
    try:
        cwd = Path.cwd()
        if cwd.is_dir() and cwd not in out:
            out.append(cwd)
    except Exception:
        pass
    return out


def folder_of(path) -> dict:
    """某个路径落在哪个"已知文件夹"里 —— 顺带说明它有没有被重定向。

    用来回答用户那句"为什么拖进来的路径是 `D:\\Backup\\Downloads\\`？"
    """
    try:
        p = Path(os.path.abspath(str(path)))
        cand = p if p.is_dir() else p.parent
        cand_s = os.path.normcase(str(cand))
    except Exception:
        return {}

    best = None
    for f in known_folders():
        if not f["exists"]:
            continue
        try:
            root = os.path.normcase(os.path.abspath(f["path"]))
        except Exception:
            continue
        if cand_s == root or cand_s.startswith(root + os.sep):
            if best is None or len(root) > len(best[1]):
                best = (f, root)
    if best is None:
        return {}

    f = dict(best[0])
    f["isRoot"] = os.path.normcase(os.path.abspath(f["path"])) == cand_s
    if f.get("redirected"):
        f["note"] = ("这是系统的「%s」文件夹，但你把它重定向到了 %s（默认位置是 %s）——"
                     "所以这个路径就是它的**真实位置**，不是工具改写的。"
                     % (f["title"], f["path"], f["defaultPath"]))
    else:
        f["note"] = "这是系统的「%s」文件夹（%s）" % (f["title"], f["path"])
    return f


def find_sibling_script(name: str) -> Path:
    """scripts/ 下的平台脚本路径（右键菜单等外部入口都指到这里）。"""
    return project_root() / SCRIPTS_DIRNAME / name


def which_all(*names: str):
    """在 PATH 里找第一个存在的可执行文件。"""
    from shutil import which
    for n in names:
        p = which(n)
        if p:
            return Path(p)
    return None
