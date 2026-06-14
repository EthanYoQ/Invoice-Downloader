param(
    [string]$Version = "0.0.0.0",
    [string]$PythonExe = ".\.venv\Scripts\python.exe",
    [string]$BuildPythonExe = ".\.venv\Scripts\python.exe",
    [string]$SignToolPath = "",
    [string]$InstallerCompilerPath = "",
    [switch]$RunPyInstaller,
    [switch]$RunPortableZip,
    [switch]$RunInstaller,
    [switch]$SignArtifacts,
    [switch]$SkipPythonVersionCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\.."))
$ManifestPath = Join-Path $ScriptDir "resources.manifest.json"

function Resolve-RepoPath {
    param([string]$RelativePath)
    return [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $RelativePath))
}

function Resolve-ToolPath {
    param([string]$PathValue)

    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return ""
    }

    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return $PathValue
    }

    if ($PathValue.Contains("\") -or $PathValue.Contains("/") -or $PathValue.StartsWith(".")) {
        return Resolve-RepoPath $PathValue
    }

    $command = Get-Command $PathValue -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return $command.Source
    }

    return $PathValue
}

function Assert-PathExists {
    param(
        [string]$PathValue,
        [string]$Label
    )

    if (-not (Test-Path $PathValue)) {
        if ($PathValue -match "[\\/]+build[\\/]+runtime[\\/]") {
            throw "$Label not found: $PathValue`nRun build\\windows\\prepare_runtime.ps1 before packaging."
        }
        throw "$Label not found: $PathValue"
    }
}

function Assert-LeafPathExists {
    param(
        [string]$PathValue,
        [string]$Label
    )

    if (-not (Test-Path $PathValue -PathType Leaf)) {
        if ($PathValue -match "[\\/]+build[\\/]+runtime[\\/]") {
            throw "$Label not found: $PathValue`nRun build\\windows\\prepare_runtime.ps1 before packaging."
        }
        throw "$Label not found: $PathValue"
    }
}

function Remove-PathIfExists {
    param([string]$PathValue)

    for ($attempt = 1; $attempt -le 5; $attempt++) {
        if (-not (Test-Path $PathValue)) {
            return
        }

        try {
            if (Test-Path $PathValue -PathType Container) {
                Remove-Item -LiteralPath $PathValue -Recurse -Force -ErrorAction Stop
            }
            else {
                Remove-Item -LiteralPath $PathValue -Force -ErrorAction Stop
            }
            if (Test-Path $PathValue) {
                throw "Path still exists after deletion attempt: $PathValue"
            }
            return
        }
        catch {
            if (-not (Test-Path $PathValue)) {
                return
            }
            if ($attempt -ge 5) {
                throw
            }
            Start-Sleep -Milliseconds 1000
        }
    }
}

function Clear-DirectoryContents {
    param([string]$PathValue)

    for ($attempt = 1; $attempt -le 5; $attempt++) {
        if (-not (Test-Path $PathValue -PathType Container)) {
            return
        }

        $items = @(Get-ChildItem -LiteralPath $PathValue -Force -ErrorAction SilentlyContinue)
        if ($items.Count -eq 0) {
            return
        }

        foreach ($item in $items) {
            try {
                if ($item.PSIsContainer) {
                    Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop
                }
                else {
                    Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
                }
            }
            catch {
                if (-not (Test-Path $item.FullName)) {
                    continue
                }
            }
        }

        $remaining = @(Get-ChildItem -LiteralPath $PathValue -Force -ErrorAction SilentlyContinue)
        if ($remaining.Count -eq 0) {
            return
        }

        Start-Sleep -Milliseconds 1000
    }

    $stillRemaining = @(Get-ChildItem -LiteralPath $PathValue -Force -ErrorAction SilentlyContinue)
    if ($stillRemaining.Count -gt 0) {
        throw "Failed to clear directory contents: $PathValue"
    }
}

function New-CleanDirectory {
    param([string]$PathValue)

    if (Test-Path $PathValue -PathType Container) {
        Clear-DirectoryContents -PathValue $PathValue
    }
    else {
        Remove-PathIfExists -PathValue $PathValue
    }
    New-Item -ItemType Directory -Force -Path $PathValue | Out-Null
}

