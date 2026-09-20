# -*- coding: utf-8 -*-
"""设备文件传输：push / pull / ls。"""

import posixpath
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..platform import paths
from .device import adb as adb_mod

# 一行 ls -la：
#   -rw-r--r-- 1 root   root      1234 2024-01-01 12:00 name
#   drwxrws--- 2 u0_a100 media_rw 3452 2026-07-04 15:04 Alarms
#   lrw-r--r-- 1 root   root        21 2026-07-21 00:52 /sdcard -> /storage/self/primary
#   -rw-r--r-- 1 root   root      1234 Jul  4  2024 name
#
# 兼容点：
#   · 有的 toybox 不输出硬链接数     → `(?:\d+\s+)?`
#   · 有 ACL / SELinux 标记时权限尾会多一个 + . @
#   · 时间有 ISO 与 "Jul 4 2024" 两种
_LS_RE = re.compile(
    r"^(?P<perm>[-dlcbps][rwxsStTl-]{9})[.+@]?\s+"
    r"(?:\d+\s+)?(?P<owner>\S+)\s+(?P<group>\S+)\s+"
    r"(?P<size>\d+)?\s*"
    r"(?:(?P<date>\d{4}-\d{2}-\d{2}|\w{3}\s+\d+\s+[\d:]+)\s+(?P<time>[\d:]+)?\s+)?"
    r"(?P<rest>.+)$")

# 路径别名：这些写法的"真身"在别处，列目录时最容易踩到
_ALIASES = ("/sdcard", "/mnt/sdcard", "/storage/self/primary")

_ERR_HINTS = (
    ("Permission denied", "没有权限 → 这是别的应用的数据目录，或该挂载点被 SELinux 限制"
                          "（普通 adb shell 读不了，本工具不 root）"),
    ("No such file or directory", "路径不存在（Android 区分大小写；也可能该分区没挂载）"),
    ("Not a directory", "这是一个文件，不是目录"),
    ("Too many symbolic links", "符号链接套太深（可能是 SELinux 限制的挂载点）"),
)


def _hint_for(text: str) -> str:
    low = text or ""
    for pat, hint in _ERR_HINTS:
        if pat.lower() in low.lower():
            return hint
    return ""


def _norm(path: str) -> str:
    p = (path or "").strip().replace("\\", "/")
    if not p:
        p = "/sdcard"
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1:
        p = p.rstrip("/")
    if not p.startswith("/"):
        p = "/" + p
    return p or "/"


def _crumbs(path: str) -> List[dict]:
    """面包屑：/ → sdcard → Download"""
    if path == "/":
        return [{"name": "/", "path": "/"}]
    out = [{"name": "/", "path": "/"}]
    cur = ""
    for part in path.strip("/").split("/"):
        cur = cur + "/" + part
        out.append({"name": part, "path": cur})
    return out


def _one(serial: str, target: str) -> Tuple[object, str]:
    """跑一次 `ls -la`。返回 (Result, 真正用的命令预览)。"""
    args = ["shell", "ls", "-la", target]
    r = adb_mod.run(args, serial=serial, timeout=30)
    return r, adb_mod.command_preview(args, serial)


def _resolve(serial: str, path: str) -> str:
    """`readlink -f` 拿规范路径（可能为空）。"""
    r = adb_mod.run(["shell", "readlink", "-f", path], serial=serial, timeout=15)
    for line in r.lines():
        line = line.strip()
        if line.startswith("/"):
            return line
    return ""


def _link_is_dir(serial: str, target: str) -> Optional[bool]:
    """符号链接指向目录还是文件（多花一次 adb 调用，只在真有链接时用）。"""
    r = adb_mod.run(["shell", "ls", "-ld", target], serial=serial, timeout=15)
    for line in r.lines():
        m = _LS_RE.match(line.strip())
        if m:
            return m.group("perm").startswith("d")
    return None


