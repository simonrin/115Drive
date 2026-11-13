param([string]$OutputPath=(Join-Path $PSScriptRoot 'FolderBadge.dll'))
$ErrorActionPreference='Stop'
$compiler=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $compiler /nologo /utf8output /target:library /platform:anycpu /optimize+ ('/out:'+$OutputPath) /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll (Join-Path $PSScriptRoot 'CloudBrand.cs') (Join-Path $PSScriptRoot 'FolderBadge.cs')
if($LASTEXITCODE -ne 0){throw 'Folder badge compilation failed.'}
