param(
    [string]$CscExe = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $projectRoot 'blender_extension\facelink\blender_manifest.toml'
$manifestText = Get-Content -LiteralPath $manifestPath -Raw
if ($manifestText -notmatch '(?m)^version\s*=\s*"([^"]+)"') {
    throw 'Could not read the FaceLink version from blender_manifest.toml.'
}
$version = $Matches[1]
$dist = Join-Path $projectRoot 'dist'
$source = Join-Path $projectRoot 'installer\FaceLink.Setup\Program.cs'
$wheel = Join-Path $dist "facelink-$version-py3-none-any.whl"
$extension = Join-Path $dist "facelink-$version.zip"
$installerScript = Join-Path $projectRoot 'scripts\install-windows.ps1'
$payloadChecksums = Join-Path $dist 'installer-payload-SHA256SUMS.txt'
$output = Join-Path $dist "FaceLink-Setup-$version.exe"

foreach ($item in @($CscExe, $source, $wheel, $extension, $installerScript)) {
    if (-not (Test-Path -LiteralPath $item -PathType Leaf)) {
        throw "Required installer input was not found: $item"
    }
}

$wheelHash = (Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash.ToLowerInvariant()
$extensionHash = (Get-FileHash -LiteralPath $extension -Algorithm SHA256).Hash.ToLowerInvariant()
$payloadText = @(
    "$wheelHash  $([IO.Path]::GetFileName($wheel))"
    "$extensionHash  $([IO.Path]::GetFileName($extension))"
) -join "`n"
[IO.File]::WriteAllText($payloadChecksums, $payloadText + "`n", [Text.UTF8Encoding]::new($false))

$arguments = @(
    '/nologo'
    '/target:winexe'
    '/optimize+'
    '/platform:anycpu'
    "/out:$output"
    '/reference:System.dll'
    '/reference:System.Core.dll'
    '/reference:System.Drawing.dll'
    '/reference:System.Windows.Forms.dll'
    "/resource:$installerScript,FaceLink.Payload.Install.ps1"
    "/resource:$wheel,FaceLink.Payload.Host.whl"
    "/resource:$extension,FaceLink.Payload.Extension.zip"
    "/resource:$payloadChecksums,FaceLink.Payload.Checksums.txt"
    $source
)
& $CscExe @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Windows installer compilation failed with exit code $LASTEXITCODE"
}
Write-Output $output
