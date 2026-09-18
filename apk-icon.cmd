@echo off
rem Right-click "Extract icon". Keep ASCII-only.
call "%~dp0apk.cmd" apk icon "%~1" --open
if errorlevel 1 pause >nul
