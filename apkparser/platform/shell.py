# -*- coding: utf-8 -*-
"""统一的外部命令执行与桌面集成。

**铁律**：`core/` 里不得直接 `subprocess.run` —— 所有外部命令走这里的
`run()`，一处加超时、加日志、加错误包装、加编码处理。
"""

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Union

from . import paths

IS_WINDOWS = paths.IS_WINDOWS
IS_MAC = paths.IS_MAC


# ---------------------------------------------------------------- 结果模型

@dataclass
class Result:
    """一次外部命令执行的结果。"""

    cmd: List[str]
    rc: int
    out: str = ""
    err: str = ""
    duration: float = 0.0
    timed_out: bool = False
    exception: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out and not self.exception

    @property
    def text(self) -> str:
        return (self.out or "") + (("\n" + self.err) if self.err else "")

    def lines(self) -> List[str]:
        return [l for l in self.text.replace("\r\n", "\n").split("\n")]

    def to_dict(self) -> dict:
        return {
            "cmd": self.cmd,
            "rc": self.rc,
            "ok": self.ok,
            "duration": round(self.duration, 3),
            "timed_out": self.timed_out,
            "error": self.exception,
            "stdout": self.out,
            "stderr": self.err,
        }


# ---------------------------------------------------------------- 编码

def _decode(raw: bytes) -> str:
    """尽力解码外部命令输出。

    中文 Windows 上 adb/aapt 的输出可能是 GBK；现代工具多为 UTF-8。
    按 UTF-8 → 系统 ANSI (GBK) → latin-1 顺序尝试，永不抛异常。
    """
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _popen_kwargs() -> dict:
    """平台相关的 subprocess 参数（隐藏黑框等）。"""
    kw = {}
    if IS_WINDOWS:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kw["startupinfo"] = si
    return kw


def run(
    cmd: Union[Sequence[str], str],
    timeout: float = 60.0,
    cwd: Optional[Union[str, Path]] = None,
    env: Optional[dict] = None,
    input_bytes: Optional[bytes] = None,
    shell: bool = False,
    check: bool = False,
    hide_window: bool = True,
) -> Result:
    """执行命令并返回 `Result`。**永不抛异常**（除非 check=True）。

    :param cmd: 参数列表（推荐）或字符串（shell=True 时）
    :param timeout: 秒；超时会杀进程树并标记 `timed_out`
    :param hide_window: 是否隐藏子进程的控制台窗口。默认隐藏；
        如果子进程要自己弹出 GUI（例如 tkinter 文件对话框），必须传 False，
        否则 `SW_HIDE` 会把它一并藏掉。
    """
    if isinstance(cmd, str):
        argv: List[str] = [cmd]
        shell = True
    else:
        argv = [str(c) for c in cmd]

    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update({k: str(v) for k, v in env.items()})

    t0 = time.time()
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            shell=shell,
            **_popen_kwargs(),
        )
    except FileNotFoundError as e:
        return Result(argv, 127, "", str(e), time.time() - t0, exception=str(e))
    except OSError as e:
        return Result(argv, 126, "", str(e), time.time() - t0, exception=str(e))

    try:
        out_b, err_b = proc.communicate(input=input_bytes, timeout=timeout)
        rc = proc.returncode
        return Result(argv, rc, _decode(out_b), _decode(err_b), time.time() - t0)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        try:
            out_b, err_b = proc.communicate(timeout=5)
        except Exception:
            out_b, err_b = b"", b""
        r = Result(argv, -1, _decode(out_b), _decode(err_b), time.time() - t0, timed_out=True)
        r.exception = "命令执行超时（%.0fs）" % timeout
        return r
    except Exception as e:  # pragma: no cover - 兜底
        return Result(argv, -1, "", str(e), time.time() - t0, exception=str(e))


def run_bytes(cmd: Sequence[str], timeout: float = 60.0) -> tuple:
    """执行命令并拿**原始字节**输出（截图等二进制场景）。

    文本版的 `run()` 会把输出解码成 str，遇到 PNG 这类二进制必然损坏。
    这里返回 `(rc, stdout_bytes, stderr_text)`。
    """
    argv = [str(c) for c in cmd]
    t0 = time.time()
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, **_popen_kwargs())
    except FileNotFoundError as e:
        return 127, b"", str(e)
    except OSError as e:
        return 126, b"", str(e)

    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or b"", _decode(err)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        return -1, b"", "命令执行超时（%.0fs）" % timeout
    except Exception as e:  # pragma: no cover
        return -1, b"", str(e)


def spawn(argv: Sequence[str], **kwargs) -> Optional[subprocess.Popen]:
    """启动一个**不等待**的子进程（录屏这类长任务用）。

    返回 Popen 对象；调用方负责 terminate。
    """
    argv = [str(a) for a in argv]
    try:
        return subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, **_popen_kwargs())
    except OSError:
        return None


def kill_tree(pid: int) -> None:
    """杀掉进程及其子进程树。"""
    if not pid:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, **_popen_kwargs(),
            )
        else:
            import signal
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


# ---------------------------------------------------------------- 桌面集成

