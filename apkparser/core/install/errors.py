# -*- coding: utf-8 -*-
"""`INSTALL_FAILED_*` 中文词表。

安装失败时最没用的就是只给一个英文错误码 —— 这里的每条都回答三个问题：
**发生了什么 / 为什么 / 怎么办**，并且"怎么办"里有一条是能直接点的动作。
"""

import re
from typing import Dict, List, Optional

# code → (含义, 常见原因, [解决办法...])
ERRORS: Dict[str, tuple] = {
    "INSTALL_FAILED_ALREADY_EXISTS": (
        "应用已存在",
        "设备上已经装了同包名的应用，且安装时没加 -r（覆盖安装）",
        ["加 -r 覆盖安装（本工具默认已加）",
         "或先卸载旧版本：app action <包名> uninstall"],
    ),
    "INSTALL_FAILED_VERSION_DOWNGRADE": (
        "版本降级被拒绝",
        "要装的包 versionCode 比设备上已装的**低**，Android 默认禁止降级",
        ["确认确实要降级后，加 -d 参数（本工具：勾选「允许降级」）",
         "或先卸载再装：app action <包名> uninstall",
         "如果只是版本号忘了改，回构建侧把 versionCode 提上去"],
    ),
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": (
        "签名不一致，无法覆盖",
        "设备上已装的应用与本包**签名不同**（换过 keystore，或装的是渠道包/第三方包）",
        ["先卸载再装（会丢失应用数据，注意）",
         "或统一签名：用与线上一致的 keystore 重新打包",
         "用「与设备已装版本对比」确认是不是签名问题"],
    ),
    "INSTALL_PARSE_FAILED_INCONSISTENT_CERTIFICATES": (
        "同一包内签名不一致",
        "APK 里 v1/v2 签名用了不同证书（签名流程出错）",
        ["重新签名：apksigner sign 时确保只用一个 keystore",
         "检查构建脚本是否对同一个包签了两次"],
    ),
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": (
        "APK 没有签名",
        "构建产物没签名（debug 包正常不会有这个问题）",
        ["用 apksigner 签名后再装",
         "检查 Gradle 的 signingConfig 是否配上了"],
    ),
    "INSTALL_FAILED_INVALID_APK": (
        "APK 本身无效",
        "文件损坏、下载不全，或不是真正的 APK",
        ["重新下载/重新构建",
         "对文件跑一次右键「解析 APK 信息」，能解析出来才说明文件是好的"],
    ),
    "INSTALL_FAILED_INVALID_URI": (
        "安装包路径无效",
        "传给系统的路径不存在或不可读",
        ["确认文件路径没被移动/删除",
         "路径含中文或空格时用引号包起来"],
    ),
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": (
        "设备存储空间不足",
        "目标分区剩余空间不够放下这个包和它的解压产物",
        ["清理设备空间后重试",
         "用 capture/gallery 清理本地产物不会影响设备；设备侧可用 device tools storage 查看"],
    ),
    "INSTALL_FAILED_DUPLICATE_PACKAGE": (
        "包名重复",
        "设备上已有同名包（多见于残留的卸载不干净）",
        ["卸载后重装",
         "必要时 pm uninstall -k --user 0 <包名>"],
    ),
    "INSTALL_FAILED_NO_MATCHING_ABIS": (
        "ABI 不兼容",
        "APK 里的 native 库架构与设备 CPU 架构都对不上",
        ["用「适应设备 ABI」重新打包（如设备是 arm64-v8a，就至少带上它）",
         "检查 APK 的 lib/ 目录：armeabi-v7a 的包装不到只支持 arm64 的设备上"],
    ),
    "INSTALL_FAILED_CPU_ABI_INCOMPATIBLE": (
        "CPU 架构不兼容",
        "同上，native 库架构不匹配",
        ["重新打包带上正确的 ABI"],
    ),
    "INSTALL_FAILED_OLDER_SDK": (
        "设备系统版本太低",
        "APK 的 minSdkVersion 高于设备 Android 版本",
        ["换台高版本设备测试",
         "或把 minSdkVersion 降下来（注意兼容性风险）"],
    ),
    "INSTALL_FAILED_NEWER_SDK": (
        "设备系统版本太高",
        "APK 声明了 maxSdkVersion 且设备超过了它",
        ["去掉 maxSdkVersion 限制后重新打包"],
    ),
    "INSTALL_FAILED_TEST_ONLY": (
        "分包被标记为 testOnly",
        "AndroidManifest 里带了 android:testOnly=\"true\"（常见于 Android Studio 的 Run 产物）",
        ["安装时加 -t 参数（本工具已支持：勾选「允许 test 包」）",
         "或构建 release 包，别用 IDE 直接 Run 出来的那个 apk"],
    ),
    "INSTALL_FAILED_MISSING_SHARED_LIBRARY": (
        "缺少共享库",
        "APK 依赖一个设备上没有的共享库（uses-library 声明了但设备没有）",
        ["确认测试机 ROM 是否包含该库",
         "如需可选依赖，把 uses-library 的 required 改为 false"],
    ),
    "INSTALL_FAILED_CONFLICTING_PROVIDER": (
        "ContentProvider 冲突",
        "两个应用声明了相同的 authorities",
        ["改掉自己应用的 authorities（加包名后缀）",
         "或卸载那个冲突的应用"],
    ),
    "INSTALL_FAILED_DUPLICATE_PERMISSION": (
        "自定义权限重复",
        "多个应用声明了同名自定义权限但签名不同",
        ["改掉权限名或统一签名"],
    ),
    "INSTALL_FAILED_USER_RESTRICTED": (
        "被系统限制安装",
        "小米/华为/OPPO 等厂商 ROM 的「USB 安装」开关没开，或被安全策略拦截",
        ["手机上：设置 → 开发者选项 → 打开「USB 安装」/「通过 USB 安装应用」",
         "小米还需关掉「MIUI 优化」或登录账号后再试",
         "部分 ROM 需要在弹窗里点「允许」"],
    ),
    "INSTALL_FAILED_VERIFICATION_FAILURE": (
        "安装校验失败",
        "系统的安装校验（如 Google Play Protect）拦截了这个包",
        ["手机上关闭「扫描安装应用」类选项后重试",
         "或在开发者选项里关闭「验证应用」"],
    ),
    "INSTALL_FAILED_VERIFICATION_TIMEOUT": (
        "安装校验超时",
        "校验服务没及时响应（网络/系统卡顿）",
        ["重试一次；仍然失败就关掉安装校验"],
    ),
    "INSTALL_FAILED_PACKAGE_CHANGED": (
        "安装过程中包被改动",
        "安装还没结束，APK 文件就被重写/删除了（常见于构建脚本同时输出）",
        ["等构建完全结束再装",
         "把 APK 复制到一个稳定目录后再安装"],
    ),
    "INSTALL_FAILED_UID_CHANGED": (
        "应用 UID 变了",
        "重装时分配到了不同的 UID（系统认为数据不兼容）",
        ["卸载后重装（会清数据）"],
    ),
    "INSTALL_FAILED_DEXOPT": (
        "dex 优化失败",
        "设备空间不足，或 dex 本身有问题",
        ["清空间后重试", "检查是否用了不兼容的 dex 版本/加固工具"],
    ),
    "INSTALL_FAILED_CONTAINER_ERROR": (
        "加密容器错误",
        "设备存储加密状态异常，或 SD 卡安装受限",
        ["重启设备后重试", "改为安装到内部存储"],
    ),
    "INSTALL_FAILED_MEDIA_UNAVAILABLE": (
        "安装位置不可用",
        "指定的安装位置（如 SD 卡）当前不可挂载",
        ["插好卡/改为内部存储"],
    ),
    "INSTALL_FAILED_INVALID_INSTALL_LOCATION": (
        "安装位置无效",
        "manifest 里的 installLocation 与设备策略冲突",
        ["改成 auto 或 internalOnly 后重新打包"],
    ),
    "INSTALL_FAILED_SHARED_USER_INCOMPATIBLE": (
        "共享用户不兼容",
        "android:sharedUserId 冲突或签名不符",
        ["确认与同名 sharedUserId 的应用签名一致",
         "新系统已废弃该机制，建议改用 ContentProvider/Service 共享数据"],
    ),
    "INSTALL_FAILED_REPLACE_COULDNT_DELETE": (
        "覆盖时删不掉旧版本",
        "旧版本正在运行或被系统占用",
        ["先在设备上停掉该应用，再重试",
         "或先卸载再装"],
    ),
    "INSTALL_PARSE_FAILED_NOT_APK": (
        "不是 APK 文件",
        "文件头不是 zip/APK（可能下载成了 HTML 错误页）",
        ["确认文件是真正的 APK；用右键「解析 APK 信息」验证"],
    ),
    "INSTALL_PARSE_FAILED_BAD_MANIFEST": (
        "AndroidManifest 畸形",
        "manifest 损坏或被改成非法内容",
        ["重新构建；若用了加固/渠道工具，检查其是否破坏了 manifest"],
    ),
    "INSTALL_PARSE_FAILED_MANIFEST_MALFORMED": (
        "AndroidManifest 格式错误",
        "manifest 里有非法节点或属性",
        ["检查最近改过的 manifest 片段（尤其是 uses-sdk、provider 的 authorities）"],
    ),
    "INSTALL_PARSE_FAILED_MANIFEST_EMPTY": (
        "AndroidManifest 为空",
        "manifest 里缺少必需内容（如没包名）",
        ["检查构建产物；确认没有被打包工具清空"],
    ),
    "INSTALL_PARSE_FAILED_UNEXPECTED_EXCEPTION": (
        "解析包里出现异常",
        "包结构不标准（改包工具、加固、zip 重打包常见）",
        ["用原始构建产物重试，排除中间加工步骤"],
    ),
    "INSTALL_PARSE_FAILED_BAD_PACKAGE_NAME": (
        "包名非法",
        "包名不符合规则（如含中划线、以数字开头）",
        ["改成合法包名后重新打包"],
    ),
    "INSTALL_PARSE_FAILED_CERTIFICATE_ENCODING": (
        "证书编码异常",
        "签名证书里有设备解析不了的编码（老工具签的包常见）",
        ["用新版 apksigner 重新签名"],
    ),
    "INSTALL_FAILED_INTERNAL_ERROR": (
        "系统内部错误",
        "PackageManager 抛了未预期的异常（多为 ROM 问题）",
        ["重启设备后重试",
         "adb kill-server 后重试",
         "换一台设备验证是否 ROM 特有问题"],
    ),
    "INSTALL_FAILED_ABORTED": (
        "安装被中止",
        "用户或系统主动取消了安装（设备端弹窗被拒）",
        ["重试并留意手机屏幕上的弹窗"],
    ),
    "INSTALL_FAILED_MISSING_FEATURE": (
        "缺少必需硬件特性",
        "manifest 声明了 uses-feature required=true，而设备没有该特性",
        ["把该 feature 改成 required=false",
         "或换台具备该硬件的设备"],
    ),
    "INSTALL_FAILED_PERMISSION_MODEL_DOWNGRADE": (
        "权限模型降级",
        "目标 SDK 从 23+ 降到 23 以下，系统不允许",
        ["把 targetSdkVersion 提回 23 及以上"],
    ),
}

