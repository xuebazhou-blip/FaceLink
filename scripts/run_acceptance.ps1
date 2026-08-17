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
    $BlenderExe = @(
        'E:\CodexData\Apps\Blender-4.2-LTS\blender-4.2.23-stable+v42.d0cbe84903e8-windows.amd64-release\blender.exe',
        'E:\CodexData\Apps\Blender-4.5-LTS\blender-4.5.12-stable+v45.84afd5f785f7-windows.amd64-release\blender.exe',
        'E:\CodexData\Apps\Blender-5.2\blender-5.2.0-stable+v52.fbe6228777e7-windows.amd64-release\blender.exe'
    ) | Where-Object { Test-Path -LiteralPath $_ }
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