def _parse_lines(lines: List[str], base: str) -> Tuple[List[dict], List[Tuple[dict, str]]]:
    """把 `ls -la` 的输出切成条目。

    返回 (entries, 需要另判目标的符号链接列表)。
    """
    entries: List[dict] = []
    links: List[Tuple[dict, str]] = []
    for line in lines:
        line = line.rstrip()
        if not line or line.startswith("total ") or line.startswith("ls: "):
            continue
        m = _LS_RE.match(line)
        if not m:
            continue
        rest = m.group("rest").strip()
        target = ""
        if " -> " in rest:
            rest, target = rest.split(" -> ", 1)
            target = target.strip()
        if rest in (".", ".."):
            continue

        perm = m.group("perm")
        is_link = perm.startswith("l")
        is_dir = perm.startswith("d")
        size = m.group("size")
        ent = {
            "name": rest,
            "rawName": rest,
            "path": posixpath.join(base, rest) if base != "/" else "/" + rest,
            "dir": is_dir,
            "link": is_link,
            "linkTarget": target,
            "perm": perm,
            "owner": m.group("owner"),
            "group": m.group("group"),
            "date": (m.group("date") or "").strip(),
            "time": (m.group("time") or "").strip(),
            # 目录的 size 是块设备上的记账值（3452 这种），显示成字节数是误导 → 不显示
            "size": (int(size) if size and size.isdigit() else 0) if not is_dir else None,
        }
        entries.append(ent)
        if is_link and target:
            # 相对目标要相对**链接所在目录**解析，不是相对链接自身
            abs_target = target if target.startswith("/") else posixpath.normpath(
                posixpath.join(posixpath.dirname(ent["path"]), target))
            ent["linkTarget"] = abs_target
            links.append((ent, abs_target))
    return entries, links


def ls(path: str = "/sdcard", device: str = "") -> dict:
    """列设备目录。

    ## 关于 `/sdcard` 这样的"别名路径"

    `/sdcard` 是**符号链接**（→ `/storage/self/primary` → `/storage/emulated/0`）。
    `ls -la /sdcard` 只输出链接自身那一行，**不会**展开成目录内容 ——
    这正是"列 /sdcard 是空的、列 /storage/emulated/0 正常"的原因。
    让它展开有两条等效的路：给路径**加尾斜杠**（POSIX 规定）
    或加 `-L`。这里用尾斜杠，并在失败时自动回退。
    """
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "detail": str(e), "entries": [], "commands": []}

    want = _norm(path)
    commands: List[str] = []

    # 1) 先试着当成目录列：非根路径加尾斜杠，强制解开符号链接
    probe = want if want == "/" else want + "/"
    r, cmd = _one(serial, probe)
    commands.append(cmd)
    fell_back = False
    if (not r.ok or not r.out.strip()) and "not a directory" in (r.err or "").lower():
        # 那是一"个"文件（或指向文件的链接）→ 退回不带斜杠
        r, cmd = _one(serial, want)
        commands.append(cmd)
        fell_back = True

    out_text = r.out or ""
    err_text = (r.err or "").strip()
    swapped_to = ""

    if not out_text.strip():
        # 有的别名路径本身被 SELinux 挡住（例如 `/mnt/sdcard` 报 Permission denied），
        # 但它的**真身**是能列的 —— 用 readlink -f 换条路再试一次。
        real = _resolve(serial, want)
        if real and real != want:
            r2, cmd2 = _one(serial, real if real == "/" else real + "/")
            commands.append(cmd2)
            if (r2.out or "").strip():
                out_text = r2.out
                err_text = (r2.err or "").strip()
                swapped_to = real

    if not out_text.strip():
        return {
            "ok": False,
            "detail": err_text[:200] or "读取失败",
            "hint": _hint_for(err_text),
            "path": want, "entries": [], "commands": commands,
        }

    entries, links = _parse_lines(out_text.replace("\r\n", "\n").split("\n"), want)

    # 2) 回退后只拿到一条、且名字就是这个路径本身 → 传进来的是个"文件"
    if fell_back and len(entries) == 1 and entries[0]["rawName"].rstrip("/") == want:
        ent = entries[0]
        return {
            "ok": True, "isFile": True, "path": want,
            "detail": "这是一个文件%s" % ("（符号链接）" if ent["link"] else ""),
            "hint": "要对文件操作请用「拉回本机」；路径栏填它所在的目录才能浏览",
            "entries": [], "count": 0, "dirCount": 0, "fileCount": 0,
            "file": {k: v for k, v in ent.items() if k != "rawName"},
            "parent": posixpath.dirname(want) or "/",
            "crumbs": _crumbs(posixpath.dirname(want) or "/"),
            "commands": commands,
        }

    # 3) 链接另判：多一次 adb 调用，但只在真有链接时发生（顺手把"能不能进去"标出来）
    for ent, abs_target in links[:6]:
        ent["dir"] = bool(_link_is_dir(serial, abs_target))
        commands.append(adb_mod.command_preview(
            ["shell", "ls", "-ld", abs_target], serial))

    for ent in entries:
        ent.pop("rawName", None)

    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    dirs = sum(1 for e in entries if e["dir"])
    files = len(entries) - dirs

    res = {
        "ok": True,
        "path": want,
        "detail": "%d 项（%d 目录 / %d 文件）" % (len(entries), dirs, files),
        "count": len(entries), "dirCount": dirs, "fileCount": files,
        "entries": entries,
        "parent": posixpath.dirname(want) or "/",
        "crumbs": _crumbs(want),
        "commands": commands,
        "isAlias": want in _ALIASES or "/sdcard/" in want + "/",
    }

    # 规范路径：和输入不同就带上（前端显示成 "≡ 真身"，比一句长解释好用）
    real = swapped_to or _resolve(serial, want)
    if real and real != want:
        res["resolved"] = real
        # 只有当**末段变了**才说明"这个路径本身就是个链接"，否则只是途经了别名
        if posixpath.basename(real) != posixpath.basename(want):
            res["aliasNote"] = ("%s 是个符号链接，真身是 %s —— 内容完全一样"
                                % (want, real))
        elif swapped_to:
            res["aliasNote"] = ("%s 本身被 SELinux 挡住，这里列的是它的真身 %s"
                                % (want, real))
    # stdout 有内容但 stderr 报警（例如部分子项没权限）也要让用户知道
    if err_text:
        res["warning"] = err_text[:200]
        res["warningHint"] = _hint_for(err_text)
    return res


