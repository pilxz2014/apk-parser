# -*- coding: utf-8 -*-
"""macOS 集成。

macOS 没有注册表那种"注册即生效"的右键扩展：
正规做法是 Automator Quick Action（.workflow）或 Finder Sync Extension。
本模块生成 Quick Action 的工作流骨架 + 一份启用指引 ——
因为最终需要用户在「系统设置 → 隐私与安全性 → 扩展」里勾一次，
这是 macOS 的设计，无法绕过，所以我们把该做的做掉、把剩下的一步说清楚。
"""

import plistlib
from pathlib import Path
from typing import Dict, List

from . import paths

SERVICES_DIR = Path.home() / "Library" / "Services"
WORKFLOW_NAME = "APK 工具.workflow"

ACTIONS = [
    ("解析 APK 信息", "apk-info.sh"),
    ("安装到设备", "apk-install.sh"),
    ("提取图标", "apk-icon.sh"),
    ("复制包名", "apk-copyname.sh"),
]


def available() -> bool:
    return paths.IS_MAC


def _workflow_plist(script: Path, label: str) -> dict:
    """生成一个 Run Shell Script 型 Quick Action 的 plist。"""
    return {
        "AMApplicationBuild": "521",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [{
            "action": {
                "AMAccepts": {"Container": "List", "Optional": False, "Types": ["com.apple.cocoa.string"]},
                "AMActionVersion": "2.0.3",
                "AMApplication": ["Automator"],
                "AMParameterProperties": {
                    "COMMAND_STRING": {"default value": str(script)},
                    "CheckedForUserDefaultShell": {"default value": True},
                    "inputMethod": {"default value": 1},
                    "shell": {"default value": "/bin/bash"},
                    "source": {"default value": ""},
                },
                "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
                "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
                "ActionName": "Run Shell Script",
                "ActionParameters": {
                    "COMMAND_STRING": '"%s" "$@"' % script,
                    "inputMethod": 1,
                    "shell": "/bin/bash",
                },
                "BundleIdentifier": "com.apple.Automator.RunShellScript",
                "CFBundleVersion": "2.0.3",
                "Class": "RunShellScript",
                "InputUUID": "00000000-0000-0000-0000-000000000000",
                "OutputUUID": "00000000-0000-0000-0000-000000000001",
                "UUID": "00000000-0000-0000-0000-000000000002",
            },
            "isViewVisible": 1,
        }],
        "workflowMetaData": {
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": 0,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
        "name": label,
    }


def install(project_root: Path = None) -> Dict:
    if not available():
        return {"ok": False, "error": "此平台不支持", "steps": []}

    root = Path(project_root) if project_root else paths.project_root()
    SERVICES_DIR.mkdir(parents=True, exist_ok=True)

    steps: List[str] = []
    missing: List[str] = []

    for label, script in ACTIONS:
        sfile = root / script
        if not sfile.is_file():
            missing.append(script)
        wf = SERVICES_DIR / ("%s.workflow" % label)
        contents = wf / "Contents"
        contents.mkdir(parents=True, exist_ok=True)
        try:
            (contents / "document.wflow").write_bytes(
                plistlib.dumps(_workflow_plist(sfile, label)))
            (contents / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "local.apkparser.%s" % abs(hash(label)),
                "CFBundleName": label,
                "CFBundlePackageType": "WFP",
            }))
            steps.append("生成 Quick Action：%s" % wf)
        except Exception as e:
            steps.append("生成失败 %s：%s" % (wf, e))

    steps.append("")
    steps.append("还需手动一步（macOS 的限制，无法自动化）：")
    steps.append("  系统设置 → 隐私与安全性 → 扩展 → 访达扩展 → 勾选「APK 工具」各项")
    steps.append("  之后在 Finder 里右键 .apk → 快速操作 就能看到它们")

    return {"ok": not missing, "steps": steps, "missingScripts": missing}


def uninstall() -> Dict:
    if not available():
        return {"ok": False, "error": "此平台不支持", "steps": []}
    steps = []
    for label, _script in ACTIONS:
        wf = SERVICES_DIR / ("%s.workflow" % label)
        if wf.exists():
            import shutil
            try:
                shutil.rmtree(wf)
                steps.append("已删除 %s" % wf)
            except OSError as e:
                steps.append("删除失败 %s：%s" % (wf, e))
    return {"ok": True, "steps": steps or ["没有发现已安装的 Quick Action"]}


def status() -> Dict:
    if not available():
        return {"supported": False, "installed": False, "items": []}
    items = []
    for label, _script in ACTIONS:
        wf = SERVICES_DIR / ("%s.workflow" % label)
        items.append({"key": label, "label": label, "registered": wf.exists(),
                      "command": str(wf)})
    return {
        "supported": True,
        "installed": any(i["registered"] for i in items),
        "menuRoot": str(SERVICES_DIR),
        "items": items,
        "note": "Quick Action 需在「系统设置 → 隐私与安全性 → 扩展」中启用一次",
    }
