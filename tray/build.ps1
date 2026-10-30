param([switch]$SelfTest,[string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$twodriveRoot = Split-Path $PSScriptRoot
$twodriveFramework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$twodriveOutput = Join-Path $twodriveRoot 'bin\TwoDriveTray'
if ($OutputDirectory) { $twodriveOutput = $OutputDirectory }
New-Item -ItemType Directory -Path $twodriveOutput -Force | Out-Null
Add-Type -AssemblyName System.Drawing
if (-not ('TwoDrive.CloudBrand' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'CloudBrand.cs') -ReferencedAssemblies System.Drawing }
[TwoDrive.CloudBrand]::WriteIcon((Join-Path $PSScriptRoot '115Drive.ico'))
$twodriveReferences = @('System.dll','System.Core.dll','System.Drawing.dll','System.Windows.Forms.dll','System.Web.Extensions.dll','System.Xaml.dll','WPF\WindowsBase.dll','WPF\PresentationCore.dll','WPF\PresentationFramework.dll') | ForEach-Object { '/reference:' + (Join-Path $twodriveFramework $_) }
$twodriveExe = Join-Path $twodriveOutput 'TwoDriveTray.exe'
& (Join-Path $twodriveFramework 'csc.exe') /nologo /utf8output /codepage:65001 /target:winexe /platform:anycpu /optimize+ ('/out:'+$twodriveExe) ('/win32manifest:'+(Join-Path $PSScriptRoot 'app.manifest')) ('/win32icon:'+(Join-Path $PSScriptRoot '115Drive.ico')) $twodriveReferences (Join-Path $PSScriptRoot 'TrayApp.cs') (Join-Path $PSScriptRoot 'NativeSettings.cs') (Join-Path $PSScriptRoot 'ActivityActions.cs') (Join-Path $PSScriptRoot 'CloudBrand.cs')
if ($LASTEXITCODE -ne 0) { throw 'Tray compilation failed.' }
if ($SelfTest) {
 $twodriveTest = Start-Process -FilePath $twodriveExe -ArgumentList ('--self-test --root "'+$twodriveRoot+'"') -WindowStyle Hidden -PassThru
 $twodriveTest.WaitForExit()
 if ($twodriveTest.ExitCode -ne 0) { throw 'Native tray UI verification failed; inspect runtime\tray-ui-test.json.' }
}
Get-Item -LiteralPath $twodriveExe | Select-Object FullName,Length