function Invoke-Robocopy {
    param(
        [string]$Source,
        [string]$Destination
    )

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    robocopy $Source $Destination /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "Robocopy failed: $Source -> $Destination (exit code $LASTEXITCODE)"
    }
}

function Get-PythonVersion {
    param([string]$Executable)

    $version = & $Executable -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query Python version from: $Executable"
    }
    return ($version | Select-Object -First 1).Trim()
}

function Convert-VersionToTuple {
    param([string]$RawVersion)

    $parts = @()
    foreach ($segment in ($RawVersion -split "[^0-9]+")) {
        if ($segment -match "^\d+$") {
            $parts += [int]$segment
        }
    }

    while ($parts.Count -lt 4) {
        $parts += 0
    }

    return ($parts[0..3] -join ", ")
}

function Render-VersionInfo {
    param(
        [string]$TemplatePath,
        [string]$OutputPath,
        [string]$VersionText,
        [string]$ProductName
    )

    $tuple = Convert-VersionToTuple -RawVersion $VersionText
    $content = Get-Content -Raw -Encoding UTF8 $TemplatePath
    $content = $content.Replace("{{VERSION}}", $VersionText)
    $content = $content.Replace("{{VERSION_TUPLE}}", $tuple)
    $content = $content.Replace("{{PRODUCT_NAME}}", $ProductName)
    $content = $content.Replace("{{FILE_DESCRIPTION}}", "$ProductName Windows desktop application")
    $content = $content.Replace("{{COMPANY_NAME}}", "InvoiceFlowAI")
    Set-Content -Path $OutputPath -Value $content -Encoding UTF8
}

function Resolve-GitRevision {
    param([string]$RootPath)

    $gitCommand = Get-Command git -ErrorAction SilentlyContinue
    if (-not $gitCommand) {
        return "snapshot"
    }

    try {
        $revision = & $gitCommand.Source -C $RootPath rev-parse --short HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($revision)) {
            return ($revision | Select-Object -First 1).Trim()
        }
    }
    catch {
    }

    return "snapshot"
}

function Write-BuildIdentity {
    param(
        [string]$PythonExe,
        [string]$OutputPath,
        [string]$SourceRevision
    )

    $previousBuildTime = $env:INVOICEFLOW_BUILD_TIME
    $previousSourceRevision = $env:INVOICEFLOW_BUILD_SOURCE_REVISION
    $previousBuildLabel = $env:INVOICEFLOW_BUILD_LABEL
    try {
        $env:INVOICEFLOW_BUILD_TIME = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
        $env:INVOICEFLOW_BUILD_SOURCE_REVISION = $SourceRevision
        $env:INVOICEFLOW_BUILD_LABEL = "desktop-release"
        $script = @'
import json
import os
import sys
from build_identity import build_runtime_identity

output_path = sys.argv[1]
payload = build_runtime_identity(
    build_time=os.getenv("INVOICEFLOW_BUILD_TIME"),
    source_revision=os.getenv("INVOICEFLOW_BUILD_SOURCE_REVISION"),
    build_label=os.getenv("INVOICEFLOW_BUILD_LABEL"),
)
with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
'@
        $script | & $PythonExe - $OutputPath
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to render build identity JSON."
        }
    }
    finally {
        if ($null -ne $previousBuildTime) { $env:INVOICEFLOW_BUILD_TIME = $previousBuildTime } else { Remove-Item Env:INVOICEFLOW_BUILD_TIME -ErrorAction SilentlyContinue }
        if ($null -ne $previousSourceRevision) { $env:INVOICEFLOW_BUILD_SOURCE_REVISION = $previousSourceRevision } else { Remove-Item Env:INVOICEFLOW_BUILD_SOURCE_REVISION -ErrorAction SilentlyContinue }
        if ($null -ne $previousBuildLabel) { $env:INVOICEFLOW_BUILD_LABEL = $previousBuildLabel } else { Remove-Item Env:INVOICEFLOW_BUILD_LABEL -ErrorAction SilentlyContinue }
    }
}

