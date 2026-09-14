@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if /i "%~1"=="--syntax-check" (
  echo BATCH_SYNTAX_OK
  exit /b 0
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "WB_EXIT_CODE=%ERRORLEVEL%"
exit /b %WB_EXIT_CODE%
