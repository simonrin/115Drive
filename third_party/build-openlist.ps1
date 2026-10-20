param([string]$Go='go.exe')
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot
$destination=Join-Path $root 'build\openlist-source'
if (Test-Path -LiteralPath $destination) { throw 'Use a clean source extraction directory.' }
Expand-Archive -LiteralPath (Join-Path $PSScriptRoot 'OpenList-4.2.6-twodrive-source.zip') -DestinationPath $destination
$source=Join-Path $destination 'OpenList-4.2.6'
$output=Join-Path $root 'bin\openlist-v4.2.6'
New-Item -ItemType Directory -Path $output -Force | Out-Null
$env:CGO_ENABLED='0'
Push-Location $source
try {
    & $Go test ./internal/twodrive_resume ./drivers/115/... ./internal/op -run TwoDrive -count=1
    if ($LASTEXITCODE) { throw 'OpenList regression tests failed' }
    & $Go build -trimpath -ldflags '-s -w -X github.com/OpenListTeam/OpenList/v4/internal/conf.Version=v4.2.6 -X github.com/OpenListTeam/OpenList/v4/internal/conf.WebVersion=v4.2.6 -X github.com/OpenListTeam/OpenList/v4/internal/conf.GitCommit=twodrive-resume-tray-20261020' -o (Join-Path $output 'openlist.exe') .
    if ($LASTEXITCODE) { throw 'OpenList build failed' }
} finally { Pop-Location }
