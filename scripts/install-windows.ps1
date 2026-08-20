param(
    [Parameter(Mandatory = $true)]
    [string]$WheelPath,
    [Parameter(Mandatory = $true)]
    [string]$ExtensionZipPath,
    [string]$ChecksumsPath,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'FaceLink'),
    [string]$PythonExe,
    [string]$BlenderExe = $env:FACELINK_BLENDER_EXE,
    [switch]$PlanOnly,
    [switch]$SkipExtensionInstall
)

$ErrorActionPreference = 'Stop'
$minimumBlender = [version]'4.2.0'
$blenderDownloadUrl = 'https://www.blender.org/download/lts/'

function Resolve-ExistingFile([string]$Path, [string]$Label) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label was not found: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Resolve-Python([string]$ExplicitPath) {
    if ($ExplicitPath) {
        return Resolve-ExistingFile $ExplicitPath 'Python executable'
    }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $output = & $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
        $exitCode = $LASTEXITCODE
        $candidate = $output | Select-Object -First 1
        if ($exitCode -eq 0 -and $candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) {
        $output = & $command.Source -c 'import sys; print(sys.executable)' 2>$null
        $exitCode = $LASTEXITCODE
        $candidate = $output | Select-Object -First 1
        if ($exitCode -eq 0 -and $candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'Python 3.11 or newer was not found. Install Python, then rerun this script.'
}

function Read-PythonVersion([string]$Path) {
    $output = & $Path -c 'import platform; print(platform.python_version())' 2>$null
    $exitCode = $LASTEXITCODE
    $text = $output | Select-Object -First 1
    if ($exitCode -ne 0 -or -not $text) { throw "Could not probe Python: $Path" }
    $version = [version]$text
    if ($version -lt [version]'3.11.0') { throw "Python $version is too old; FaceLink requires 3.11+." }
    return $version.ToString()
}

function Resolve-Blender([string]$ExplicitPath) {
    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($ExplicitPath) { $candidates.Add($ExplicitPath) }
    $command = Get-Command blender -ErrorAction SilentlyContinue
    if ($command) { $candidates.Add($command.Source) }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if (-not $root) { continue }
        $parent = if ($root -eq $env:LOCALAPPDATA) {
            Join-Path $root 'Programs\Blender Foundation'
        } else {
            Join-Path $root 'Blender Foundation'
        }
        if (Test-Path -LiteralPath $parent -PathType Container) {
            Get-ChildItem -LiteralPath $parent -Filter blender.exe -File -Recurse -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending |
                ForEach-Object { $candidates.Add($_.FullName) }
        }
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "Blender 4.2+ was not found. Install an official LTS release from $blenderDownloadUrl or pass -BlenderExe."
}

function Read-BlenderVersion([string]$Path) {
    $output = & $Path --version 2>&1
    $exitCode = $LASTEXITCODE
    $text = $output | Select-Object -First 1
    if ($exitCode -ne 0 -or $text -notmatch '^Blender\s+(\d+\.\d+\.\d+)') {
        throw "Could not probe Blender: $Path"
    }
    $version = [version]$Matches[1]
    if ($version -lt $minimumBlender) {
        throw "Blender $version is too old; FaceLink requires 4.2+. Download: $blenderDownloadUrl"
    }
    return $version.ToString()
}

function Confirm-Checksum([string]$FilePath, [string]$ChecksumFile) {
    if (-not $ChecksumFile) { return }
    $name = [IO.Path]::GetFileName($FilePath)
    $escaped = [regex]::Escape($name)
    $line = Get-Content -LiteralPath $ChecksumFile | Where-Object { $_ -match "^([0-9a-fA-F]{64})\s+[*]?$escaped$" } | Select-Object -First 1
    if (-not $line) { throw "SHA-256 entry for $name was not found in $ChecksumFile" }
    $expected = ([regex]::Match($line, '^([0-9a-fA-F]{64})')).Groups[1].Value.ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "SHA-256 verification failed for $name" }
}

$wheel = Resolve-ExistingFile $WheelPath 'FaceLink wheel'
$extensionZip = Resolve-ExistingFile $ExtensionZipPath 'FaceLink Blender extension ZIP'
$checksums = if ($ChecksumsPath) { Resolve-ExistingFile $ChecksumsPath 'Checksum file' } else { $null }
$python = Resolve-Python $PythonExe
$pythonVersion = Read-PythonVersion $python
$blender = Resolve-Blender $BlenderExe
$blenderVersion = Read-BlenderVersion $blender
$resolvedInstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$venvRoot = Join-Path $resolvedInstallRoot 'host'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$mcpLauncher = Join-Path $venvRoot 'Scripts\facelink-mcp.exe'

Confirm-Checksum $wheel $checksums
Confirm-Checksum $extensionZip $checksums

$plan = [ordered]@{
    blender_bundled = $false
    blender_executable = $blender
    blender_version = $blenderVersion
    python_executable = $python
    python_version = $pythonVersion
    wheel = $wheel
    extension_zip = $extensionZip
    checksums_verified = [bool]$checksums
    install_root = $resolvedInstallRoot
    mcp_launcher = $mcpLauncher
    extension_install = -not $SkipExtensionInstall
}
if ($PlanOnly) {
    $plan | ConvertTo-Json -Depth 3
    exit 0
}

New-Item -ItemType Directory -Force -Path $resolvedInstallRoot | Out-Null
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    & $python -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed with exit code $LASTEXITCODE" }
}
& $venvPython -m pip install --upgrade $wheel
if ($LASTEXITCODE -ne 0) { throw "FaceLink host installation failed with exit code $LASTEXITCODE" }

if (-not $SkipExtensionInstall) {
    & $blender --command extension install-file -r user_default -e $extensionZip
    if ($LASTEXITCODE -ne 0) {
        throw 'Blender extension installation failed. If FaceLink is already installed, update it from Blender Preferences or remove the old version first.'
    }
}

& $venvPython -m facelink.cli doctor --blender-exe $blender
$doctorExit = $LASTEXITCODE
$plan['doctor_exit_code'] = $doctorExit
$plan['installed'] = $true
$plan | ConvertTo-Json -Depth 3
if ($doctorExit -ne 0) {
    Write-Warning 'FaceLink installed, but Doctor found an incomplete setup. Start the bridge in Blender and rerun Doctor.'
}
