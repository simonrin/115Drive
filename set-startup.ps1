param([ValidateSet('0','1')][string]$Enabled)
$ErrorActionPreference = 'Stop'
$cfg = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$tasks = @('TwoDrive-115-Sync','TwoDrive-115-Tray') | ForEach-Object { Get-ScheduledTask -TaskName $_ }
foreach ($task in $tasks) {
    if ($task.Actions.WorkingDirectory -ne $PSScriptRoot) { throw 'Task belongs to another installation.' }
}
foreach ($task in $tasks) {
    foreach ($trigger in $task.Triggers) { $trigger.Enabled = ($Enabled -eq '1' -and -not ($task.TaskName -eq 'TwoDrive-115-Sync' -and $cfg.legacy_enabled -eq $false)) }
    Set-ScheduledTask -InputObject $task | Out-Null
}
