@echo off
setlocal
title APK Console - Stop
cd /d "%~dp0"

rem ---- find a usable python -------------------------------------------
set "PY="
for %%P in ("python" "py" "python3") do if not defined PY (
    where %%~P >nul 2>nul && %%~P -c "import sys" >nul 2>nul && set "PY=%%~P"
)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY (
    echo [X] Python not found in PATH. Please install Python 3.8+ first.
    pause
    exit /b 1
)

%PY% apktool.py web stop
echo.
pause
endlocal
