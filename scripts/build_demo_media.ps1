param(
    [string]$BlenderExe = $env:FACELINK_BLENDER_EXE,
    [string]$FfmpegExe = 'D:\tools\ffmpeg-2025-07-12-git-35a6de137a-essentials_build\ffmpeg-2025-07-12-git-35a6de137a-essentials_build\bin\ffmpeg.exe'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $BlenderExe) {
    $command = Get-Command blender -ErrorAction SilentlyContinue
    if ($command) { $BlenderExe = $command.Source }
}
if (-not $BlenderExe -or -not (Test-Path -LiteralPath $BlenderExe -PathType Leaf)) {
    throw 'Pass Blender 4.5 LTS with -BlenderExe.'
}
if (-not (Test-Path -LiteralPath $FfmpegExe -PathType Leaf)) {
    throw 'Pass an ffmpeg executable with -FfmpegExe.'
}

$output = Join-Path $projectRoot 'artifacts\demo'
$frames = Join-Path $output 'frames'
$assets = Join-Path $projectRoot 'docs\assets'
New-Item -ItemType Directory -Force -Path $frames,$assets | Out-Null
$env:FACELINK_DEMO_OUTPUT = $output

& $BlenderExe --background --factory-startup --python-exit-code 1 --python `
    (Join-Path $projectRoot 'scripts\create_demo_media.py')
if ($LASTEXITCODE -ne 0) { throw "Blender demo render failed with exit code $LASTEXITCODE" }

$framePattern = Join-Path $frames 'frame_%04d.png'
$video = Join-Path $assets 'facelink-demo.mp4'
$poster = Join-Path $assets 'facelink-demo-poster.png'
$palette = Join-Path $output 'palette.png'
$gif = Join-Path $assets 'facelink-demo.gif'
$font = 'C\:/Windows/Fonts/segoeui.ttf'
$overlay = "drawbox=x=0:y=0:w=iw:h=82:color=0x080b14@0.86:t=fill," +
    "drawtext=fontfile='$font':text='FaceLink  /  editable Blender animation':fontcolor=white:fontsize=26:x=28:y=16," +
    "drawtext=fontfile='$font':text='Move the actor around the blocks to the orange target':fontcolor=0x77ddff:fontsize=17:x=29:y=49," +
    "drawbox=x=0:y=ih-45:w=iw:h=45:color=0x080b14@0.82:t=fill," +
    "drawtext=fontfile='$font':text='SCAN  >  PLAN  >  STAGE  >  REVIEW  >  APPLY':fontcolor=white:fontsize=17:x=(w-text_w)/2:y=h-31," +
    'pad=ceil(iw/2)*2:ceil(ih/2)*2'

& $FfmpegExe -y -framerate 24 -i $framePattern -vf $overlay `
    -c:v libx264 -preset slow -crf 19 -pix_fmt yuv420p -movflags +faststart $video
if ($LASTEXITCODE -ne 0) { throw "MP4 encoding failed with exit code $LASTEXITCODE" }
& $FfmpegExe -y -ss 3.4 -i $video -frames:v 1 -update 1 $poster
if ($LASTEXITCODE -ne 0) { throw "Poster extraction failed with exit code $LASTEXITCODE" }
& $FfmpegExe -y -i $video -vf 'fps=10,scale=720:-1:flags=lanczos,palettegen=max_colors=128:stats_mode=diff' -frames:v 1 -update 1 $palette
if ($LASTEXITCODE -ne 0) { throw "GIF palette generation failed with exit code $LASTEXITCODE" }
& $FfmpegExe -y -i $video -i $palette `
    -lavfi 'fps=10,scale=720:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3' `
    -loop 0 $gif
if ($LASTEXITCODE -ne 0) { throw "GIF encoding failed with exit code $LASTEXITCODE" }

Get-Item -LiteralPath $video,$gif,$poster,(Join-Path $assets 'facelink-demo.blend') |
    Select-Object FullName,Length
