param([string]$Python='python.exe',[string]$DependencyRoot,[string]$OutputDirectory)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot
if (-not $DependencyRoot) { $DependencyRoot=$root }
if (-not $OutputDirectory) { $OutputDirectory=Join-Path $root 'dist' }
$output=[System.IO.Path]::GetFullPath($OutputDirectory)
$build=Join-Path $root 'build\release'
$stage=Join-Path $build '115Drive'
if (Test-Path -LiteralPath $stage) { throw 'Staging directory exists. Use a clean checkout/build directory.' }
New-Item -ItemType Directory -Path $stage,$output -Force | Out-Null
& $Python -m PyInstaller --noconfirm --clean --windowed --onedir --name TwoDrive.Service --hidden-import tests.smoke --distpath (Join-Path $build 'service-dist') --workpath (Join-Path $build 'pyinstaller-work') --specpath $build (Join-Path $root 'desktop_service.py')
if ($LASTEXITCODE) { throw 'Backend compilation failed' }
Copy-Item -LiteralPath (Join-Path $build 'service-dist\TwoDrive.Service') -Destination (Join-Path $stage 'service') -Recurse
& (Join-Path $root 'tray\build.ps1') -OutputDirectory (Join-Path $build 'tray')
if ($LASTEXITCODE) { throw 'Native UI compilation failed' }
Copy-Item -LiteralPath (Join-Path $build 'tray\TwoDriveTray.exe') -Destination (Join-Path $stage '115Drive.exe')
foreach ($directory in @('tray','bin\openlist-v4.2.6','bin\rclone-v1.75.1\rclone-v1.75.1-windows-amd64','licenses','third_party')) { New-Item -ItemType Directory -Path (Join-Path $stage $directory) -Force | Out-Null }
foreach ($file in @('Panel.xaml','Settings.xaml')) { Copy-Item -LiteralPath (Join-Path $root ('tray\'+$file)) -Destination (Join-Path $stage 'tray') }
foreach ($file in @('filters.txt','install-startup.ps1','install-tray.ps1','set-startup.ps1','LICENSE','README.md','PRIVACY.md','CONTRIBUTING.md','THIRD_PARTY_NOTICES.md')) { Copy-Item -LiteralPath (Join-Path $root $file) -Destination $stage }
Copy-Item -LiteralPath (Join-Path $root 'docs') -Destination (Join-Path $stage 'docs') -Recurse
Copy-Item -LiteralPath (Join-Path $DependencyRoot 'bin\openlist-v4.2.6\openlist.exe') -Destination (Join-Path $stage 'bin\openlist-v4.2.6')
Copy-Item -LiteralPath (Join-Path $DependencyRoot 'bin\rclone-v1.75.1\rclone-v1.75.1-windows-amd64\rclone.exe') -Destination (Join-Path $stage 'bin\rclone-v1.75.1\rclone-v1.75.1-windows-amd64')
Copy-Item -Path (Join-Path $root 'licenses\*') -Destination (Join-Path $stage 'licenses')
Copy-Item -Path (Join-Path $root 'third_party\*.zip') -Destination (Join-Path $stage 'third_party')
Copy-Item -LiteralPath (Join-Path $root 'third_party\build-openlist.ps1') -Destination (Join-Path $stage 'third_party')
& $Python (Join-Path $PSScriptRoot 'verify-package.py') $stage
if ($LASTEXITCODE) { throw 'Package privacy/structure verification failed' }
& $Python (Join-Path $PSScriptRoot 'zip-package.py') $stage (Join-Path $output '115Drive-0.2.0-windows-x64.zip')
if ($LASTEXITCODE) { throw 'Package archive failed' }
$framework=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
& (Join-Path $framework 'csc.exe') /nologo /utf8output /codepage:65001 /target:winexe /platform:x64 /optimize+ ('/out:'+(Join-Path $output '115Drive-Setup-0.2.0-x64.exe')) ('/win32icon:'+(Join-Path $root 'tray\115Drive.ico')) ('/resource:'+(Join-Path $output '115Drive-0.2.0-windows-x64.zip')+',payload.zip') /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.IO.Compression.dll (Join-Path $PSScriptRoot 'Installer.cs') (Join-Path $root 'tray\CloudBrand.cs')
if ($LASTEXITCODE) { throw 'Installer compilation failed' }
Get-ChildItem -LiteralPath $output -File | Where-Object Name -ne 'SHA256SUMS.txt' | Get-FileHash -Algorithm SHA256 | ForEach-Object { $_.Hash.ToLower()+'  '+[System.IO.Path]::GetFileName($_.Path) } | Set-Content -LiteralPath (Join-Path $output 'SHA256SUMS.txt') -Encoding ASCII
Get-ChildItem -LiteralPath $output -File | Select-Object Name,Length