function Assert-PythonDependencies {
    param([string]$Executable)

    $probeScript = @'
import importlib
import json

modules = [
    ("PyInstaller", "PyInstaller"),
    ("playwright", "playwright"),
    ("playwright.sync_api", "playwright.sync_api"),
    ("webview", "webview"),
    ("pythonnet", "pythonnet"),
    ("clr_loader", "clr_loader"),
    ("openpyxl", "openpyxl"),
    ("fitz", "fitz"),
    ("PIL", "PIL"),
    ("pyzbar", "pyzbar"),
    ("requests", "requests"),
    ("beautifulsoup4", "bs4"),
    ("python-dotenv", "dotenv"),
    ("tenacity", "tenacity"),
]

rows = []
missing = []
for label, module_name in modules:
    try:
        importlib.import_module(module_name)
        rows.append({"label": label, "module": module_name, "status": "ok"})
    except Exception as exc:
        rows.append(
            {
                "label": label,
                "module": module_name,
                "status": "missing",
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        )
        missing.append(label)

print(json.dumps({"rows": rows, "missing": missing}, ensure_ascii=False))
'@

    $probeOutput = $probeScript | & $Executable -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to validate Python module imports using: $Executable"
    }

    $result = ($probeOutput | Select-Object -Last 1) | ConvertFrom-Json
    if ($result.missing.Count -gt 0) {
        throw "Missing Python build dependencies: $($result.missing -join ', ')"
    }

    return $result
}

function Resolve-CallableTool {
    param(
        [string]$PathValue,
        [string]$Label
    )

    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($PathValue)) {
        $candidates += (Resolve-ToolPath $PathValue)
        if (-not [System.IO.Path]::IsPathRooted($PathValue)) {
            $command = Get-Command $PathValue -ErrorAction SilentlyContinue
            if ($command -and $command.Source) {
                $candidates += $command.Source
            }
        }
    }

    foreach ($candidate in $candidates | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command -and $command.Source) {
            return $command.Source
        }
        if (Test-Path $candidate -PathType Leaf) {
            return (Resolve-Path $candidate).Path
        }
    }

    throw "$Label not found or not callable: $PathValue"
}

