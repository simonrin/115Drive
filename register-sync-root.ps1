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
    try { $manager::Unregister($id) }
    catch {
        # A never-started task or a retried removal may have no shell entry.
        if ($_.Exception.GetBaseException().HResult -ne -2147023728) { throw }
    }
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
Write-Output 'Registered'
