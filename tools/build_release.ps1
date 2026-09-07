[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$Version = "",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$projectPath = Join-Path $repoRoot "pyproject.toml"
$initPath = Join-Path $repoRoot "src\workbuddy_pythongo\__init__.py"

if (-not (Test-Path -LiteralPath $projectPath -PathType Leaf)) {
    throw "Project metadata not found: $projectPath"
}
if (-not (Test-Path -LiteralPath $initPath -PathType Leaf)) {
    throw "Package version file not found: $initPath"
}

$projectText = Get-Content -LiteralPath $projectPath -Raw
$projectVersionMatch = [regex]::Match(
    $projectText,
    '(?m)^version\s*=\s*"(?<version>[^"]+)"\s*$'
)
if (-not $projectVersionMatch.Success) {
    throw "Project version not found in pyproject.toml"
}
$projectVersion = $projectVersionMatch.Groups["version"].Value
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = $projectVersion
}
if ($Version -ne $projectVersion) {
    throw "Requested version $Version does not match pyproject.toml version $projectVersion"
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[A-Za-z0-9._-]+)?$') {
    throw "Unsupported release version: $Version"
}

$initText = Get-Content -LiteralPath $initPath -Raw
if ($initText -notmatch ('__version__\s*=\s*"' + [regex]::Escape($Version) + '"')) {
    throw "Requested version $Version does not match src\workbuddy_pythongo\__init__.py"
}

$expectedWheel = "workbuddy_pythongo_bridge-$Version-py3-none-any.whl"
$zipName = "workbuddy-pythongo-bridge-$Version.zip"
$releaseNotesName = "RELEASE-v$Version.md"
$releaseNotesPath = Join-Path $repoRoot "docs\$releaseNotesName"

if (-not (Test-Path -LiteralPath $releaseNotesPath -PathType Leaf)) {
    throw "Release notes not found: $releaseNotesPath"
}
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot "dist"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$releasePatterns = @(
    "workbuddy_pythongo_bridge-*-py3-none-any.whl",
    "workbuddy-pythongo-bridge-*.zip",
    "workbuddy-pythongo-bridge-*.zip.sha256",
    "SHA256SUMS.txt"
)
Get-ChildItem -LiteralPath $OutputDirectory -File | Where-Object {
    $name = $_.Name
    @($releasePatterns | Where-Object { $name -like $_ }).Count -gt 0
} | Remove-Item -Force

& $Python -m pip wheel $repoRoot --no-deps --no-build-isolation --no-cache-dir --wheel-dir $OutputDirectory
if ($LASTEXITCODE -ne 0) {
    throw "Wheel build failed with exit code $LASTEXITCODE"
}

$wheelPath = Join-Path $OutputDirectory $expectedWheel
if (-not (Test-Path -LiteralPath $wheelPath -PathType Leaf)) {
    throw "Expected wheel was not created: $wheelPath"
}

$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$stageFull = [System.IO.Path]::GetFullPath(
    (Join-Path $tempRoot ("workbuddy-pythongo-release-" + [guid]::NewGuid().ToString("N")))
)
if (-not $stageFull.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe temporary staging path: $stageFull"
}

