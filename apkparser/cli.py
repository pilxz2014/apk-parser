# -*- coding: utf-8 -*-
"""命令行入口 —— 所有 GUI 能力都有对应子命令。

CLI 是四种载体里"可被脚本消费"的那一个：`--json` 输出是**稳定契约**
（只增字段、不删不改），可以直接接进 `D:\\bat\\*.sh` 做断言。

    python apktool.py apk info app.apk --json
    python apktool.py launch start com.jxw.xwsf/.ui.WifiListActivity --wait
"""

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, config
from .core import apk as apk_core
from .platform import finders, menu, paths, shell
from .ui import console
from .ui import popup as popup_ui

# ---------------------------------------------------------------- 输出辅助


def emit(data, as_json: bool, human=None) -> int:
    """统一出口：--json 走 JSON，否则走人读渲染。"""
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if human:
        human(data)
    return 0


def fail(msg: str, as_json: bool = False, code: int = 1, **extra) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": msg, **extra}, ensure_ascii=False, indent=2))
    else:
        console.error(msg)
    return code


def human_size(n: int) -> str:
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(v) < 1024 or unit == "GB":
            return ("%d B" % int(v)) if unit == "B" else ("%.1f %s" % (v, unit))
        v /= 1024.0
    return "%d B" % int(v)


def _add_common(p, device: bool = True, json_flag: bool = True):
    if json_flag:
        p.add_argument("--json", action="store_true", help="输出 JSON（稳定契约）")
    if device:
        p.add_argument("--device", "-s", default="", help="目标设备序列号；缺省用唯一在线设备")
    return p


# ---------------------------------------------------------------- apk 域

