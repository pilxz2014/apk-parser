# -*- coding: utf-8 -*-
"""脚本存档库 —— 把 bat / sh 脚本收进来、分类、备注、点一下就跑。

## 为什么单独做一个库，而不是"哪儿的脚本都能加"

以前常用的几条 adb 测试脚本散落在各个目录里，每次用都得先想起来它在哪、
再 `cd` 过去、再手打一遍参数。工具里既然已经有"我的入口"这种收藏机制，
脚本当然也该有：

  * **收进来** —— 加进来的是一份**副本**，原脚本不会被改动、不会被移动。
    这样"把 D:\\bat 下的脚本加进来"不会把人家的目录结构搞乱。
  * **分类** —— 目录就是分类（adb / 打包 / 巡检…），界面左边点一下就换。
  * **备注** —— 名字只够写 `login-test`，写不下"登录模块冒烟，需先装测试包"。
    备注存在库根的 `.scripts.json` 里，**不污染脚本文件本身**。
  * **点一下就跑** —— 命令由这里统一拼（见 `_run_argv`），界面预览和实际
    执行是同一段代码，不会出现"看到的命令"和"跑的命令"不是一回事。

## 两个刻意的选择

1. **备注集中存一个 JSON，而不是每个脚本配一个 `.meta` 文件**：
   后者会把库目录搞得一个脚本两个文件，复制、打包、备份都容易漏掉一半。
2. **运行前不改脚本内容**：Windows 的 `.bat` 常见 GBK 编码，读的时候按
   探测到的编码读、保存时按原编码写回（`_read_text` / `_write_text`），
   绝不顺手"统一成 UTF-8" —— 那会让 cmd 里的中文变成乱码。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..platform import paths, shell

__all__ = [
    "library_root", "list_dir", "make_folder", "add_existing", "create",
    "read_script", "write_script", "rename", "move", "set_note", "delete",
    "run", "folder_choices",
]

META_NAME = ".scripts.json"
MAX_OUT = 8000            # 回给界面的输出上限（超出会标 truncated）
DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 900.0

# 扩展名 → 类别。认不出来的一律归到 ""（能存、能编辑、不能运行）。
KIND_OF_EXT = {
    ".bat": "bat", ".cmd": "bat",
    ".sh": "sh", ".bash": "sh", ".zsh": "sh",
    ".ps1": "ps1",
    ".py": "py",
}

KIND_LABEL = {
    "bat": "批处理", "sh": "Shell", "ps1": "PowerShell", "py": "Python", "": "文本",
}

# 新建脚本时的起始内容。**全部 ASCII** —— 中文注释进了 .bat 会以当前代码页
# 落盘，换台机器就乱码；说明留给用户自己写在备注里。
TEMPLATES = {
    "bat": "@echo off\nrem description: \n",
    "sh": "#!/usr/bin/env bash\nset -e\n# description: \n",
    "ps1": "# description: \n",
    "py": "# -*- coding: utf-8 -*-\n",
    "": "",
}

SCRIPT_TYPES = [("脚本文件", "*.bat *.cmd *.sh *.bash *.ps1 *.py"),
                ("所有文件", "*.*")]


# ---------------------------------------------------------------- 库根 / 路径

def library_root() -> Path:
    """脚本库根目录（用户级，可被 config.json 的 `scripts_dir` 覆盖）。"""
    return paths.scripts_dir()


def _root() -> Path:
    return paths.scripts_dir()


def _resolve(rel: str) -> Path:
    """相对路径 → 库内绝对路径。**越界一律报错**（拒绝 `..` 与绝对路径）。"""
    root = _root().resolve()
    raw = str(rel or "")
    # 绝对路径直接拒绝：strip("/") 之后 "C:\x" 会变成库内的相对路径，
    # 虽然也越不了界，但报错信息会变成"文件不存在"，把人往错路上引
    if raw.startswith(("/", "\\")) or (len(raw) > 1 and raw[1] == ":"):
        raise ValueError("只接受脚本库内的相对路径：%s" % raw)
    rel = raw.replace("\\", "/").strip("/")
    p = (root / rel).resolve() if rel else root
    if p != root and root not in p.parents:
        raise ValueError("路径超出脚本库：%s" % rel)
    return p


def _rel_of(p: Path) -> str:
    try:
        return p.resolve().relative_to(_root().resolve()).as_posix()
    except Exception:
        return p.name


def _fail(msg: str, **extra) -> dict:
    out = {"ok": False, "error": msg}
    out.update(extra)
    return out


# ---------------------------------------------------------------- 元信息

def _meta_path() -> Path:
    return _root() / META_NAME


def _load_meta() -> dict:
    p = _meta_path()
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        # 元信息坏了不能让整个库打不开 —— 但也不能悄悄丢，备份一份再当空的处理
        try:
            p.replace(p.with_suffix(".json.bak"))
        except Exception:
            pass
        return {}


def _save_meta(d: dict) -> None:
    p = _meta_path()
    _root().mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _meta_of(rel: str) -> dict:
    return _load_meta().get(rel) or {}


def _drop_meta(rel: str) -> None:
    d = _load_meta()
    if rel in d:
        d.pop(rel)
        _save_meta(d)


# ---------------------------------------------------------------- 读 / 写编码

def _read_text(p: Path) -> Tuple[str, str]:
    """读脚本内容，返回 (文本, 探测到的编码)。

    Windows 的 .bat 极常见 GBK；统一按 UTF-8 读会得到满屏乱码，
    于是按 utf-8-sig → utf-8 → gbk 顺序试。**ASCII 文件对三种都一样**，
    所以这条探测对纯英文脚本零影响。
    """
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def _write_text(p: Path, text: str, enc: str = "") -> None:
    """写回脚本。**保持原编码**：原来是 GBK 就还写 GBK，不让 cmd 读崩。"""
    if enc == "gbk":
        try:
            text.encode("gbk")
            p.write_text(text, encoding="gbk")
            return
        except UnicodeEncodeError:
            pass          # 出现了 GBK 装不下的字符 → 退回 UTF-8
    p.write_text(text, encoding="utf-8")


def _kind_of(name: str) -> str:
    return KIND_OF_EXT.get(Path(name).suffix.lower(), "")


def _summary(text: str) -> str:
    """取脚本开头的注释当摘要（用户懒得写备注时，列表里也不至于一片空白）。"""
    for line in (text or "").splitlines()[:8]:
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") or s.lower().startswith("rem ") or s.startswith("::"):
            s = s.lstrip("#").strip()
            if s.lower().startswith("rem "):
                s = s[4:].strip()
            s = s.lstrip(":").strip()
            if s and not s.startswith("!"):
                return s[:80]
    return ""


# ---------------------------------------------------------------- 运行方式

def _run_argv(p: Path, kind: str, args: str) -> Tuple[List[str], str]:
    """把"跑这个脚本"变成命令。**界面预览与实际执行共用这一段**。

    返回 (argv, 展示用的命令串)。
    """
    extra = [a for a in (args or "").split(" ") if a]
    if paths.IS_WINDOWS:
        if kind in ("bat", ""):
            return ["cmd", "/c", str(p)] + extra, "cmd /c " + str(p)
        if kind == "ps1":
            return (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(p)] + extra, "powershell -File " + str(p))
        if kind == "py":
            return [shell_python(), str(p)] + extra, "python " + str(p)
        if kind == "sh":
            bash = _find_bash()
            if not bash:
                raise ValueError("Windows 上没有找到 bash（Git Bash / WSL），无法运行 .sh；"
                                 "可以改用 .bat，或把脚本内容转成批处理")
            return [bash, str(p)] + extra, "bash " + str(p)
    else:
        if kind == "sh":
            return ["/usr/bin/env", "bash", str(p)] + extra, "bash " + str(p)
        if kind == "py":
            return [shell_python(), str(p)] + extra, "python " + str(p)
        if kind in ("bat", "ps1"):
            raise ValueError("%s 是 Windows 脚本，当前系统跑不了"
                             % (".bat/.cmd" if kind == "bat" else ".ps1"))
    raise ValueError("不支持直接运行这个类型的文件（可以编辑查看，或改扩展名）")


def shell_python() -> str:
    import sys
    return sys.executable or "python"


def _find_bash() -> str:
    for n in ("bash.exe", "bash"):
        hit = shutil.which(n)
        if hit:
            return hit
    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files (x86)\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(cand).is_file():
            return cand
    return ""


def _runnable(kind: str) -> bool:
    """当前系统能不能直接跑这个类型（列表里给不可运行的打灰标）。"""
    if paths.IS_WINDOWS:
        if kind in ("bat", "ps1", "py"):
            return True
        if kind == "sh":
            return bool(_find_bash())
        return True          # 无扩展名/其他 → 交给 cmd 试
    return kind in ("sh", "py")


# ---------------------------------------------------------------- 列出

def list_dir(rel: str = "") -> dict:
    """列一个目录：子文件夹 + 脚本文件，带备注与子信息。"""
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        p = _resolve(rel)
    except ValueError as e:
        return _fail(str(e))
    if not p.is_dir():
        return _fail("不是目录：%s" % rel)

    meta = _load_meta()
    folders: List[dict] = []
    items: List[dict] = []
    try:
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except OSError as e:
        return _fail("读目录失败：%s" % e)
    for e in entries:
        if e.name.startswith("."):
            continue
        if e.is_dir():
            try:
                n = len([c for c in e.iterdir() if not c.name.startswith(".")])
            except OSError:
                n = 0
            folders.append({"name": e.name, "rel": _rel_of(e), "count": n,
                            "kind": "dir"})
            continue
        if not e.is_file():
            continue
        items.append(_item_of(e, meta.get(_rel_of(e)) or {}))

    return {
        "ok": True, "root": str(root), "rel": _rel_of(p),
        "name": p.name, "absPath": str(p),
        "crumbs": _crumbs(_rel_of(p)),
        "folders": folders, "items": items,
        "counts": {"folders": len(folders), "scripts": len(items)},
    }


def _crumbs(rel: str) -> List[dict]:
    out = [{"name": "脚本库", "rel": ""}]
    acc: List[str] = []
    for part in [x for x in (rel or "").split("/") if x]:
        acc.append(part)
        out.append({"name": part, "rel": "/".join(acc)})
    return out


def _item_of(p: Path, meta: dict) -> dict:
    kind = _kind_of(p.name)
    st = p.stat()
    text, enc = "", ""
    summary = ""
    lines = 0
    try:
        text, enc = _read_text(p)
        lines = len(text.splitlines())
        summary = _summary(text)
    except OSError:
        pass
    rel = _rel_of(p)
    return {
        "name": p.name, "rel": rel, "kind": kind,
        "kindText": KIND_LABEL.get(kind, "文本"),
        "ext": p.suffix.lower(), "size": st.st_size, "mtime": st.st_mtime,
        "lines": lines, "encoding": enc, "summary": summary,
        "note": meta.get("note", ""), "runs": int(meta.get("runs") or 0),
        "lastRun": meta.get("lastRun") or 0,
        "runnable": _runnable(kind), "absPath": str(p),
        "command": _preview_command(p, kind),
    }


def _preview_command(p: Path, kind: str) -> str:
    try:
        _argv, shown = _run_argv(p, kind, "")
        return shown
    except Exception as e:
        return "(不能在这里运行：%s)" % e


def folder_choices() -> List[dict]:
    """所有文件夹（「移到…」下拉用）。"""
    root = _root()
    out = [{"name": "脚本库（根）", "rel": ""}]
    try:
        for d in sorted(root.rglob("*"), key=lambda x: x.as_posix().lower()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if d.resolve() == root.resolve():
                continue
            out.append({"name": _rel_of(d), "rel": _rel_of(d)})
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- 增 / 改 / 删

def _unique_path(p: Path) -> Path:
    """重名就在名字后面加 -2 / -3，绝不覆盖已有脚本。"""
    if not p.exists():
        return p
    stem, suffix, parent = p.stem, p.suffix, p.parent
    i = 2
    while True:
        cand = parent / ("%s-%d%s" % (stem, i, suffix))
        if not cand.exists():
            return cand
        i += 1


def _safe_name(name: str) -> str:
    """文件名：去路径分隔符与控制字符。"""
    bad = '\\/:*?"<>|\r\n\t'
    out = "".join(("_" if c in bad or ord(c) < 32 else c) for c in str(name or "").strip())
    return out.strip(" .") or ""


def make_folder(parent_rel: str, name: str) -> dict:
    name = _safe_name(name)
    if not name:
        return _fail("请输入文件夹名")
    try:
        p = _resolve(parent_rel) / name
    except ValueError as e:
        return _fail(str(e))
    if p.exists():
        return _fail("已存在同名文件夹：%s" % name)
    try:
        p.mkdir(parents=True)
    except OSError as e:
        return _fail("创建失败：%s" % e)
    return {"ok": True, "name": name, "rel": _rel_of(p), "absPath": str(p)}


def _target_file(folder_rel: str, name: str, kind: str) -> Tuple[Path, str]:
    """算出 (目标路径, 最终文件名)；没写扩展名就按类别补一个。"""
    name = _safe_name(name)
    if not name:
        raise ValueError("请输入脚本名")
    if not Path(name).suffix:
        # 按类别补扩展名 —— 用户写"重启adb"就行，不必记得 .bat
        ext = {"bat": ".bat", "sh": ".sh", "ps1": ".ps1", "py": ".py"}.get(kind, "")
        name += ext
    return _resolve(folder_rel) / name, name


def create(folder_rel: str, name: str, kind: str = "bat", text: str = "") -> dict:
    """新建脚本。"""
    try:
        p, name = _target_file(folder_rel, name, kind)
    except ValueError as e:
        return _fail(str(e))
    if p.exists():
        return _fail("已存在同名脚本：%s" % name)
    body = text if text else TEMPLATES.get(_kind_of(name), "")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_text(p, body, "")
    except OSError as e:
        return _fail("创建失败：%s" % e)
    return {"ok": True, "name": name, "rel": _rel_of(p), "absPath": str(p),
            "item": _item_of(p, {})}


def add_existing(src: str, folder_rel: str = "", name: str = "") -> dict:
    """把本机已有脚本**复制**进库（原文件不动）。"""
    src_p = Path(str(src or ""))
    if not str(src or "").strip():
        return _fail("没有选择脚本文件")
    if not src_p.is_file():
        return _fail("文件不存在：%s" % src)
    inside = False
    try:
        inside = _root().resolve() in src_p.resolve().parents
    except Exception:
        inside = False
    if inside:
        return _fail("这个文件已经在脚本库里了（可直接编辑/重命名，不用再添加）")
    try:
        dest = _resolve(folder_rel) / (_safe_name(name) or src_p.name)
    except ValueError as e:
        return _fail(str(e))
    dest = _unique_path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_p), str(dest))
    except OSError as e:
        return _fail("复制失败：%s" % e)
    return {"ok": True, "name": dest.name, "rel": _rel_of(dest),
            "absPath": str(dest), "src": str(src_p), "item": _item_of(dest, {})}


def read_script(rel: str) -> dict:
    try:
        p = _resolve(rel)
    except ValueError as e:
        return _fail(str(e))
    if not p.is_file():
        return _fail("文件不存在：%s" % rel)
    try:
        text, enc = _read_text(p)
    except OSError as e:
        return _fail("读取失败：%s" % e)
    m = _meta_of(_rel_of(p))
    return {"ok": True, "name": p.name, "rel": _rel_of(p), "text": text,
            "encoding": enc, "kind": _kind_of(p.name), "note": m.get("note", ""),
            "size": p.stat().st_size, "absPath": str(p)}


def write_script(rel: str, text: str) -> dict:
    try:
        p = _resolve(rel)
    except ValueError as e:
        return _fail(str(e))
    if not p.is_file():
        return _fail("文件不存在：%s" % rel)
    try:
        _old, enc = _read_text(p)
    except OSError:
        enc = ""
    try:
        _write_text(p, str(text or ""), enc)
    except OSError as e:
        return _fail("保存失败：%s" % e)
    return {"ok": True, "rel": _rel_of(p), "name": p.name,
            "size": p.stat().st_size, "encoding": enc}


def _rename_meta(old_rel: str, new_rel: str) -> None:
    d = _load_meta()
    if old_rel in d:
        d[new_rel] = d.pop(old_rel)
        _save_meta(d)


def rename(rel: str, name: str) -> dict:
    name = _safe_name(name)
    if not name:
        return _fail("请输入新名字")
    try:
        p = _resolve(rel)
    except ValueError as e:
        return _fail(str(e))
    if not p.exists():
        return _fail("不存在：%s" % rel)
    dest = p.parent / name
    if p.is_dir():
        if dest.exists():
            return _fail("已存在同名文件夹：%s" % name)
    else:
        stem, suffix = Path(name).stem, Path(name).suffix
        if not suffix:                      # 改名时忘了扩展名 → 沿用原来的
            dest = p.parent / (stem + p.suffix)
    if dest != p and dest.exists():
        return _fail("已存在同名：%s" % dest.name)
    old_rel = _rel_of(p)
    try:
        p.rename(dest)
    except OSError as e:
        return _fail("重命名失败：%s" % e)
    _rename_meta(old_rel, _rel_of(dest))
    return {"ok": True, "rel": _rel_of(dest), "name": dest.name,
            "renamed": {"from": old_rel, "to": _rel_of(dest)}}


def move(rel: str, folder_rel: str) -> dict:
    """把脚本 / 文件夹移到另一个分类目录。"""
    try:
        p = _resolve(rel)
        d = _resolve(folder_rel)
    except ValueError as e:
        return _fail(str(e))
    if not p.exists():
        return _fail("不存在：%s" % rel)
    if not d.is_dir():
        return _fail("目标不是目录：%s" % folder_rel)
    if d.resolve() == p.parent.resolve():
        return {"ok": True, "rel": _rel_of(p), "moved": False, "name": p.name}
    dest = _unique_path(d / p.name) if (d / p.name).exists() else (d / p.name)
    old_rel = _rel_of(p)
    try:
        p.rename(dest)
    except OSError as e:
        return _fail("移动失败：%s" % e)
    _rename_meta(old_rel, _rel_of(dest))
    return {"ok": True, "rel": _rel_of(dest), "name": dest.name, "moved": True,
            "folder": _rel_of(d)}


def set_note(rel: str, note: str) -> dict:
    try:
        p = _resolve(rel)
    except ValueError as e:
        return _fail(str(e))
    if not p.exists():
        return _fail("不存在：%s" % rel)
    d = _load_meta()
    key = _rel_of(p)
    m = d.get(key) or {}
    m["note"] = str(note or "")
    m["updated"] = time.time()
    d[key] = m
    _save_meta(d)
    return {"ok": True, "rel": key, "note": m["note"]}


def delete(rel: str) -> dict:
    try:
        p = _resolve(rel)
    except ValueError as e:
        return _fail(str(e))
    if not p.exists():
        return _fail("不存在：%s" % rel)
    root = _root().resolve()
    if p.resolve() == root:
        return _fail("不能删除脚本库根目录")
    n = 1
    if p.is_dir():
        n = len([c for c in p.rglob("*") if c.is_file() and not c.name.startswith(".")])
        try:
            shutil.rmtree(str(p))
        except OSError as e:
            return _fail("删除失败：%s" % e)
        # 目录没了，它下面的备注一起清掉
        d = _load_meta()
        for k in [k for k in d if k == _rel_of(p) or k.startswith(_rel_of(p) + "/")]:
            d.pop(k)
        _save_meta(d)
    else:
        try:
            p.unlink()
        except OSError as e:
            return _fail("删除失败：%s" % e)
        _drop_meta(_rel_of(p))
    return {"ok": True, "rel": rel, "deleted": n, "isDir": p.is_dir() if p.exists() else False}


# ---------------------------------------------------------------- 运行

def run(rel: str, args: str = "", timeout: float = DEFAULT_TIMEOUT) -> dict:
    """跑一个脚本，把输出带回来。

    `cwd` 是**脚本所在目录** —— 脚本里写相对路径是常态（读同目录的配置、
    往同目录写日志），从库根跑会让这些相对路径全指向错地方。
    """
    try:
        p = _resolve(rel)
    except ValueError as e:
        return _fail(str(e))
    if not p.is_file():
        return _fail("文件不存在：%s" % rel)
    kind = _kind_of(p.name)
    try:
        argv, shown = _run_argv(p, kind, args)
    except ValueError as e:
        return _fail("无法运行：%s" % e)
    try:
        tmo = float(timeout or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        tmo = DEFAULT_TIMEOUT
    tmo = max(5.0, min(MAX_TIMEOUT, tmo))

    r = shell.run(argv, timeout=tmo, cwd=str(p.parent))
    out = (r.out or "")
    err = (r.err or "")
    merged = out if not err else (out + ("\n" if out and not out.endswith("\n") else "") + err)
    truncated = len(merged) > MAX_OUT
    if truncated:
        merged = merged[:MAX_OUT] + "\n…（输出过长，已截断）"

    # 跑一次就记一次：列表里的"跑过 N 次 / 上次 X" 全靠它
    d = _load_meta()
    key = _rel_of(p)
    m = d.get(key) or {}
    m["runs"] = int(m.get("runs") or 0) + 1
    m["lastRun"] = time.time()
    d[key] = m
    try:
        _save_meta(d)
    except Exception:
        pass

    return {
        "ok": bool(r.ok), "rc": r.rc, "name": p.name, "rel": key,
        "output": merged, "truncated": truncated,
        "command": shown + (" " + args if args else ""),
        "seconds": round(max(0.0, getattr(r, "duration", 0.0) or 0.0), 2),
        "timedOut": bool(getattr(r, "timed_out", False)),
        "error": r.exception or ("" if r.ok else "退出码 %s" % r.rc),
        "runs": m["runs"],
    }
