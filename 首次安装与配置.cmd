@echo off
setlocal
chcp 65001 >nul
title WorkBuddy PythonGO 首次安装与配置
cd /d "%~dp0"

if /i "%~1"=="--syntax-check" (
  echo BATCH_SYNTAX_OK
  exit /b 0
)

echo ============================================================
echo WorkBuddy-PythonGO Bridge - 首次安装与配置
echo ============================================================
echo 本安装器将：
echo   1. 检查Python 3.10或更高版本。
echo   2. 安装或更新Bridge软件包到当前用户的默认Python环境。
echo   3. 引导配置稳定运行目录、MCP、账号、无限易部署和桌面入口。
echo   4. 显示开始使用查询功能所需的最后操作。
echo.
echo 默认行为：
echo   - 新环境固定从OBSERVE_ONLY开始，查询功能不要求先完成P0。
echo   - 修改MCP、绑定账号、部署策略和创建入口前都会明确提示。
echo   - 不会自动启动无限易、Worker或WorkBuddy。
echo   - 不会签名Profile，也不会启用交易。
echo ============================================================
echo.
echo 按任意键继续，按Ctrl+C取消。
pause >nul

echo [步骤 1/4] 正在检查Python 3.10或更高版本……
set "PYTHON_CMD="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"
if defined PYTHON_CMD goto :python_ready
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=python"

:python_ready
if not defined PYTHON_CMD goto :python_missing
echo Python检查通过：%PYTHON_CMD%

echo.
echo [步骤 2/4] 正在安装或更新WorkBuddy-PythonGO Bridge……
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
echo 安装包：%WB_WHEEL%
%PYTHON_CMD% -m pip install --user --upgrade --force-reinstall --no-deps --disable-pip-version-check "%WB_WHEEL%"
goto :install_done

:install_source
echo 源代码目录：%~dp0
%PYTHON_CMD% -m pip install --user --upgrade --force-reinstall --no-deps --no-build-isolation --disable-pip-version-check "%~dp0."

:install_done
if errorlevel 1 goto :install_failed
echo 软件包安装完成。

echo.
echo [步骤 3/4] 正在启动中文配置向导……
echo 每个输入项都有明确提示；直接按Enter可接受方括号中的默认值。
echo.
set "WB_RUNTIME_ROOT=%LOCALAPPDATA%\WorkBuddyPythonGO\runtime"
if not defined LOCALAPPDATA set "WB_RUNTIME_ROOT=%USERPROFILE%\AppData\Local\WorkBuddyPythonGO\runtime"
%PYTHON_CMD% -m workbuddy_pythongo.desktop setup --root "%WB_RUNTIME_ROOT%" --discover-existing --legacy-root "%~dp0runtime"
if errorlevel 1 goto :setup_failed

echo.
echo [步骤 4/4] 首次配置已完成。
echo 关闭本窗口不会启动Worker、无限易或WorkBuddy。
echo 请按向导上方显示的“查询功能下一步”操作。
echo 完整说明：README-RELEASE.zh-CN.md
set "WB_EXIT_CODE=0"
goto :finish

:python_missing
echo.
echo [错误] 未找到Python 3.10或更高版本。
echo 请安装Python并勾选“Add Python to PATH”，然后重新运行本文件。
set "WB_EXIT_CODE=2"
goto :finish

:install_source_missing
echo.
echo [错误] 本文件旁边既没有发布wheel，也没有pyproject.toml。
echo 请完整解压发布包，或从项目根目录运行本文件。
set "WB_EXIT_CODE=2"
goto :finish

:multiple_wheels
echo.
echo [错误] 本文件旁边发现了多个WorkBuddy-PythonGO wheel。
echo 请只保留当前版本的wheel，然后重新运行本文件。
set "WB_EXIT_CODE=2"
goto :finish

:install_failed
echo.
echo [错误] 软件包安装或更新失败。
echo 请保留上方完整pip错误，并按提示处理Python、pip或权限问题。
set "WB_EXIT_CODE=2"
goto :finish

:setup_failed
echo.
echo [错误] 配置向导未完成。
echo 现有配置、密钥、token和Profile没有被静默覆盖。
echo 请按上方具体错误处理后重新运行本文件。
set "WB_EXIT_CODE=2"
goto :finish

:finish
echo.
echo 按任意键关闭本窗口……
pause >nul
exit /b %WB_EXIT_CODE%
