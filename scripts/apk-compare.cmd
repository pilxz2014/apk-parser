@echo off
rem Right-click "Compare with device version". Keep ASCII-only.
title APK Compare - %~nx1
call "%~dp0apk.cmd" apk compare-device "%~1"
echo.
echo --------------------------------------------
echo Press any key to close this window...
pause >nul
