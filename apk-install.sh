#!/usr/bin/env bash
# 「安装到设备」→ 在终端里安装并显示完整日志
# 额外参数原样透传（写法与 adb install 对齐）：
#   apk-install.sh a.apk -t -r -d       -y = 遇到降级不再询问，直接装
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "APK 安装 - $(basename "$1")"
echo "────────────────────────────────────────────"
"$HERE/apk.sh" install one "$@"
echo
read -r -p "按回车关闭..." _
