param([Parameter(Mandatory=$true)][string]$Root,
      [ValidateSet('Inspect','Suspend','Stop','Restore','Resume')][string]$Action,
      [string]$Data='e30=')
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$Root=[IO.Path]::GetFullPath($Root).TrimEnd('\')
$inputData=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Data)) | ConvertFrom-Json
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class InstallArguments {
 [DllImport("shell32.dll",SetLastError=true)] static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)]string text,out int count);
 [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr value);
 public static string[] Parse(string text) {int count;IntPtr value=CommandLineToArgvW(text,out count);if(value==IntPtr.Zero)throw new Exception("Invalid command line");try{string[] args=new string[count];for(int i=0;i<count;i++)args[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(value,i*IntPtr.Size));return args;}finally{LocalFree(value);}}
}
'@
$owned=@{
    (Join-Path $Root '115Drive.exe')='tray'
    (Join-Path $Root 'service\TwoDrive.Service.exe')='service'
    (Join-Path $Root 'bin\openlist-v4.2.6\openlist.exe')='server'
    (Join-Path $Root 'bin\rclone-v1.75.1\rclone-v1.75.1-windows-amd64\rclone.exe')='transfer'
}
function ArgumentMatches($arguments,$key,$expected,$optional=$false) {
    $indices=@(for($i=1;$i -lt $arguments.Length;$i++){if($arguments[$i] -eq $key){$i}})
    if ($indices.Count -eq 0) { return $optional }
    if ($indices.Count -ne 1 -or $indices[0]+1 -ge $arguments.Length) { return $false }
    try { return [IO.Path]::GetFullPath($arguments[$indices[0]+1]).TrimEnd('\') -eq $expected }
    catch { return $false }
}
function Processes {
    @(Get-CimInstance Win32_Process -Filter "Name='115Drive.exe' OR Name='TwoDrive.Service.exe' OR Name='openlist.exe' OR Name='rclone.exe'" | ForEach-Object {
        if ($_.ExecutablePath -and $owned.ContainsKey($_.ExecutablePath)) {
            $role=$owned[$_.ExecutablePath]
            $arguments=[InstallArguments]::Parse($_.CommandLine)
            $valid=if($role -eq 'server'){ArgumentMatches $arguments '--data' (Join-Path $Root 'runtime\private\openlist')}
                   elseif($role -eq 'transfer'){ArgumentMatches $arguments '--config' (Join-Path $Root 'runtime\private\rclone.conf')}
                   else {ArgumentMatches $arguments '--root' $Root ($role -eq 'tray')}
            if(-not $valid){$role='foreign'}
            @{pid=$_.ProcessId; path=$_.ExecutablePath; started=$_.CreationDate.ToUniversalTime().ToString('o'); role=$role;worker=($role -eq 'service' -and $arguments.Length -gt 1 -and $arguments[1] -eq 'worker')}
        }
    })
}
function Startup {
    @(foreach ($name in @('TwoDrive-115-Sync','TwoDrive-115-Tray')) {
        $task=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task -and $task.Actions.Count -eq 1 -and $task.Actions[0].WorkingDirectory -eq $Root) {
            if(-not $owned.ContainsKey([string]$task.Actions[0].Execute)){throw 'Startup action does not belong to this installation.'}
            @{name=$name; enabled=[bool]$task.Settings.Enabled; running=($task.State -eq 'Running');execute=$task.Actions[0].Execute;arguments=$task.Actions[0].Arguments}
        }
    })
}
if ($Action -eq 'Inspect') {
    @{processes=@(Processes);startup=@(Startup)} | ConvertTo-Json -Depth 5 -Compress
    exit
}
if ($Action -eq 'Stop') {
    foreach ($row in @($inputData.processes | Sort-Object @{Expression={if ($_.role -eq 'tray') {0} elseif ($_.role -eq 'service') {1} else {2}}})) {
        if ($row.role -notin @('tray','service','server') -or -not $owned.ContainsKey([string]$row.path)) { throw 'Unexpected process.' }
        $actual=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$row.pid)
        if (-not $actual) { continue }
        if ($actual.ExecutablePath -ne $row.path -or $actual.CreationDate.ToUniversalTime().ToString('o') -ne $row.started) { throw 'Process identity changed.' }
        Stop-Process -Id $actual.ProcessId
        Wait-Process -Id $actual.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
} else {
    foreach ($row in @($inputData.tasks_startup)) {
        if ($row.name -notin @('TwoDrive-115-Sync','TwoDrive-115-Tray')) { throw 'Unexpected startup task.' }
        $task=Get-ScheduledTask -TaskName $row.name -ErrorAction Stop
        if ($task.Actions.Count -ne 1 -or $task.Actions[0].WorkingDirectory -ne $Root -or $task.Actions[0].Execute -ne $row.execute -or $task.Actions[0].Arguments -ne $row.arguments) { throw 'Startup ownership changed.' }
        if ($Action -eq 'Resume') {
            if ($row.running) {
                if($row.enabled){Start-ScheduledTask -InputObject $task}
                else {Start-Process -FilePath $task.Actions[0].Execute -ArgumentList $task.Actions[0].Arguments -WorkingDirectory $Root -WindowStyle Hidden | Out-Null}
            }
        }
        elseif ($Action -eq 'Restore' -and $row.enabled) { Enable-ScheduledTask -InputObject $task | Out-Null }
        else { Disable-ScheduledTask -InputObject $task | Out-Null }
    }
    if ($Action -eq 'Resume') {
        if($inputData.worker_running -and -not @($inputData.tasks_startup | Where-Object {$_.name -eq 'TwoDrive-115-Sync' -and $_.running}).Count) {
            Start-Process -FilePath (Join-Path $Root 'service\TwoDrive.Service.exe') -ArgumentList ('worker --root "'+$Root+'"') -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
        }
        Start-Process -FilePath (Join-Path $Root '115Drive.exe') -ArgumentList ('--root "'+$Root+'"') -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
    }
}
'{}'