def push(src: str, dst: str, device: str = "") -> dict:
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "detail": str(e), "commands": []}

    s = Path(src)
    if not s.exists():
        return {"ok": False, "detail": "本地路径不存在：%s" % s, "commands": []}

    args = ["push", str(s), dst]
    r = adb_mod.run(args, serial=serial, timeout=600)
    command = adb_mod.command_preview(args, serial)
    out = (r.out or "").strip()
    return {"ok": r.ok, "detail": out.splitlines()[-1] if out else (r.err or "失败").strip(),
            "stdout": out[:800], "commands": [command]}


def pull(src: str, dst: str = "", device: str = "") -> dict:
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "detail": str(e), "commands": []}

    dest = dst or str(paths.output_dir() / "pulled")
    if not dst:
        Path(dest).mkdir(parents=True, exist_ok=True)

    args = ["pull", src, dest]
    r = adb_mod.run(args, serial=serial, timeout=1800)
    command = adb_mod.command_preview(args, serial)
    out = (r.out or "").strip()
    return {"ok": r.ok, "detail": out.splitlines()[-1] if out else (r.err or "失败").strip(),
            "dest": dest, "file": _pulled_file(dest, src) if r.ok else "",
            "commands": [command]}


def _pulled_file(dest: str, src: str) -> str:
    """落地的那个文件的完整路径。

    `adb pull src dest` 的规则：dest 是**已存在的目录** → 文件落进去，文件名沿用
    设备上的；否则 dest 就是文件名本身。界面要告诉用户"存到哪儿了"，
    所以这里把这条规则显式算出来 —— 前端拿 `file`，不用自己拼。
    """
    try:
        p = Path(dest)
        if p.is_dir():
            return str(p / Path(str(src).replace("\\", "/")).name)
        return str(p)
    except Exception:
        return ""


def rm(path: str, device: str = "", recursive: bool = False) -> dict:
    try:
        serial = adb_mod.pick_device(device)
    except adb_mod.AdbError as e:
        return {"ok": False, "detail": str(e), "commands": []}
    args = ["shell", "rm"] + (["-rf"] if recursive else ["-f"]) + [path]
    r = adb_mod.run(args, serial=serial, timeout=30)
    return {"ok": r.ok, "detail": "已删除 %s" % path if r.ok else (r.err or "失败").strip(),
            "commands": [adb_mod.command_preview(args, serial)]}
