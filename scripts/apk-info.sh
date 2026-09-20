#!/usr/bin/env bash
# 右键「解析 APK 信息」→ 弹信息卡
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$HERE/apk.sh" apk popup "$1" --quiet || {
  echo
  read -r -p "解析失败，按回车关闭..." _
}
