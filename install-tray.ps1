param([switch]$Show)
$ErrorActionPreference = 'Stop'
$twodriveRoot = $PSScriptRoot
$twodriveExe = Join-Path $twodriveRoot 'bin\TwoDriveTray\TwoDriveTray.exe'
if (Test-Path -LiteralPath (Join-Path $twodriveRoot '115Drive.exe')) { $twodriveExe = Join-Path $twodriveRoot '115Drive.exe' }
if (-not (Test-Path -LiteralPath $twodriveExe)) { & (Join-Path $twodriveRoot 'tray\build.ps1') }
$twodriveSyncTask = Get-ScheduledTask -TaskName 'TwoDrive-115-Sync'
$twodrivePythonw = $twodriveSyncTask.Actions.Execute
if (-not (Test-Path -LiteralPath $twodrivePythonw)) { throw 'Configured Python runtime is unavailable.' }
if ($twodrivePythonw -like '*pythonw.exe') { @{pythonw=$twodrivePythonw} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $twodriveRoot 'tray\settings.json') -Encoding UTF8 }
$twodriveIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$twodriveArguments = '--root "' + $twodriveRoot + '"'
$twodriveOldTask = Get-ScheduledTask -TaskName 'TwoDrive-115-Tray' -ErrorAction SilentlyContinue
if ($twodriveOldTask -and ($twodriveOldTask.Actions.Execute -ne $twodriveExe -or $twodriveOldTask.Actions.Arguments -ne $twodriveArguments)) { throw 'Tray task name is used by a different application.' }
$twodriveAction = New-ScheduledTaskAction -Execute $twodriveExe -Argument $twodriveArguments -WorkingDirectory $twodriveRoot
$twodriveTrigger = New-ScheduledTaskTrigger -AtLogOn -User $twodriveIdentity
$cfg = Get-Content -LiteralPath (Join-Path $twodriveRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($cfg.start_with_windows -eq $false) { $twodriveTrigger.Enabled = $false }
$twodriveSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$twodrivePrincipal = New-ScheduledTaskPrincipal -UserId $twodriveIdentity -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'TwoDrive-115-Tray' -Action $twodriveAction -Trigger $twodriveTrigger -Settings $twodriveSettings -Principal $twodrivePrincipal -Description '115Drive system tray and live synchronization progress panel.' -Force | Out-Null
Start-ScheduledTask -TaskName 'TwoDrive-115-Tray'
if ($Show) { Start-Process -FilePath $twodriveExe -ArgumentList ('--show '+$twodriveArguments) -WorkingDirectory $twodriveRoot -WindowStyle Hidden }
Get-ScheduledTask -TaskName 'TwoDrive-115-Tray' | Select-Object TaskName,State
