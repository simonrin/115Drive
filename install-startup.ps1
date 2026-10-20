$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$service = Join-Path $root 'service\TwoDrive.Service.exe'
if (Test-Path -LiteralPath $service) {
    $workerExe = $service
    $workerArgs = 'worker --root "' + $root + '"'
} else {
    $workerExe = Join-Path (Split-Path (Get-Command python.exe).Source) 'pythonw.exe'
    $workerArgs = '"' + (Join-Path $root 'twodrive.py') + '" worker'
}
if (-not (Test-Path (Join-Path $root 'runtime\private\initialized.json'))) {
    throw 'Initial synchronization has not passed. Startup was not enabled.'
}
$taskName = 'TwoDrive-115-Sync'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $workerExe -Argument $workerArgs -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$cfg = Get-Content -LiteralPath (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($cfg.start_with_windows -eq $false) { $trigger.Enabled = $false }
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$old = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($old -and $old.Actions.Arguments -ne $action.Arguments) { throw 'A different task already uses this name.' }
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Sync the configured local folder with 115 /115Drive using OpenList and rclone.' -Force | Out-Null
Remove-Item -LiteralPath (Join-Path $root 'runtime\stop') -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
if ((Test-Path -LiteralPath (Join-Path $root 'bin\TwoDriveTray\TwoDriveTray.exe')) -or (Test-Path -LiteralPath (Join-Path $root '115Drive.exe'))) {
    & (Join-Path $root 'install-tray.ps1')
}
