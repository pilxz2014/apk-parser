@echo off
rem Right-click "Open console": start (or focus) the web console. Keep ASCII-only.
call "%~dp0apk.cmd" web start
if errorlevel 1 pause >nul
