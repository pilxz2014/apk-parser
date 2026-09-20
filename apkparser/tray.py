# -*- coding: utf-8 -*-
"""托盘宿主 —— 打包版（APKConsole.exe / APKConsole.app）的主程序。

交互设计（用户原话："只要双击，就可以启动在后台服务，同时有个菜单挂在
底部工具栏，可以右键菜单退出……双击启动的时候发现已经启动了就不要再次
启动服务"）：

  · 双击 → 本进程内起 Web 服务（后台线程），托盘常驻
  · 托盘右键：状态 / 打开主控台 / 重启服务 / 启动·停止 / 退出
  · 端口上已有实例 → 不再起服务，打开页面后**本进程直接退出**
    （单例职责在第一个进程手里；第二次双击的反馈就是浏览器打开主控台）

依赖：pystray + Pillow（仅托盘展示用；核心解析仍然零第三方依赖）。
两个包不可用时优雅降级成控制台常驻（源码环境调试用）。
"""

import os
import sys
import threading

from apkparser.platform import shell
from apkparser.web import server

# 状态只在托盘线程与动作回调间读写，用 dict 免去 global 声明
STATE = {"running": False, "port": 0, "url": "", "error": ""}


# ---------------------------------------------------------------- 服务控制

def start_service(port: int = 0, open_browser: bool = False) -> dict:
    """在本进程内把服务跑起来（start() 内部用 daemon 线程，不阻塞）。"""
    res = server.start(port=port, open_browser=open_browser, foreground=False)
    if res.get("ok") and not res.get("alreadyRunning"):
        STATE.update(running=True, port=res["port"], url=res["url"], error="")
    elif res.get("alreadyRunning"):
        STATE.update(running=True, port=res["port"], url=res["url"], error="")
    else:
        STATE.update(running=False, error=res.get("detail", "启动失败"))
    return res


def restart_service(port: int = 0) -> dict:
    res = server.restart_local(port=port, open_browser=False)
    if res.get("ok"):
        STATE.update(running=True, port=res["port"], url=res["url"], error="")
    else:
        STATE.update(running=False, error=res.get("detail", "重启失败"))
    return res


def stop_service() -> dict:
    res = server.stop_local()
    if res.get("ok"):
        STATE.update(running=False, error="")
    return res


# ---------------------------------------------------------------- 托盘

def _icon_image(running: bool):
    """程序化生成图标（绿=运行中，灰=已停止），不依赖外部图片资源。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([3, 3, 61, 61], radius=14,
                        fill=(34, 197, 94, 255) if running else (148, 163, 184, 255))
    try:
        font = ImageFont.load_default(size=22)
    except TypeError:                      # 老版 Pillow 不支持 size 参数
        font = ImageFont.load_default()
    d.text((32, 33), "APK", font=font, fill=(255, 255, 255, 255), anchor="mm")
    return img


def _refresh(icon) -> None:
    """按 STATE 同步图标外观与提示（菜单文本用 callable 动态取，无需刷）。"""
    icon.icon = _icon_image(STATE["running"])
    icon.title = _status_text()


def _status_text(_icon=None) -> str:
    if STATE["running"]:
        return "运行中 · %s" % (STATE["url"] or "http://127.0.0.1:%d" % STATE["port"])
    if STATE["error"]:
        return "已停止（%s）" % STATE["error"]
    return "已停止"


def _notify(icon, msg: str) -> None:
    try:
        icon.notify(msg, "APK 主控台")
    except Exception:
        pass                                # 个别后端不支持通知，静默


def _open_console(icon=None, item=None) -> None:
    url = STATE.get("url")
    if not url or not STATE["running"]:
        res = start_service(port=STATE.get("port") or 0)
        url = res.get("url") or ""
        if icon is not None:
            _refresh(icon)
    if url:
        shell.open_url(url)


def _act_restart(icon, item) -> None:
    res = restart_service(port=0)
    _refresh(icon)
    _notify(icon, res.get("detail") or ("已重启 → %s" % res.get("url", "")))


def _act_start(icon, item) -> None:
    res = start_service(port=0)
    _refresh(icon)
    _notify(icon, res.get("detail") or "服务已启动")


def _act_stop(icon, item) -> None:
    res = stop_service()
    _refresh(icon)
    _notify(icon, res.get("detail") or "服务已停止")


def _act_quit(icon, item) -> None:
    stop_service()                          # 托盘进程退出时带走自己的服务
    icon.stop()


def build_menu():
    import pystray

    # visible 传 callable：每次展开菜单时按 STATE 重新求值（pystray 支持）
    return pystray.Menu(
        pystray.MenuItem(_status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("打开主控台", _open_console, default=True),
        pystray.MenuItem("重启服务", _act_restart),
        pystray.MenuItem("启动服务", _act_start, visible=lambda icon: not STATE["running"]),
        pystray.MenuItem("停止服务", _act_stop, visible=lambda icon: STATE["running"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _act_quit),
    )


def run_tray(port: int) -> int:
    import pystray

    icon = pystray.Icon("APKConsole", _icon_image(STATE["running"]),
                        _status_text(), build_menu())

    def _setup(icon_) -> None:
        # 自定义 setup **必须自己点亮 visible**（pystray 只有在默认 setup 里才会设），
        # 否则 _show()/NIM_ADD 永远不会被调用 —— 进程活着、消息循环在转，
        # 托盘却静默地什么都不显示（v0.2.6 实际踩过的坑，见 docs §5.6）。
        icon_.visible = True
        _refresh(icon_)
        if STATE["running"]:
            _notify(icon_, "服务已启动 · %s" % STATE["url"])
        else:
            _notify(icon_, STATE["error"] or "服务未启动")

    icon.run(setup=_setup)                  # 阻塞直到 退出
    return 0


def run_console(port: int) -> int:
    """降级：没有托盘环境（缺 pystray/Pillow、或无 GUI 会话）时控制台常驻。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if STATE["running"]:
        print("主控台已启动：%s" % STATE["url"])
        print("（托盘不可用，此窗口即服务；Ctrl+C 停止）")
    else:
        print("服务启动失败：%s" % STATE["error"])
        return 1
    try:
        threading.Event().wait()            # 等价 while True，Ctrl+C 可中断
    except KeyboardInterrupt:
        stop_service()
        print("已停止")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = 0
    if "--port" in argv:                    # 够用就好：--port N（数字跟在后面）
        i = argv.index("--port")
        if i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                port = 0

    # 单例判定：端口上已有本工具实例 → 不起服务，打开页面后本进程退出，
    # 单例职责留在第一个进程（那边有托盘）。
    no_open = bool(os.environ.get("APKCONSOLE_NO_OPEN"))   # 自动化测试用
    res = start_service(port=port, open_browser=False)
    if res.get("alreadyRunning"):
        if not no_open:
            shell.open_url(res["url"])
        return 0
    if not res.get("ok"):
        # 起不来也把托盘挂上（状态=已停止），用户可以从菜单里重启
        pass

    try:
        return run_tray(port)
    except Exception:
        # pystray/Pillow 缺失或 GUI 会话不可用 → 控制台常驻
        try:
            return run_console(port)
        except Exception:
            return 1


if __name__ == "__main__":
    sys.exit(main())
