param([switch]$SelfTest,[string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$twodriveRoot = Split-Path $PSScriptRoot
$twodriveFramework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$twodriveOutput = Join-Path $twodriveRoot 'bin\TwoDriveTray'
if ($OutputDirectory) { $twodriveOutput = $OutputDirectory }
New-Item -ItemType Directory -Path $twodriveOutput -Force | Out-Null
Add-Type -AssemblyName System.Drawing
$twodriveBitmap = New-Object System.Drawing.Bitmap 32,32
$twodriveGraphics = [System.Drawing.Graphics]::FromImage($twodriveBitmap)
$twodriveGraphics.SmoothingMode = 'AntiAlias'
$twodriveBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(28,111,233))
$twodriveGraphics.FillEllipse($twodriveBrush,1,1,30,30)
$twodriveFont = New-Object System.Drawing.Font 'Segoe UI',10,([System.Drawing.FontStyle]::Bold),([System.Drawing.GraphicsUnit]::Pixel)
$twodriveFormat = New-Object System.Drawing.StringFormat
$twodriveFormat.Alignment = 'Center'; $twodriveFormat.LineAlignment = 'Center'
$twodriveGraphics.DrawString('115',$twodriveFont,[System.Drawing.Brushes]::White,(New-Object System.Drawing.RectangleF 0,0,32,32),$twodriveFormat)
$twodriveIcon = [System.Drawing.Icon]::FromHandle($twodriveBitmap.GetHicon())
$twodriveIconStream = [System.IO.File]::Create((Join-Path $PSScriptRoot '115Drive.ico'))
$twodriveIcon.Save($twodriveIconStream);$twodriveIconStream.Close()
$twodriveIcon.Dispose();$twodriveFormat.Dispose();$twodriveFont.Dispose();$twodriveBrush.Dispose();$twodriveGraphics.Dispose();$twodriveBitmap.Dispose()
$twodriveReferences = @('System.dll','System.Core.dll','System.Drawing.dll','System.Windows.Forms.dll','System.Web.Extensions.dll','System.Xaml.dll','WPF\WindowsBase.dll','WPF\PresentationCore.dll','WPF\PresentationFramework.dll') | ForEach-Object { '/reference:' + (Join-Path $twodriveFramework $_) }
$twodriveExe = Join-Path $twodriveOutput 'TwoDriveTray.exe'
& (Join-Path $twodriveFramework 'csc.exe') /nologo /utf8output /codepage:65001 /target:winexe /platform:anycpu /optimize+ ('/out:'+$twodriveExe) ('/win32manifest:'+(Join-Path $PSScriptRoot 'app.manifest')) ('/win32icon:'+(Join-Path $PSScriptRoot '115Drive.ico')) $twodriveReferences (Join-Path $PSScriptRoot 'TrayApp.cs') (Join-Path $PSScriptRoot 'NativeSettings.cs') (Join-Path $PSScriptRoot 'ActivityActions.cs')
if ($LASTEXITCODE -ne 0) { throw 'Tray compilation failed.' }
if ($SelfTest) {
 $twodriveTest = Start-Process -FilePath $twodriveExe -ArgumentList ('--self-test --root "'+$twodriveRoot+'"') -WindowStyle Hidden -PassThru
 $twodriveTest.WaitForExit()
 if ($twodriveTest.ExitCode -ne 0) { throw 'Native tray UI verification failed; inspect runtime\tray-ui-test.json.' }
}
Get-Item -LiteralPath $twodriveExe | Select-Object FullName,Length
