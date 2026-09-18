# -*- coding: utf-8 -*-
"""原生「选择文件」对话框 —— 本机工具才有的便利。

浏览器出于安全**永远**拿不到 `<input type=file>` 里文件的真实路径
（`File.path` 是 Electron 私货）。本工具的服务端就跑在本机，所以可以由
服务端弹一个原生对话框，把真实路径直接回给页面 —— 这样解析一个 APK
就不必先把它复制一份。

## 为什么不能直接用 sys.executable

**打包 / 隔离环境里的 Python 经常裁掉了 tkinter**（本机 workbuddy managed
3.13 就是：连 `Lib/tkinter`、`DLLs/_tkinter.pyd`、`tcl/` 都不存在）。
所以必须先探测，必要时换用系统 Python。

## 后端优先级（探测结果缓存，进程内有效）

1. **有 tkinter 的 Python 解释器**（跨平台首选）
   —— 候选顺序：自己 → 同目录 → 注册表登记的官方安装 → `py` launcher
   → 常见安装路径 → PATH。第一个能 `import tkinter` 的胜出。
2. **Windows: PowerShell + WinForms `OpenFileDialog`**
   —— 不依赖任何 Python 模块，是 Windows 上最兜底的原生途径。

两条都不可用 → `ok=False`，由上层降级（Web 端改用浏览器选择 + 上传）。

## 关于输出编码

对话框返回的路径**一律写临时文件**（UTF-8），不走子进程 stdout ——
stdout 编码随环境变（UTF-8 模式 / GBK），中文路径经管道必被搅。
"""

from __future__ import annotations

import base64
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import paths

__all__ = ["pick_file", "pick_dir", "find_gui_python", "backend_info", "reset_cache"]

# 探测一次要起子进程（百毫秒级），缓存起来 —— 主控台是长驻进程，只探一次。
_LOCK = threading.Lock()
_GUI_PY: Dict[str, object] = {"probed": False, "exe": None, "tried": []}
_PS: Dict[str, object] = {"probed": False, "exe": None}


def reset_cache() -> None:
    """清掉探测缓存（测试 / 用户装了新 Python 后想重探）。"""
    with _LOCK:
        _GUI_PY.update({"probed": False, "exe": None, "tried": []})
        _PS.update({"probed": False, "exe": None})


# ---------------------------------------------------------------- 候选解释器

def _is_store_stub(p: str) -> bool:
    """微软商店的 python.exe 是个转发器，运行会弹应用商店。

    它当然没有 tkinter，但探测它会把商店窗口弹到用户脸上 —— 直接跳过。
    """
    return "windowsapps" in p.replace("/", "\\").lower()


def _registry_installs() -> List[str]:
    """从注册表读官方安装器登记的 Python 安装路径（Windows）。

    `HKCU\\Software\\Python\\PythonCore\\<版本>\\InstallPath` 是官方安装器
    写的，比 glob 猜目录可靠得多（能覆盖装在 D 盘、自定义目录的情况）。
    """
    if not paths.IS_WINDOWS:
        return []
    out: List[str] = []
    try:
        import winreg
    except Exception:
        return []

    roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Python\PythonCore"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Python\PythonCore"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Python\PythonCore"),
    ]
    for hive, base in roots:
        try:
            with winreg.OpenKey(hive, base) as k:
                n = winreg.QueryInfoKey(k)[0]
                for i in range(n):
                    try:
                        ver = winreg.EnumKey(k, i)
                        with winreg.OpenKey(k, ver + r"\InstallPath") as ip:
                            v = winreg.QueryValueEx(ip, "")[0]
                        if v:
                            out.append(str(Path(v) / "python.exe"))
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def _candidates() -> List[str]:
    """按"可信度"排序的候选解释器列表。"""
    out: List[str] = []

    def add(p) -> None:
        if not p:
            return
        try:
            ap = os.path.abspath(str(p))
        except Exception:
            return
        if _is_store_stub(ap):
            return
        if Path(ap).is_file() and ap not in out:
            out.append(ap)

    # 1) 自己（如果它恰好带 tkinter，就是最省事的 —— 版本最匹配）
    add(sys.executable)
    try:
        d = Path(sys.executable).parent
        for n in ("python.exe", "python3.exe", "python"):
            add(d / n)
    except Exception:
        pass

    # 2) 官方安装器登记过的（最可靠）
    for p in _registry_installs():
        add(p)
        try:
            add(Path(p).with_name("pythonw.exe"))   # 探测时用 python.exe，实跑优先 pythonw
        except Exception:
            pass

    # 3) py launcher（`py -c` 会转发，探测成功能反查出真实解释器）
    for n in ("py.exe", "py"):
        add(shutil.which(n))
    try:
        local = os.environ.get("LOCALAPPDATA") or ""
        if local:
            add(Path(local) / "Programs" / "Python" / "Launcher" / "py.exe")
    except Exception:
        pass

    # 4) 常见安装位置（glob 兜底）
    pats = []
    for var in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(var)
        if base:
            pats.append(str(Path(base) / "Programs" / "Python" / "Python3*" / "python.exe"))
            pats.append(str(Path(base) / "Python3*" / "python.exe"))
    pats += [r"C:\Python3*\python.exe", r"D:\Python3*\python.exe",
             r"D:\Program Files\Python3*\python.exe"]
    for pat in pats:
        try:
            for hit in sorted(glob.glob(pat), reverse=True):   # 版本高的优先
                add(hit)
        except Exception:
            continue

    # 5) PATH（最后考虑：可能指向任何东西）
    for n in ("python3", "python"):
        add(shutil.which(n))

    return out