def cmd_apk_info(ns) -> int:
    info = apk_core.parse_apk(
        ns.file,
        with_signature=not ns.no_sign,
        with_icon=not ns.no_icon,
        verify_signature=ns.verify,
    )
    if ns.json:
        print(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
        return 0 if info.ok else 1
    if info.error:
        console.error(info.error)
        return 1

    console.banner(info.app_label or info.file_name, info.path)
    print("  %s" % console.bold(info.package))
    print("  %s   %s" % (
        console.cyan("v%s" % (info.version_name or "-")),
        console.grey("build %s" % (info.version_code or "-"))))
    print()

    rows = [
        ("文件大小", "%s（解压后 %s）" % (human_size(info.file_size), human_size(sum(info.size_breakdown.values())))),
        ("minSdk / targetSdk", "%d / %d%s" % (info.min_sdk, info.target_sdk,
                                              "   compileSdk %d" % info.compile_sdk if info.compile_sdk else "")),
        ("ABI", " / ".join(info.abis) or "（无 native 库）"),
        ("Dex", "%d 个  %s" % (len(info.dex_names), " ".join(info.dex_names))),
        ("主 Activity", info.main_activity or "-"),
        ("组件", "Activity %d · Service %d · Receiver %d · Provider %d" % (
            len(info.activities), len(info.services), len(info.receivers), len(info.providers))),
        ("权限", "%d 项%s" % (len(info.permissions),
                            "（危险 %d）" % len(info.dangerous_permissions) if info.dangerous_permissions else "")),
    ]
    if info.channel:
        rows.insert(0, ("渠道", info.channel))
    console.kv(rows)

    if info.signature:
        s = info.signature
        print()
        console.step("签名：%s" % (" · ".join(x.upper() for x in s.schemes) or "无"))
        if s.ok:
            c = s.certificates[0]
            console.kv([
                ("主体", c.subject or "（解析失败）"),
                ("SHA-256", c.sha256),
                ("有效期", "%s → %s" % (c.not_before, c.not_after)),
                ("公钥", "%s %s" % (c.public_key_algorithm,
                                    ("%d bits" % c.public_key_bits) if c.public_key_bits else "")),
            ])
        else:
            console.warn(s.error)
        for w in s.warnings:
            console.warn(w)
        if s.verified:
            console.hint(s.verified)

    if info.dangerous_permissions:
        print()
        console.warn("危险权限：%s" % "、".join(
            p.rsplit(".", 1)[-1] for p in info.dangerous_permissions))

    for w in info.warnings:
        console.warn(w)

    print()
    console.hint("解析耗时 %d ms" % info.parse_ms)
    return 0


def cmd_apk_popup(ns) -> int:
    """右键入口：解析后弹信息卡（有主控台就推过去，没有就开临时页）。"""
    use_server = not getattr(ns, "no_server", False)
    # getattr 兜底：handler 和 argparse 注册表之间没有任何编译期约束，
    # 少注册一个 dest 就会在**运行到这一行**时炸掉（右键菜单只看到一个 AttributeError）。
    info = apk_core.parse_apk(ns.file, with_signature=not getattr(ns, "no_sign", False))
    # 连包名都没解析出来 = 这个文件没读成，卡片里会是错误块。
    # 记住它，最后要返回非 0（见函数末尾的说明）。
    failed = bool(info.error and not info.package)
    if failed:
        console.error(info.error)
    if ns.out:
        p = popup_ui.write_card(info, ns.out)
        console.ok("信息卡已写入 %s" % p)
        if ns.open:
            shell.open_url(Path(p).as_uri())
        return 1 if failed else 0

    res = popup_ui.show(info, prefer_server=use_server)
    if res["mode"] == "failed":
        console.warn(res["detail"])
        return 1
    if not ns.quiet:
        console.ok("%s（%s）" % (res["detail"], res["target"]))
    # 失败必须返回非 0：右键走的 apk-info.cmd 靠 `if errorlevel 1 pause` 把
    # 窗口停住。返回 0 的话，控制台连同一整段错误信息一起"闪一下"就没了 ——
    # 用户只能看到"点了没反应"，连排查的线索都拿不到。
    return 1 if failed else 0


def cmd_apk_icon(ns) -> int:
    info = apk_core.parse_apk(ns.file, with_signature=False)
    if not info.icon_data_uri:
        return fail("该 APK 里没有找到可提取的图标（可能是 XML 矢量图或资源混淆过）",
                    ns.json, code=1)
    import base64
    raw = base64.b64decode(info.icon_data_uri.split(",", 1)[1])
    out = Path(ns.out) if ns.out else (
        paths.output_dir() / ("%s-icon.%s" % (info.package or Path(ns.file).stem,
                                              info.icon_mime.split("/")[-1])))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    if ns.json:
        print(json.dumps({"ok": True, "path": str(out), "source": info.icon_path,
                          "mime": info.icon_mime, "bytes": len(raw)}, ensure_ascii=False, indent=2))
    else:
        console.ok("图标已提取 → %s（%s，%d 字节）" % (out, info.icon_mime, len(raw)))
        console.hint("来源：%s" % info.icon_path)
        if ns.open:
            shell.open_path(out)
    return 0


def cmd_apk_sign(ns) -> int:
    info = apk_core.parse_apk(ns.file, with_signature=True, with_icon=False,
                              verify_signature=ns.verify)
    sig = info.signature
    if not sig:
        return fail("未能解析签名", ns.json, code=1)

    data = sig.to_dict()
    data["path"] = info.path
    data["package"] = info.package
    if ns.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if sig.ok else 1

    console.banner("签名信息", "%s · %s" % (info.file_name, info.package or "-"))
    if not sig.ok:
        console.error(sig.error)
        return 1
    console.kv([
        ("签名方案", " · ".join(s.upper() for s in sig.schemes) or "-"),
        ("签名标识", sig.digest_identity),
        ("是否 debug", "是（不可正式分发）" if sig.is_debug else "否"),
    ])
    for i, c in enumerate(sig.certificates, 1):
        print()
        console.step("签名者 #%d  [%s]" % (i, " · ".join(c.schemes)))
        console.kv([
            ("主体", c.subject),
            ("颁发者", c.issuer),
            ("SHA-256", c.sha256),
            ("SHA-1", c.sha1),
            ("有效期", "%s → %s" % (c.not_before, c.not_after)),
            ("公钥", "%s %s bits" % (c.public_key_algorithm, c.public_key_bits)),
            ("签名算法", c.signature_algorithm),
        ])
    for w in sig.warnings:
        console.warn(w)
    if sig.verified:
        console.hint(sig.verified)
    else:
        console.hint("apksigner 不可用，未做有效性校验（本工具只提取，不断言有效）")
    return 0


def cmd_apk_copyname(ns) -> int:
    """右键「复制包名」：把包名塞进剪贴板。"""
    info = apk_core.parse_apk(ns.file, with_signature=False, with_icon=False,
                              with_components=False)
    if not info.package:
        return fail(info.error or "解析不出包名", ns.json)
    what = {"pkg": info.package, "path": info.path,
            "version": info.version_name or ""}.get(ns.what, info.package)
    ok = shell.copy_to_clipboard(what)
    if ns.json:
        print(json.dumps({"ok": ok, "copied": what}, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    if ok:
        console.ok("已复制到剪贴板：%s" % what)
    else:
        console.warn("复制失败，包名是：%s" % what)
    return 0 if ok else 1


def cmd_apk_compare_device(ns) -> int:
    """右键「与设备已装版本对比」：版本号 + 签名一起比。"""
    from .core.device import apps as apps_mod, adb as adb_mod, signature as sign_mod

    info = apk_core.parse_apk(ns.file, with_signature=True, with_icon=False,
                              with_components=False)
    if info.error and not info.package:
        return fail(info.error, ns.json)

    try:
        serial = adb_mod.pick_device(ns.device)
    except adb_mod.AdbError as e:
        return fail(str(e), ns.json)

    dev = apps_mod.app_detail(info.package, serial)
    if dev.get("error"):
        result = {"ok": True, "verdict": "not-installed",
                  "detail": "设备上还没装 %s" % info.package,
                  "local": {"versionName": info.version_name, "versionCode": info.version_code}}
        if ns.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            console.banner("设备对比", info.package)
            console.info("设备（%s）上还没装这个包 —— 这是一次全新安装" % serial)
            console.kv([("本机版本", "v%s (%s)" % (info.version_name, info.version_code))])
        return 0

    lvc, dvc = info.version_code or 0, int(dev.get("versionCode") or 0)
    if dvc and lvc:
        if lvc > dvc:
            verdict, vtext = "upgrade", "本机更高 → 覆盖安装是升级"
        elif lvc < dvc:
            verdict, vtext = "downgrade", "本机更低 → 覆盖安装会被拒绝（需 -d）"
        else:
            verdict, vtext = "same", "版本号相同 → 等同重装"
    else:
        verdict, vtext = "unknown", "版本号取不到，无法比较"

    cmp_res = sign_mod.compare_with_apk(info.package, ns.file, serial)

    result = {
        "ok": True, "verdict": verdict, "device": serial,
        "package": info.package,
        "local": {"versionName": info.version_name, "versionCode": info.version_code,
                  "path": info.path},
        "installed": {"versionName": dev.get("versionName"), "versionCode": dev.get("versionCode"),
                      "apkPath": dev.get("codePath"), "lastUpdate": dev.get("lastUpdateTime")},
        "versionVerdict": vtext,
        "signature": cmp_res,
        "detail": "%s；%s" % (vtext, cmp_res.get("reason", "")),
    }
    if ns.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    console.banner("与设备已装版本对比", info.package)
    console.kv([
        ("本机版本", "v%s  build %s" % (info.version_name, info.version_code)),
        ("设备版本", "v%s  build %s" % (dev.get("versionName") or "-", dev.get("versionCode") or "-")),
        ("版本结论", vtext),
    ])
    v = cmp_res.get("verdict")
    (console.ok if v == "same" else console.warn)("签名：%s —— %s" % (v, cmp_res.get("reason", "")))
    return 0


def cmd_apk_slim(ns) -> int:
    """体积构成：各目录占比 + 可优化提示。"""
    info = apk_core.parse_apk(ns.file, with_signature=False, with_icon=False)
    if info.error:
        return fail(info.error, ns.json)
    rep = apk_core.size_report(info)
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    console.banner("体积构成", "%s · %s" % (info.file_name, human_size(info.file_size)))
    rows = [(i["name"], "%s  %s" % (i["percent"], human_size(i["bytes"]))) for i in rep["items"]]
    console.table(["条目", "占比 / 大小"], rows)
    if rep["hints"]:
        print()
        for h in rep["hints"]:
            console.warn(h)
    return 0


def cmd_apk_diff(ns) -> int:
    """双包对比。两侧各自可以是**本机 APK 文件**或**设备上的已装应用**。

    历史用法 `apk diff A.apk B.apk` 行为不变。新增：

        apk diff A.apk --b-pkg com.x     B 侧 = 设备上的 com.x
        apk diff A.apk                   B 留空 → 设备上的**同名**应用
        apk diff --a-pkg com.x --b-pkg com.y      两个设备应用互比

    与右键菜单那条「与设备已装版本对比」的分工：那条只比签名（快，0.3s），
    这条比整包（权限 / 组件 / 体积 / ABI），慢 1~2 秒但信息全。
    """
    from .core.device.compare import SideSpec, diff_sides

    a_pkg, b_pkg = ns.a_pkg or "", ns.b_pkg or ""
    a_file, b_file = ns.a or "", ns.b or ""
    if a_pkg and a_file:
        return fail("A 侧同时给了路径和 --a-pkg，只能二选一", ns.json)
    if b_pkg and b_file:
        return fail("B 侧同时给了路径和 --b-pkg，只能二选一", ns.json)

    spec_a = (SideSpec.device(a_pkg) if a_pkg
              else (SideSpec.file(a_file) if a_file else SideSpec("", "")))
    spec_b = (SideSpec.device(b_pkg) if b_pkg
              else (SideSpec.file(b_file) if b_file else SideSpec("", "")))

    # 只给了一份本机 APK：默认理解成"跟设备上的同名应用比"（最常用的那件事）
    if spec_b.empty and spec_a.kind == "file":
        spec_b = SideSpec.device("")
    elif spec_a.empty and spec_b.kind == "file":
        spec_a = SideSpec.device("")
    if spec_a.empty or spec_b.empty:
        return fail("两侧都要填：给 APK 路径，或用 --a-pkg / --b-pkg 指定设备上的包名",
                    ns.json)

    d = diff_sides(spec_a, spec_b, device=getattr(ns, "device", ""))
    if not d.get("ok"):
        return fail(d.get("error") or "对比失败", ns.json)
    if ns.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0

    console.banner("双包对比", "%s  ⟷  %s" % (_side_head(d["a"]), _side_head(d["b"])))
    _print_sides(d)
    console.table(
        ["字段", "A", "B", ""],
        [[r["label"], r["a"], r["b"], "" if r["same"] else "≠"] for r in d["fields"]],
        max_widths=[16, 34, 34, 2],
    )
    print()
    console.step("体积变化：%s（A→B）" % d["sizeDelta"]["text"])
    if d["signature"]:
        v = d["signature"]["verdict"]
        line = "%s —— %s" % (v, d["signature"]["reason"])
        (console.ok if v == "same" else console.warn)(line)
    for key, label in (("permissions", "权限"), ("components", "组件")):
        blk = d[key]
        if key == "permissions":
            if blk["onlyA"]:
                print(); console.step("仅 A 有：%s" % "、".join(p.rsplit(".", 1)[-1] for p in blk["onlyA"]))
            if blk["onlyB"]:
                console.step("仅 B 有：%s" % "、".join(p.rsplit(".", 1)[-1] for p in blk["onlyB"]))
        else:
            for ctype, cd in blk.items():
                if cd["onlyA"] or cd["onlyB"]:
                    print(); console.step("%s 差异：" % ctype)
                    for n in cd["onlyA"]:
                        console.hint("仅 A：%s" % n)
                    for n in cd["onlyB"]:
                        console.hint("仅 B：%s" % n)
    return 0


def _side_head(side: dict) -> str:
    """对比里一侧的标题：文件名 + 包名。"""
    name = side.get("fileName") or side.get("path") or "?"
    pkg = side.get("package") or ""
    return "%s（%s）" % (name, pkg) if pkg and pkg not in name else name


def _print_sides(d: dict) -> None:
    """把两侧"数据从哪来"打出来。

    这是**结论可信度的一部分**：A 是本机文件、B 是设备上按需读出来的，
    与"两边都是本机文件"完全是两回事 —— 不能只给结论不给来路。
    """
    from .core.device.compare import SOURCE_LABELS

    src = d.get("sources") or {}
    for tag in ("a", "b"):
        s = src.get(tag) or {}
        if not s:
            continue
        where = SOURCE_LABELS.get(s.get("from"), s.get("from") or "")
        extra = " @ %s" % d["device"] if (s.get("kind") == "device" and d.get("device")) else ""
        console.hint("%s 侧  %s%s   [%s]" % (tag.upper(), s.get("value") or "", extra, where))
    rs = d.get("remoteStats") or {}
    if rs.get("adbCalls"):
        console.hint("设备侧读取代价：%s 次 adb 调用、%s（原包 %s）—— 未整包 pull"
                     % (rs.get("adbCalls"), _human_bytes(rs.get("fetched")),
                        _human_bytes(rs.get("size"))))
    for n in (d.get("notes") or []):
        console.warn(n)


def _human_bytes(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % int(n))
        n /= 1024.0
    return "%d B" % int(n)


def cmd_apk_scan(ns) -> int:
    root = Path(ns.dir)
    if not root.is_dir():
        return fail("目录不存在：%s" % root, ns.json)
    files = sorted([p for p in root.rglob("*.apk") if p.is_file()])
    if ns.limit:
        files = files[:ns.limit]
    results = []
    for f in files:
        info = apk_core.parse_apk(f, with_signature=False, with_icon=False)
        results.append({
            "path": str(f), "package": info.package, "versionName": info.version_name,
            "versionCode": info.version_code, "appLabel": info.app_label,
            "size": info.file_size, "minSdk": info.min_sdk, "targetSdk": info.target_sdk,
            "error": info.error,
        })
    if ns.json:
        print(json.dumps({"count": len(results), "items": results}, ensure_ascii=False, indent=2))
    else:
        console.banner("批量解析", "%d 个 APK" % len(results))
        console.table(
            ["包名", "版本", "应用名", "大小"],
            [[r["package"] or "(失败)", r["versionName"], r["appLabel"], human_size(r["size"])]
             for r in results],
            max_widths=[42, 16, 20, 10],
        )
    if ns.csv:
        import csv
        with open(ns.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(results[0].keys()) if results else ["path"])
            w.writeheader()
            w.writerows(results)
        if not ns.json:
            console.ok("已导出 CSV → %s" % ns.csv)
    return 0


# ---------------------------------------------------------------- device 域

def cmd_device_list(ns) -> int:
    from .core.device import adb as adb_mod
    devs = adb_mod.list_devices()
    data = [d.to_dict() for d in devs]
    if ns.json:
        print(json.dumps({"devices": data}, ensure_ascii=False, indent=2))
        return 0
    if not devs:
        console.warn("没有检测到设备。检查 USB 调试是否打开，或执行 `adb devices` 看提示。")
        return 1
    console.table(["序列号", "状态", "型号", "Android", "分辨率"],
                  [[d.serial, d.state, d.model, d.android_version, d.resolution] for d in devs],
                  max_widths=[24, 10, 22, 10, 12])
    return 0


def cmd_device_info(ns) -> int:
    from .core.device import adb as adb_mod
    info = adb_mod.device_info(ns.device)
    if ns.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if info.get("error"):
        return fail(info["error"], ns.json)
    console.banner("设备信息", info.get("serial", ""))
    console.kv([(k, v) for k, v in info.items() if k not in ("serial", "error")])
    return 0


def cmd_device_wifi(ns) -> int:
    from .core.device import tools as dev_tools
    if ns.action == "connect":
        res = dev_tools.wifi_connect(ns.target, ns.device)
    else:
        res = dev_tools.wifi_disconnect(ns.device)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    (console.ok if res.get("ok") else console.error)(res.get("detail", ""))
    return 0 if res.get("ok") else 1


def cmd_device_tools(ns) -> int:
    from .core.device import tools as dev_tools
    fn = getattr(dev_tools, ns.op, None)
    if fn is None:
        return fail("未知操作：%s" % ns.op, ns.json)
    args = [a for a in (ns.arg or [])]
    res = fn(*args, device=ns.device)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        (console.ok if res.get("ok") else console.error)(res.get("detail", str(res)))
        for c in res.get("commands", []):
            console.hint("$ " + c)
    return 0 if res.get("ok") else 1


def cmd_app_list(ns) -> int:
    from .core.device import apps as apps_mod
    items = apps_mod.list_apps(filter_kw=ns.filter or "", include_system=ns.system, device=ns.device)
    if ns.json:
        print(json.dumps({"count": len(items), "apps": [a.to_dict() for a in items]},
                         ensure_ascii=False, indent=2))
        return 0
    if not items:
        console.warn("没有匹配的应用（或设备未连接）")
        return 1
    console.table(["包名", "应用名", "版本", "类型"],
                  [[a.package, a.label or "-", a.version_name or "-",
                    "系统" if a.is_system else "第三方"] for a in items],
                  max_widths=[46, 22, 16, 8])
    console.info("共 %d 个" % len(items))
    return 0


def cmd_app_labels(ns) -> int:
    """补齐应用名。

    只从设备读 APK 的 `AndroidManifest.xml` + `resources.arsc` 两个条目
    （见 core/apk/remote.py），约 1 秒 / 包 —— 不 pull 整包。
    结果按 `包名|versionCode` 落盘缓存，下次列表直接命中。
    """
    import time as _t

    from .core.device import adb as adb_mod
    from .core.device import apps as apps_mod
    from .core.device import labels as labels_mod

    serial = adb_mod.pick_device(ns.device)
    items, _cmds = apps_mod.list_packages(include_system=ns.system, device=serial)

    if ns.clear_cache:
        n = labels_mod.clear()
        console.info("已清空应用名缓存（%d 条）" % n)

    before = labels_mod.stats()["cached"]
    pkgs = [{"package": a.package, "versionCode": a.version_code,
             "apkPath": a.apk_path} for a in items]
    known = sum(1 for a in items if labels_mod.get(a.package, a.version_code))

    if ns.json:
        labels_mod.start_sync(pkgs, device=serial, workers=ns.workers)
        while True:
            st = labels_mod.sync_state()
            if not st["running"]:
                break
            _t.sleep(0.5)
        st["knownBefore"] = known
        st["totalApps"] = len(items)
        st["cachedBefore"] = before
        st["cachedAfter"] = labels_mod.stats()["cached"]
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    console.banner("应用名补齐", "%d 个应用，已知 %d 个" % (len(items), known))
    if known == len(items):
        console.info("全都已经有了，无需补齐")
        return 0

    labels_mod.start_sync(pkgs, device=serial, workers=ns.workers)
    last = -1
    while True:
        st = labels_mod.sync_state()
        if not st["running"]:
            break
        if st["done"] != last:
            last = st["done"]
            print("  %5d/%-5d  成功 %-4d 失败 %-4d  当前 %s"
                  % (st["done"], st["total"], st["succeeded"], st["failed"], st["current"]))
        _t.sleep(0.6)

    st = labels_mod.sync_state()
    console.info("完成：成功 %d / 失败 %d / 跳过 %d，耗时 %.1fs"
                 % (st["succeeded"], st["failed"], st["skipped"], st["elapsedMs"] / 1000.0))
    after = labels_mod.stats()["cached"]
    console.info("缓存：%d → %d 条（%s）" % (before, after, labels_mod.cache_file()))
    for e in st.get("errors", [])[:5]:
        console.warn("  %s：%s" % (e.get("package"), e.get("error")))
    return 0


def cmd_app_info(ns) -> int:
    from .core.device import apps as apps_mod
    info = apps_mod.app_detail(ns.package, device=ns.device)
    if ns.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if not info.get("error") else 1
    if info.get("error"):
        return fail(info["error"], ns.json)
    console.banner(info.get("label") or info["package"], "已安装应用")
    console.kv([(k, v) for k, v in info.items()
                if k in ("package", "versionName", "versionCode", "minSdk", "targetSdk",
                         "uid", "firstInstallTime", "lastUpdateTime", "apkPath",
                         "dataDir", "isSystem", "installer", "abi")])
    return 0


def cmd_app_sign(ns) -> int:
    from .core.device import signature as sig_mod
    res = sig_mod.get_signature(ns.package, device=ns.device, fast=ns.fast,
                                refresh=ns.refresh, full=not ns.fast)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if not res.get("ok"):
        console.error(res.get("reason", "取不到签名"))
        for cmd in res.get("commands", []):
            console.hint("$ " + cmd)
        return 1
    console.banner("%s 的签名" % ns.package, "来源：%s" % res.get("source", "-"))
    console.kv([
        ("签名方案", " · ".join(s.upper() for s in (res.get("schemes") or [])) or "-"),
        ("签名标识", res.get("identity", "-")),
        ("证书", "完整" if res.get("full") else "仅摘要（快路径）"),
    ])
    for c in res.get("certificates", []):
        print()
        console.step("签名者")
        console.kv([("主体", c.get("subject", "")), ("SHA-256", c.get("sha256", "")),
                    ("有效期", "%s → %s" % (c.get("notBefore", ""), c.get("notAfter", "")))])
    return 0


def cmd_app_sign_cmp(ns) -> int:
    from .core.device import signature as sig_mod
    res = sig_mod.compare_with_apk(ns.package, ns.apk, device=ns.device)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    v = res.get("verdict")
    (console.ok if v == "same" else console.warn)("%s —— %s" % (v, res.get("reason", "")))
    return 0


def cmd_app_action(ns) -> int:
    from .core.device import apps as apps_mod
    res = apps_mod.app_action(ns.package, ns.op, device=ns.device)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        (console.ok if res.get("ok") else console.error)(res.get("detail", str(res)))
        for c in res.get("commands", []):
            console.hint("$ " + c)
    return 0 if res.get("ok") else 1


def cmd_app_export(ns) -> int:
    from .core.device import apps as apps_mod
    res = apps_mod.export_apk(ns.package, ns.out, device=ns.device)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    (console.ok if res.get("ok") else console.error)(
        res.get("detail") or ("已导出 → %s" % res.get("path", "")))
    return 0 if res.get("ok") else 1


# ---------------------------------------------------------------- launch

def cmd_launch_list(ns) -> int:
    from .core.launch import components as comp_mod
    items = comp_mod.list_components(ns.package, ctype=ns.type or "", device=ns.device)
    if ns.json:
        print(json.dumps({"components": items}, ensure_ascii=False, indent=2))
        return 0
    if not items:
        console.warn("没有解析到组件（该 ROM 的 dumpsys 输出可能不标准）")
        return 1
    console.table(["类型", "组件", "导出", "备注"],
                  [[c["type"], c["name"], "是" if c["exported"] else "否", c.get("summary", "")]
                   for c in items],
                  max_widths=[9, 58, 5, 30])
    return 0


def cmd_launch_start(ns) -> int:
    from .core.device import adb as adb_mod
    from .core.launch import components as comp_mod
    from .core.launch import history as hist_mod
    from .core.launch.intent import Intent

    intent = Intent.from_cli(ns)
    if not intent.target and not intent.action:
        return fail("至少要指定组件（pkg/.Class）或 --action", ns.json)

    # 补全 + 校验 —— 和 Web / 右键菜单走的是同一段逻辑。
    # 只给包名 → 向设备问 launcher；裸全限定类名 → 用 --package 或设备包名集合拆开；
    # 什么都定不下来就直接驳回，绝不去执行一条空的 `am start`。
    try:
        serial = adb_mod.pick_device(ns.device)
    except Exception:
        serial = ""
    intent.resolve(serial)
    problem = intent.validate()
    if problem:
        return fail(problem, ns.json)

    popen_args = ["am", "start"] + intent.to_am_args()
    if ns.dry_run:
        cmdline = "adb %s shell %s" % (
            ("-s %s" % ns.device) if ns.device else "",
            intent.to_display())
        if ns.json:
            print(json.dumps({"ok": True, "dryRun": True, "amArgs": popen_args,
                              "target": intent.normalized_target,
                              "command": cmdline.strip()}, ensure_ascii=False, indent=2))
        else:
            console.step("将要执行（dry-run）：")
            print("  " + console.cyan(cmdline.strip()))
        return 0

    res = comp_mod.start_component(intent, device=ns.device, wait=ns.wait)
    hist_mod.record(intent, res, device=ns.device or "")
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    console.hint("$ %s" % res.get("command", ""))
    if res.get("ok"):
        console.ok(res.get("detail", "已发送启动请求"))
        if res.get("totalTime"):
            console.kv([("TotalTime", "%s ms" % res["totalTime"]),
                        ("ThisTime", "%s ms" % res.get("thisTime", 0))])
        if res.get("warning"):
            console.warn(res["warning"])
        if ns.logcat:
            _tail_logcat(ns)
    else:
        console.error(res.get("detail", "启动失败"))
        if res.get("hint"):
            console.hint(res["hint"])
    return 0 if res.get("ok") else 1


def _tail_logcat(ns) -> None:
    from .core.capture import logcat as logcat_mod
    console.step("抓取 %ss logcat（Ctrl+C 停止）" % ns.logcat)
    for line in logcat_mod.stream(package=ns.package or "", level=ns.log_level or "V",
                                  device=ns.device, duration=float(ns.logcat)):
        print(line)


def cmd_launch_broadcast(ns) -> int:
    from .core.launch import components as comp_mod
    from .core.launch.intent import Intent
    intent = Intent.from_cli(ns)
    intent.kind = "broadcast"
    res = comp_mod.send_broadcast(intent, device=ns.device)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    (console.ok if res.get("ok") else console.error)(res.get("detail", ""))
    console.hint("$ %s" % res.get("command", ""))
    return 0 if res.get("ok") else 1


def cmd_launch_history(ns) -> int:
    from .core.launch import history as hist_mod
    items = hist_mod.list_entries(limit=ns.limit)
    if ns.json:
        print(json.dumps({"history": items}, ensure_ascii=False, indent=2))
        return 0
    if not items:
        console.info("还没有启动记录")
        return 0
    console.table(["时间", "组件", "结果"],
                  [[i["time"], i["component"], i["result"]] for i in items],
                  max_widths=[20, 55, 20])
    return 0


def cmd_launch_shortcut(ns) -> int:
    from .core.launch import history as hist_mod
    if ns.op == "list":
        items = hist_mod.list_shortcuts()
        if ns.json:
            print(json.dumps({"shortcuts": items}, ensure_ascii=False, indent=2))
        else:
            if not items:
                console.info("还没有「我的入口」")
            else:
                console.table(["名称", "组件", "参数"],
                              [[i["name"], i["component"], i.get("args", "")] for i in items],
                              max_widths=[18, 50, 30])
        return 0
    if ns.op == "add":
        res = hist_mod.add_shortcut(ns.name, ns.component, ns.args or "")
    else:
        res = hist_mod.del_shortcut(ns.name)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        (console.ok if res.get("ok") else console.error)(res.get("detail", str(res)))
    return 0 if res.get("ok") else 1


# ---------------------------------------------------------------- capture

def cmd_capture_screenshot(ns) -> int:
    from .core.capture import screen as screen_mod
    res = screen_mod.screenshot(out=ns.out, device=ns.device)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if not res.get("ok"):
        return fail(res.get("error", "截图失败"), ns.json)
    console.ok("截图已保存 → %s（%.1f KB，%d ms）" % (
        res["path"], res["bytes"] / 1024.0, res["ms"]))
    console.hint("$ %s" % res["command"])
    if ns.open:
        shell.open_path(res["path"])
    return 0


def cmd_capture_record(ns) -> int:
    from .core.capture import record as rec_mod
    if ns.op == "start":
        res = rec_mod.start(bitrate=ns.bitrate, size=ns.size, time_limit=ns.time_limit,
                            device=ns.device)
    else:
        res = rec_mod.stop(device=ns.device)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        (console.ok if res.get("ok") else console.error)(res.get("detail", str(res)))
        if res.get("path"):
            console.hint("文件：%s" % res["path"])
        if res.get("command"):
            console.hint("$ %s" % res["command"])
    return 0 if res.get("ok") else 1


def cmd_capture_mirror(ns) -> int:
    from .core.capture import record as rec_mod
    res = rec_mod.mirror(record_to=ns.record, device=ns.device)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        (console.ok if res.get("ok") else console.error)(res.get("detail", str(res)))
        if res.get("command"):
            console.hint("$ %s" % res["command"])
    return 0 if res.get("ok") else 1


def cmd_capture_logcat(ns) -> int:
    from .core.capture import logcat as logcat_mod
    if ns.duration:
        for line in logcat_mod.stream(package=ns.package or "", level=ns.level or "V",
                                      filter_kw=ns.filter or "", device=ns.device,
                                      duration=float(ns.duration)):
            print(line)
        return 0
    res = logcat_mod.snapshot(package=ns.package or "", level=ns.level or "V",
                              filter_kw=ns.filter or "", device=ns.device, lines=ns.lines)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    for line in res.get("lines", []):
        print(line)
    return 0


def cmd_capture_artifacts(ns) -> int:
    from .core.capture import gallery as gallery_mod
    items = gallery_mod.list_artifacts(limit=ns.limit)
    if ns.json:
        print(json.dumps({"artifacts": items}, ensure_ascii=False, indent=2))
        return 0
    if not items:
        console.info("还没有截图/录屏产物")
        return 0
    console.table(["时间", "类型", "文件", "大小"],
                  [[i["time"], i["kind"], i["name"], human_size(i["bytes"])] for i in items],
                  max_widths=[20, 8, 46, 10])
    return 0


# ---------------------------------------------------------------- install

_DOWNGRADE_CODE = "INSTALL_FAILED_VERSION_DOWNGRADE"


def _downgrade_only(res: dict) -> list:
    """挑出「**纯粹**因为版本降级而被拦下」的那些文件；否则返回空。

    只要有一个文件是别的原因失败的，就返回空 —— 那种情况下不该替用户
    自作主张加 -d：`-d` 是对整批文件生效的开关，可能把别的冲突一起放过去。
    这儿也是"降级"这件事唯一的判据来源：看预检项里的 `code`，不是看文案。
    """
    hits = []
    for r in (res.get("results") or []):
        if r.get("ok"):
            continue
        codes = [i.get("code") for i in (r.get("precheck") or [])
                 if i.get("level") == "fail"]
        if codes != [_DOWNGRADE_CODE]:
            return []
        hits.append(r)
    return hits


def _pre_item(r: dict, code: str) -> dict:
    return next((i for i in (r.get("precheck") or []) if i.get("code") == code), {})


def _can_prompt() -> bool:
    """能不能问用户？

    双击 .apk 时人在控制台窗口前，stdin 是终端 → 能问。
    被脚本/管道/CI 调用时问不了，这时候必须自动降级为"报错 + 给出 -d 提示"，
    否则会卡在等输入上把调用方挂死。
    """
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def _ask_yes(question: str) -> bool:
    try:
        ans = input("%s [y/N] " % question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes", "是", "好", "1")


def _show_downgrade(hits: list) -> None:
    print()
    console.warn("设备上已经装了这个包，而且版本更高 —— 这是**降级安装**")
    for r in hits:
        it = _pre_item(r, _DOWNGRADE_CODE)
        console.kv([
            ("包名", it.get("package") or r.get("file") or ""),
            ("设备上", "v%s (build %s)" % (it.get("deviceVersionName") or "-",
                                          it.get("deviceVersionCode") or "?")),
            ("你装的", "v%s (build %s)" % (it.get("versionName") or "-",
                                          it.get("versionCode") or "?")),
        ])
    console.hint("Android 默认拒绝降级，必须显式加 -d 才会放行。")
    console.hint("降级覆盖一般会保留应用数据，但版本会退回去。")


def cmd_install_one(ns) -> int:
    from .core.install import installer

    # --console：在独立控制台窗口里重跑一遍自己（安装是有过程的，用户要看得见）。
    # 用环境变量做递归保护，避免子进程再开一个窗口。
    if ns.console and not os.environ.get("APKPARSER_IN_CONSOLE"):
        argv = [sys.executable, str(paths.project_root() / "apktool.py"),
                "install", "one"] + list(ns.apk)
        if ns.device:
            argv += ["-s", ns.device]
        if ns.no_reinstall:
            argv.append("--no-reinstall")
        elif ns.reinstall:
            argv.append("-r")
        if ns.downgrade:
            argv.append("-d")
        if ns.test:
            argv.append("-t")
        if ns.grant:
            argv.append("-g")
        if ns.yes:
            argv.append("-y")
        if ns.check_only:
            argv.append("--check-only")
        ok = shell.spawn_console(argv, title="APK 安装", keep_open=True,
                                 env_extra={"APKPARSER_IN_CONSOLE": "1"})
        if not ok:
            return fail("无法打开控制台窗口", ns.json)
        if not ns.json:
            console.ok("已在独立控制台窗口中开始安装")
        return 0

    def run(downgrade: bool) -> dict:
        return installer.install(list(ns.apk), device=ns.device,
                                 reinstall=not ns.no_reinstall,
                                 downgrade=downgrade, test=ns.test, grant=ns.grant,
                                 check_only=ns.check_only)

    res = run(ns.downgrade)

    # ---- 版本比设备低：不是直接失败，而是**问一句** ----
    hits = [] if (ns.downgrade or ns.check_only) else _downgrade_only(res)
    if hits:
        if ns.yes:
            console.warn("设备上的版本更高：按 -y 直接降级安装")
            _show_downgrade(hits)
            print()
            res = run(True)
        elif _can_prompt():
            _show_downgrade(hits)
            if _ask_yes("确认降级覆盖安装吗？"):
                print()
                console.step("已确认 —— 带 -d 重新安装")
                res = run(True)
            else:
                print()
                console.warn("已取消，设备没有任何改动")
                console.hint("想装的话重来一次并加 -d（或在这里按 y）")
                return 1
        # 问不了（管道 / 不是终端）→ 维持原样：把预检结论打出来，让调用方自己决定

    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _render_install(res)
    return 0 if res.get("ok") else 1


def cmd_install_dir(ns) -> int:
    from .core.install import installer
    from pathlib import Path
    d = Path(ns.dir)
    if not d.is_dir():
        return fail("目录不存在：%s" % d, ns.json)
    apks = sorted(str(p) for p in d.glob("*.apk"))
    if not apks:
        return fail("目录里没有 .apk", ns.json)
    res = installer.install(apks, device=ns.device, parallel=ns.parallel)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _render_install(res)
    return 0 if res.get("ok") else 1


def cmd_install_all(ns) -> int:
    from .core.install import installer
    devices = [d.strip() for d in (ns.devices or "").split(",") if d.strip()]
    if not devices:
        return fail("请用 --devices a,b,c 指定设备", ns.json)
    res = installer.install_multi(ns.apk, devices)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        console.banner("多机并发安装", "%s → %d 台设备" % (ns.apk, len(devices)))
        for r in res.get("results", []):
            (console.ok if r.get("ok") else console.error)(
                "%s：%s" % (r.get("device"), r.get("detail", "")))
    return 0 if res.get("ok") else 1


def cmd_install_errors(ns) -> int:
    from .core.install import errors as err_mod
    if ns.code:
        e = err_mod.explain(ns.code)
        if ns.json:
            print(json.dumps(e, ensure_ascii=False, indent=2))
        else:
            console.banner(ns.code)
            console.kv([("含义", e["meaning"]), ("原因", e["cause"]),
                        ("怎么办", "；".join(e["fixes"]))])
        return 0
    table = err_mod.all_codes()
    if ns.json:
        print(json.dumps({"errors": table}, ensure_ascii=False, indent=2))
        return 0
    console.table(["错误码", "含义", "常见原因"],
                  [[e["code"], e["meaning"], e["cause"]] for e in table],
                  max_widths=[34, 22, 46])
    return 0


def _render_install(res: dict) -> None:
    if res.get("precheck"):
        print()
        console.step("安装前预检")
        for line in res["precheck"]:
            mark = {"pass": "✔", "warn": "⚠", "fail": "✖", "info": "·"}.get(line["level"], "·")
            printer = {"pass": console.ok, "warn": console.warn,
                       "fail": console.error, "info": console.info}.get(line["level"], console.info)
            printer("%s：%s" % (line["title"], line["detail"]))
    for r in res.get("results", []):
        print()
        if r.get("ok"):
            console.ok("%s 安装成功%s" % (r.get("file"), "（%s）" % r.get("duration") if r.get("duration") else ""))
        else:
            console.error("%s 安装失败" % r.get("file"))
            e = r.get("explain") or {}
            if e:
                console.kv([("错误码", e.get("code", "")), ("含义", e.get("meaning", "")),
                            ("原因", e.get("cause", ""))])
                for f in e.get("fixes", []):
                    console.hint(f)
            elif r.get("detail"):
                console.hint(r["detail"])
        # 「不猜命令」：把真跑过的命令回显出来。installer 的结果里键叫 `commands`
        # （列表），早先这里读的是不存在的 `command` → 每次都打出一行空的 "$ "。
        for c in (r.get("commands") or [])[-2:]:
            console.hint("$ %s" % c)
        for line in r.get("log", [])[-4:]:
            console.hint(line)
    if res.get("ok"):
        print()
        console.ok(res.get("detail", "全部完成"))


# ---------------------------------------------------------------- file

def cmd_file(ns) -> int:
    from .core import files as files_mod
    if ns.op == "push":
        res = files_mod.push(ns.src, ns.dst, device=ns.device)
    elif ns.op == "pull":
        res = files_mod.pull(ns.src, ns.dst, device=ns.device)
    else:
        res = files_mod.ls(ns.src, device=ns.device)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        if res.get("entries") is not None:
            console.table(["名称", "大小", "权限"],
                          [[e["name"], human_size(e["size"]) if e["size"] is not None else "-",
                            e.get("perm", "")] for e in res["entries"]],
                          max_widths=[50, 12, 12])
        else:
            (console.ok if res.get("ok") else console.error)(res.get("detail", str(res)))
    return 0 if res.get("ok") else 1


# ---------------------------------------------------------------- sys

def cmd_sys_doctor(ns) -> int:
    data = finders.doctor()
    if ns.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    console.banner("环境自检", "Python %s · %s" % (data["python"]["version"], data["python"]["platform"]))
    for it in data["tools"]:
        ok = bool(it["path"])
        tag = console.green("✔") if ok else (
            console.red("✖") if it["required"] else console.yellow("○"))
        ver = ("  " + console.grey(it["version"][:60])) if it["version"] else ""
        print("  %s %-10s %s%s" % (tag, it["name"], it["path"] or console.grey("未找到"), ver))
        if not ok:
            print("      %s" % console.grey(it["hint"]))
    print()
    console.step("目录")
    console.kv([("项目", data["dirs"]["project"]), ("配置", data["dirs"]["config"]),
                ("产物", data["dirs"]["output"]), ("缓存", data["dirs"]["cache"])])
    missing_required = [t["name"] for t in data["tools"] if t["required"] and not t["path"]]
    if missing_required:
        print()
        console.error("缺少必需工具：%s（设备域功能不可用，本机域不受影响）" % "、".join(missing_required))
        return 1
    print()
    console.ok("环境检查通过")
    return 0


def cmd_sys_config(ns) -> int:
    if ns.op == "get":
        if ns.key:
            val = config.get(ns.key)
            if ns.json:
                print(json.dumps({"key": ns.key, "value": val}, ensure_ascii=False, indent=2))
            else:
                print(val)
        else:
            data = config.all_values()
            if ns.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                console.banner("配置", config.path())
                console.kv(sorted(data.items()))
        return 0
    if ns.op == "set":
        if not ns.key:
            return fail("用法：sys config set <key> <value>", ns.json)
        ok = config.set_value(ns.key, ns.value)
        if ns.json:
            print(json.dumps({"ok": ok, "key": ns.key, "value": config.get(ns.key)},
                             ensure_ascii=False, indent=2))
        else:
            console.ok("%s = %r" % (ns.key, config.get(ns.key)))
        return 0 if ok else 1
    # reset
    ok = config.save(dict(config.DEFAULTS))
    console.ok("已恢复默认配置") if ok else console.error("写入失败")
    return 0 if ok else 1


def cmd_sys_menu(ns) -> int:
    if ns.op == "status":
        st = menu.status()
        if ns.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
            return 0
        if not st.get("supported"):
            console.warn("此平台不支持菜单注入")
            return 1
        console.banner("菜单注入状态", st.get("menuRoot", ""))
        _style = st.get("style") or ""
        if _style:
            console.info("样式：%s" % ("级联子菜单（cascade）" if _style == "cascade"
                                      else "平铺（flat）"))
        if _style == "cascade" and not st.get("cascade", {}).get("ok"):
            console.warn("级联配置自检未通过：%s"
                         % json.dumps(st.get("cascade", {}), ensure_ascii=False))
        for it in st.get("items", []):
            tag = console.green("✔") if it["registered"] else console.grey("○")
            print("  %s %s" % (tag, it["label"]))
            if it.get("command"):
                print("      %s" % console.grey(it["command"]))
        print()
        console.info("打开方式候选：%s" % ("已注册" if st.get("openWithRegistered") else "未注册"))
        if st.get("note"):
            console.hint(st["note"])
        return 0

    if ns.op == "install":
        res = menu.install(style=getattr(ns, "style", "cascade"))
        if ns.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            for s in res.get("steps", []):
                print("  " + s)
            if res.get("warning"):
                console.warn(res["warning"])
            ok = res.get("ok")
            console.ok("右键菜单已注册（无需管理员权限，仅写 HKCU）") if ok else None
            if res.get("style") == "flat":
                console.hint("平铺模式：右键 .apk 可直接看到「APK 工具 · …」各项")
            else:
                console.hint("在任意 .apk 上右键 → 「APK 工具」即可看到各项")
            console.hint("默认打开方式未被修改；如需双击安装，请右键 → 打开方式 → 选择「APK 安装到设备」")
            console.hint("若级联子菜单不展开，可改用保底平铺样式："
                         "python apktool.py sys menu install --style flat")
        return 0 if res.get("ok") else 1

    res = menu.uninstall()
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for s in res.get("steps", []):
            print("  " + s)
    console.ok("已卸载菜单注册")
    return 0


def cmd_web(ns) -> int:
    from .web import server as web_server
    if ns.op == "restart":
        # 一键启动的语义：先把上一次的服务停干净，再起新的。
        # 旧实例不存在 / 残留了死 pid 的 server.json，都不算错，继续往下走。
        st = web_server.status()
        if st.get("running") or st.get("stale"):
            r = web_server.stop()
            (console.ok if r.get("ok") else console.info)(r.get("detail", "旧实例已清理"))
            if st.get("port"):
                web_server.wait_port_free(int(st["port"]), timeout=5.0)
        else:
            console.info("没有在运行的旧实例")
        return web_server.cli_start(port=ns.port, open_browser=not ns.no_open,
                                    foreground=ns.foreground)
    if ns.op == "start":
        return web_server.cli_start(port=ns.port, open_browser=not ns.no_open,
                                    foreground=ns.foreground)
    if ns.op == "stop":
        res = web_server.stop()
        if ns.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            (console.ok if res.get("ok") else console.warn)(res.get("detail", ""))
        return 0 if res.get("ok") else 1
    st = web_server.status()
    if ns.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    if st.get("running"):
        console.ok("主控台运行中：%s（pid %s）" % (st["url"], st.get("pid")))
    else:
        console.info("主控台未运行。启动：python apktool.py web start")
    return 0


# ---------------------------------------------------------------- 解析器

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apktool",
        description="APK Parser Toolkit —— APK 解析 + 设备操控工具箱",
        epilog="加 --json 可获得稳定的机器可读输出，便于脚本消费。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-V", "--version", action="version", version="apktool %s" % __version__)
    sub = p.add_subparsers(dest="group", metavar="<group>")

    # ---- apk ----
    apk = sub.add_parser("apk", help="本机 APK 解析与分析（不需要设备）")
    a = apk.add_subparsers(dest="cmd", metavar="<cmd>")

    c = _add_common(a.add_parser("info", help="解析 APK 信息"), device=False)
    c.add_argument("file")
    c.add_argument("--no-sign", action="store_true", help="跳过签名解析")
    c.add_argument("--no-icon", action="store_true", help="跳过图标提取")
    c.add_argument("--verify", action="store_true", help="额外用 apksigner 做权威校验")
    c.set_defaults(handler=cmd_apk_info)

    c = _add_common(a.add_parser("popup", help="弹信息卡（右键菜单入口）"), device=False)
    c.add_argument("file")
    c.add_argument("--out", default="", help="写出到指定 HTML 文件而不是打开浏览器")
    c.add_argument("--open", action="store_true", help="配合 --out：写完后打开")
    c.add_argument("--no-server", action="store_true", help="不推给主控台，强制用临时页")
    c.add_argument("--quiet", action="store_true")
    # 这个开关必须注册：右键菜单走的正是 `apk popup`，而 cmd_apk_popup 读的是
    # ns.no_sign。少注册一个 dest，右键点一下就是 AttributeError 糊脸上。
    c.add_argument("--no-sign", action="store_true", help="跳过签名解析（更快）")
    c.set_defaults(handler=cmd_apk_popup)

    c = _add_common(a.add_parser("icon", help="提取图标"), device=False)
    c.add_argument("file")
    c.add_argument("-o", "--out", default="")
    c.add_argument("--open", action="store_true")
    c.set_defaults(handler=cmd_apk_icon)

    c = _add_common(a.add_parser("sign", help="签名信息（本机文件，完整）"), device=False)
    c.add_argument("file")
    c.add_argument("--verify", action="store_true")
    c.set_defaults(handler=cmd_apk_sign)

    c = _add_common(a.add_parser("copyname", help="复制包名/路径/版本到剪贴板"), device=False)
    c.add_argument("file")
    c.add_argument("--what", default="pkg", choices=["pkg", "path", "version"],
                   help="复制哪一项（默认包名）")
    c.set_defaults(handler=cmd_apk_copyname)

    c = _add_common(a.add_parser("compare-device", help="与设备已装版本对比（版本 + 签名）"))
    c.add_argument("file")
    c.set_defaults(handler=cmd_apk_compare_device)

    c = _add_common(a.add_parser("slim", help="体积构成"), device=False)
    c.add_argument("file")
    c.set_defaults(handler=cmd_apk_slim)

    c = _add_common(a.add_parser("diff", help="双包对比（两侧都可以来自设备）"), device=True)
    c.add_argument("a", nargs="?", default="", help="A 侧：本机 APK 路径")
    c.add_argument("b", nargs="?", default="", help="B 侧：本机 APK 路径（可省略）")
    c.add_argument("--a-pkg", default="", help="A 侧改用设备上的包名")
    c.add_argument("--b-pkg", default="", help="B 侧改用设备上的包名；留空 = 设备上的同名应用")
    c.set_defaults(handler=cmd_apk_diff)

    c = _add_common(a.add_parser("scan", help="批量解析目录"), device=False)
    c.add_argument("dir")
    c.add_argument("--csv", default="")
    c.add_argument("--limit", type=int, default=0)
    c.set_defaults(handler=cmd_apk_scan)

    # ---- device ----
    dev = sub.add_parser("device", help="设备管理")
    d = dev.add_subparsers(dest="cmd", metavar="<cmd>")
    c = _add_common(d.add_parser("list", help="设备列表"), device=False)
    c.set_defaults(handler=cmd_device_list)
    c = _add_common(d.add_parser("info", help="设备信息"))
    c.set_defaults(handler=cmd_device_info)
    c = _add_common(d.add_parser("wifi", help="无线 adb"))
    c.add_argument("action", choices=["connect", "disconnect"])
    c.add_argument("target", nargs="?", default="")
    c.set_defaults(handler=cmd_device_wifi)
    c = _add_common(d.add_parser("tools", help="设备工具集"))
    c.add_argument("op", help="stop|clear|uninstall|dpi|rotate|airplane|reboot|input_text|wake ...")
    c.add_argument("arg", nargs="*")
    c.set_defaults(handler=cmd_device_tools)

    # ---- app ----
    app = sub.add_parser("app", help="已安装应用")
    s = app.add_subparsers(dest="cmd", metavar="<cmd>")
    c = _add_common(s.add_parser("list", help="应用列表"))
    c.add_argument("--filter", default="")
    c.add_argument("--system", action="store_true", help="包含系统应用")
    c.set_defaults(handler=cmd_app_list)
    c = _add_common(s.add_parser("info", help="应用详情"))
    c.add_argument("package")
    c.set_defaults(handler=cmd_app_info)
    c = _add_common(s.add_parser("sign", help="已安装应用签名（免 root）"))
    c.add_argument("package")
    c.add_argument("--fast", action="store_true", help="只走快路径（dumpsys，约 0.3s）")
    c.add_argument("--refresh", action="store_true", help="忽略缓存重新拉取")
    c.set_defaults(handler=cmd_app_sign)
    c = _add_common(s.add_parser("sign-cmp", help="与本机 APK 比对签名"))
    c.add_argument("package")
    c.add_argument("--apk", required=True)
    c.set_defaults(handler=cmd_app_sign_cmp)
    c = _add_common(s.add_parser("action", help="强制停止/清数据/卸载"))
    c.add_argument("package")
    c.add_argument("op", choices=["stop", "clear", "uninstall", "enable", "disable", "restart"])
    c.set_defaults(handler=cmd_app_action)
    c = _add_common(s.add_parser("export", help="导出已安装 APK"))
    c.add_argument("package")
    c.add_argument("-o", "--out", default="")
    c.set_defaults(handler=cmd_app_export)
    c = _add_common(s.add_parser("labels", help="补齐应用名（不拉整包，约 1 秒/个）"))
    c.add_argument("--system", action="store_true", help="连系统应用一起补")
    c.add_argument("--workers", type=int, default=6, help="并发数（默认 6）")
    c.add_argument("--clear-cache", action="store_true", help="先清空已有缓存")
    c.set_defaults(handler=cmd_app_labels)

    # ---- launch ----
    lp = sub.add_parser("launch", help="启动组件")
    l = lp.add_subparsers(dest="cmd", metavar="<cmd>")
    c = _add_common(l.add_parser("list", help="列出可启动组件"))
    c.add_argument("package")
    c.add_argument("--type", choices=["activity", "service", "receiver", "provider"], default="")
    c.set_defaults(handler=cmd_launch_list)
    c = _add_common(l.add_parser("start", help="启动组件"))
    _add_intent_args(c)
    c.add_argument("--dry-run", action="store_true", help="只回显命令，不真的执行")
    c.add_argument("--logcat", type=int, default=0, help="启动后抓 N 秒日志")
    c.add_argument("--log-level", default="V")
    c.set_defaults(handler=cmd_launch_start)
    c = _add_common(l.add_parser("broadcast", help="发送广播"))
    _add_intent_args(c)
    c.set_defaults(handler=cmd_launch_broadcast)
    c = _add_common(l.add_parser("history", help="启动历史"), device=False)
    c.add_argument("--limit", type=int, default=20)
    c.set_defaults(handler=cmd_launch_history)
    c = _add_common(l.add_parser("shortcut", help="我的入口"), device=False)
    c.add_argument("op", choices=["list", "add", "del"])
    c.add_argument("--name", default="")
    c.add_argument("--component", default="")
    c.add_argument("--args", default="")
    c.set_defaults(handler=cmd_launch_shortcut)

    # ---- capture ----
    cp = sub.add_parser("capture", help="截图 / 录屏 / 日志")
    k = cp.add_subparsers(dest="cmd", metavar="<cmd>")
    c = _add_common(k.add_parser("screenshot", help="截图"))
    c.add_argument("-o", "--out", default="")
    c.add_argument("--open", action="store_true")
    c.set_defaults(handler=cmd_capture_screenshot)
    c = _add_common(k.add_parser("record", help="录屏"))
    c.add_argument("op", choices=["start", "stop"])
    c.add_argument("--bitrate", default="8M")
    c.add_argument("--size", default="")
    c.add_argument("--time-limit", type=int, default=180)
    c.set_defaults(handler=cmd_capture_record)
    c = _add_common(k.add_parser("mirror", help="scrcpy 实时镜像"))
    c.add_argument("--record", default="")
    c.set_defaults(handler=cmd_capture_mirror)
    c = _add_common(k.add_parser("logcat", help="日志"))
    c.add_argument("--package", default="")
    c.add_argument("--level", default="V")
    c.add_argument("--filter", default="")
    c.add_argument("--duration", type=float, default=0, help=">0 则流式输出该秒数")
    c.add_argument("--lines", type=int, default=200)
    c.set_defaults(handler=cmd_capture_logcat)
    c = _add_common(k.add_parser("artifacts", help="产物列表"), device=False)
    c.add_argument("--limit", type=int, default=50)
    c.set_defaults(handler=cmd_capture_artifacts)

    # ---- install ----
    ip = sub.add_parser("install", help="安装")
    i = ip.add_subparsers(dest="cmd", metavar="<cmd>")
    c = _add_common(i.add_parser("one", help="安装一个或多个 APK"))
    c.add_argument("apk", nargs="+")
    # -r 与 --no-reinstall 互斥：覆盖安装本来就是默认行为，-r 是显式写法，
    # 让 `-t -r -d` 这种"照着 adb install 的参数写"的命令行能直接用。
    rg = c.add_mutually_exclusive_group()
    rg.add_argument("-r", "--reinstall", action="store_true",
                    help="覆盖安装（默认已开；显式写出便于与 adb install 对齐）")
    rg.add_argument("--no-reinstall", action="store_true",
                    help="不加 -r（设备上已有同包名时会失败）")
    c.add_argument("-d", "--downgrade", action="store_true", help="允许降级")
    c.add_argument("-t", "--test", action="store_true", help="允许 test 包")
    c.add_argument("-g", "--grant", action="store_true", help="自动授予运行权限")
    c.add_argument("-y", "--yes", action="store_true",
                   help="遇到版本降级不再询问，直接按 -d 安装")
    c.add_argument("--check-only", action="store_true", help="只做预检不安装")
    c.add_argument("--console", action="store_true", help="在独立控制台窗口里执行")
    c.set_defaults(handler=cmd_install_one)
    c = _add_common(i.add_parser("dir", help="安装目录下所有 APK"))
    c.add_argument("dir")
    c.add_argument("--parallel", action="store_true")
    c.set_defaults(handler=cmd_install_dir)
    c = _add_common(i.add_parser("all", help="多机并发安装"), device=False)
    c.add_argument("apk")
    c.add_argument("--devices", default="")
    c.set_defaults(handler=cmd_install_all)
    c = _add_common(i.add_parser("errors", help="INSTALL_FAILED_* 中文词表"), device=False)
    c.add_argument("code", nargs="?", default="")
    c.set_defaults(handler=cmd_install_errors)

    # ---- file ----
    fp = sub.add_parser("file", help="文件传输")
    f = fp.add_subparsers(dest="cmd", metavar="<cmd>")
    for op, src, dst in (("push", "本地路径", "设备路径"), ("pull", "设备路径", "本地路径")):
        c = _add_common(f.add_parser(op, help="%s → %s" % (src, dst)))
        c.add_argument("src")
        c.add_argument("dst")
        c.set_defaults(handler=cmd_file, op=op)
    c = _add_common(f.add_parser("ls", help="列设备目录"))
    c.add_argument("src")
    c.set_defaults(handler=cmd_file, op="ls")

    # ---- sys ----
    sp = sub.add_parser("sys", help="系统与环境")
    y = sp.add_subparsers(dest="cmd", metavar="<cmd>")
    c = _add_common(y.add_parser("doctor", help="环境自检"), device=False)
    c.set_defaults(handler=cmd_sys_doctor)
    c = _add_common(y.add_parser("config", help="配置读写"), device=False)
    c.add_argument("op", choices=["get", "set", "reset"])
    c.add_argument("key", nargs="?", default="")
    c.add_argument("value", nargs="?", default="")
    c.set_defaults(handler=cmd_sys_config)
    c = _add_common(y.add_parser("menu", help="右键菜单注入"), device=False)
    c.add_argument("op", choices=["install", "uninstall", "status"])
    c.add_argument("--style", choices=["cascade", "flat"], default="cascade",
                   help="cascade=一个「APK 工具」级联菜单（默认）；"
                        "flat=各项平铺在 .apk 菜单里（保底，一定能显示）")
    c.set_defaults(handler=cmd_sys_menu)

    # ---- web ----
    wp = sub.add_parser("web", help="主控台")
    w = wp.add_subparsers(dest="cmd", metavar="<cmd>")
    c = _add_common(w.add_parser("start", help="启动主控台"), device=False)
    c.add_argument("--port", type=int, default=0)
    c.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    c.add_argument("--foreground", action="store_true", help="前台运行（Ctrl+C 停止）")
    c.set_defaults(handler=cmd_web, op="start")
    c = _add_common(w.add_parser("stop", help="停止主控台"), device=False)
    c.set_defaults(handler=cmd_web, op="stop")
    c = _add_common(w.add_parser("restart", help="重启主控台（先停旧实例再启动）"), device=False)
    c.add_argument("--port", type=int, default=0)
    c.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    c.add_argument("--foreground", action="store_true", help="前台运行（Ctrl+C / 关窗口即停止）")
    c.set_defaults(handler=cmd_web, op="restart")
    c = _add_common(w.add_parser("status", help="主控台状态"), device=False)
    c.set_defaults(handler=cmd_web, op="status")

    return p


def _add_intent_args(c) -> None:
    c.add_argument("target", nargs="?", default="",
                   help="组件，形如 pkg/.Activity 或 pkg/com.x.Cls")
    c.add_argument("-a", "--action", default="")
    c.add_argument("-d", "--data", default="")
    c.add_argument("-c", "--cat", action="append", default=[], help="可多次")
    c.add_argument("-f", "--flag", action="append", default=[], help="NEW_TASK / CLEAR_TOP / ...")
    c.add_argument("-n", "--component", default="", help="与 target 等价，显式写法")
    c.add_argument("--es", action="append", nargs=2, default=[], metavar=("K", "V"))
    c.add_argument("--ei", action="append", nargs=2, default=[], metavar=("K", "V"))
    c.add_argument("--ez", action="append", nargs=2, default=[], metavar=("K", "V"))
    c.add_argument("--ef", action="append", nargs=2, default=[], metavar=("K", "V"))
    c.add_argument("--el", action="append", nargs=2, default=[], metavar=("K", "V"))
    c.add_argument("--eu", action="append", nargs=2, default=[], metavar=("K", "V"))
    c.add_argument("-W", "--wait", action="store_true", help="等待启动完成并输出 TotalTime")
    c.add_argument("--package", default="", help="限定包（logcat 过滤用）")
    c.add_argument("--user", default="", help="--user N")


# ---------------------------------------------------------------- 主入口

def main(argv=None) -> int:
    console.init()
    if argv is None:
        argv = sys.argv[1:]
    # --traceback 是调试开关，不进 argparse（免得每个子命令都要注册一遍）。
    # 必须在 parse_args **之前**摘掉，否则 argparse 直接报 unrecognized arguments，
    # 这个参数就永远用不了（历史坑：提示让加 --traceback，加了却被拒收）。
    want_tb = "--traceback" in argv
    argv = [a for a in argv if a != "--traceback"]

    parser = build_parser()
    ns = parser.parse_args(argv)

    handler = getattr(ns, "handler", None)
    if handler is None:
        parser.print_help()
        return 2

    try:
        return int(handler(ns) or 0)
    except KeyboardInterrupt:
        console.warn("已中断")
        return 130
    except Exception as e:
        import traceback
        console.error("%s: %s" % (type(e).__name__, e))
        if getattr(ns, "verbose", False) or want_tb:
            traceback.print_exc()
        else:
            console.hint("加 --traceback 可看完整堆栈")
        return 1
