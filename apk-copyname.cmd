@echo off
rem Right-click "Copy package name" to clipboard. Keep ASCII-only.
call "%~dp0apk.cmd" apk copyname "%~1"
if errorlevel 1 (
  echo.
  pause >nul
)
