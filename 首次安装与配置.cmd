@echo off
setlocal
chcp 65001 >nul
title WorkBuddy PythonGO First Setup
cd /d "%~dp0"

if /i "%~1"=="--syntax-check" (
  echo BATCH_SYNTAX_OK
  exit /b 0
)

echo ============================================================
echo WorkBuddy-PythonGO Bridge - First Setup
echo ============================================================
echo This installer will:
echo   1. Check for Python 3.10 or newer.
echo   2. Install or update the Bridge package.
echo   3. Start a Chinese guided setup for runtime, MCP, account binding and shortcuts.
echo   4. Show the remaining manual actions in InfiniTrader.
echo.
echo Safety defaults:
echo   - New environments always start in OBSERVE_ONLY.
echo   - The wizard asks before MCP changes, account binding or shortcut creation.
echo   - It does not start InfiniTrader, Worker or WorkBuddy.
echo   - It does not sign a Profile or enable trading.
echo ============================================================
echo.
echo Press any key to continue. Press Ctrl+C to cancel.
pause >nul

echo [Step 1/4] Checking for Python 3.10 or newer...
set "PYTHON_CMD="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"
if defined PYTHON_CMD goto :python_ready
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=python"

:python_ready
if not defined PYTHON_CMD goto :python_missing
echo Python check passed: %PYTHON_CMD%

echo.
echo [Step 2/4] Installing or updating WorkBuddy-PythonGO Bridge...
set "WB_WHEEL="
set "WB_WHEEL_COUNT=0"
for %%F in ("%~dp0workbuddy_pythongo_bridge-*.whl") do if exist "%%~fF" (
  set /a WB_WHEEL_COUNT+=1
  set "WB_WHEEL=%%~fF"
)
if %WB_WHEEL_COUNT% GTR 1 goto :multiple_wheels
if defined WB_WHEEL goto :install_wheel
if exist "%~dp0pyproject.toml" goto :install_source
goto :install_source_missing

:install_wheel
echo Release wheel: %WB_WHEEL%
%PYTHON_CMD% -m pip install --user --upgrade --force-reinstall --no-deps --disable-pip-version-check "%WB_WHEEL%"
goto :install_done

:install_source
echo Source directory: %~dp0
%PYTHON_CMD% -m pip install --upgrade --force-reinstall --no-deps --no-build-isolation --disable-pip-version-check "%~dp0."

:install_done
if errorlevel 1 goto :install_failed
echo Package installation completed.

echo.
echo [Step 3/4] Starting the Chinese guided setup...
echo The wizard marks every input. Press Enter to accept a value shown in brackets.
echo.
%PYTHON_CMD% -m workbuddy_pythongo.desktop setup --root "%~dp0runtime"
if errorlevel 1 goto :setup_failed

echo.
echo [Step 4/4] First setup completed.
echo Closing this window does not start Worker, InfiniTrader or WorkBuddy.
echo Follow the manual actions printed by the guided setup above.
echo Full guide: README-RELEASE.zh-CN.md
set "WB_EXIT_CODE=0"
goto :finish

:python_missing
echo.
echo [ERROR] Python 3.10 or newer was not found.
echo Install Python, enable Add Python to PATH, and run this file again.
set "WB_EXIT_CODE=2"
goto :finish

:install_source_missing
echo.
echo [ERROR] Neither a release wheel nor pyproject.toml was found beside this file.
echo Extract the complete release package or run this file from the project root.
set "WB_EXIT_CODE=2"
goto :finish

:multiple_wheels
echo.
echo [ERROR] More than one WorkBuddy-PythonGO wheel was found beside this file.
echo Keep only the wheel from the current release and run this file again.
set "WB_EXIT_CODE=2"
goto :finish

:install_failed
echo.
echo [ERROR] Package installation or update failed.
echo Keep the complete pip error above and fix the reported Python, pip or permission problem.
set "WB_EXIT_CODE=2"
goto :finish

:setup_failed
echo.
echo [ERROR] Guided setup did not complete.
echo Existing config, keys, token and Profile were not silently overwritten.
echo Fix the specific error shown above and run this file again.
set "WB_EXIT_CODE=2"
goto :finish

:finish
echo.
echo Press any key to close this window...
pause >nul
exit /b %WB_EXIT_CODE%
