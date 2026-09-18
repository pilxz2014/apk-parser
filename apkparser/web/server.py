# -*- coding: utf-8 -*-
"""主控台 HTTP 服务。

设计要点：
  · **单实例**：启动前先探测端口/已有实例，已在跑就直接开页面，不重复起
  · **只绑 127.0.0.1**：本地工具，绝不监听 0.0.0.0
  · ThreadingHTTPServer：SSE 是长连接，必须多线程
  · 启动时写 `~/.apk-parser/server.json`（pid/port）与一次性 `token`
"""

import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config
from ..platform import paths, shell
from . import routes

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 64 * 1024 * 1024


# ---------------------------------------------------------------- 工具

def _is_port_free(port: int) -> bool:
    """端口是否真的空着。

    **绝对不要设 SO_REUSEADDR** —— 在 Windows 上它允许 bind 一个*已被别人监听*的端口，
    于是这个函数会永远返回 True，"端口被占"和"端口空闲"再也分不出来。
    （历史坑：原实现带 SO_REUSEADDR，靠后面 alive() 探活把问题盖住了；
    一旦加了"空端口就跳过探活"的快速路径，单实例探测立刻失效。）
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _read_server_file() -> dict:
    try:
        return json.loads(paths.server_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_server_file(port: int, token: str) -> None:
    try:
        f = paths.server_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": os.getpid(), "port": port, "token": token,
                   "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "cwd": str(paths.project_root())}
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)
    except Exception:
        pass

    # token 单独一份，给右键脚本读
    try:
        tf = paths.token_file()
        tf.parent.mkdir(parents=True, exist_ok=True)
        tf.write_text(token, encoding="utf-8")
        try:
            os.chmod(str(tf), 0o600)
        except OSError:
            pass
    except Exception:
        pass


def _http_get(url: str, timeout: float = 1.5) -> Optional[dict]:
    """探活请求。

    **必须显式绕过代理**：`urlopen` 默认会读 `HTTP_PROXY` 之类的环境变量，
    而本机常挂着代理（开发机、公司网络、CI 沙箱都有）。让 `127.0.0.1` 的请求
    绕到代理上，轻则白等一个超时，重则代理直接回 502 —— 于是"服务明明活着，
    却判定成没启动"。对回环地址来说走代理永远是错的，所以这里一律直连。
    """
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def alive(port: int) -> bool:
    """探测某个端口上是不是我们的服务在跑。"""
    d = _http_get("http://127.0.0.1:%d/api/health" % port)
    return bool(d and d.get("ok"))


def wait_port_free(port: int, timeout: float = 5.0) -> bool:
    """等端口释放（taskkill 之后 socket 归还需一点时间）。

    "重启"场景里，旧实例刚被 kill 就立刻 bind 同一端口，偶尔会撞上
    WSAEADDRINUSE —— 虽然有 allow_reuse_address 兜底，主动等一下更稳。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_port_free(port):
            return True
        time.sleep(0.2)
    return _is_port_free(port)


# ---------------------------------------------------------------- Handler

