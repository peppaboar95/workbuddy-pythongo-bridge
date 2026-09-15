[CmdletBinding()]
param([switch]$SyntaxCheck)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

try {
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
}
catch {
    # The host may not expose a console when the script is run by automation.
}

if ($SyntaxCheck) {
    Write-Output "POWERSHELL_INSTALLER_SYNTAX_OK"
    exit 0
}

function Wait-ForInstallerKey {
    param([string]$Message)

    Write-Host $Message
    try {
        if (-not [Console]::IsInputRedirected) {
            $null = [Console]::ReadKey($true)
        }
    }
    catch {
        # Do not turn a completed install into a failure when no interactive console exists.
    }
}

function Test-Python310 {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$PrefixArguments = @()
    )

    if (-not (Get-Command $Executable -ErrorAction SilentlyContinue)) {
        return $false
    }
    & $Executable @PrefixArguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 1>$null 2>$null
    return $LASTEXITCODE -eq 0
}

function Invoke-Installer {
    $script:InstallerExitCode = 0

    Write-Host "============================================================"
    Write-Host "WorkBuddy-PythonGO Bridge - 首次安装与配置"
    Write-Host "============================================================"
    Write-Host "本安装器将："
    Write-Host "  1. 检查Python 3.10或更高版本。"
    Write-Host "  2. 安装或更新Bridge软件包到当前用户的默认Python环境。"
    Write-Host "  3. 引导配置稳定运行目录、MCP、账号、无限易部署和桌面入口。"
    Write-Host "  4. 显示开始使用查询功能所需的最后操作。"
    Write-Host ""
    Write-Host "默认行为："
    Write-Host "  - 新环境固定从OBSERVE_ONLY开始，查询功能不要求先完成P0。"
    Write-Host "  - 修改MCP、绑定账号、部署策略和创建入口前都会明确提示。"
    Write-Host "  - 不会自动启动无限易、Worker或WorkBuddy。"
    Write-Host "  - 不会签名Profile，也不会启用交易。"
    Write-Host "============================================================"
    Write-Host ""
    Wait-ForInstallerKey "按任意键继续，按Ctrl+C取消。"

    Write-Host "[步骤 1/4] 正在检查Python 3.10或更高版本……"
    $pythonExecutable = $null
    $pythonPrefix = @()
    $pythonDisplay = $null
    if (Test-Python310 -Executable "py.exe" -PrefixArguments @("-3")) {
        $pythonExecutable = "py.exe"
        $pythonPrefix = @("-3")
        $pythonDisplay = "py -3"
    }
    elseif (Test-Python310 -Executable "python.exe") {
        $pythonExecutable = "python.exe"
        $pythonDisplay = "python"
    }
    else {
        Write-Host ""
        Write-Host "[错误] 未找到Python 3.10或更高版本。" -ForegroundColor Red
        Write-Host "请安装Python并勾选“Add Python to PATH”，然后重新运行本文件。"
        $script:InstallerExitCode = 2
        return
    }
    Write-Host "Python检查通过：$pythonDisplay"

    Write-Host ""
    Write-Host "[步骤 2/4] 正在安装或更新WorkBuddy-PythonGO Bridge……"
    $wheels = @(
        Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter "workbuddy_pythongo_bridge-*.whl"
    )
    if ($wheels.Count -gt 1) {
        Write-Host ""
        Write-Host "[错误] 本文件旁边发现了多个WorkBuddy-PythonGO wheel。" -ForegroundColor Red
        Write-Host "请只保留当前版本的wheel，然后重新运行本文件。"
        $script:InstallerExitCode = 2
        return
    }

    $pipArguments = @(
        "-m", "pip", "install", "--user", "--upgrade", "--force-reinstall",
        "--no-deps", "--disable-pip-version-check"
    )
    if ($wheels.Count -eq 1) {
        Write-Host "安装包：$($wheels[0].FullName)"
        $installTarget = $wheels[0].FullName
    }
    elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot "pyproject.toml") -PathType Leaf) {
        Write-Host "源代码目录：$PSScriptRoot"
        $pipArguments += "--no-build-isolation"
        $installTarget = $PSScriptRoot
    }
    else {
        Write-Host ""
        Write-Host "[错误] 本文件旁边既没有发布wheel，也没有pyproject.toml。" -ForegroundColor Red
        Write-Host "请完整解压发布包，或从项目根目录运行本文件。"
        $script:InstallerExitCode = 2
        return
    }

    & $pythonExecutable @pythonPrefix @pipArguments $installTarget
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "[错误] 软件包安装或更新失败。" -ForegroundColor Red
        Write-Host "请保留上方完整pip错误，并按提示处理Python、pip或权限问题。"
        $script:InstallerExitCode = 2
        return
    }
    Write-Host "软件包安装完成。"

    Write-Host ""
    Write-Host "[步骤 3/4] 正在启动中文配置向导……"
    Write-Host "每个输入项都有明确提示；直接按Enter可接受方括号中的默认值。"
    Write-Host ""
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $runtimeRoot = Join-Path $env:USERPROFILE "AppData\Local\WorkBuddyPythonGO\runtime"
    }
    else {
        $runtimeRoot = Join-Path $env:LOCALAPPDATA "WorkBuddyPythonGO\runtime"
    }
    $legacyRoot = Join-Path $PSScriptRoot "runtime"
    & $pythonExecutable @pythonPrefix -m workbuddy_pythongo.desktop setup `
        --root $runtimeRoot --discover-existing --legacy-root $legacyRoot
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "[错误] 配置向导未完成。" -ForegroundColor Red
        Write-Host "现有配置、密钥、token和Profile没有被静默覆盖。"
        Write-Host "请按上方具体错误处理后重新运行本文件。"
        $script:InstallerExitCode = 2
        return
    }

    Write-Host ""
    Write-Host "[步骤 4/4] 首次配置已完成。"
    Write-Host "关闭本窗口不会启动Worker、无限易或WorkBuddy。"
    Write-Host "请按向导上方显示的“查询功能下一步”操作。"
    Write-Host "完整说明：README-RELEASE.zh-CN.md"
}

try {
    $Host.UI.RawUI.WindowTitle = "WorkBuddy PythonGO 首次安装与配置"
}
catch {
}

Push-Location -LiteralPath $PSScriptRoot
try {
    Invoke-Installer
}
catch {
    Write-Host ""
    Write-Host "[错误] 安装器发生未预期错误：$($_.Exception.Message)" -ForegroundColor Red
    $script:InstallerExitCode = 2
}
finally {
    Pop-Location
}

Write-Host ""
Wait-ForInstallerKey "按任意键关闭本窗口……"
exit $script:InstallerExitCode
