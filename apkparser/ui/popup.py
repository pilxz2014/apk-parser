# -*- coding: utf-8 -*-
"""轻弹窗 —— 右键 `.apk` 的信息卡片。

设计要点（对应需求「秒开秒关、零常驻」）：
  · 主控台**在跑** → 把卡片推给它，用模态框展示（多几个"下一步"按钮）
  · 主控台**没跑** → 生成一张自包含的临时 HTML，用系统浏览器打开
    → 功能不降级，不会"没开工具就用不了"

卡片里所有内容都是内联的（图标是 data URI），所以离线、可移动、可截图。
"""

import html
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from ..core.apk.info import ApkInfo
from ..platform import paths, shell


# ---------------------------------------------------------------- 与人对话

def _h(n: int) -> str:
    """字节数 → 人类可读。"""
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(v) < 1024 or unit == "GB":
            return ("%d B" % int(v)) if unit == "B" else ("%.1f %s" % (v, unit))
        v /= 1024.0
    return "%d B" % int(v)


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


# ---------------------------------------------------------------- 服务探测

# 主控台**页面**每 2.5 秒轮询一次卡片。超过这个窗口没来取过，就认为
# "这次推送没人看得见"，退回必然可见的浏览器卡片。
#
# 为什么取 4 倍余量而不是 2 倍：浏览器会限制**后台标签页**的定时器
# （Chrome 隐藏 5 分钟后降到每分钟一次），阈值太紧会出现"页面明明还开着、
# 却还是弹了张卡片"的抖动。宁可多弹一张卡片，也不要推给空气 ——
# 看得见永远优先于"体验统一"。
UI_WATCH_WINDOW = 10.0


def server_endpoint() -> Tuple[bool, str, Optional[float]]:
    """主控台在跑吗？返回 (是否存活, base_url, 页面距今多少秒没来取卡片)。

    第三个值是 `None` = **从没有页面来取过卡片**。它可能是刚启动还没轮到
    第一次轮询，也可能是页面压根没打开 —— 调用方不能把它当 0 处理。

    为什么探活要带上这个：`server.json` 只能证明**进程**还活着，
    证明不了**有人在看**。早先只按前者判断，于是卡片被推进了一个
    没人打开的页面，右键表现为"cmd 一闪、什么都没有、也没有报错"。
    """
    f = paths.server_file()
    if not f.is_file():
        return False, "", None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        port = int(data.get("port") or 0)
        if not port:
            return False, "", None
        url = "http://127.0.0.1:%d" % port
        with urllib.request.urlopen(url + "/api/health", timeout=1.2) as resp:
            if resp.status == 200:
                try:
                    body = json.loads(resp.read().decode("utf-8"))
                except ValueError:
                    body = {}
                ago = body.get("uiAgo")
                # 注意 bool 是 int 的子类，这里不会误收 True/False：
                # 服务端只会给数字或 null。
                return True, url, (float(ago) if isinstance(ago, (int, float)) else None)
    except Exception:
        return False, "", None
    return False, "", None


