@echo off
rem Double-click / right-click "Install to device": run install in a
rem console window with full logs. Keep ASCII-only (see apk.cmd note).
rem
rem Extra args are forwarded, so adb-style flags work too:
rem   apk-install.cmd "D:\a.apk" -t -r -d
rem   -r  overwrite (already the default)   -t  allow test-only pkg
rem   -d  allow version downgrade           -y  no prompt on downgrade
rem Version lower than the device's? The installer asks y/N before using -d.
title APK Install - %~nx1
call "%~dp0apk.cmd" install one %*
echo.
echo --------------------------------------------
echo Press any key to close this window...
pause >nul
