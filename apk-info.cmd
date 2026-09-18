@echo off
rem Right-click "Parse APK info": popup info card. Keep ASCII-only.
call "%~dp0apk.cmd" apk popup "%~1" --quiet
if errorlevel 1 (
  echo.
  echo Parse failed. Press any key to close...
  pause >nul
)
