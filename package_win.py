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

# 发行包里的菜单薄壳：**不调 python / apktool.py**（绿色包里都没有），
# 改成调同目录的 apkcli.exe（console 子系统，和托盘共用 _internal）。
# 必须纯 ASCII —— cmd.exe 按 OEM 代码页读批处理，非 ASCII 会乱码。
DIST_APK_CMD = """@echo off
rem Packaged build: the exe ships a CLI mode, so no Python / apktool.py needed.
rem Called by every apk-*.cmd wrapper. Keep this file ASCII-only.
chcp 65001 >nul
setlocal
"%~dp0..\\apkcli.exe" %*
exit /b %ERRORLEVEL%
"""

DIST_CONSOLE_CMD = """@echo off
rem Right-click "Open console": start the tray host (single instance just opens the page).
start "" "%~dp0..\\APKConsole.exe"
"""

# 根目录一键启停（exe 同级）。主控台的「系统」页会核对这 4 个文件，缺了就标红。
# 打包版没有 python / apktool.py，启停全部改走 apkcli.exe / 托盘 exe：
#   启动 = 先请旧实例退出（web stop 对所有本工具实例生效），再拉起托盘；
#   停止 = apkcli.exe web stop（health 探活 5170-5179，发 /api/sys/shutdown）。
# chcp 65001：apkcli.exe 输出 UTF-8（✔ / 中文），GBK 代码页的窗口会乱码。
# 必须纯 ASCII。
DIST_START_CMD = """@echo off
rem Packaged build: stop any running instance first, then start the tray host.
rem (Same restart semantics as the source-tree start.bat. Keep this ASCII-only.)
chcp 65001 >nul
"%~dp0apkcli.exe" web stop >nul 2>nul
start "" "%~dp0APKConsole.exe"
"""

DIST_STOP_CMD = """@echo off
rem Packaged build: stop the console (all instances of this tool).
chcp 65001 >nul
"%~dp0apkcli.exe" web stop
echo.
pause
"""

# 两个入口、一份 _internal：托盘是 --windowed，CLI 必须是 console 子系统，
# 否则右键脚本跑起来一行日志都看不见（print 写到 None）。
# 分两次调用 PyInstaller 做不出"共享 _internal"，只能走 spec。
SPEC_TEMPLATE = '''# -*- mode: python ; coding: utf-8 -*-
# 由 package_win.py 生成，不要手改（下次打包会被覆盖）。
from PyInstaller.utils.hooks import collect_all

ROOT = r"@ROOT@"
ICON = r"@ICON@"

pystray_datas, pystray_binaries, pystray_hidden = collect_all("pystray")
STATIC = [(ROOT + "/apkparser/web/static", "apkparser/web/static")]

tray_a = Analysis(
    [ROOT + "/APKConsole.py"],
    pathex=[ROOT],
    binaries=pystray_binaries,
    datas=STATIC + pystray_datas,
    hiddenimports=pystray_hidden,
)
cli_a = Analysis(
    [ROOT + "/apkcli.py"],
    pathex=[ROOT],
    binaries=[],
    datas=STATIC,
    hiddenimports=[],
)

tray_pyz = PYZ(tray_a.pure)
cli_pyz = PYZ(cli_a.pure)

tray_exe = EXE(
    tray_pyz, tray_a.scripts, [],
    exclude_binaries=True, name="APKConsole", console=False, icon=ICON,
    debug=False, strip=False, upx=False, bootloader_ignore_signals=False,
)
cli_exe = EXE(
    cli_pyz, cli_a.scripts, [],
    exclude_binaries=True, name="apkcli", console=True, icon=ICON,
    debug=False, strip=False, upx=False, bootloader_ignore_signals=False,
)

coll = COLLECT(
    tray_exe, tray_a.binaries, tray_a.datas,
    cli_exe, cli_a.binaries, cli_a.datas,
    strip=False, upx=False, name="APKConsole",
)
'''


def write_spec(icon: str) -> Path:
    spec = ROOT / (APP + ".spec")
    text = (SPEC_TEMPLATE
            .replace("@ROOT@", str(ROOT))
            .replace("@ICON@", icon or ""))
    spec.write_text(text, encoding="utf-8", newline="\n")
    return spec


