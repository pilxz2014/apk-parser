#!/usr/bin/env bash
# 「提取图标」
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$HERE/apk.sh" apk icon "$1" --open