# ---------------------------------------------------------------- tkinter 后端

_TK_PROBE = (
    "import sys\n"
    "try:\n"
    "    import tkinter as _t\n"
    "except Exception as _e:\n"
    "    sys.stderr.write('NO_TK %r' % (_e,)); sys.exit(3)\n"
    "sys.stdout.write(sys.executable)\n"
)

_TK_SCRIPT = r'''
import json
import os
import sys

OUT = os.environ.get("APKPARSER_PICK_OUT", "")
if not OUT:
    # 环境变量没传到 = 上层有 bug。必须显式报错 —— 否则 emit() 静默失败，
    # 结果文件不存在，上层会把"没跑成"误读成"用户取消"。
    sys.stderr.write("__NO_OUT__ APKPARSER_PICK_OUT not set")
    sys.exit(5)


def emit(text):
    """结果写临时文件（UTF-8）。不用 stdout —— 编码随环境变，中文路径必被搅。"""
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(text or "")
    except Exception:
        pass


def die(tag, msg):
    emit("")
    sys.stderr.write(tag + str(msg))
    sys.exit(4)


try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as e:
    die("__NO_TK__", e)

try:
    types = json.loads(os.environ.get("APKPARSER_PICK_TYPES") or "[]")
    ftypes = [tuple(t) for t in types] or [("All files", "*.*")]
except Exception:
    ftypes = [("All files", "*.*")]

# mode=dir → 选目录；其余 → 选文件。同一个脚本两种模式，
# 免得"选目录"那条路自成一套、只有它自己会坏。
MODE = (os.environ.get("APKPARSER_PICK_MODE") or "file").strip().lower()

try:
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    init = os.environ.get("APKPARSER_PICK_DIR") or None
    if init and not os.path.isdir(init):
        init = None
    title = os.environ.get("APKPARSER_PICK_TITLE") or "Select"
    if MODE == "dir":
        # mustexist=True：导出/扫描都要一个真实存在的目录，
        # 允许现编一个不存在的路径只会在下一步才报错。
        path = filedialog.askdirectory(title=title, initialdir=init,
                                       mustexist=True)
        if path:
            path = os.path.abspath(path)
    else:
        path = filedialog.askopenfilename(
            title=title,
            initialdir=init,
            initialfile=(os.environ.get("APKPARSER_PICK_FILE") or None),
            filetypes=ftypes)
    root.destroy()
except Exception as e:
    die("__DIALOG_ERR__", e)

emit(path or "")
'''


