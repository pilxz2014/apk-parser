#!/usr/bin/env bash
# 「复制包名」到剪贴板
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$HERE/apk.sh" apk copyname "$1"
