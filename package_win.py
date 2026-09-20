#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Windows 打包脚本 —— 用法：python package_win.py

产物：dist/APKConsole/APKConsole.exe（onedir，双击即用）+ dist/APKConsole-win.zip

要点：
  · onedir 而不是 onefile：onefile 每次启动要解压到临时目录，白白多几秒
    （v0.2.5 刚把启动从十几秒修到几秒，不能在打包这步还回去）
  · 打包前自动补齐构建依赖（pyinstaller / pystray / Pillow）到当前解释器
  · 打包后默认做一次冒烟：静默拉起 exe，探 /api/health（--no-smoke 跳过）
"""

import os
import subprocess
import sys
import time
import urllib.request
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = "APKConsole"
DEPS = ["pyinstaller>=6.0", "pystray>=0.19", "pillow>=9.0"]
DIST = ROOT / "dist"
# 发行说明书：唯一真相是 docs/ 里那份，打包时复制成 exe 同级的 使用说明.txt
README_SRC = ROOT / "docs" / "05-分发与使用说明.md"
README_NAME = "使用说明.txt"


def copy_readme(dest_dir: Path) -> bool:
    """把说明书放进发行目录（UTF-8 **带 BOM** —— Windows 记事本不猜编码，
    没 BOM 的老版本会把中文显示成乱码）。"""
    try:
        text = README_SRC.read_text(encoding="utf-8")
    except OSError as e:
        print("[X] 读不到说明书 %s：%s" % (README_SRC, e))
        return False
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / README_NAME).write_text(text, encoding="utf-8-sig", newline="\r\n")
    print("[..] 已放入说明书：%s" % (dest_dir / README_NAME))
    return True


def fail(msg: str) -> "int":
    print("[X] " + msg)
    return 1


def ensure_deps() -> bool:
    missing = []
    for spec in DEPS:
        mod = {"pyinstaller": "PyInstaller", "pystray": "pystray",
               "pillow": "PIL"}[spec.split(">")[0].lower()]
        try:
            __import__(mod)
        except ImportError:
            missing.append(spec)
    if not missing:
        return True
    print("[..] 安装构建依赖：%s" % ", ".join(missing))
    r = subprocess.run([sys.executable, "-m", "pip", "install", *missing])
    return r.returncode == 0


def make_icon() -> str:
    """程序化生成 icon.ico（免维护一张二进制资源）。"""
    from PIL import Image, ImageDraw, ImageFont

    out = ROOT / "assets" / "icon.ico"
    out.parent.mkdir(exist_ok=True)
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([10, 10, 246, 246], radius=56, fill=(34, 197, 94, 255))
    try:
        font = ImageFont.load_default(size=96)
    except TypeError:
        font = ImageFont.load_default()
    d.text((128, 132), "APK", font=font, fill=(255, 255, 255, 255), anchor="mm")
    img.save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return str(out)


def build() -> int:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--windowed", "--onedir", "--name", APP,
           "--add-data", os.path.join("apkparser", "web", "static")
                         + ";" + os.path.join("apkparser", "web", "static"),
           "--collect-all", "pystray",
           str(ROOT / "APKConsole.py")]
    ico = make_icon()
    if ico:
        cmd += ["--icon", ico]
    print("[..] PyInstaller 构建中…")
    r = subprocess.run(cmd, cwd=str(ROOT))
    return r.returncode


def smoke() -> bool:
    """静默拉起 exe → 探活 → 单例（再起一个应即退）→ 停掉。"""
    exe = DIST / APP / (APP + ".exe")
    if not exe.is_file():
        print("[X] 找不到产物：%s" % exe)
        return False
    env = dict(os.environ, APKCONSOLE_NO_OPEN="1")
    port = 5177                              # 避开常驻的 5170
    p1 = subprocess.Popen([str(exe), "--port", str(port)], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ok = False
    for _ in range(40):
        try:
            with op.open("http://127.0.0.1:%d/api/health" % port, timeout=2) as r:
                d = json.loads(r.read().decode("utf-8"))
                ok = bool(d.get("ok"))
                break
        except Exception:
            time.sleep(0.5)
    print("[..] 打包版 /api/health：%s" % ("OK" if ok else "FAIL"))
    # 单例：第二个实例应在几秒内自己退出
    p2 = subprocess.Popen([str(exe), "--port", str(port)], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        p2.wait(timeout=10)
        singleton = True
    except subprocess.TimeoutExpired:
        p2.kill()
        singleton = False
    print("[..] 第二实例自动退出（单例）：%s" % ("OK" if singleton else "FAIL"))
    # 收尾：整树 kill 托盘进程（GUI 循环没法从外部优雅退出）
    subprocess.run(["taskkill", "/PID", str(p1.pid), "/T", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ok and singleton


def main() -> int:
    if not sys.platform.startswith("win"):
        return fail("本脚本只能在 Windows 上运行（mac 用 package_mac.py）")
    if not ensure_deps():
        return fail("构建依赖安装失败，请手动：pip install " + " ".join(DEPS))
    if build() != 0:
        return fail("PyInstaller 构建失败，看上面的报错")
    exe = DIST / APP / (APP + ".exe")
    if not exe.is_file():
        return fail("构建完成但找不到 %s" % exe)
    if "--no-smoke" not in sys.argv:
        smoke()
    if not copy_readme(exe.parent):
        return fail("说明书没能放进发行目录")
    zip_base = str(DIST / (APP + "-win"))
    shutil.make_archive(zip_base, "zip", root_dir=str(DIST), base_dir=APP)
    print()
    print("[OK] 打包完成：")
    print("  程序目录：%s" % exe.parent)
    print("  压缩包  ：%s.zip" % zip_base)
    print("  双击 APKConsole.exe → 托盘常驻 + 浏览器打开主控台")
    return 0


if __name__ == "__main__":
    sys.exit(main())
