param(
    [string]$PythonExe = ".\\.venv\\Scripts\\python.exe",
    [switch]$SkipPlaywright,
    [switch]$SkipWebView2,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\\.."))
$RuntimeManifestPath = Join-Path $ScriptDir "runtime-sources.json"
$RuntimeManifest = Get-Content -Raw -Encoding UTF8 $RuntimeManifestPath | ConvertFrom-Json

function Resolve-RepoPath {
    param([string]$RelativePath)
    return [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $RelativePath))
}

function Assert-LeafPathExists {
    param(
        [string]$PathValue,
        [string]$Label
    )

    if (-not (Test-Path $PathValue -PathType Leaf)) {
        throw "$Label not found: $PathValue"
    }
}

function Remove-PathIfExists {
    param([string]$PathValue)

    if (Test-Path $PathValue) {
        Remove-Item -LiteralPath $PathValue -Recurse -Force
    }
}

function New-CleanDirectory {
    param([string]$PathValue)

    Remove-PathIfExists -PathValue $PathValue
    New-Item -ItemType Directory -Force -Path $PathValue | Out-Null
}

function Assert-Hash {
    param(
        [string]$PathValue,
        [string]$ExpectedSha256
    )

    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $PathValue).Hash
    if ($actual -ne $ExpectedSha256) {
        throw "SHA256 mismatch for $PathValue. Expected $ExpectedSha256, got $actual"
    }
}

function Install-PlaywrightRuntime {
    param([string]$PythonExecutable)

    $playwrightRuntimeDir = Resolve-RepoPath $RuntimeManifest.playwright.runtime_dir
    $expectedEntries = @($RuntimeManifest.playwright.expected_entries)
    $needsInstall = $Force

    if (-not $needsInstall) {
        foreach ($entry in $expectedEntries) {
            if (-not (Test-Path (Join-Path $playwrightRuntimeDir $entry))) {
                $needsInstall = $true
                break
            }
        }
    }

    if (-not $needsInstall) {
        Write-Host "Playwright runtime already hydrated: $playwrightRuntimeDir"
        return
    }

    New-Item -ItemType Directory -Force -Path $playwrightRuntimeDir | Out-Null
    $env:PLAYWRIGHT_BROWSERS_PATH = $playwrightRuntimeDir
    & $PythonExecutable -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        throw "Playwright runtime install failed with exit code $LASTEXITCODE"
    }
}

function Install-WebView2Runtime {
    $runtimeDir = Resolve-RepoPath $RuntimeManifest.webview2.runtime_dir
    $archivePath = Resolve-RepoPath $RuntimeManifest.webview2.archive_path
    $downloadUrl = [string]$RuntimeManifest.webview2.download_url
    $requiredFiles = @($RuntimeManifest.webview2.required_files)

    $needsInstall = $Force
    if (-not $needsInstall) {
        foreach ($entry in $requiredFiles) {
            if (-not (Test-Path (Join-Path $runtimeDir $entry))) {
                $needsInstall = $true
                break
            }
        }
    }

    if (-not $needsInstall) {
        Write-Host "WebView2 runtime already hydrated: $runtimeDir"
        return
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $archivePath) | Out-Null
    if ($Force -or -not (Test-Path $archivePath -PathType Leaf)) {
        Invoke-WebRequest -Uri $downloadUrl -OutFile $archivePath
    }

    Assert-LeafPathExists -PathValue $archivePath -Label "WebView2 runtime archive"
    Assert-Hash -PathValue $archivePath -ExpectedSha256 ([string]$RuntimeManifest.webview2.archive_sha256)

    $extractDir = Join-Path (Split-Path -Parent $archivePath) "_webview2_extract"
    New-CleanDirectory -PathValue $extractDir
    New-CleanDirectory -PathValue $runtimeDir

    & expand.exe $archivePath -F:* $extractDir | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to extract WebView2 runtime archive"
    }

    Copy-Item -LiteralPath (Join-Path $extractDir "*") -Destination $runtimeDir -Recurse -Force
    Remove-PathIfExists -PathValue $extractDir

    foreach ($entry in $requiredFiles) {
        if (-not (Test-Path (Join-Path $runtimeDir $entry))) {
            throw "Hydrated WebView2 runtime is missing required entry: $entry"
        }
    }
}

$resolvedPythonExe = if ([System.IO.Path]::IsPathRooted($PythonExe)) { $PythonExe } else { Resolve-RepoPath $PythonExe }
Assert-LeafPathExists -PathValue $resolvedPythonExe -Label "Python interpreter"

if (-not $SkipPlaywright) {
    Install-PlaywrightRuntime -PythonExecutable $resolvedPythonExe
}

if (-not $SkipWebView2) {
    Install-WebView2Runtime
}

Write-Host "Runtime hydration completed."

