# -*- coding: utf-8 -*-
"""外部工具探测：adb / aapt2 / scrcpy / ffmpeg / java。

探测顺序：**显式配置 → 环境变量 → PATH → 常见安装位置**。
结果带进程内缓存；`refresh=True` 可强制重探。
"""

import os
import re
from pathlib import Path
from typing import List, Optional

from . import paths

IS_WINDOWS = paths.IS_WINDOWS
IS_MAC = paths.IS_MAC

_EXE = ".exe" if IS_WINDOWS else ""

_cache: dict = {}


# ---------------------------------------------------------------- 候选目录

def _sdk_roots() -> List[Path]:
    """Android SDK 根目录候选。"""
    roots: List[Path] = []
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(var)
        if v:
            roots.append(Path(v))
    home = Path.home()
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        roots += [
            Path(local) / "Android" / "Sdk",
            Path("D:/SDK"), Path("C:/SDK"),
            Path("D:/Android/Sdk"), Path("C:/Android/Sdk"),
            Path("D:/Program Files/Android/Sdk"),
            Path("C:/Program Files (x86)/Android/android-sdk"),
        ]
    elif IS_MAC:
        roots += [home / "Library" / "Android" / "sdk", Path("/usr/local/share/android-sdk")]
    else:
        roots += [home / "Android" / "Sdk", home / "Android" / "sdk", Path("/opt/android-sdk")]
    # 去重保序
    seen, out = set(), []
    for r in roots:
        k = str(r).lower()
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _version_key(name: str) -> tuple:
    """把 '37.0.0' 这类版本名变成可排序元组。"""
    nums = re.findall(r"\d+", name)
    return tuple(int(n) for n in nums) if nums else (0,)


def _latest_in(parent: Path, pattern: str, exe: str) -> Optional[Path]:
    """在 parent 下按版本号最高的子目录里找 exe。"""
    if not parent.is_dir():
        return None
    try:
        cands = sorted([p for p in parent.iterdir() if p.is_dir()], key=lambda p: _version_key(p.name), reverse=True)
    except OSError:
        return None
    for c in cands:
        p = c / (exe + _EXE)
        if p.is_file():
            return p
        p2 = c / exe
        if p2.is_file():
            return p2
    # 再试直接在 parent 下
    p = parent / (exe + _EXE)
    return p if p.is_file() else None


# ---------------------------------------------------------------- 探测函数

def find_adb(refresh: bool = False) -> Optional[Path]:
    """找 adb。"""
    if not refresh and "adb" in _cache:
        return _cache["adb"]

    result: Optional[Path] = None

    # 1) 显式配置
    try:
        from ..config import get as cfg_get
        v = cfg_get("adb_path", "")
        if v and Path(v).is_file():
            result = Path(v)
    except Exception:
        pass

    # 2) PATH
    if result is None:
        result = paths.which_all("adb" + _EXE, "adb")

    # 3) SDK 常见位置
    if result is None:
        for root in _sdk_roots():
            p = root / "platform-tools" / ("adb" + _EXE)
            if p.is_file():
                result = p
                break

    _cache["adb"] = result
    return result


def find_aapt2(refresh: bool = False) -> Optional[Path]:
    """找 aapt2（build-tools 下，取版本最高的）。"""
    if not refresh and "aapt2" in _cache:
        return _cache["aapt2"]

    result: Optional[Path] = None
    try:
        from ..config import get as cfg_get
        v = cfg_get("aapt2_path", "")
        if v and Path(v).is_file():
            result = Path(v)
    except Exception:
        pass

    if result is None:
        result = paths.which_all("aapt2" + _EXE, "aapt2")

    if result is None:
        for root in _sdk_roots():
            p = _latest_in(root / "build-tools", "*", "aapt2")
            if p:
                result = p
                break

    _cache["aapt2"] = result
    return result


def find_aapt(refresh: bool = False) -> Optional[Path]:
    """找 aapt（老版 build-tools）。"""
    if not refresh and "aapt" in _cache:
        return _cache["aapt"]
    result = paths.which_all("aapt" + _EXE, "aapt")
    if result is None:
        for root in _sdk_roots():
            p = _latest_in(root / "build-tools", "*", "aapt")
            if p:
                result = p
                break
    _cache["aapt"] = result
    return result


def find_scrcpy(refresh: bool = False) -> Optional[Path]:
    """找 scrcpy（含 Windows 便携包目录）。"""
    if not refresh and "scrcpy" in _cache:
        return _cache["scrcpy"]

    result: Optional[Path] = None
    try:
        from ..config import get as cfg_get
        v = cfg_get("scrcpy_path", "")
        if v and Path(v).is_file():
            result = Path(v)
    except Exception:
        pass

    if result is None:
        result = paths.which_all("scrcpy" + _EXE, "scrcpy")

    if result is None and IS_WINDOWS:
        for parent in (Path("D:/Program Files/scrcpy"), Path("C:/Program Files/scrcpy"),
                       Path("D:/scrcpy"), Path("C:/scrcpy")):
            p = _latest_in(parent, "*", "scrcpy")
            if p:
                result = p
                break

    _cache["scrcpy"] = result
    return result


def find_ffmpeg(refresh: bool = False) -> Optional[Path]:
    if not refresh and "ffmpeg" in _cache:
        return _cache["ffmpeg"]
    result = paths.which_all("ffmpeg" + _EXE, "ffmpeg")
    _cache["ffmpeg"] = result
    return result


