#!/usr/bin/env bash
# 右键「打开主控台」
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/apk.sh" web start
