param(
    [string]$BlenderExe = $env:FACELINK_BLENDER_EXE
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$sourceDir = Join-Path $projectRoot 'blender_extension\facelink'
$outputDir = Join-Path $projectRoot 'dist'
if (-not $BlenderExe) {
    $command = Get-Command blender -ErrorAction SilentlyContinue
    if ($command) { $BlenderExe = $command.Source }
}
if (-not $BlenderExe) {
    $developmentRoot = 'E:\CodexData\Apps\Blender-4.5-LTS'
    if (Test-Path -LiteralPath $developmentRoot) {
        $BlenderExe = Get-ChildItem -LiteralPath $developmentRoot -Filter blender.exe -File -Recurse |
            Sort-Object FullName -Descending |
            Select-Object -First 1 -ExpandProperty FullName
    }
}
if (-not $BlenderExe -or -not (Test-Path -LiteralPath $BlenderExe)) {
    throw 'Blender was not found. Set FACELINK_BLENDER_EXE to Blender 4.5 LTS blender.exe.'
}

New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
& $BlenderExe --command extension build --source-dir $sourceDir --output-dir $outputDir
if ($LASTEXITCODE -ne 0) {
    throw "Blender extension build failed with exit code $LASTEXITCODE"
}