# 客户端提前断开时，socketserver 会在**读请求行**阶段就抛异常（还没进我们的
# 分发逻辑），然后由 handle_error 往控制台吐一整段 traceback。浏览器切页面、
# SSE 断流、curl 超时都会触发 —— 属正常现象，静音掉。
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                TimeoutError, socket.timeout)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # 保留 HTTPServer 的默认值（允许快速重启后重新绑定）。
    # 注意 Windows 上 SO_REUSEADDR 的语义是"允许抢占"，会让人误以为端口还空着 ——
    # 所以判空一律走 `_is_port_free()` 的真 bind 探测，不要拿这个字段当依据。
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, _CLIENT_GONE):
            return
        if os.environ.get("APKPARSER_HTTP_LOG"):
            ThreadingHTTPServer.handle_error(self, request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "apkparser"
    protocol_version = "HTTP/1.1"

    def handle_one_request(self) -> None:
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except _CLIENT_GONE:
            self.close_connection = True

    # -- 静音默认日志（我们要自己控制输出） --------------------------

    def log_message(self, fmt, *args):
        if os.environ.get("APKPARSER_HTTP_LOG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- 入口 --------------------------------------------------------

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def _handle(self, method: str) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)

            # "原始体"路由（目前只有上传）：**不**预读 body，把 rfile 交给 handler
            # 自己流式消费。必须这样 —— 预读路径有 MAX_BODY 上限，且会把 body
            # 当 JSON/表单解析，几百 MB 的 APK 走不通。
            raw_mode = path.startswith("/api/") and routes.wants_raw_body(method, path)
            body = self._raw_body() if raw_mode else self._read_body()

            try:
                if path.startswith("/api/"):
                    status, payload, ctype = routes.dispatch(method, path, query, body, dict(self.headers))
                    if ctype == "text/event-stream":
                        self._send_sse(payload)
                    elif isinstance(payload, bytes):
                        self._send(status, payload, ctype)
                    else:
                        self._send_json(status, payload)
                    return

            finally:
                # handler 万一只读了一半，把剩余字节读掉，否则下一个请求会被
                # 残留数据污染（HTTP/1.1 keep-alive 复用同一条连接）。
                if raw_mode and isinstance(body, routes.RawBody):
                    body.drain()

            if path.startswith("/files/"):
                self._serve_file(path)
                return

            self._serve_static(path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self._send_json(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
            except Exception:
                pass

    # -- 请求体 ------------------------------------------------------

    def _raw_body(self) -> "routes.RawBody":
        """不解析、不预读 —— 把 rfile 直接交给 handler（上传大文件用）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return routes.RawBody(self.rfile, max(0, length))

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            return {}
        raw = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "json" in ctype or raw[:1] in (b"{", b"["):
            try:
                return json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                return {}
        # 表单
        try:
            qs = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
            return {k: v[0] for k, v in qs.items()}
        except Exception:
            return {}

    # -- 响应 --------------------------------------------------------

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, status: int, body: bytes, ctype: str, extra: Dict[str, str] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_sse(self, stream) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()
        try:
            for event, data in stream.gen:
                self.wfile.write(routes.tasks_mod.sse_format(event, data))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            pass

    # -- 静态文件 ----------------------------------------------------

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send(403, b"forbidden", "text/plain")
            return
        if not target.is_file():
            # 单页应用：未知路径都回 index.html
            target = STATIC_DIR / "index.html"
        if not target.is_file():
            self._send(404, b"static not found", "text/plain")
            return
        self._send_file(target, cache=False)

    def _serve_file(self, path: str) -> None:
        """`/files/captures/x.png`、`/files/output/y.apk` —— 只允许产物目录。"""
        rel = path[len("/files/"):]
        allowed = {
            "captures": paths.captures_dir(),
            "output": paths.output_dir(),
            "logs": paths.logs_dir(),
        }
        parts = rel.split("/", 1)
        if len(parts) != 2 or parts[0] not in allowed:
            self._send(403, b"forbidden", "text/plain")
            return
        root = allowed[parts[0]].resolve()
        target = (root / parts[1]).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self._send(403, b"forbidden", "text/plain")
            return
        if not target.is_file():
            self._send(404, b"not found", "text/plain")
            return
        self._send_file(target, cache=True)

    def _send_file(self, target: Path, cache: bool) -> None:
        try:
            data = target.read_bytes()
        except OSError:
            self._send(404, b"read error", "text/plain")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------- 生命周期

_server: Optional[ThreadingHTTPServer] = None


def find_free_port(start: int = 5170, tries: int = 20) -> int:
    for i in range(tries):
        p = start + i
        if _is_port_free(p):
            return p
    return 0


def start(port: int = 0, open_browser: bool = True, foreground: bool = False) -> dict:
    """启动主控台。已有一个在跑就直接返回它。"""
    global _server

    cfg_port = int(config.get("port", 5170) or 5170)

    # 1) 已有实例？先用配置端口探一下
    #    注意：端口要是空着（能 bind 上），就**不可能**有实例在跑，不必浪费一次 HTTP 探活。
    #    这很重要 —— 某些环境（防火墙 / 沙箱）连回环的关闭端口都要 2s 才 refused，
    #    无脑探活会让"冷启动"凭空多等好几秒。
    for p in filter(None, [port, cfg_port, _read_server_file().get("port")]):
        p = int(p)
        if p <= 0 or _is_port_free(p):
            continue
        if alive(p):
            # 显式指定了**另一个**端口，且那个端口空着 —— 尊重调用方，另起一个
            # （自动化测试 / 多开对比都靠这条；不带 --port 时仍然复用旧实例）
            if port and int(port) != p and _is_port_free(int(port)):
                break
            base = "http://127.0.0.1:%d" % p
            if open_browser:
                shell.open_url(base)
            return {"ok": True, "alreadyRunning": True, "url": base, "port": p,
                    "detail": "主控台已在运行，直接打开页面"}

    # 2) 选端口
    want = int(port or cfg_port)
    if not _is_port_free(want):
        free = find_free_port(want + 1)
        if not free:
            return {"ok": False, "detail": "端口 %d 及其后 20 个端口都被占用，请用 --port 指定" % want}
        detail_port = free
    else:
        detail_port = want

    token = secrets.token_urlsafe(24)

    try:
        httpd = Server(("127.0.0.1", detail_port), Handler)
    except OSError as e:
        return {"ok": False, "detail": "无法绑定 127.0.0.1:%d —— %s" % (detail_port, e)}

    _server = httpd
    _write_server_file(detail_port, token)

    url = "http://127.0.0.1:%d" % detail_port
    if open_browser:
        threading.Timer(0.6, lambda: shell.open_url(url)).start()

    if foreground:
        print("主控台已启动：%s" % url)
        print("按 Ctrl+C 停止")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            _cleanup()
        return {"ok": True, "url": url, "port": detail_port, "foreground": True}

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return {"ok": True, "url": url, "port": detail_port, "token": token,
            "pid": os.getpid(), "detail": "主控台已启动"}


def _cleanup() -> None:
    try:
        f = paths.server_file()
        if f.is_file():
            data = json.loads(f.read_text(encoding="utf-8"))
            if int(data.get("pid") or 0) == os.getpid():
                f.unlink()
    except Exception:
        pass


def cli_start(port: int = 0, open_browser: bool = True, foreground: bool = False) -> int:
    """`web start` 的实现（后台模式需要保持进程存活）。"""
    from ..ui import console

    res = start(port=port, open_browser=open_browser, foreground=foreground)
    if not res.get("ok"):
        console.error(res.get("detail", "启动失败"))
        return 1

    if res.get("alreadyRunning"):
        console.ok(res["detail"])
        console.info(res["url"])
        return 0

    console.ok("主控台已启动 → %s" % res["url"])
    console.hint("按 Ctrl+C 停止；或在另一个窗口执行 apktool web stop")

    if foreground:
        return 0

    # 后台模式：主线程保持存活（daemon 线程会随主线程退出）
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        console.warn("正在停止…")
        stop()
    return 0


def stop() -> dict:
    """停止主控台。

    默认停掉**所有**本工具实例，不只是 server.json 里记的那个 —— 原因见
    `stop_all()`。绝大多数场景下用户说"停止主控台"就是不想让任何一个跑着。
    """
    res = stop_all()
    if res["stopped"]:
        bits = "、".join(":%d(pid %s)" % (s["port"], s["pid"]) for s in res["stopped"])
        return {"ok": True, "detail": "已停止主控台 %s" % bits,
                "stopped": res["stopped"]}
    if res["failed"]:
        bits = "、".join(":%d(pid %s)" % (s["port"], s["pid"]) for s in res["failed"])
        return {"ok": False, "detail": "以下实例停不掉（权限不足？）：%s" % bits,
                "failed": res["failed"]}
    _cleanup_quiet()
    return {"ok": False, "detail": "主控台没有在运行"}


def _health(port: int, timeout: float = 1.5) -> Optional[dict]:
    """读某端口上服务的 /api/health；不是本工具就返回 None。"""
    d = _http_get("http://127.0.0.1:%d/api/health" % port, timeout=timeout)
    return d if (d and d.get("ok")) else None


def _post_shutdown(port: int, timeout: float = 5.0) -> Optional[dict]:
    """请某个实例自己退出（走它自己的 /api/sys/shutdown）。"""
    import urllib.request
    try:
        # 回环一律绕代理 —— 环境里的 HTTP_PROXY 会把 127.0.0.1 的请求送出去
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/sys/shutdown" % port, data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def stop_all(extra_ports=None) -> dict:
    """停掉所有本工具的主控台实例。

    **为什么需要**：`server.json` 只记**最后一个**启动的实例。用不同端口起过
    多个（测试、或手滑带不同 --port）时，其余实例就成了"孤儿"，`stop()` 找不到
    它们。孤儿有个很阴的后果 —— `start()` 探到端口上"已有实例"就**复用并直接
    返回**，于是用户以为换上了新代码重启过，实际连的还是旧进程（这个坑真踩过：
    新加的路由在旧实例上返回 500）。

    识别靠 `/api/health` 自报的 pid（比 pid 文件可信），关停优先走
    `/api/sys/shutdown` 请它自己退，失败才 taskkill。
    """
    cfg_port = int(config.get("port", 5170) or 5170)
    sf = _read_server_file()
    ports: List[int] = []
    for p in [cfg_port, int(sf.get("port") or 0)] + list(extra_ports or []) \
            + list(range(5170, 5180)):
        p = int(p or 0)
        if p > 0 and p not in ports:
            ports.append(p)

    stopped, failed, found = [], [], []
    me = os.getpid()
    for p in ports:
        # 端口空着（能 bind 上）就**不可能**有实例在跑，HTTP 探活直接跳过。
        # 必须先 bind 再探活：某些环境下回环的关闭端口要 1.5s+ 才 refused，
        # 无脑探活十几个端口 = 冷启动白等十几秒（start() 里同样修过这个坑）。
        if _is_port_free(p):
            continue
        h = _health(p)
        if not h:
            continue
        pid = int(h.get("pid") or 0)
        found.append({"port": p, "pid": pid})
        if pid and pid == me:
            continue                       # 别关自己
        if _post_shutdown(p) is not None:
            stopped.append({"port": p, "pid": pid, "via": "http"})
            continue
        if pid and _kill_pid(pid):
            stopped.append({"port": p, "pid": pid, "via": "taskkill"})
        else:
            failed.append({"port": p, "pid": pid})

    if stopped:
        time.sleep(0.6)                    # 让端口真正释放
    _cleanup_quiet()
    return {"stopped": stopped, "failed": failed, "found": found}


def request_shutdown() -> dict:
    """停止**当前进程里的**服务（Web 界面上的"停止主控台"）。

    先回响应、再关服务 —— 所以真正干活的是延迟线程。
    后台模式下主线程卡在 `while True: sleep(1)` 里，光 `httpd.shutdown()`
    并不会让进程退出，这里用 `os._exit(0)` 干脆了结。
    """
    global _server
    httpd = _server
    if httpd is None:
        return {"ok": False, "detail": "当前进程没有在跑服务"}

    def _later() -> None:
        time.sleep(0.5)              # 留时间把 HTTP 响应写完
        try:
            httpd.shutdown()
        except Exception:
            pass
        _cleanup()
        time.sleep(0.3)
        os._exit(0)

    threading.Thread(target=_later, daemon=True).start()
    return {"ok": True, "detail": "主控台正在停止"}


def _kill_pid(pid: int) -> bool:
    if paths.IS_WINDOWS:
        r = shell.run(["taskkill", "/PID", str(pid), "/F"], timeout=15)
        return r.rc == 0
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _cleanup_quiet() -> None:
    try:
        f = paths.server_file()
        if f.is_file():
            f.unlink()
    except Exception:
        pass


def status() -> dict:
    data = _read_server_file()
    port = int(data.get("port") or 0)
    # 先 bind 再探活：端口空着就不必花 1.5s 等一次注定失败的 HTTP 请求
    running = bool(port and not _is_port_free(port) and alive(port))
    return {
        "running": running,
        "pid": data.get("pid"),
        "port": port,
        "url": ("http://127.0.0.1:%d" % port) if port else "",
        "started": data.get("started"),
        "stale": bool(data and not running),
    }