def _read_token() -> str:
    try:
        return paths.token_file().read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def push_to_server(info: ApkInfo, base_url: str) -> bool:
    """把卡片推给正在运行的主控台。"""
    payload = json.dumps({
        "token": _read_token(),
        "kind": "apk-info",
        "data": info.to_dict(),
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/api/popup/info", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------- 卡片 HTML

_CSS = """
:root{
  --bg:#f6f7f9; --card:#ffffff; --tx:#1b1f26; --tx2:#5b6472; --line:#e4e7ec;
  --accent:#2f6feb; --accent-soft:#eaf1fe; --ok:#128a5b; --warn:#b26a00;
  --danger:#c62828; --chip:#f1f3f6; --mono:ui-monospace,"Cascadia Mono",Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#151719; --card:#1e2126; --tx:#eef1f5; --tx2:#a5aebb; --line:#2c3037;
    --accent:#5b9bff; --accent-soft:#1d2837; --ok:#4fc48c; --warn:#e0a54a;
    --danger:#ff6b6b; --chip:#262a30;
  }
}
*{box-sizing:border-box}
body{margin:0;padding:22px;background:var(--bg);color:var(--tx);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:760px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.hero{display:flex;gap:16px;padding:20px 22px;border-bottom:1px solid var(--line)}
.icon{width:64px;height:64px;border-radius:14px;background:var(--chip);flex:none;
  display:flex;align-items:center;justify-content:center;overflow:hidden}
.icon img{width:100%;height:100%;object-fit:contain}
.icon .ph{font-size:26px;color:var(--tx2)}
.hero .m{min-width:0;flex:1}
.appname{font-size:21px;font-weight:650;letter-spacing:.2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pkg{font-family:var(--mono);font-size:13px;color:var(--tx2);margin-top:3px;word-break:break-all}
.verline{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}
.badge{font-family:var(--mono);font-size:13px;font-weight:600;padding:3px 9px;border-radius:7px;
  background:var(--accent-soft);color:var(--accent)}
.badge.plain{background:var(--chip);color:var(--tx2);font-weight:500}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{font-size:12px;padding:2px 8px;border-radius:999px;background:var(--chip);color:var(--tx2)}
.chip.bad{background:rgba(198,40,40,.12);color:var(--danger);font-weight:600}
.chip.good{background:rgba(18,138,91,.12);color:var(--ok)}
.body{padding:6px 22px 18px}
section{padding:14px 0;border-bottom:1px solid var(--line)}
section:last-child{border-bottom:0}
h3{margin:0 0 10px;font-size:13px;font-weight:600;color:var(--tx2);
  letter-spacing:.6px;text-transform:uppercase}
.kv{display:grid;grid-template-columns:130px 1fr;gap:6px 14px;margin:0;font-size:13.5px}
.kv dt{color:var(--tx2)}
.kv dd{margin:0;word-break:break-all}
.mono{font-family:var(--mono)}
.fp{font-family:var(--mono);font-size:12px;word-break:break-all;line-height:1.6}
.fp b{color:var(--tx2);font-weight:600}
.fprow{display:flex;gap:8px;align-items:flex-start;margin-bottom:6px}
.fprow .v{flex:1;min-width:0}
.err{background:rgba(198,40,40,.08);border:1px solid rgba(198,40,40,.25);color:var(--danger);
  padding:10px 12px;border-radius:9px;font-size:13px}
.note{font-size:12.5px;color:var(--tx2);margin-top:8px}
.actions{display:flex;flex-wrap:wrap;gap:8px;padding:14px 22px;background:var(--chip);
  border-top:1px solid var(--line)}
button, .btn{font:inherit;font-size:13px;padding:7px 13px;border-radius:9px;border:1px solid var(--line);
  background:var(--card);color:var(--tx);cursor:pointer;text-decoration:none;display:inline-block}
button:hover,.btn:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{filter:brightness(1.08);color:#fff}
.foot{padding:12px 4px;color:var(--tx2);font-size:12px;display:flex;justify-content:space-between;gap:10px}
.warnbox{background:rgba(178,106,0,.1);border:1px solid rgba(178,106,0,.28);color:var(--warn);
  padding:9px 12px;border-radius:9px;font-size:12.5px;margin-top:10px}
"""

_JS = """
function cp(el){
  const t = el.getAttribute('data-copy') || el.textContent.trim();
  navigator.clipboard.writeText(t).then(()=>{
    const old = el.textContent; el.textContent = '已复制 ✓';
    setTimeout(()=>{ el.textContent = old; }, 900);
  }).catch(()=>{ alert('复制失败，请手动选中'); });
}
"""


def build_card_html(info: ApkInfo, standalone: bool = True) -> str:
    """生成信息卡 HTML（standalone=True 时是一个完整可打开的页面）。"""
    sig = info.signature
    parts = []

    # ---- 顶部关键信息（包名 + 版本是最大字号，符合"一眼看到"）----
    icon_html = ('<img src="%s" alt="">' % _esc(info.icon_data_uri)) if info.icon_data_uri \
        else '<span class="ph">🤖</span>'

    chips = []
    chips.append('<span class="chip">%s</span>' % _esc(_h(info.file_size)))
    if info.abis:
        chips.append('<span class="chip">%s</span>' % _esc(" / ".join(info.abis)))
    if info.channel:
        chips.append('<span class="chip good">渠道 %s</span>' % _esc(info.channel))
    chips.append('<span class="chip %s">minSdk %d</span>' % ("bad" if info.min_sdk >= 26 else "", info.min_sdk))
    chips.append('<span class="chip">targetSdk %d</span>' % info.target_sdk)
    if info.debuggable:
        chips.append('<span class="chip bad">debuggable</span>')
    if info.uses_cleartext:
        chips.append('<span class="chip bad">明文流量</span>')
    if sig and sig.is_debug:
        chips.append('<span class="chip bad">debug 签名</span>')

    parts.append("""
<div class="hero">
  <div class="icon">{icon}</div>
  <div class="m">
    <div class="appname">{label}</div>
    <div class="pkg">{pkg}</div>
    <div class="verline">
      <span class="badge">v{ver}</span>
      <span class="badge plain">build {code}</span>
    </div>
    <div class="chips">{chips}</div>
  </div>
</div>""".format(
        icon=icon_html,
        label=_esc(info.app_label or info.file_name),
        pkg=_esc(info.package or "（未解析出包名）"),
        ver=_esc(info.version_name or "-"),
        code=_esc(info.version_code if info.version_code else "-"),
        chips="".join(chips),
    ))

    if info.error:
        body = ['<div class="body"><section><div class="err">%s</div></section></div>']
        parts.append(body[0] % _esc(info.error))
        return _page(parts, info, standalone)

    # ---- 概览 ----
    rows = [
        ("文件", info.file_name),
        ("路径", info.path),
        ("大小", "%s（解压后 %s）" % (_h(info.file_size), _h(sum(info.size_breakdown.values())))),
        ("主 Activity", info.main_activity or "-"),
        ("组件", "Activity %d · Service %d · Receiver %d · Provider %d" % (
            len(info.activities), len(info.services), len(info.receivers), len(info.providers))),
        ("Dex", "%d 个（%s）" % (len(info.dex_names), ", ".join(info.dex_names) or "-")),
        ("权限", "%d 项%s" % (len(info.permissions), (
            "，其中 %d 项为危险权限" % len(info.dangerous_permissions)) if info.dangerous_permissions else "")),
    ]
    if info.compile_sdk:
        rows.insert(4, ("compileSdk", info.compile_sdk))
    if info.application_class:
        rows.append(("Application", info.application_class))

    ov = "".join("<dt>%s</dt><dd class=\"%s\">%s</dd>" % (
        _esc(k), "mono" if k in ("路径", "主 Activity", "Application", "文件") else "",
        _esc(v)) for k, v in rows)
    parts.append('<div class="body"><section><h3>概览</h3><dl class="kv">%s</dl>' % ov)

    if info.dangerous_permissions:
        parts.append('<div class="note">危险权限：%s</div>' % _esc(
            "、".join(p.rsplit(".", 1)[-1] for p in info.dangerous_permissions)))
    parts.append("</section>")

    # ---- 签名 ----
    parts.append(_sig_section(sig))

    # ---- 体积 ----
    total = sum(info.size_breakdown.values()) or 1
    top = list(info.size_breakdown.items())[:6] if isinstance(info.size_breakdown, dict) else []
    if top:
        bars = "".join(
            '<dt>%s</dt><dd class="mono">%s · %.1f%%</dd>' % (
                _esc(k), _h(v), v * 100.0 / total) for k, v in top)
        parts.append('<section><h3>体积构成</h3><dl class="kv">%s</dl></section>' % bars)

    parts.append("</div>")

    return _page(parts, info, standalone)


def _sig_section(sig) -> str:
    """签名区块：方案 + 证书 + 指纹（可复制）。"""
    if sig is None:
        return ""
    if not sig.ok:
        return ('<section><h3>签名</h3><div class="err">%s</div>'
                '<div class="note">未申请 root。若这是从设备读到的包，'
                '通常是该 ROM 不支持免 root 读取签名。</div></section>' % _esc(
                    sig.error or "未找到签名信息"))

    out = ['<section><h3>签名</h3>']
    schemes = " · ".join(s.upper() for s in sig.schemes) or "-"
    out.append('<dl class="kv"><dt>签名方案</dt><dd class="mono">%s</dd>' % _esc(schemes))
    out.append('<dt>签名标识</dt><dd class="mono">%s<span class="note"> （与其它包比对可快速判断是否同签名）</span></dd></dl>'
               % _esc(sig.digest_identity))
    if sig.verified:
        out.append('<div class="note">%s</div>' % _esc(sig.verified))

    for i, c in enumerate(sig.certificates, 1):
        label = "签名者 #%d" % i
        out.append('<div style="margin-top:12px">')
        out.append('<div class="note" style="margin:0 0 6px"><b>%s</b>%s</div>' % (
            _esc(label), " · ".join(c.schemes)))
        out.append('<dl class="kv">')
        out.append('<dt>主体</dt><dd>%s</dd>' % _esc(c.subject or "（证书主体解析失败）"))
        if c.issuer and c.issuer != c.subject:
            out.append('<dt>颁发者</dt><dd>%s</dd>' % _esc(c.issuer))
        if c.not_before or c.not_after:
            out.append('<dt>有效期</dt><dd class="mono">%s → %s</dd>' % (
                _esc(c.not_before), _esc(c.not_after)))
        if c.public_key_algorithm:
            out.append('<dt>公钥</dt><dd class="mono">%s%s</dd>' % (
                _esc(c.public_key_algorithm),
                (" %d bits" % c.public_key_bits) if c.public_key_bits else ""))
        if c.signature_algorithm:
            out.append('<dt>签名算法</dt><dd class="mono">%s</dd>' % _esc(c.signature_algorithm))
        if c.serial:
            out.append('<dt>序列号</dt><dd class="mono">%s</dd>' % _esc(c.serial))
        out.append("</dl>")

        for name, val in (("SHA-256", c.sha256), ("SHA-1", c.sha1)):
            if not val:
                continue
            out.append(
                '<div class="fprow" style="margin-top:8px">'
                '<span class="fp v"><b>%s</b><br>%s</span>'
                '<button onclick="cp(this)" data-copy="%s">复制</button></div>'
                % (_esc(name), _esc(val), _esc(val.replace(":", ""))))
        out.append("</div>")

    for wmsg in (sig.warnings or []):
        out.append('<div class="warnbox">%s</div>' % _esc(wmsg))
    out.append("</section>")
    return "".join(out)


def _page(parts, info: ApkInfo, standalone: bool) -> str:
    actions = """
<div class="actions">
  <button class="primary" onclick="cp(this)" data-copy="{pkg}">复制包名</button>
  <button onclick="cp(this)" data-copy="{path}">复制路径</button>
  <button onclick="cp(this)" data-copy="{am}">复制启动命令</button>
  <a class="btn" href="{server}/#/apps/{pkg}" target="_blank">在主控台打开</a>
</div>"""
    am = ""
    if info.package and info.main_activity:
        am = "adb shell am start -n %s/%s" % (info.package, info.main_activity)

    actions_html = actions.format(
        pkg=_esc(info.package), path=_esc(info.path), am=_esc(am),
        server=("http://127.0.0.1:%d" % _server_port()) if _server_port() else "#",
    )

    body = "".join(parts)
    inner = """
<div class="wrap">
  <div class="card">
    %s
    %s
  </div>
  <div class="foot">
    <span>APK Parser Toolkit</span>
    <span>解析耗时 %d ms</span>
  </div>
</div>""" % (body, actions_html, info.parse_ms)

    if not standalone:
        return inner

    return """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s · %s</title>
<style>%s</style>
</head><body>
%s
<script>%s</script>
</body></html>""" % (_esc(info.app_label or info.file_name), _esc(info.package), _CSS, inner, _JS)


def _server_port() -> int:
    try:
        data = json.loads(paths.server_file().read_text(encoding="utf-8"))
        return int(data.get("port") or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------- 对外入口

def show(info: ApkInfo, prefer_server: bool = True, keep_days: int = 2) -> dict:
    """展示信息卡。

    返回 {"mode": "server"|"browser"|"failed", "target": ..., "detail": ...}

    ⚠ 唯一的硬要求是**用户必须看得见**。历史教训：早先只要主控台*进程*
    活着就推送，页面没打开时卡片就推进了一个没人看的列表 —— 用户右键
    一次，cmd 一闪，屏幕上什么都没出现，也没有任何报错，只能来问
    "本来还正常的，怎么坏了"。所以现在推之前先确认"确实有页面在轮询"。
    """
    if prefer_server:
        alive, base, ui_ago = server_endpoint()
        watching = alive and ui_ago is not None and ui_ago <= UI_WATCH_WINDOW
        if watching and push_to_server(info, base):
            return {"mode": "server", "target": base,
                    "detail": "已推送到主控台（页面自动弹出）"}
        res = _open_browser_card(info, keep_days)
        if alive and res["mode"] != "failed":
            # 主控台活着但页面没在看 → 说清楚为什么没用它，
            # 免得用户以为是工具坏了，而不是"页面没打开"。
            res["detail"] += "（主控台在跑，但页面没打开，已改用独立卡片）"
        return res

    return _open_browser_card(info, keep_days)


def _open_browser_card(info: ApkInfo, keep_days: int = 2) -> dict:
    """生成自包含 HTML 卡片并交给系统浏览器 —— **必然可见**的那条路。"""
    try:
        paths.tmp_dir().mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch for ch in (info.package or info.file_name or "apk")
                       if ch.isalnum() or ch in "._-")[:60]
        out = paths.tmp_dir() / ("apk-info-%s-%s.html" % (safe, stamp))
        out.write_text(build_card_html(info, standalone=True), encoding="utf-8")
        ok = shell.open_url(out.as_uri())
        _cleanup_old(keep_days)
        if not ok:
            return {"mode": "failed", "target": str(out),
                    "detail": "无法自动打开浏览器，请手动打开该文件"}
        return {"mode": "browser", "target": str(out), "detail": "已用浏览器打开信息卡"}
    except Exception as e:
        return {"mode": "failed", "target": "", "detail": "生成卡片失败：%s" % e}


def _cleanup_old(keep_days: int = 2) -> None:
    """清掉过期的临时卡片，避免 tmp/ 堆积。"""
    try:
        cutoff = time.time() - keep_days * 86400
        for f in paths.tmp_dir().glob("apk-info-*.html"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except Exception:
        pass


def write_card(info: ApkInfo, out_path) -> Path:
    """把卡片写到指定路径（`--out` 用，也方便调试）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_card_html(info, standalone=True), encoding="utf-8")
    return p
