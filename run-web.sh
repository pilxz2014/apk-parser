#!/usr/bin/env bash
# 启动 APK 工具主控台
# 语义与 Windows 侧 start.bat 一致：先停掉上一次的服务，再启动新的（web restart）
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/apk.sh" web restart
