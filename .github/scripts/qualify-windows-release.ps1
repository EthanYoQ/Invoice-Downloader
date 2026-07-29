param(
    [Parameter(Mandatory = $true)]
    [string]$Tag
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$Version = $Tag.TrimStart("v")
if ($Version -notmatch "^\d+\.\d+\.\d+\.\d+$") {
    throw "Release tag must use v<major>.<minor>.<patch>.<build>: $Tag"
}

$VersionParts = @($Version -split "\." | ForEach-Object { [int]$_ })
if ($VersionParts[3] -lt 1) {
    throw "The release contract needs a nonzero build component for the installer upgrade check."
}
$VersionParts[3] -= 1
$PredecessorVersion = $VersionParts -join "."
$AssetDir = Join-Path $RepoRoot "release-assets\windows"
$EvidencePath = Join-Path $AssetDir "windows-x64-qualification.json"
$RunnerTemp = [System.IO.Path]::GetFullPath($env:RUNNER_TEMP)
$TestRoot = Join-Path $RunnerTemp "invoiceflowai-release-$Version"
$InstallDir = Join-Path $TestRoot "installed"
$PortableDir = Join-Path $TestRoot "portable"
$PredecessorDir = Join-Path $TestRoot "predecessor"

function Assert-File {
    param([string]$PathValue, [string]$Label)
    if (-not (Test-Path -LiteralPath $PathValue -PathType Leaf)) {
        throw "$Label was not produced: $PathValue"
    }
}

function Invoke-Installer {
    param([string]$InstallerPath, [string]$TargetDir)
    $process = Start-Process -FilePath $InstallerPath -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/DIR=$TargetDir"
    ) -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Installer failed with exit code $($process.ExitCode): $InstallerPath"
    }
}

function Invoke-PackagedRuntimeSmoke {
    param([string]$ExecutablePath)
    $previous = $env:INVOICEFLOWAI_PLAYWRIGHT_RUNTIME_SMOKE
    try {
        $env:INVOICEFLOWAI_PLAYWRIGHT_RUNTIME_SMOKE = "1"
        & $ExecutablePath --help | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Packaged Playwright runtime smoke failed with exit code ${LASTEXITCODE}: $ExecutablePath"
        }
    }
    finally {
        if ($null -eq $previous) {
            Remove-Item Env:INVOICEFLOWAI_PLAYWRIGHT_RUNTIME_SMOKE -ErrorAction SilentlyContinue
        }
        else {
            $env:INVOICEFLOWAI_PLAYWRIGHT_RUNTIME_SMOKE = $previous
        }
    }
}

function Assert-ApplicationStarts {
    param([string]$ExecutablePath)
    $process = Start-Process -FilePath $ExecutablePath -PassThru
    Start-Sleep -Seconds 8
    if ($process.HasExited) {
        throw "Packaged application exited during launch check (exit $($process.ExitCode)): $ExecutablePath"
    }
    Stop-Process -Id $process.Id -Force
    $process.WaitForExit()
}

