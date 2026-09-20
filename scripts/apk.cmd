@echo off
rem ============================================================
rem  APK Toolkit - common entry (all .cmd are thin wrappers)
rem  Usage: apk.cmd info "D:\a.apk"
rem         apk.cmd install one "D:\a.apk"
rem         apk.cmd web start
rem  NOTE: keep this file ASCII-only. cmd.exe parses batch files
rem  with the OEM codepage; non-ASCII text here will garble.
rem ============================================================
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] python not found. Install Python 3.8+ and add to PATH.
    exit /b 9009
  )
  set "PY=py -3"
)
set "PYTHONUTF8=1"
%PY% "%HERE%..\apktool.py" %*
exit /b %ERRORLEVEL%