def find_java(refresh: bool = False) -> Optional[Path]:
    if not refresh and "java" in _cache:
        return _cache["java"]
    result = paths.which_all("java" + _EXE, "java")
    if result is None and os.environ.get("JAVA_HOME"):
        p = Path(os.environ["JAVA_HOME"]) / "bin" / ("java" + _EXE)
        if p.is_file():
            result = p
    _cache["java"] = result
    return result


def find_keytool(refresh: bool = False) -> Optional[Path]:
    """keytool（在 java 同一个 bin 目录）。"""
    if not refresh and "keytool" in _cache:
        return _cache["keytool"]
    result = paths.which_all("keytool" + _EXE, "keytool")
    if result is None:
        j = find_java()
        if j:
            p = j.parent / ("keytool" + _EXE)
            if p.is_file():
                result = p
    _cache["keytool"] = result
    return result


def find_apksigner(refresh: bool = False) -> Optional[Path]:
    """apksigner（build-tools 下；Windows 上是 .bat）。"""
    if not refresh and "apksigner" in _cache:
        return _cache["apksigner"]
    result = paths.which_all("apksigner" + _EXE, "apksigner")
    if result is None:
        for root in _sdk_roots():
            p = _latest_in(root / "build-tools", "*", "apksigner")
            if p:
                result = p
                break
    _cache["apksigner"] = result
    return result


def clear_cache() -> None:
    _cache.clear()


# ---------------------------------------------------------------- 汇总

def doctor() -> dict:
    """环境自检（`sys doctor` 用）。

    返回结构化结果，由调用方决定怎么渲染。
    """
    from . import shell

    def _ver(path: Optional[Path], args: List[str], timeout: float = 8.0) -> str:
        if not path:
            return ""
        r = shell.run([str(path)] + args, timeout=timeout)
        first = next((l.strip() for l in r.lines() if l.strip()), "")
        return first or (r.err.strip().splitlines()[0] if r.err.strip() else "")

    items = []

    adb = find_adb()
    items.append({
        "key": "adb",
        "name": "adb",
        "required": True,
        "path": str(adb) if adb else "",
        "version": _ver(adb, ["version"]) if adb else "",
        "hint": "设备域功能必需。装 Android SDK Platform-Tools 或设置 ANDROID_HOME。",
    })

    aapt2 = find_aapt2()
    items.append({
        "key": "aapt2",
        "name": "aapt2",
        "required": False,
        "path": str(aapt2) if aapt2 else "",
        "version": _ver(aapt2, ["version"]) if aapt2 else "",
        "hint": "可选。用于 APK 解析的交叉校验与异常包兜底。",
    })

    scrcpy = find_scrcpy()
    items.append({
        "key": "scrcpy",
        "name": "scrcpy",
        "required": False,
        "path": str(scrcpy) if scrcpy else "",
        "version": _ver(scrcpy, ["--version"]) if scrcpy else "",
        "hint": "可选。实时镜像 / 边镜像边录屏。",
    })

    ffmpeg = find_ffmpeg()
    items.append({
        "key": "ffmpeg",
        "name": "ffmpeg",
        "required": False,
        "path": str(ffmpeg) if ffmpeg else "",
        "version": _ver(ffmpeg, ["-version"]) if ffmpeg else "",
        "hint": "可选。录屏转 GIF。",
    })

    apksigner = find_apksigner()
    items.append({
        "key": "apksigner",
        "name": "apksigner",
        "required": False,
        "path": str(apksigner) if apksigner else "",
        "version": "",
        "hint": "可选。签名校验的权威结论（自家解析只给指纹，不做有效性断言）。",
    })

    keytool = find_keytool()
    items.append({
        "key": "keytool",
        "name": "keytool",
        "required": False,
        "path": str(keytool) if keytool else "",
        "version": "",
        "hint": "可选。签名解析的交叉校验。",
    })

    # 「浏览…」按钮的后端能力。
    #
    # 单列一项，是因为**它跟当前解释器没关系** —— 隔离 / 打包环境里的 Python
    # 经常被裁掉 tkinter（本机 managed 3.13 就是），此时 `sys.executable` 弹不出
    # 对话框，但系统里的另一个 Python 或者 PowerShell 可以。这一项能一眼看出
    # 到底能不能弹、走的是哪条路，免得排查时各处猜。
    try:
        from . import filedialog as fd_mod
        pick = fd_mod.backend_info()
    except Exception as e:
        pick = {"native": False, "backend": None, "detail": "探测失败：%s" % e}

    items.append({
        "key": "filepicker",
        "name": "原生文件对话框",
        "required": False,
        "path": str(pick.get("tkinterPython") or pick.get("powershell") or ""),
        "version": str(pick.get("backend") or ""),
        "hint": pick.get("detail") or "",
    })

    return {
        "python": {
            "version": "%d.%d.%d" % __import__("sys").version_info[:3],
            "executable": __import__("sys").executable,
            "platform": __import__("sys").platform,
        },
        "dirs": {
            "project": str(paths.project_root()),
            "config": str(paths.config_dir()),
            "output": str(paths.output_dir()),
            "cache": str(paths.cache_dir()),
        },
        "tools": items,
    }