try {
    New-Item -ItemType Directory -Path $stageFull | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $stageFull "examples") | Out-Null

    $installerCandidates = @(Get-ChildItem -LiteralPath $repoRoot -File -Filter "*.cmd")
    if ($installerCandidates.Count -ne 1) {
        throw "Expected exactly one CMD installer in the repository root; found $($installerCandidates.Count)"
    }

    Copy-Item -LiteralPath $wheelPath -Destination $stageFull
    Copy-Item -LiteralPath $installerCandidates[0].FullName -Destination $stageFull
    Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination $stageFull
    Copy-Item -LiteralPath $releaseNotesPath -Destination $stageFull
    Copy-Item -LiteralPath (Join-Path $repoRoot "docs\README-RELEASE.zh-CN.md") -Destination (Join-Path $stageFull "README-RELEASE.zh-CN.md")
    Copy-Item -LiteralPath (Join-Path $repoRoot "docs\DESIGN.zh-CN.md") -Destination (Join-Path $stageFull "workbuddy-pythongo-bridge-design.md")
    Copy-Item -LiteralPath (Join-Path $repoRoot "docs\RELEASE-MANIFEST.zh-CN.md") -Destination $stageFull
    Copy-Item -LiteralPath (Join-Path $repoRoot "examples\P0-CHECKLIST.zh-CN.md") -Destination (Join-Path $stageFull "examples")
    Copy-Item -LiteralPath (Join-Path $repoRoot "examples\trade-request.observe.json") -Destination (Join-Path $stageFull "examples")
    Copy-Item -LiteralPath (Join-Path $repoRoot "examples\workbuddy.mcp.example.json") -Destination $stageFull
    Copy-Item -LiteralPath (Join-Path $repoRoot "examples\workbuddy.mcp.example.README.md") -Destination $stageFull

    $releaseGuidePath = Join-Path $stageFull "README-RELEASE.zh-CN.md"
    $releaseGuideText = Get-Content -LiteralPath $releaseGuidePath -Raw
    $releaseGuideText = $releaseGuideText.Replace(
        "(../examples/P0-CHECKLIST.zh-CN.md)",
        "(examples/P0-CHECKLIST.zh-CN.md)"
    ).Replace(
        "(DESIGN.zh-CN.md)",
        "(workbuddy-pythongo-bridge-design.md)"
    )
    Set-Content -LiteralPath $releaseGuidePath -Value $releaseGuideText -Encoding utf8NoBOM -NoNewline

    $internalFiles = @(Get-ChildItem -LiteralPath $stageFull -File -Recurse | Sort-Object FullName)
    $internalHashes = foreach ($file in $internalFiles) {
        $relative = [System.IO.Path]::GetRelativePath($stageFull, $file.FullName).Replace("\", "/")
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $relative"
    }
    $internalHashes | Set-Content -LiteralPath (Join-Path $stageFull "SHA256SUMS.txt") -Encoding utf8NoBOM

    $zipPath = Join-Path $OutputDirectory $zipName
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $stageFull,
        $zipPath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )

    $requiredEntries = @(
        $installerCandidates[0].Name,
        "LICENSE",
        "README-RELEASE.zh-CN.md",
        "workbuddy-pythongo-bridge-design.md",
        "RELEASE-MANIFEST.zh-CN.md",
        $releaseNotesName,
        "SHA256SUMS.txt",
        $expectedWheel,
        "examples/P0-CHECKLIST.zh-CN.md",
        "examples/trade-request.observe.json",
        "workbuddy.mcp.example.json",
        "workbuddy.mcp.example.README.md"
    )
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $archiveEntries = @($archive.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
        $missingEntries = @($requiredEntries | Where-Object { $_ -notin $archiveEntries })
        if ($missingEntries.Count -gt 0) {
            throw "Release ZIP is missing required entries: $($missingEntries -join ', ')"
        }
    }
    finally {
        $archive.Dispose()
    }

    $wheelHash = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    @(
        "$wheelHash  $expectedWheel"
        "$zipHash  $zipName"
    ) | Set-Content -LiteralPath (Join-Path $OutputDirectory "SHA256SUMS.txt") -Encoding ascii
    "$zipHash  $zipName" | Set-Content -LiteralPath (Join-Path $OutputDirectory "$zipName.sha256") -Encoding ascii

    Write-Host "Release build completed:"
    Get-ChildItem -LiteralPath $OutputDirectory -File |
        Where-Object { $_.Name -in @($expectedWheel, $zipName, "$zipName.sha256", "SHA256SUMS.txt") } |
        Sort-Object Name |
        Select-Object Name, Length, LastWriteTime |
        Format-Table -AutoSize
}
finally {
    if (Test-Path -LiteralPath $stageFull -PathType Container) {
        $resolvedStage = [System.IO.Path]::GetFullPath($stageFull)
        if (-not $resolvedStage.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove unsafe temporary path: $resolvedStage"
        }
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