def open_path(path: Union[str, Path]) -> bool:
    """用系统默认程序打开文件或目录。"""
    p = str(path)
    try:
        if IS_WINDOWS:
            os.startfile(p)  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", p], **_popen_kwargs())
        else:
            subprocess.Popen(["xdg-open", p], **_popen_kwargs())
        return True
    except Exception:
        return False


def reveal_path(path: Union[str, Path]) -> bool:
    """在文件管理器中定位并选中该文件。"""
    p = Path(path)
    try:
        if IS_WINDOWS:
            subprocess.Popen(["explorer", "/select,", str(p)], **_popen_kwargs())
        elif IS_MAC:
            subprocess.Popen(["open", "-R", str(p)], **_popen_kwargs())
        else:
            # Linux 各家文件管理器对"选中"支持不一，退化为打开父目录
            subprocess.Popen(["xdg-open", str(p.parent)], **_popen_kwargs())
        return True
    except Exception:
        return False


def open_url(url: str) -> bool:
    import webbrowser
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def copy_to_clipboard(text: str) -> bool:
    """把文本放进系统剪贴板（中文要正确，所以 Windows 走 Win32 API）。

    不用 `clip` 命令：它按当前控制台代码页读 stdin，中文必乱。
    """
    if IS_WINDOWS:
        return _win_copy(text)
    cmd = None
    for cand, args in (("pbcopy", []), ("wl-copy", []), ("xclip", ["-selection", "clipboard"]),
                       ("xsel", ["--clipboard", "--input"])):
        from shutil import which
        if which(cand):
            cmd = [cand] + args
            break
    if not cmd:
        return False
    r = run(cmd, input_bytes=text.encode("utf-8"), timeout=10)
    return r.rc == 0


def _win_copy(text: str) -> bool:
    import ctypes
    import time as _time

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # ---- 64 位进程的血泪：ctypes 默认 restype 是 32 位 int，HANDLE 会被
        # 截断。GlobalAlloc 返回 64 位句柄（现代 Windows 堆常在 >4GB 地址），
        # 截断后 GlobalLock/SetClipboardData 拿垃圾句柄 → 必失败。
        # 必须显式声明：GlobalAlloc/GlobalLock 返回 c_void_p。
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
        kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)

        # OpenClipboard 可能因别的进程正占着剪贴板而短暂失败 → 重试几次
        opened = False
        for _ in range(8):
            if user32.OpenClipboard(None):
                opened = True
                break
            _time.sleep(0.05)
        if not opened:
            return False
        handle = None
        try:
            user32.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buf)
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return False
            ctypes.memmove(ptr, buf, size)
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                # 失败时所有权仍在自己手里，要归还，否则泄漏
                kernel32.GlobalFree(handle)
                return False
            handle = None  # 成功后所有权移交剪贴板，系统负责释放
            return True
        finally:
            user32.CloseClipboard()
            if handle:
                kernel32.GlobalFree(handle)
    except Exception:
        return False


def spawn_console(argv: Sequence[str], title: str = "APK 工具", keep_open: bool = True,
                  env_extra: Optional[dict] = None) -> bool:
    """在新终端窗口里执行命令（双击安装的"cmd 窗口弹出"）。

    keep_open=True 时命令跑完窗口不关，方便看完整日志。
    env_extra 会叠加到子进程环境上（给子进程传"我已在新窗口里"的标记，防递归）。

    注意：这里**不能**用 `_popen_kwargs()` —— 那个会给窗口设 SW_HIDE，
    而本函数的目的恰恰是把窗口露出来。
    """
    args = [str(a) for a in argv]
    env = dict(os.environ)
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})
    try:
        if IS_WINDOWS:
            # 用 list2cmdline 做 Windows 参数转义，避免路径含空格/中文被拆
            inner = subprocess.list2cmdline(args)
            tail = "cmd /k" if keep_open else "cmd /c"
            full = 'start "%s" %s %s' % (title, tail, inner)
            subprocess.Popen(full, shell=True, env=env)
            return True
        if IS_MAC:
            inner = " ".join(_sh_quote(a) for a in args)
            script = 'tell application "Terminal" to do script "%s"' % inner.replace('"', '\\"')
            subprocess.Popen(["osascript", "-e", script], env=env)
            return True
        # Linux：依次探测常见终端
        base = " ".join(_sh_quote(a) for a in args)
        for term, flag in (
            ("x-terminal-emulator", "-e"),
            ("gnome-terminal", "--"),
            ("konsole", "-e"),
            ("xfce4-terminal", "-x"),
            ("xterm", "-e"),
        ):
            from shutil import which
            if which(term):
                inner = base
                if keep_open:
                    inner = "%s; echo; read -p '按回车关闭...'" % inner
                subprocess.Popen([term, flag, "bash", "-c", inner], env=env)
                return True
        return False
    except Exception:
        return False


def _sh_quote(s: str) -> str:
    if s == "" or any(c in s for c in " \t\"'\\$`&|;<>()*?[]{}!#~"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def quote_for_display(s: str) -> str:
    """人类可读的命令预览：含特殊字符时加引号。"""
    if s == "" or any(c in s for c in ' \t"\'&|<>^%'):
        return '"' + s.replace('"', '\\"') + '"'
    return s
