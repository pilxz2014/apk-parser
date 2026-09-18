#!/usr/bin/env bash
# 「与设备已装版本对比」
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "APK 对比 - $(basename "$1")"
echo "────────────────────────────────────────────"
"$HERE/apk.sh" apk compare-device "$1"
echo
read -r -p "按回车关闭..." _
