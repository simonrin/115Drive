param([Parameter(Mandatory=$true)][string]$AppRoot,[Parameter(Mandatory=$true)][string]$Executable)
$ErrorActionPreference='Stop'
$app=[System.IO.Path]::GetFullPath($AppRoot).TrimEnd('\')
$exe=[System.IO.Path]::GetFullPath($Executable)
if(-not (Test-Path -LiteralPath $exe -PathType Leaf)){throw 'Missing shell command executable.'}
foreach($item in @(
    @{key='115Drive.KeepLocal';title='始终保留在此设备上';policy='always_keep';icon='keep-local.ico'},
    @{key='115Drive.FreeSpace';title='释放空间';policy='online_only';icon='free-space.ico'})) {
    $icon=Join-Path $app ('tray/'+$item.icon)
    if(-not (Test-Path -LiteralPath $icon -PathType Leaf)){throw 'Missing shell command icon.'}
    $key='HKCU:\Software\Classes\AllFilesystemObjects\shell\'+$item.key
    New-Item -Path ($key+'\command') -Force | Out-Null
    New-ItemProperty -LiteralPath $key -Name MUIVerb -Value $item.title -PropertyType String -Force | Out-Null
    New-ItemProperty -LiteralPath $key -Name Icon -Value ($icon+',0') -PropertyType String -Force | Out-Null
    New-ItemProperty -LiteralPath $key -Name AppliesTo -Value 'System.StorageProviderId:="115Drive"' -PropertyType String -Force | Out-Null
    New-ItemProperty -LiteralPath $key -Name MultiSelectModel -Value 'Document' -PropertyType String -Force | Out-Null
    $command='"'+$exe+'" --root "'+$app+'" --file-policy '+$item.policy+' --path "%1"'
    Set-Item -LiteralPath ($key+'\command') -Value $command
}
