#!/usr/bin/env bash
# ============================================================
#  APK 工具 —— 通用入口（macOS / Linux）
#  用法：./apk.sh apk popup ~/a.apk
#        ./apk.sh install one ~/a.apk
# ============================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then PY=python3
  elif command -v python >/dev/null 2>&1; then PY=python
  else
    echo "[错误] 找不到 python3。请安装 Python 3.8+。" >&2
    exit 9009
  fi
fi

export PYTHONUTF8=1
exec "$PY" "$HERE/../apktool.py" "$@"
