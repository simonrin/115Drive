param(
    [Parameter(Mandatory=$true)][ValidateSet('Register','Unregister')][string]$Action,
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{32}$')][string]$TaskId,
    [string]$DisplayName = '115Drive',
    [string]$IconPath,
    [string]$ContextBase64
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$manager = [Windows.Storage.Provider.StorageProviderSyncRootManager,Windows.Storage.Provider,ContentType=WindowsRuntime]
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$id = '115Drive!' + $sid + '!115-' + $TaskId
if ($Action -eq 'Unregister') {
    try {
        $activeInfo = $manager::GetSyncRootInformationForId($id)
        if ([IO.Path]::GetFullPath($activeInfo.Path.Path).TrimEnd('\') -ne [IO.Path]::GetFullPath($Root).TrimEnd('\')) { throw 'Sync-root path mismatch; registration retained.' }
    } catch {
        if ($_.Exception.GetBaseException().HResult -ne -2147023728) { throw }
    }
    # CfUnregisterSyncRoot can remove UserSyncRoots before WinRT sees the ID.
    # Capture the exact per-user Shell shortcut so a retry can remove that
    # orphan too. Never touch another path or another user's registration.
    $shellClass = $null
    $registration = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\SyncRootManager\' + $id
    if (Test-Path -LiteralPath $registration) {
        $candidate = (Get-ItemProperty -LiteralPath $registration).NamespaceCLSID
        if ($candidate -match '^\{[0-9a-fA-F-]{36}\}$') {
            $bag = "HKCU:\Software\Classes\CLSID\$candidate\Instance\InitPropertyBag"
            if (Test-Path -LiteralPath $bag) {
                $target = (Get-ItemProperty -LiteralPath $bag).TargetFolderPath
                if ($target -and [IO.Path]::GetFullPath($target).TrimEnd('\') -eq [IO.Path]::GetFullPath($Root).TrimEnd('\')) { $shellClass = $candidate }
            }
        }
    }
    try { $manager::Unregister($id) }
    catch {
        # A never-started task or a retried removal may have no shell entry.
        if ($_.Exception.GetBaseException().HResult -ne -2147023728) { throw }
    }
    if ($shellClass) {
        foreach ($key in @("Software\Microsoft\Windows\CurrentVersion\Explorer\Desktop\NameSpace\$shellClass", "Software\Classes\CLSID\$shellClass")) {
            [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($key,$false)
        }
        $hide = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel',$true)
        if ($hide) { try { $hide.DeleteValue($shellClass,$false) } finally { $hide.Dispose() } }
    }
    $badge = Join-Path $PSScriptRoot 'register-folder-badge.ps1'
    if (Test-Path -LiteralPath $badge) {
        try { & $badge -Action Remove -AppRoot $PSScriptRoot -Root $Root -TaskId $TaskId | Out-Null }
        catch { Write-Warning ('Folder badge restore: ' + $_.Exception.Message) }
    }
    $navigation = Join-Path $PSScriptRoot 'register-account-navigation.ps1'
    if (Test-Path -LiteralPath $navigation) { & $navigation -AppRoot $PSScriptRoot -SkipTaskId $TaskId }
    return
}
$resolved = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
$info = New-Object Windows.Storage.Provider.StorageProviderSyncRootInfo
$info.Id = $id
$operation = [Windows.Storage.StorageFolder,Windows.Storage,ContentType=WindowsRuntime]::GetFolderFromPathAsync($resolved)
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
$await = $asTask.MakeGenericMethod([Windows.Storage.StorageFolder]).Invoke($null,@($operation))
$await.Wait()
$info.Path = $await.Result
$info.DisplayNameResource = $DisplayName
$info.Version = '0.3.0'
if ($IconPath) { $info.IconResource = [System.IO.Path]::GetFullPath($IconPath) + ',0' }
else { $info.IconResource = Join-Path $env:SystemRoot 'System32\imageres.dll,3' }
$info.HydrationPolicy = [Windows.Storage.Provider.StorageProviderHydrationPolicy]::Full
$info.PopulationPolicy = [Windows.Storage.Provider.StorageProviderPopulationPolicy]::AlwaysFull
$info.InSyncPolicy = [Windows.Storage.Provider.StorageProviderInSyncPolicy]::FileLastWriteTime
$info.HardlinkPolicy = [Windows.Storage.Provider.StorageProviderHardlinkPolicy]::None
$info.ShowSiblingsAsGroup = $false
# Use provider-scoped Chinese verbs with icons when the packaged handler exists.
# This suppresses only the default Shell pin verbs; CfAPI pinning is unchanged.
$menuScript=Join-Path $PSScriptRoot 'register-shell-menu.ps1'
if ($IconPath -and (Test-Path -LiteralPath $menuScript) -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'tray/keep-local.ico')) -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'tray/free-space.ico'))) {
    & $menuScript -AppRoot $PSScriptRoot -Executable $IconPath
    $info.AllowPinning = $false
}
if (-not $ContextBase64) { throw 'Missing sync-root ownership context.' }
$info.Context = [System.Runtime.InteropServices.WindowsRuntime.WindowsRuntimeBufferExtensions]::AsBuffer([Convert]::FromBase64String($ContextBase64))
$manager::Register($info)
$badge = Join-Path $PSScriptRoot 'register-folder-badge.ps1'
if (Test-Path -LiteralPath $badge) {
    try { & $badge -AppRoot $PSScriptRoot -Root $resolved -TaskId $TaskId -ContextBase64 $ContextBase64 | Out-Null }
    catch { Write-Warning ('Folder badge: ' + $_.Exception.Message) }
}
$navigation = Join-Path $PSScriptRoot 'register-account-navigation.ps1'
if (Test-Path -LiteralPath $navigation) { & $navigation -AppRoot $PSScriptRoot -IconPath $IconPath }
Write-Output 'Registered'
