# -*- coding: utf-8 -*-
"""安装前预检 —— 把"装完才知道失败"变成"装之前就知道"。

这是安装功能里最有价值的一块：五类最常见的安装失败（版本降级、签名冲突、
ABI 不兼容、minSdk 过高、test-only）**全都能在安装前判定**，
判定结果直接映射到 `errors.py` 里的同一个错误码，措辞一致。
"""

from pathlib import Path
from typing import Dict, List

from ..device import adb as adb_mod


def _item(level: str, title: str, detail: str, hint: str = "", code: str = "",
          **extra) -> dict:
    """一条预检结论。

    额外的关键字参数会原样并进结果里，作为**机器可读**的字段
    （比如版本降级时的 `package` / `versionCode` / `deviceVersionCode`）。
    上层要判断"是不是降级"、"降了多少"就看这些字段 —— 别去 parse detail 字符串，
    那种写法改一次文案就全断。
    """
    d = {"level": level, "title": title, "detail": detail, "hint": hint, "code": code}
    if extra:
        d.update(extra)
    return d


def precheck(apk_path, serial: str = "", deep_sign: bool = False) -> List[dict]:
    """对一个 APK 做安装前检查。

    :param deep_sign: 是否做签名比对（需要拉设备上的包，慢一点）
    """
    items: List[dict] = []
    p = Path(apk_path)

    if not p.is_file():
        return [_item("fail", "文件", "找不到文件：%s" % p)]

    # ---- 1. 解析 APK ----
    from ..apk import parse_apk
    info = parse_apk(p, with_signature=False, with_icon=False, with_components=False)
    if info.error:
        items.append(_item("fail", "APK 可解析", info.error,
                           "文件可能损坏或不是 APK", "INSTALL_FAILED_INVALID_APK"))
        return items
    items.append(_item("pass", "APK 可解析",
                       "%s · v%s (%s)" % (info.package, info.version_name, info.version_code),
                       package=info.package, versionName=info.version_name,
                       versionCode=info.version_code))

    if not serial:
        return items

    # ---- 2. 设备信息 ----
    dev_sdk = 0
    dev_abis: List[str] = []
    try:
        from ..device import adb as adb_mod2
        r = adb_mod2.shell_cmd("getprop ro.build.version.sdk", serial=serial, timeout=15)
        dev_sdk = int((r.out or "0").strip() or 0)
        r2 = adb_mod2.shell_cmd("getprop ro.product.cpu.abilist", serial=serial, timeout=15)
        dev_abis = [a.strip() for a in (r2.out or "").split(",") if a.strip()]
        if not dev_abis:
            r3 = adb_mod2.shell_cmd("getprop ro.product.cpu.abi", serial=serial, timeout=15)
            dev_abis = [(r3.out or "").strip()] if (r3.out or "").strip() else []
    except Exception:
        pass

    # ---- 3. minSdk ----
    if dev_sdk and info.min_sdk:
        if info.min_sdk > dev_sdk:
            items.append(_item("fail", "系统版本",
                               "APK 要求 minSdk %d，设备是 %d" % (info.min_sdk, dev_sdk),
                               "换台高版本设备，或把 minSdkVersion 调低",
                               "INSTALL_FAILED_OLDER_SDK"))
        else:
            items.append(_item("pass", "系统版本",
                               "minSdk %d ≤ 设备 SDK %d" % (info.min_sdk, dev_sdk)))

    # ---- 4. ABI ----
    if info.abis and dev_abis:
        ok_abi = [a for a in info.abis if a in dev_abis]
        if not ok_abi:
            items.append(_item("fail", "CPU 架构",
                               "APK 含 %s，设备支持 %s，没有交集" % (
                                   "/".join(info.abis), "/".join(dev_abis)),
                               "重新打包带上设备的 ABI（如 arm64-v8a）",
                               "INSTALL_FAILED_NO_MATCHING_ABIS"))
        elif len(ok_abi) < len(info.abis):
            items.append(_item("info", "CPU 架构",
                               "APK 含 %s，设备可用 %s（其余架构用不上，属正常）" % (
                                   "/".join(info.abis), "/".join(ok_abi))))
        else:
            items.append(_item("pass", "CPU 架构", "设备支持 %s" % "/".join(ok_abi)))
    elif not info.abis:
        items.append(_item("info", "CPU 架构", "纯 Java 包（无 native 库），任何架构都能装"))

    # ---- 5. 已安装版本 ----
    try:
        from ..device import apps as apps_mod
        detail = apps_mod.app_detail(info.package, serial)
        if detail.get("error"):
            items.append(_item("pass", "设备上未安装",
                               "这是一次全新安装（不会覆盖任何应用）"))
        else:
            inst_vc = int(detail.get("versionCode") or 0)
            inst_vn = detail.get("versionName") or "-"
            if inst_vc and info.version_code:
                if info.version_code < inst_vc:
                    items.append(_item(
                        "fail", "版本号",
                        "要装 build %s，设备上已是 build %s —— 覆盖安装会被拒绝（降级）" % (
                            info.version_code, inst_vc),
                        "确认要降级就勾选「允许降级」（对应 -d）",
                        "INSTALL_FAILED_VERSION_DOWNGRADE",
                        package=info.package,
                        versionName=info.version_name, versionCode=info.version_code,
                        deviceVersionName=inst_vn, deviceVersionCode=inst_vc))
                elif info.version_code == inst_vc:
                    items.append(_item("warn", "版本号",
                                       "与设备上已装的 build %s 相同，属于重装" % inst_vc,
                                       "会保留应用数据",
                                       package=info.package,
                                       versionName=info.version_name,
                                       versionCode=info.version_code,
                                       deviceVersionName=inst_vn,
                                       deviceVersionCode=inst_vc))
                else:
                    items.append(_item("pass", "版本号",
                                       "升级：build %s → %s" % (inst_vc, info.version_code),
                                       package=info.package,
                                       versionName=info.version_name,
                                       versionCode=info.version_code,
                                       deviceVersionName=inst_vn,
                                       deviceVersionCode=inst_vc))

            # ---- 6. 签名比对 ----
            if info.signature is None:
                info.signature = None
            local_sig = info.signature
            if local_sig is None:
                from ..apk.cert import analyze as _an
                from ..apk.reader import ApkReader
                try:
                    with ApkReader(p) as rd:
                        local_sig = _an(rd)
                except Exception:
                    local_sig = None

            if deep_sign and local_sig and local_sig.ok:
                from ..device import signature as sig_mod
                dev_sig = sig_mod.get_signature(info.package, device=serial,
                                                fast=True, full=False)
                if dev_sig.get("ok") and dev_sig.get("identity"):
                    if dev_sig["identity"] == local_sig.digest_identity:
                        items.append(_item("pass", "签名",
                                           "与设备已装版本签名一致（%s）" % local_sig.digest_identity))
                    else:
                        items.append(_item(
                            "fail", "签名",
                            "本包 %s ≠ 设备上 %s —— 这是换过签名，覆盖安装一定失败" % (
                                local_sig.digest_identity, dev_sig["identity"]),
                            "先卸载设备上的版本，或用一致的 keystore 重新打包",
                            "INSTALL_FAILED_UPDATE_INCOMPATIBLE"))
                else:
                    items.append(_item("info", "签名",
                                       "设备侧签名取不到，跳过比对（不影响安装）"))
            else:
                items.append(_item("info", "签名", "签名：%s" % (
                    " · ".join(s.upper() for s in local_sig.schemes) if local_sig and local_sig.ok else "未签名/未解析"),
                    "如需与设备已装版本比对，勾选「深度预检」"))
    except Exception as e:
        items.append(_item("info", "设备检查", "部分检查跳过：%s" % e))

    # ---- 7. test-only / debuggable ----
    if info.debuggable:
        items.append(_item("warn", "调试标记",
                           "这个包带 debuggable=true（debug 构建）",
                           "正式分发前记得用 release 包"))

    # ---- 8. targetSdk 过低的提醒 ----
    if info.target_sdk and info.target_sdk < 23:
        items.append(_item("warn", "targetSdk",
                           "targetSdk 只有 %d，在 Android 6+ 上可能拿不到运行时权限" % info.target_sdk))

    return items


def summary(items: List[dict]) -> dict:
    """预检结果统计。"""
    fails = [i for i in items if i["level"] == "fail"]
    warns = [i for i in items if i["level"] == "warn"]
    return {
        "pass": len([i for i in items if i["level"] == "pass"]),
        "warn": len(warns),
        "fail": len(fails),
        "blocking": bool(fails),
        "firstError": fails[0]["code"] if fails else "",
        "headline": (
            "预检发现 %d 个会直接导致失败的问题" % len(fails) if fails
            else ("预检通过，有 %d 条提醒" % len(warns) if warns else "预检通过")
        ),
    }