def copy_scripts(dest_dir: Path) -> bool:
    """把右键菜单要执行的脚本放进发行目录（exe 同级 scripts/）。

    这是 v0.2.14 修的真问题：以前打包不带 scripts/，主控台在发行版里
    照样能"安装菜单"，注册表项全指向不存在的脚本 —— 双击 .apk 直接弹
    "你要以何种方式打开此文件"。脚本进包 + apk.cmd 改调 exe 才闭环。
    """
    src = ROOT / "scripts"
    dst = dest_dir / "scripts"
    names = sorted(p.name for p in src.glob("*.cmd"))
    if not names:
        print("[X] 源码里没有 scripts/*.cmd，菜单注册出来会是死项")
        return False
    dst.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy2(src / name, dst / name)
    # 覆盖成打包版：调 exe 而不是 python
    (dst / "apk.cmd").write_text(DIST_APK_CMD, encoding="ascii", newline="\r\n")
    (dst / "apk-console.cmd").write_text(DIST_CONSOLE_CMD, encoding="ascii", newline="\r\n")
    # 根目录一键启停（主控台「系统」页会核对这 4 个）
    (dest_dir / "start.bat").write_text(DIST_START_CMD, encoding="ascii", newline="\r\n")
    (dest_dir / "run-web.cmd").write_text(DIST_START_CMD, encoding="ascii", newline="\r\n")
    (dest_dir / "stop.bat").write_text(DIST_STOP_CMD, encoding="ascii", newline="\r\n")
    (dest_dir / "stop-web.cmd").write_text(DIST_STOP_CMD, encoding="ascii", newline="\r\n")
    print("[..] 已放入菜单脚本：%s（%d 个，已改为调 apkcli.exe）" % (dst, len(names)))
    print("[..] 已放入一键启停：start.bat / stop.bat / run-web.cmd / stop-web.cmd（exe 同级）")
    return True


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


def stop_running() -> None:
    """打包前先停掉正在跑的 APKConsole.exe。

    不这么做的话 COLLECT 会**先清空 dist/APKConsole/ 再重建**：它删得掉
    _internal 下的资源，却覆盖不了正在运行的 exe，构建中途失败。结果是
    exe 还在跑、静态文件却已经没了 —— 页面上只剩一句 "static not found"，
    跟"打包其实失败了"这个真正的原因离得很远，很难查。
    """
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s.exe" % APP, "/NH"],
                           capture_output=True, text=True, errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return
    if (APP + ".exe") not in (r.stdout or ""):
        return
    print("[..] 检测到 %s.exe 正在运行，先停掉（不停的话打包会失败）" % APP)
    subprocess.run(["taskkill", "/IM", APP + ".exe", "/T", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)


def build() -> int:
    ico = make_icon()
    spec = write_spec(ico)
    print("[..] PyInstaller 构建中（APKConsole.exe + apkcli.exe，共享 _internal）…")
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                        str(spec)], cwd=str(ROOT))
    return r.returncode


def smoke() -> bool:
    """静默拉起 exe → 探活 → 单例（再起一个应即退）→ 停掉；顺带验 CLI。"""
    exe = DIST / APP / (APP + ".exe")
    if not exe.is_file():
        print("[X] 找不到产物：%s" % exe)
        return False
    cli = DIST / APP / "apkcli.exe"
    cli_ok = False
    if cli.is_file():
        r = subprocess.run([str(cli), "--version"], capture_output=True, text=True,
                           errors="replace", timeout=60)
        cli_ok = r.returncode == 0 and "apktool" in (r.stdout or "")
    print("[..] CLI 入口 apkcli.exe：%s" % ("OK" if cli_ok else "FAIL"))
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
    return ok and singleton and cli_ok


def main() -> int:
    if not sys.platform.startswith("win"):
        return fail("本脚本只能在 Windows 上运行（mac 用 package_mac.py）")
    if not ensure_deps():
        return fail("构建依赖安装失败，请手动：pip install " + " ".join(DEPS))
    stop_running()
    if build() != 0:
        return fail("PyInstaller 构建失败，看上面的报错")
    exe = DIST / APP / (APP + ".exe")
    if not exe.is_file():
        return fail("构建完成但找不到 %s" % exe)
    if "--no-smoke" not in sys.argv:
        smoke()
    if not copy_readme(exe.parent):
        return fail("说明书没能放进发行目录")
    if not copy_scripts(exe.parent):
        return fail("菜单脚本没能放进发行目录（右键菜单注册出来会是死项）")
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