def _run(cmd: Sequence[str], env: Dict[str, str], timeout: float,
         hide_window: bool = True) -> Tuple[int, str, str]:
    """跑一个子进程，返回 (rc, stdout, stderr)。

    这里**不**复用 `shell.run` —— 它面向"给用户看的命令"，会做命令预览、
    超时包装等额外动作；本模块只关心原始返回值，自己起进程更直白。
    """
    full_env = dict(os.environ)
    full_env.update({str(k): str(v) for k, v in env.items()})
    full_env["PYTHONDONTWRITEBYTECODE"] = "1"

    kwargs = {}
    if paths.IS_WINDOWS:
        # 隐藏我们自己拉起的控制台窗口；对话框是子进程另建的顶层窗口，不受影响。
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0 if hide_window else 1     # SW_HIDE / SW_SHOWNORMAL
        kwargs["startupinfo"] = si
        kwargs["creationflags"] = 0x08000000         # CREATE_NO_WINDOW

    try:
        # ⚠ env 必须显式传！否则子进程看不到 APKPARSER_PICK_* ——
        # 症状是对话框标题/过滤器退回脚本里的英文默认值，且**结果写不出来**，
        # 而写不出来又会被误判成"用户取消"，于是一个坏掉的流程返回 ok=True。
        r = subprocess.run(list(cmd), capture_output=True, timeout=timeout,
                           env=full_env, **kwargs)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 127, "", "%s: %s" % (type(e).__name__, e)
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    return int(r.returncode or 0), out, err


def find_gui_python(timeout: float = 15.0) -> Optional[str]:
    """找一个**带 tkinter** 的解释器；找不到返回 None。结果缓存。"""
    with _LOCK:
        if _GUI_PY["probed"]:
            return _GUI_PY["exe"]     # type: ignore[return-value]

        exe = None
        tried: List[str] = []
        for cand in _candidates():
            rc, out, err = _run([cand, "-c", _TK_PROBE], {}, timeout=timeout)
            if rc == 0 and out:
                exe = out            # 可能是 py.exe 转发后反查到的真实解释器
                tried.append("%s → ✔" % cand)
                break
            tried.append("%s → ✖" % cand)
            # 探测完的候选不值得再试第二次

        _GUI_PY.update({"probed": True, "exe": exe, "tried": tried})
        return exe


# ---------------------------------------------------------------- PowerShell 后端