function Assert-SignToolReady {
    param([string]$PathValue)

    $resolved = Resolve-CallableTool -PathValue $PathValue -Label "SignTool"
    cmd /c "`"$resolved`" /? >nul 2>nul" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "SignTool is not callable: $resolved"
    }
    return $resolved
}

function Test-IsRecognizedChromiumEntry {
    param([string]$EntryName)

    switch -Wildcard ($EntryName) {
        ".gitignore" { return $true }
        ".links" { return $true }
        "chromium-*" { return $true }
        "chromium_headless_shell-*" { return $true }
        "ffmpeg-*" { return $true }
        "winldd-*" { return $true }
        default { return $false }
    }
}

function Test-IsCopyableChromiumEntry {
    param(
        [string]$EntryName,
        [string[]]$ExcludedEntryNames
    )

    if (-not (Test-IsRecognizedChromiumEntry -EntryName $EntryName)) {
        return $false
    }

    if ($ExcludedEntryNames -contains $EntryName) {
        return $false
    }

    return ($EntryName -like "chromium-*") -or
        ($EntryName -like "chromium_headless_shell-*") -or
        ($EntryName -like "ffmpeg-*") -or
        ($EntryName -like "winldd-*")
}

function Assert-ChromiumStagingClean {
    param([string]$PathValue)

    Assert-PathExists -PathValue $PathValue -Label "Chromium staging directory"
    $entries = @(Get-ChildItem -Force $PathValue)
    if ($entries.Count -eq 0) {
        throw "Chromium staging directory is empty: $PathValue"
    }

    $payloadEntries = @()
    $invalidEntries = @()
    foreach ($entry in $entries) {
        if (Test-IsRecognizedChromiumEntry -EntryName $entry.Name) {
            if ($entry.Name -like "chromium-*") {
                $payloadEntries += $entry.Name
            }
            continue
        }

        $invalidEntries += $entry.Name
    }

    if ($payloadEntries.Count -eq 0) {
        throw "Chromium staging directory does not contain a chromium-* payload: $PathValue"
    }

    if ($invalidEntries.Count -gt 0) {
        throw "Chromium staging directory contains unexpected top-level entries: $($invalidEntries -join ', ')"
    }

    return [PSCustomObject]@{
        Entries = $entries.Name
        ChromiumPayloads = $payloadEntries
    }
}

function Copy-SanitizedChromiumPayload {
    param(
        [string]$SourceDir,
        [string]$DestinationDir,
        [string[]]$ExcludedEntryNames
    )

    New-CleanDirectory -PathValue $DestinationDir
    foreach ($entry in Get-ChildItem -Force $SourceDir) {
        if (-not (Test-IsCopyableChromiumEntry -EntryName $entry.Name -ExcludedEntryNames $ExcludedEntryNames)) {
            continue
        }

        $targetPath = Join-Path $DestinationDir $entry.Name
        if ($entry.PSIsContainer) {
            Invoke-Robocopy -Source $entry.FullName -Destination $targetPath
        }
        else {
            Copy-Item -LiteralPath $entry.FullName -Destination $targetPath -Force
        }
    }
}

function Assert-DistributionTree {
    param(
        [string]$PathValue,
        [string]$AppName
    )

    Assert-PathExists -PathValue $PathValue -Label "Unsigned app staging directory"
    Assert-LeafPathExists -PathValue (Join-Path $PathValue "$AppName.exe") -Label "Packaged executable"
    Assert-PathExists -PathValue (Join-Path $PathValue "_internal") -Label "Packaged _internal directory"
}

function Assert-DistributionTreeSanitized {
    param([string]$PathValue)

    $entries = $null
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            $entries = @(Get-ChildItem -LiteralPath $PathValue -Recurse -Force -ErrorAction Stop)
            break
        }
        catch {
            if ($attempt -ge 5) {
                throw
            }
            Start-Sleep -Milliseconds 500
        }
    }

    $forbiddenHits = @()
    foreach ($entry in $entries) {
        $name = $entry.Name
        if ($entry.PSIsContainer -and $name -eq "diagnostics") {
            $forbiddenHits += $entry.FullName
            continue
        }
        if ($entry.PSIsContainer -and $name -eq "release_prep") {
            $forbiddenHits += $entry.FullName
            continue
        }
        if ($entry.PSIsContainer -and $name -eq ".links") {
            $forbiddenHits += $entry.FullName
            continue
        }
        if ($name -in @(".env", "Local State", "Login Data", "History", "Cookies", "Web Data")) {
            $forbiddenHits += $entry.FullName
        }
    }

    if ($forbiddenHits.Count -gt 0) {
        throw "Unsigned app staging contains forbidden entries: $($forbiddenHits -join '; ')"
    }
}

function Remove-PackagedChromiumExcludedEntries {
    param(
        [string]$PathValue,
        [string[]]$ExcludedEntryNames
    )

    $runtimeRoot = Join-Path $PathValue "_internal\\runtime\\ms-playwright"
    if (-not (Test-Path $runtimeRoot -PathType Container)) {
        return
    }

    foreach ($entryName in $ExcludedEntryNames) {
        if ([string]::IsNullOrWhiteSpace($entryName)) {
            continue
        }
        Remove-PathIfExists -PathValue (Join-Path $runtimeRoot $entryName)
    }
}

function Assert-BuildIdentityPayload {
    param([string]$PathValue)

    $identityPath = Join-Path $PathValue "_internal\\build_meta\\build-identity.generated.json"
    Assert-LeafPathExists -PathValue $identityPath -Label "Build identity payload"
    $templatePath = Join-Path $PathValue "_internal\\templates\\index_app.js"
    Assert-LeafPathExists -PathValue $templatePath -Label "Packaged frontend template"
    $validationScript = @'
import json
import sys
from pathlib import Path

identity_path = Path(sys.argv[1])
template_path = Path(sys.argv[2])
identity = json.loads(identity_path.read_text(encoding="utf-8-sig"))

expected_manual = "\u5f85\u4eba\u5de5\u590d\u6838"
expected_non_target = "\u975e\u76ee\u6807\u516c\u53f8\u53d1\u7968"
required_markers = [
    'name: "InvoiceFlowAI"',
    'subtitle: "AI\u53d1\u7968\u7ba1\u5bb6"',
    "const APP_VISIBLE_COPY = {",
]

if str(identity.get("manual_review_folder", "")) != expected_manual:
    raise SystemExit(f"Build identity manual_review_folder mismatch: {identity.get('manual_review_folder')}")
if str(identity.get("non_target_company_folder", "")) != expected_non_target:
    raise SystemExit(f"Build identity non_target_company_folder mismatch: {identity.get('non_target_company_folder')}")
if not str(identity.get("url_strategy_version", "")).strip():
    raise SystemExit("Build identity url_strategy_version is missing.")

template_text = template_path.read_text(encoding="utf-8-sig")
for marker in required_markers:
    if marker not in template_text:
        raise SystemExit(f"Packaged frontend template is missing required marker: {marker}")
'@
    $validationScript | & $resolvedBuildPythonExe - $identityPath $templatePath
    if ($LASTEXITCODE -ne 0) {
        throw "Build identity validation failed."
    }
}

function New-PortableArchive {
    param(
        [string]$SourceDir,
        [string]$PortableWorkDir,
        [string]$RootName,
        [string]$OutputPath
    )

    New-Item -ItemType Directory -Force -Path $PortableWorkDir | Out-Null

    Remove-PathIfExists -PathValue $OutputPath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::Open($OutputPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        $sourceRoot = (Get-Item -LiteralPath $SourceDir).FullName
        foreach ($entry in Get-ChildItem -LiteralPath $SourceDir -Recurse -Force -File) {
            $fullName = $entry.FullName
            $relativePath = $fullName.Substring($sourceRoot.Length).TrimStart('\')
            $archivePath = (Join-Path $RootName $relativePath) -replace "\\", "/"
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip,
                $fullName,
                $archivePath,
                [System.IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    }
    finally {
        $zip.Dispose()
    }

    Assert-LeafPathExists -PathValue $OutputPath -Label "Portable archive"
}

function Resolve-InstallerCompiler {
    param(
        [string]$InstallerCompilerPath,
        [string]$ManifestCompilerPath
    )

    if (-not [string]::IsNullOrWhiteSpace($InstallerCompilerPath)) {
        return Resolve-CallableTool -PathValue $InstallerCompilerPath -Label "Inno Setup compiler"
    }

    if (-not [string]::IsNullOrWhiteSpace($ManifestCompilerPath)) {
        return Resolve-CallableTool -PathValue $ManifestCompilerPath -Label "Inno Setup compiler"
    }

    return Resolve-CallableTool -PathValue "iscc" -Label "Inno Setup compiler"
}

function Invoke-InnoSetupBuild {
    param(
        [string]$CompilerPath,
        [string]$ScriptPath,
        [string]$SourceDir,
        [string]$OutputDir,
        [string]$OutputBaseName,
        [string]$VersionText
    )

    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    Remove-PathIfExists -PathValue (Join-Path $OutputDir "$OutputBaseName.exe")

    $arguments = @(
        "/DSourceDir=$SourceDir",
        "/DOutputDir=$OutputDir",
        "/DOutputBaseName=$OutputBaseName",
        "/DAppVersion=$VersionText",
        $ScriptPath
    )

    & $CompilerPath @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup compilation failed with exit code $LASTEXITCODE"
    }
}

function Write-Sha256Manifest {
    param(
        [string]$OutputPath,
        [System.Collections.ArrayList]$FilePaths
    )

    if ($FilePaths.Count -eq 0) {
        return
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null
    $rows = @()
    foreach ($path in $FilePaths | Select-Object -Unique) {
        if (-not (Test-Path $path -PathType Leaf)) {
            continue
        }
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash
        $rows += "{0} *{1}" -f $hash, [System.IO.Path]::GetFileName($path)
    }
    Set-Content -Path $OutputPath -Value $rows -Encoding UTF8
}

$manifest = Get-Content -Raw -Encoding UTF8 $ManifestPath | ConvertFrom-Json

foreach ($requiredPath in $manifest.requiredPaths) {
    Assert-PathExists -PathValue (Resolve-RepoPath $requiredPath) -Label "Required source"
}

$resolvedBasePythonExe = Resolve-ToolPath $PythonExe
$resolvedBuildPythonExe = Resolve-ToolPath $BuildPythonExe

Assert-LeafPathExists -PathValue $resolvedBasePythonExe -Label "Base Python interpreter"
Assert-LeafPathExists -PathValue $resolvedBuildPythonExe -Label "Build Python interpreter"

$basePythonVersion = Get-PythonVersion -Executable $resolvedBasePythonExe
$buildPythonVersion = Get-PythonVersion -Executable $resolvedBuildPythonExe
if (-not $SkipPythonVersionCheck -and -not $basePythonVersion.StartsWith("$($manifest.pythonVersion).")) {
    throw "Python $($manifest.pythonVersion).x is required for the base interpreter. Current interpreter: $basePythonVersion"
}
if (-not $SkipPythonVersionCheck -and -not $buildPythonVersion.StartsWith("$($manifest.pythonVersion).")) {
    throw "Python $($manifest.pythonVersion).x is required for the build interpreter. Current interpreter: $buildPythonVersion"
}

$distDir = Resolve-RepoPath $manifest.distDir
$workDir = Resolve-RepoPath $manifest.workDir
$packagingWorkDir = Resolve-RepoPath $manifest.packagingWorkDir
$portableWorkDir = Join-Path $packagingWorkDir "portable_stage"
$chromiumStageDir = Resolve-RepoPath $manifest.chromium.stagingDir
$sanitizedChromiumDir = Resolve-RepoPath $manifest.chromium.sanitizedWorkDir
$chromiumExcludeEntries = @($manifest.chromium.excludeEntries)
$portableZipPath = Resolve-RepoPath $manifest.portable.outputFile
$portableRootName = [string]$manifest.portable.copyRootName
$installerScriptPath = Resolve-RepoPath $manifest.installer.script
$installerOutputDir = Resolve-RepoPath $manifest.installer.outputDir
$installerOutputBaseName = [string]$manifest.installer.outputBaseName
$versionTemplatePath = Resolve-RepoPath $manifest.version.template
$versionOutputPath = Resolve-RepoPath $manifest.version.rendered
$buildIdentityOutputPath = Resolve-RepoPath "build/windows/build-identity.generated.json"
$specPath = Resolve-RepoPath "build/windows/InvoiceFlowAI.spec"
$runtimeHookPath = Resolve-RepoPath $manifest.runtimeHook
$iconPath = Resolve-ToolPath ($manifest.icon.exeIconPath)
$sha256OutputPath = Join-Path $distDir $manifest.sha256.outputFile

Assert-LeafPathExists -PathValue $specPath -Label "PyInstaller spec"
Assert-LeafPathExists -PathValue $versionTemplatePath -Label "Version template"
Assert-LeafPathExists -PathValue $runtimeHookPath -Label "Runtime hook"
Assert-LeafPathExists -PathValue $installerScriptPath -Label "Installer script"
if (-not [string]::IsNullOrWhiteSpace($manifest.icon.exeIconPath)) {
    Assert-LeafPathExists -PathValue $iconPath -Label "EXE icon"
}

$dependencyCheck = Assert-PythonDependencies -Executable $resolvedBuildPythonExe
$signingEnabled = [bool]($manifest.signing.enabled)
$resolvedSignToolPath = ""
if ($signingEnabled) {
    $resolvedSignToolPath = Assert-SignToolReady -PathValue $SignToolPath
}
else {
    $resolvedSignToolPath = "skipped (signing disabled)"
}
$chromiumCheck = Assert-ChromiumStagingClean -PathValue $chromiumStageDir

New-Item -ItemType Directory -Force -Path $packagingWorkDir | Out-Null
Render-VersionInfo -TemplatePath $versionTemplatePath -OutputPath $versionOutputPath -VersionText $Version -ProductName $manifest.appName
Write-BuildIdentity -PythonExe $resolvedBuildPythonExe -OutputPath $buildIdentityOutputPath -SourceRevision (Resolve-GitRevision -RootPath $RepoRoot)
Set-Item -Path "Env:$($manifest.chromium.envVar)" -Value $chromiumStageDir
$env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1"

Write-Host "Release skeleton prepared."
Write-Host "Repo root: $RepoRoot"
Write-Host "Base Python: $resolvedBasePythonExe"
Write-Host "Base Python version: $basePythonVersion"
Write-Host "Build Python: $resolvedBuildPythonExe"
Write-Host "Build Python version: $buildPythonVersion"
Write-Host "Python dependencies: OK"
Write-Host "SignTool path: $resolvedSignToolPath"
Write-Host "Version file: $versionOutputPath"
Write-Host "Build identity file: $buildIdentityOutputPath"
Write-Host "Chromium staging dir: $chromiumStageDir"
Write-Host "Chromium payloads: $($chromiumCheck.ChromiumPayloads -join ', ')"
Write-Host "PyInstaller spec: $specPath"
Write-Host "Runtime hook: $runtimeHookPath"
Write-Host "Installer script: $installerScriptPath"
if ($chromiumExcludeEntries.Count -gt 0) {
    Write-Host "Chromium packaging exclusions: $($chromiumExcludeEntries -join ', ')"
}
if (-not [string]::IsNullOrWhiteSpace($manifest.icon.exeIconPath)) {
    Write-Host "EXE icon: $iconPath"
}
else {
    Write-Warning "EXE icon is not configured. Windows shortcuts will inherit the executable icon when one is wired later."
}
Write-Host "Dist root: $distDir"
Write-Host "Portable zip target: $portableZipPath"
Write-Host "Installer output dir: $installerOutputDir"
Write-Host "SHA256 output target: $sha256OutputPath"

if (-not $RunPyInstaller -and -not $RunPortableZip -and -not $RunInstaller) {
    Write-Warning "No build phase requested. Precheck completed without entering PyInstaller, portable packaging, or installer compilation."
    return
}

$artifactPaths = New-Object System.Collections.ArrayList
$packagedExePath = Join-Path $distDir "$($manifest.appName).exe"
$installerArtifactPath = Join-Path $installerOutputDir "$installerOutputBaseName.exe"

if ($RunPyInstaller) {
    Write-Host "Preparing clean unsigned app staging..."
    Remove-PathIfExists -PathValue $distDir
    Remove-PathIfExists -PathValue $workDir
    Remove-PathIfExists -PathValue $portableZipPath
    Remove-PathIfExists -PathValue $installerOutputDir
    Remove-PathIfExists -PathValue $sha256OutputPath

    $env:INVOICEFLOW_RUNTIME_SOURCE = $chromiumStageDir
    try {
        $arguments = @(
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            (Split-Path -Parent $distDir),
            "--workpath",
            $workDir,
            $specPath
        )

        & $resolvedBuildPythonExe @arguments
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Remove-Item Env:INVOICEFLOW_RUNTIME_SOURCE -ErrorAction SilentlyContinue
    }

    Assert-DistributionTree -PathValue $distDir -AppName $manifest.appName
    Remove-PackagedChromiumExcludedEntries -PathValue $distDir -ExcludedEntryNames $chromiumExcludeEntries
    Assert-DistributionTreeSanitized -PathValue $distDir
    Assert-BuildIdentityPayload -PathValue $distDir
    [void]$artifactPaths.Add($packagedExePath)
}
else {
    Assert-DistributionTree -PathValue $distDir -AppName $manifest.appName
    Remove-PackagedChromiumExcludedEntries -PathValue $distDir -ExcludedEntryNames $chromiumExcludeEntries
    Assert-DistributionTreeSanitized -PathValue $distDir
    Assert-BuildIdentityPayload -PathValue $distDir
}

if ($RunPortableZip) {
    Write-Host "Building unsigned portable zip..."
    New-PortableArchive -SourceDir $distDir -PortableWorkDir $portableWorkDir -RootName $portableRootName -OutputPath $portableZipPath
    [void]$artifactPaths.Add($portableZipPath)
}

if ($RunInstaller) {
    Write-Host "Building unsigned installer..."
    $resolvedInstallerCompiler = Resolve-InstallerCompiler -InstallerCompilerPath $InstallerCompilerPath -ManifestCompilerPath ([string]$manifest.installer.compilerPath)
    Invoke-InnoSetupBuild `
        -CompilerPath $resolvedInstallerCompiler `
        -ScriptPath $installerScriptPath `
        -SourceDir $distDir `
        -OutputDir $installerOutputDir `
        -OutputBaseName $installerOutputBaseName `
        -VersionText $Version
    Assert-LeafPathExists -PathValue $installerArtifactPath -Label "Unsigned installer"
    [void]$artifactPaths.Add($installerArtifactPath)
}

if ($artifactPaths.Count -gt 0) {
    Write-Sha256Manifest -OutputPath $sha256OutputPath -FilePaths $artifactPaths
}

if ($SignArtifacts) {
    if (-not $signingEnabled) {
        Write-Warning "Signing is disabled in resources.manifest.json. Skipping signing hook."
    }
    else {
        Write-Warning "Signing hook requested, but signing execution remains intentionally disabled in this release-prep round."
    }
}

Write-Host "Build pipeline completed."
