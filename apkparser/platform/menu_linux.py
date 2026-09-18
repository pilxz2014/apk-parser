# -*- coding: utf-8 -*-
"""Linux 集成。

两条线：
  1. **文件管理器动作**（`~/.local/share/file-manager/actions/*.desktop`）
     —— KDE/Dolphin、PCManFM、Nemo 等支持；GNOME Files 原生不支持，
     所以同时给出一条 `nautilus-scripts` 路径作为兜底。
  2. **默认打开方式**（`~/.local/share/applications/*.desktop`）
"""

from pathlib import Path
from typing import Dict, List

from . import paths

ACTIONS_DIR = Path.home() / ".local" / "share" / "file-manager" / "actions"
APPS_DIR = Path.home() / ".local" / "share" / "applications"
NAUTILUS_DIR = Path.home() / ".local" / "share" / "nautilus" / "scripts"

ITEMS = [
    ("info", "解析 APK 信息", "apk-info.sh", "APKParsed"),
    ("install", "安装到设备", "apk-install.sh", "APKInstall"),
    ("icon", "提取图标", "apk-icon.sh", "APKExtractIcon"),
    ("copyname", "复制包名", "apk-copyname.sh", "APKCopyName"),
]

ACTION_TPL = """[Desktop Entry]
Type=Action
Name={label}
Name[zh_CN]={label}
Profiles=APKParser;

[X-Action-Profile APKParser]
MimeTypes=application/vnd.android.package-archive;
Exec={script} %f
"""

APP_TPL = """[Desktop Entry]
Type=Application
Name=APK 工具
Comment=APK 解析 / 安装 / 启动组件
Exec={root}/run-web.sh
MimeType=application/vnd.android.package-archive;
Terminal=false
Categories=Development;Utility;
"""


def available() -> bool:
    return paths.IS_LINUX


def install(project_root: Path = None) -> Dict:
    if not available():
        return {"ok": False, "error": "此平台不支持", "steps": []}

    root = Path(project_root) if project_root else paths.project_root()
    steps: List[str] = []
    missing: List[str] = []

    ACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    for key, label, script, _icon in ITEMS:
        sfile = root / script
        if not sfile.is_file():
            missing.append(script)
        try:
            sfile.chmod(0o755)
        except OSError:
            pass
        dest = ACTIONS_DIR / ("apk-parser-%s.desktop" % key)
        dest.write_text(ACTION_TPL.format(label=label, script=sfile), encoding="utf-8")
        steps.append("生成文件管理器动作：%s" % dest)

    # GNOME Files 兜底
    try:
        NAUTILUS_DIR.mkdir(parents=True, exist_ok=True)
        for key, label, script, _icon in ITEMS:
            dest = NAUTILUS_DIR / ("APK 工具 - %s" % label)
            dest.write_text('#!/bin/bash\nexec "%s" "$@"\n' % (root / script), encoding="utf-8")
            dest.chmod(0o755)
        steps.append("生成 Nautilus 脚本：%s（GNOME Files 用）" % NAUTILUS_DIR)
    except OSError as e:
        steps.append("Nautilus 脚本生成失败：%s" % e)

    # 打开方式
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    app = APPS_DIR / "apk-parser.desktop"
    app.write_text(APP_TPL.format(root=root), encoding="utf-8")
    steps.append("注册打开方式：%s" % app)
    steps.append("设为默认：xdg-mime default apk-parser.desktop application/vnd.android.package-archive")

    return {"ok": not missing, "steps": steps, "missingScripts": missing}


def uninstall() -> Dict:
    if not available():
        return {"ok": False, "error": "此平台不支持", "steps": []}
    steps = []
    for f in list(ACTIONS_DIR.glob("apk-parser-*.desktop")):
        try:
            f.unlink()
            steps.append("已删除 %s" % f)
        except OSError:
            pass
    for key, label, _s, _i in ITEMS:
        f = NAUTILUS_DIR / ("APK 工具 - %s" % label)
        if f.exists():
            try:
                f.unlink()
                steps.append("已删除 %s" % f)
            except OSError:
                pass
    app = APPS_DIR / "apk-parser.desktop"
    if app.exists():
        try:
            app.unlink()
            steps.append("已删除 %s" % app)
        except OSError:
            pass
    return {"ok": True, "steps": steps or ["没有发现需要清理的文件"]}


def status() -> Dict:
    if not available():
        return {"supported": False, "installed": False, "items": []}
    items = []
    for key, label, _script, _icon in ITEMS:
        f = ACTIONS_DIR / ("apk-parser-%s.desktop" % key)
        items.append({"key": key, "label": label, "registered": f.exists(), "command": str(f)})
    return {
        "supported": True,
        "installed": any(i["registered"] for i in items),
        "menuRoot": str(ACTIONS_DIR),
        "openWithRegistered": (APPS_DIR / "apk-parser.desktop").exists(),
        "items": items,
    }
