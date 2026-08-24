param(
    [string[]]$BlenderExe
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$cacheRoot = Join-Path $projectRoot '.cache'
$tempRoot = Join-Path $cacheRoot 'temp'
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $cacheRoot 'uv' }
$env:TEMP = $tempRoot
$env:TMP = $tempRoot

if (-not $BlenderExe -or $BlenderExe.Count -eq 0) {
    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($env:FACELINK_BLENDER_MATRIX) {
        $env:FACELINK_BLENDER_MATRIX -split ';' |
            Where-Object { $_ } | ForEach-Object { $candidates.Add($_) }
    }
    if ($env:FACELINK_BLENDER_EXE) { $candidates.Add($env:FACELINK_BLENDER_EXE) }
    $command = Get-Command blender -ErrorAction SilentlyContinue
    if ($command) { $candidates.Add($command.Source) }
    $roots = @(
        (Join-Path $env:ProgramFiles 'Blender Foundation'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Blender Foundation')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) }
    foreach ($root in $roots) {
        Get-ChildItem -LiteralPath $root -Filter blender.exe -File -Recurse |
            ForEach-Object { $candidates.Add($_.FullName) }
    }
    $BlenderExe = $candidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        ForEach-Object { (Resolve-Path -LiteralPath $_).Path } |
        Sort-Object -Unique
}
if (-not $BlenderExe -or $BlenderExe.Count -eq 0) {
    throw 'No Blender executable found. Pass one or more paths with -BlenderExe.'
}

Push-Location $projectRoot
$acceptanceExit = 1
try {
    uv sync --extra dev --locked
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE" }
    $arguments = @('scripts\acceptance.py')
    foreach ($path in $BlenderExe) {
        $arguments += @('--blender', $path)
    }
    & '.\.venv\Scripts\python.exe' @arguments
    $acceptanceExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $acceptanceExit
