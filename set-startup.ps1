param([ValidateSet('0','1')][string]$Enabled)
$ErrorActionPreference = 'Stop'
$tasks = @('TwoDrive-115-Sync','TwoDrive-115-Tray') | ForEach-Object { Get-ScheduledTask -TaskName $_ }
foreach ($task in $tasks) {
    if ($task.Actions.WorkingDirectory -ne $PSScriptRoot) { throw 'Task belongs to another installation.' }
}
foreach ($task in $tasks) {
    foreach ($trigger in $task.Triggers) { $trigger.Enabled = ($Enabled -eq '1') }
    Set-ScheduledTask -InputObject $task | Out-Null
}