# `Failure [CODE: 附加信息]`
_CODE_RE = re.compile(r"(INSTALL_[A-Z_]+)")


def explain(code: str) -> dict:
    """把一个错误码翻译成结构化解释。

    未知错误码也返回结构（给出原始码 + 通用建议），不返回空。
    """
    raw = (code or "").strip()
    m = _CODE_RE.search(raw)
    key = m.group(1) if m else raw

    entry = ERRORS.get(key)
    if entry:
        meaning, cause, fixes = entry
    else:
        meaning = "未收录的错误码"
        cause = "该错误码不在本工具的词表里（可能是新版 Android 新增，或厂商自定义）"
        fixes = [
            "把完整的报错原文贴出来搜一下（本工具保留了原始 stderr）",
            "adb logcat 里搜 PackageManager 能看到更详细的原因",
        ]

    return {
        "code": key or raw or "UNKNOWN",
        "raw": raw,
        "meaning": meaning,
        "cause": cause,
        "fixes": list(fixes),
        "severity": "error",
    }


def all_codes() -> List[dict]:
    out = []
    for code, (meaning, cause, _fixes) in sorted(ERRORS.items()):
        out.append({"code": code, "meaning": meaning, "cause": cause})
    return out


def from_output(text: str) -> Optional[dict]:
    """从 `adb install` 的输出里识别错误码。识别不到返回 None。"""
    if not text:
        return None
    m = _CODE_RE.search(text)
    if not m:
        return None
    return explain(m.group(1))