if (Test-Path -LiteralPath $TestRoot) {
    Remove-Item -LiteralPath $TestRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $AssetDir, $TestRoot, $PredecessorDir | Out-Null

& choco install innosetup --no-progress -y
if ($LASTEXITCODE -ne 0) {
    throw "Chocolatey could not install Inno Setup."
}
$CompilerPath = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
Assert-File -PathValue $CompilerPath -Label "Inno Setup compiler"

Push-Location $RepoRoot
try {
    $PythonExe = Join-Path ([string]$env:pythonLocation) "python.exe"
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        $PythonExe = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    }
    python -m pip install --upgrade pip
    python -m pip install -r requirements.release.txt pyinstaller
    & .\build\windows\prepare_runtime.ps1 -PythonExe $PythonExe
    & .\build\windows\build_release.ps1 `
        -Version $Version `
        -PythonExe $PythonExe `
        -BuildPythonExe $PythonExe `
        -InstallerCompilerPath $CompilerPath `
        -RunPyInstaller `
        -RunPortableZip `
        -RunInstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Windows release build failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

$InstallerSource = Join-Path $RepoRoot "dist\installer\InvoiceFlowAI-Setup-unsigned.exe"
$PortableSource = Join-Path $RepoRoot "dist\InvoiceFlowAI-portable-unsigned.zip"
$PackagedExe = Join-Path $RepoRoot "dist\InvoiceFlowAI\InvoiceFlowAI.exe"
Assert-File -PathValue $InstallerSource -Label "Windows installer"
Assert-File -PathValue $PortableSource -Label "Windows portable ZIP"
Assert-File -PathValue $PackagedExe -Label "Packaged application"

Invoke-PackagedRuntimeSmoke -ExecutablePath $PackagedExe

$PredecessorInstaller = Join-Path $PredecessorDir "InvoiceFlowAI-Setup-predecessor.exe"
& $CompilerPath "/DSourceDir=$([System.IO.Path]::GetDirectoryName($PackagedExe))" "/DOutputDir=$PredecessorDir" "/DOutputBaseName=InvoiceFlowAI-Setup-predecessor" "/DAppVersion=$PredecessorVersion" (Join-Path $RepoRoot "build\windows\installer\InvoiceFlowAI.iss")
if ($LASTEXITCODE -ne 0) {
    throw "Predecessor installer compilation failed with exit code $LASTEXITCODE."
}
Assert-File -PathValue $PredecessorInstaller -Label "Predecessor installer"
Invoke-Installer -InstallerPath $PredecessorInstaller -TargetDir $InstallDir

$SettingsDir = Join-Path $env:APPDATA "InvoiceFlowAI"
$SettingsPath = Join-Path $SettingsDir "user_settings.json"
if (Test-Path -LiteralPath $SettingsPath) {
    throw "Hosted runner unexpectedly already contains InvoiceFlowAI user settings; refusing to overwrite them."
}
New-Item -ItemType Directory -Force -Path $SettingsDir | Out-Null
$UpgradeMarker = "release-upgrade-$([guid]::NewGuid().ToString('N'))"
[System.IO.File]::WriteAllText($SettingsPath, "{`"values`":{`"release_marker`":`"$UpgradeMarker`"},`"protected`":{}}", [System.Text.UTF8Encoding]::new($false))

Invoke-Installer -InstallerPath $InstallerSource -TargetDir $InstallDir
$InstalledExe = Join-Path $InstallDir "InvoiceFlowAI.exe"
Assert-File -PathValue $InstalledExe -Label "Installed application"
if ((Get-Content -LiteralPath $SettingsPath -Raw) -notmatch [regex]::Escape($UpgradeMarker)) {
    throw "Installer upgrade removed user settings outside the install directory."
}
Invoke-PackagedRuntimeSmoke -ExecutablePath $InstalledExe
Assert-ApplicationStarts -ExecutablePath $InstalledExe

Expand-Archive -LiteralPath $PortableSource -DestinationPath $PortableDir -Force
$PortableExe = Join-Path $PortableDir "InvoiceFlowAI\InvoiceFlowAI.exe"
Assert-File -PathValue $PortableExe -Label "Portable application"
Invoke-PackagedRuntimeSmoke -ExecutablePath $PortableExe

$Uninstaller = Join-Path $InstallDir "unins000.exe"
Assert-File -PathValue $Uninstaller -Label "Uninstaller"
$uninstallProcess = Start-Process -FilePath $Uninstaller -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-") -Wait -PassThru
if ($uninstallProcess.ExitCode -ne 0 -or (Test-Path -LiteralPath $InstallDir)) {
    throw "Uninstall qualification failed."
}

$InstallerAsset = "InvoiceFlowAI-$Tag-windows-x64-setup.exe"
$PortableAsset = "InvoiceFlowAI-$Tag-windows-x64-portable.zip"
Copy-Item -LiteralPath $InstallerSource -Destination (Join-Path $AssetDir $InstallerAsset) -Force
Copy-Item -LiteralPath $PortableSource -Destination (Join-Path $AssetDir $PortableAsset) -Force
$revision = (git -C $RepoRoot rev-parse HEAD).Trim()
@{
    platform = "windows-x64"
    tag = $Tag
    source_revision = $revision
    assets = @($InstallerAsset, $PortableAsset)
    acceptance = @{
        packaged_playwright_smoke = $true
        installer_upgrade_preserves_user_settings = $true
        installed_application_launches = $true
        portable_playwright_smoke = $true
        uninstall = $true
    }
    signing = @{
        status = "unsigned"
        impact = "Windows may show a SmartScreen warning because this release is intentionally unsigned."
    }
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $EvidencePath -Encoding utf8

Remove-Item -LiteralPath $SettingsPath -Force
if ((Get-ChildItem -LiteralPath $SettingsDir -Force | Measure-Object).Count -eq 0) {
    Remove-Item -LiteralPath $SettingsDir -Force
}
Write-Host "Windows qualification passed. Assets: $InstallerAsset, $PortableAsset"
