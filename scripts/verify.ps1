$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$blender = $env:FACELINK_BLENDER_EXE
if (-not $blender) {
    $command = Get-Command blender -ErrorAction SilentlyContinue
    if ($command) { $blender = $command.Source }
}
if (-not $blender) {
    $developmentRoot = 'E:\CodexData\Apps\Blender-4.5-LTS'
    if (Test-Path -LiteralPath $developmentRoot) {
        $blender = Get-ChildItem -LiteralPath $developmentRoot -Filter blender.exe -File -Recurse |
            Sort-Object FullName -Descending |
            Select-Object -First 1 -ExpandProperty FullName
    }
}
if (-not $blender) { throw 'Set FACELINK_BLENDER_EXE to Blender 4.5 LTS blender.exe.' }

$cacheRoot = Join-Path $projectRoot '.cache'
$tempRoot = Join-Path $cacheRoot 'temp'
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $cacheRoot 'uv' }
$env:TEMP = $tempRoot
$env:TMP = $tempRoot

Push-Location $projectRoot
try {
    uv sync --extra dev
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE" }
    uv run ruff check .
    if ($LASTEXITCODE -ne 0) { throw "Ruff failed with exit code $LASTEXITCODE" }
    uv run pytest
    if ($LASTEXITCODE -ne 0) { throw "Pytest failed with exit code $LASTEXITCODE" }
    & $blender --background --factory-startup --python-exit-code 1 --python (Join-Path $projectRoot 'tests\blender_acceptance.py')
    if ($LASTEXITCODE -ne 0) {
        throw "Blender acceptance test failed with exit code $LASTEXITCODE"
    }
    & $blender --background --factory-startup --python-exit-code 1 --python (Join-Path $projectRoot 'tests\blender_bridge_acceptance.py')
    if ($LASTEXITCODE -ne 0) {
        throw "Blender bridge acceptance test failed with exit code $LASTEXITCODE"
    }
    & (Join-Path $PSScriptRoot 'build_extension.ps1') -BlenderExe $blender
    if ($LASTEXITCODE -ne 0) { throw "Extension build failed with exit code $LASTEXITCODE" }
    & $blender --command extension validate (Join-Path $projectRoot 'dist\facelink-0.2.1.zip')
    if ($LASTEXITCODE -ne 0) { throw "Extension validation failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