# 注意：脚本内容保持纯 ASCII，中文（标题 / 过滤器）一律经环境变量传入 ——
# PowerShell 读环境变量是 Unicode，天生不乱码；而 .ps1 / -Command 的编码
# 受 BOM 与代码页影响，是个没必要的风险面。
_PS_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$out = $env:APKPARSER_PICK_OUT
if (-not $out) {
    [Console]::Error.Write('__NO_OUT__ APKPARSER_PICK_OUT not set')
    exit 5
}
function Write-Result([string]$text) {
    [System.IO.File]::WriteAllText($out, $text, (New-Object System.Text.UTF8Encoding($false)))
}
try {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
} catch {
    Write-Result ''
    [Console]::Error.Write('__NO_WINFORMS__' + $_.Exception.Message)
    exit 3
}
try {
    $mode = $env:APKPARSER_PICK_MODE
    if ($mode -eq 'dir') {
        # FolderBrowserDialog 在 .NET Framework 上是老式树形对话框：
        # 没有路径输入框、也不能新建文件夹之外的东西 —— 但它是唯一
        # 不依赖第三方程序集的"选目录"途径，兜底够用。
        $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
        $dlg.Description = $env:APKPARSER_PICK_TITLE
        $dlg.ShowNewFolderButton = $true
        $d = $env:APKPARSER_PICK_DIR
        if ($d -and (Test-Path -LiteralPath $d)) { $dlg.SelectedPath = $d }
        if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            Write-Result $dlg.SelectedPath
        } else {
            Write-Result ''
        }
    } else {
        $dlg = New-Object System.Windows.Forms.OpenFileDialog
        $dlg.Title = $env:APKPARSER_PICK_TITLE
        $dlg.Filter = $env:APKPARSER_PICK_PSFILTER
        $dlg.Multiselect = $false
        $dlg.CheckFileExists = $true
        $dlg.RestoreDirectory = $true
        $d = $env:APKPARSER_PICK_DIR
        if ($d -and (Test-Path -LiteralPath $d)) { $dlg.InitialDirectory = $d }
        $f = $env:APKPARSER_PICK_FILE
        if ($f) { $dlg.FileName = $f }
        if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            Write-Result $dlg.FileName
        } else {
            Write-Result ''
        }
    }
} catch {
    Write-Result ''
    [Console]::Error.Write('__DIALOG_ERR__' + $_.Exception.Message)
    exit 4
}
'''


def _find_powershell() -> Optional[str]:
    with _LOCK:
        if _PS["probed"]:
            return _PS["exe"]      # type: ignore[return-value]
        exe = None
        for n in ("powershell.exe", "pwsh.exe"):
            p = shutil.which(n)
            if p and Path(p).is_file():
                exe = p
                break
        _PS.update({"probed": True, "exe": exe})
        return exe


# ---------------------------------------------------------------- 统一入口

def _norm_types(filetypes: Optional[Sequence[Sequence[str]]]):
    """归一化成 [(label, spec)]，顺带产出 PowerShell 的 Filter 串。"""
    ft = [tuple(str(x) for x in t) for t in (filetypes or []) if len(tuple(t)) >= 2]
    if not ft:
        ft = [("所有文件", "*.*")]
    # tkinter 用空格分隔；WinForms 用分号分隔
    ps = "|".join("%s (%s)|%s" % (lbl, spec, spec.replace(" ", ";")) for lbl, spec in ft)
    return ft, ps


def pick_file(title: str = "选择文件",
              initial_dir: str = "",
              filetypes: Optional[Sequence[Sequence[str]]] = None,
              initial_file: str = "",
              timeout: float = 600.0) -> Dict:
    """弹原生**文件**对话框。返回

        {"ok": True,  "path": "...", "backend": "tkinter:python.exe"}
        {"ok": True,  "path": "",    "backend": ..., "cancelled": True}   用户取消
        {"ok": False, "error": "...", "tried": [...], "backend": None}    无可用后端

    调用方按 `ok` / `cancelled` 决定是否降级。

    `initial_file` 会预填文件名框（"上次选的就是这个"）。
    """
    return _pick("file", title, initial_dir, filetypes, initial_file, timeout)


def pick_dir(title: str = "选择文件夹",
             initial_dir: str = "",
             timeout: float = 600.0) -> Dict:
    """弹原生**目录**对话框（返回值语义与 `pick_file` 完全一致）。

    存在的理由：导出 APK / 扫目录这两个动作，用户以前得**手打一个路径**。
    导出的目标目录就是"我想放哪儿"，让人默写自己磁盘的绝对路径是没道理的。
    能弹一次原生对话框就别让人打字。
    """
    return _pick("dir", title, initial_dir, None, "", timeout)


def _pick(kind: str, title: str, initial_dir: str,
          filetypes: Optional[Sequence[Sequence[str]]],
          initial_file: str, timeout: float) -> Dict:
    ft, psfilter = _norm_types(filetypes)

    handle, out_path = tempfile.mkstemp(prefix="apkparser-pick-", suffix=".txt")
    os.close(handle)
    try:
        os.unlink(out_path)
    except OSError:
        pass

    common_env = {
        "APKPARSER_PICK_OUT": out_path,
        "APKPARSER_PICK_DIR": initial_dir or "",
        "APKPARSER_PICK_FILE": initial_file or "",
        "APKPARSER_PICK_TITLE": title,
        "APKPARSER_PICK_MODE": kind,
        "APKPARSER_PICK_TYPES": json.dumps(ft, ensure_ascii=False),
        "PYTHONUTF8": "1",
    }

    tried: List[str] = []

    # ---- 后端 1：有 tkinter 的 Python
    exe = find_gui_python()
    if exe:
        # 优先 pythonw.exe（无控制台，不会闪黑框）
        run_exe = exe
        try:
            w = Path(exe).with_name("pythonw.exe")
            if w.is_file():
                run_exe = str(w)
        except Exception:
            pass
        rc, _out, err = _run([run_exe, "-c", _TK_SCRIPT], common_env, timeout=timeout)
        found, got = _read_result(out_path)
        if err.startswith("__NO_TK__"):
            tried.append("tkinter(%s): %s" % (run_exe, err[9:][:120]))
        elif err.startswith("__NO_OUT__"):
            tried.append("tkinter(%s): %s" % (run_exe, err[9:][:120]))
        elif err.startswith("__DIALOG_ERR__"):
            tried.append("tkinter(%s): %s" % (run_exe, err[14:][:120]))
        elif rc == 0 and found:
            _cleanup(out_path)
            return {"ok": True, "path": got, "backend": "tkinter:%s" % Path(run_exe).name,
                    "cancelled": not got, "kind": kind}
        else:
            tried.append("tkinter(%s): rc=%s 结果文件%s %s"
                         % (Path(run_exe).name, rc,
                            "已写出" if found else "**没写出**（脚本没跑完）",
                            (err or "")[:120]))
    else:
        tried.append("tkinter: 没有找到带 tkinter 的 Python 解释器")

    # ---- 后端 2：Windows PowerShell + WinForms
    if paths.IS_WINDOWS:
        ps = _find_powershell()
        if ps:
            env = dict(common_env)
            env["APKPARSER_PICK_PSFILTER"] = psfilter
            enc = base64.b64encode(_PS_SCRIPT.encode("utf-16-le")).decode("ascii")
            rc, _out, err = _run(
                [ps, "-NoProfile", "-STA", "-WindowStyle", "Hidden",
                 "-EncodedCommand", enc],
                env, timeout=timeout)
            found, got = _read_result(out_path)
            if err.startswith("__NO_WINFORMS__"):
                tried.append("powershell: 无法加载 WinForms —— %s" % err[14:][:120])
            elif err.startswith("__NO_OUT__"):
                tried.append("powershell: %s" % err[9:][:120])
            elif err.startswith("__DIALOG_ERR__"):
                tried.append("powershell: %s" % err[14:][:120])
            elif rc == 0 and found:
                _cleanup(out_path)
                return {"ok": True, "path": got,
                        "backend": "powershell:%s" % Path(ps).name,
                        "cancelled": not got, "kind": kind}
            else:
                tried.append("powershell: rc=%s 结果文件%s %s"
                             % (rc, "已写出" if found else "**没写出**（脚本没跑完）",
                                (err or "")[:120]))
        else:
            tried.append("powershell: 未找到")
    else:
        tried.append("powershell: 仅 Windows")

    _cleanup(out_path)
    return {"ok": False, "error": "没有可用的原生文件对话框后端", "tried": tried,
            "backend": None, "kind": kind}


def _read_result(out_path: str) -> Tuple[bool, str]:
    """读子进程写的结果文件。返回 (文件存在?, 内容)。

    **"文件不存在" 必须当作失败，不能当作"用户取消"。**
    脚本只有在真的跑完（或走到了明确的取消分支）时才会写出这个文件；
    写不出来说明脚本压根没跑对（环境变量没传到、解释器起不来、脚本语法错…）。
    把它当"取消"会得到一个静默坏掉的流程 —— 用户选完文件什么都不发生，
    而接口层却报 ok=True。这个坑真踩过，所以这里返回值刻意是二元组。
    """
    p = Path(out_path)
    if not p.is_file():
        return False, ""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return True, p.read_text(encoding=enc).strip()
        except UnicodeDecodeError:
            continue
        except OSError:
            return False, ""
    return False, ""


def _cleanup(out_path: str) -> None:
    try:
        os.unlink(out_path)
    except OSError:
        pass


def backend_info() -> Dict:
    """给 doctor / 前端用的能力概览（不弹对话框）。"""
    if not paths.IS_WINDOWS and not paths.IS_MAC and not paths.IS_LINUX:
        pass
    exe = find_gui_python()
    info: Dict = {
        "available": bool(exe) or (paths.IS_WINDOWS and bool(_find_powershell())),
        "tkinterPython": exe or "",
        "tkinterTried": list(_GUI_PY.get("tried") or []),
        "powershell": (_find_powershell() or "") if paths.IS_WINDOWS else "",
        "native": bool(exe) or (paths.IS_WINDOWS and bool(_find_powershell())),
        "detail": "",
    }
    if exe:
        info["backend"] = "tkinter"
        info["detail"] = "原生对话框可用（tkinter：%s）" % exe
    elif paths.IS_WINDOWS and info["powershell"]:
        info["backend"] = "powershell"
        info["detail"] = "原生对话框可用（PowerShell + WinForms 兜底）"
    else:
        info["backend"] = None
        info["detail"] = ("没有可用的原生对话框后端；页面上的「浏览…」将改用浏览器"
                          "选择文件并上传到本机临时目录。把 APK 拖进页面则始终可用。")
    return info
