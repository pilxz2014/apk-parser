#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""macOS 打包脚本 —— 用法（在 mac 上）：python3 package_mac.py

产物：dist/APKConsole.app + dist/APKConsole-mac.zip

要点：
  · --windowed 让 PyInstaller 生成 .app 包（有 Dock 图标、可双击）
  · add-data 分隔符在 posix 上是冒号（Windows 才是分号）
  · pystray 的 macOS 后端依赖 PyObjC，构建依赖里显式装上
  · 打包后做一次 ad-hoc 签名（尽力而为），减少 Gatekeeper 的"已损坏"提示
"""

import os
import subprocess
import sys
import time
import urllib.request
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = "APKConsole"
DEPS = ["pyinstaller>=6.0", "pystray>=0.19", "pillow>=9.0",
        "pyobjc-framework-Cocoa>=9.0"]
DIST = ROOT / "dist"
# 发行说明书：唯一真相是 docs/ 里那份，打包时复制一份进 zip
README_SRC = ROOT / "docs" / "05-分发与使用说明.md"
README_NAME = "使用说明.txt"


def copy_readme(dest_dir: Path) -> "Path | None":
    """把说明书放到 dist（随后一起打进 zip）。mac 用纯 UTF-8、LF 换行。"""
    try:
        text = README_SRC.read_text(encoding="utf-8")
    except OSError as e:
        print("[X] 读不到说明书 %s：%s" % (README_SRC, e))
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / README_NAME
    out.write_text(text, encoding="utf-8", newline="\n")
    print("[..] 已放入说明书：%s" % out)
    return out


def fail(msg: str) -> int:
    print("[X] " + msg)
    return 1


def ensure_deps() -> bool:
    missing = []
    for spec in DEPS:
        mod = {"pyinstaller": "PyInstaller", "pystray": "pystray",
               "pillow": "PIL", "pyobjc-framework-cocoa": "AppKit"}[
                   spec.split(">")[0].lower()]
        try:
            __import__(mod)
        except ImportError:
            missing.append(spec)
    if not missing:
        return True
    print("[..] 安装构建依赖：%s" % ", ".join(missing))
    r = subprocess.run([sys.executable, "-m", "pip", "install", *missing])
    return r.returncode == 0


def build() -> int:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--windowed", "--onedir", "--name", APP,
           "--add-data", os.path.join("apkparser", "web", "static")
                         + ":" + os.path.join("apkparser", "web", "static"),
           "--collect-all", "pystray",
           str(ROOT / "APKConsole.py")]
    print("[..] PyInstaller 构建中…")
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def adhoc_sign() -> None:
    """ad-hoc 签名（尽力而为）。没有开发者证书时这能减少 Gatekeeper 拦截。"""
    app = DIST / (APP + ".app")
    if not app.is_dir():
        return
    r = subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(app)],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print("[..] ad-hoc 签名：OK")
    else:
        print("[..] ad-hoc 签名：跳过（无 codesign 或失败，不影响产物）")


def smoke() -> bool:
    """静默拉起 .app 内的二进制 → 探活 → 单例。GUI 托盘部分仍需人工确认。"""
    exe = DIST / (APP + ".app") / "Contents" / "MacOS" / APP
    if not exe.is_file():
        print("[X] 找不到产物：%s" % exe)
        return False
    env = dict(os.environ, APKCONSOLE_NO_OPEN="1")
    port = 5177
    p1 = subprocess.Popen([str(exe), "--port", str(port)], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ok = False
    for _ in range(40):
        try:
            with op.open("http://127.0.0.1:%d/api/health" % port, timeout=2) as r:
                ok = bool(json.loads(r.read().decode("utf-8")).get("ok"))
                break
        except Exception:
            time.sleep(0.5)
    print("[..] 打包版 /api/health：%s" % ("OK" if ok else "FAIL"))
    p2 = subprocess.Popen([str(exe), "--port", str(port)], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        p2.wait(timeout=10)
        singleton = True
    except subprocess.TimeoutExpired:
        p2.kill()
        singleton = False
    print("[..] 第二实例自动退出（单例）：%s" % ("OK" if singleton else "FAIL"))
    p1.terminate()
    return ok and singleton


def main() -> int:
    if sys.platform != "darwin":
        return fail("本脚本只能在 macOS 上运行（Windows 用 package_win.py）")
    if not ensure_deps():
        return fail("构建依赖安装失败，请手动：pip3 install " + " ".join(DEPS))
    if build() != 0:
        return fail("PyInstaller 构建失败，看上面的报错")
    app = DIST / (APP + ".app")
    if not app.is_dir():
        return fail("构建完成但找不到 %s" % app)
    adhoc_sign()
    if "--no-smoke" not in sys.argv:
        smoke()
    readme = copy_readme(DIST)
    zip_path = "%s.zip" % (DIST / (APP + "-mac"))
    shutil.make_archive(zip_path[:-4], "zip", root_dir=str(DIST),
                        base_dir=APP + ".app")
    if readme:                              # 说明书挂在 zip 里 .app 的旁边
        with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as z:
            z.write(str(readme), arcname=README_NAME)
    print()
    print("[OK] 打包完成：")
    print("  应用包  ：%s" % app)
    print("  压缩包  ：%s" % zip_path)
    print("  双击 APKConsole.app → 托盘常驻（菜单栏右上角）+ 浏览器打开主控台")
    print("  注：产物未公证，首次打开如被拦，右键 → 打开，或到 系统设置-隐私与安全性 放行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
