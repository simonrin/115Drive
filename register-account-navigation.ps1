param(
    [string]$AppRoot = $PSScriptRoot,
    [string]$SkipTaskId,
    [string]$IconPath
)
$ErrorActionPreference = 'Stop'
# Presentation only: single-location Windows libraries show the original
# directories without shortcut overlays. They are not sync roots or copies.
$AppRoot = [IO.Path]::GetFullPath($AppRoot).TrimEnd('\')
$config = Join-Path $AppRoot 'runtime/private/sync-tasks.json'
if (-not (Test-Path -LiteralPath $config)) { return }
function Digest([string]$value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($value)))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function ClassId([string]$value) {
    $hex = (Digest ('115Drive.navigation.v1|' + $AppRoot.ToLowerInvariant() + '|' + $value)).Substring(0,32)
    return ([guid]$hex).ToString('B').ToUpperInvariant()
}
$mutex = New-Object Threading.Mutex($false,('Local\115Drive.Navigation.' + (Digest $AppRoot.ToLowerInvariant()).Substring(0,24)))
$locked = $false
function SetValue([string]$key,[string]$name,$value,[Microsoft.Win32.RegistryValueKind]$kind='String') {
    $item = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($key)
    try { if ($item.GetValue($name,$null) -cne $value) { $item.SetValue($name,$value,$kind) } }
    finally { $item.Dispose() }
}
function RegisterFolder([string]$clsid,[string]$name,[string]$target,[bool]$pinned) {
    $base = 'Software\Classes\CLSID\' + $clsid
    $existing = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($base)
    if ($existing) {
        try { if ($existing.GetValue('115Drive.NavigationOwner') -ne $AppRoot) { throw 'Shell class ownership mismatch.' } }
        finally { $existing.Dispose() }
    }
    SetValue $base '115Drive.NavigationOwner' $AppRoot
    SetValue $base '' $name
    SetValue $base 'System.IsPinnedToNameSpaceTree' ([int]$pinned) DWord
    SetValue $base 'SortOrderIndex' 66 DWord
    SetValue "$base\DefaultIcon" '' ($IconPath + ',0')
    SetValue "$base\InProcServer32" '' '%SystemRoot%\system32\shell32.dll' ExpandString
    SetValue "$base\Instance" 'CLSID' '{0E5AAE11-A475-4C5B-AB00-C66DE400274E}'
    SetValue "$base\Instance\InitPropertyBag" 'Attributes' 17 DWord
    SetValue "$base\Instance\InitPropertyBag" 'TargetFolderPath' $target ExpandString
    SetValue "$base\ShellFolder" 'FolderValueFlags' 40 DWord
    SetValue "$base\ShellFolder" 'Attributes' ([int]-260046771) DWord # 0xF080004D
    if ($pinned) {
        SetValue ('Software\Microsoft\Windows\CurrentVersion\Explorer\Desktop\NameSpace\' + $clsid) '' $name
        SetValue 'Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel' $clsid 1 DWord
    }
}
function RemoveOwnedClass([string]$clsid) {
    if ($clsid -notmatch '^\{[0-9A-F-]{36}\}$') { throw 'Invalid shell class.' }
    $base = 'Software\Classes\CLSID\' + $clsid
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($base)
    if (-not $key) { return }
    try { if ($key.GetValue('115Drive.NavigationOwner') -ne $AppRoot) { throw 'Shell class ownership mismatch.' } }
    finally { $key.Dispose() }
    [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree(('Software\Microsoft\Windows\CurrentVersion\Explorer\Desktop\NameSpace\' + $clsid),$false)
    $hide = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel',$true)
    if ($hide) { try { $hide.DeleteValue($clsid,$false) } finally { $hide.Dispose() } }
    [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($base,$false)
}
function WriteTextIfChanged([string]$path,[string]$text) {
    RequirePlainFile $path
    if (-not [IO.File]::Exists($path) -or [IO.File]::ReadAllText($path) -cne $text) {
        if ([IO.File]::Exists($path)) { [IO.File]::SetAttributes($path,[IO.FileAttributes]::Normal) }
        [IO.File]::WriteAllText($path,$text,[Text.Encoding]::Unicode)
    }
}
function RequirePlainFile([string]$path) {
    if (Test-Path -LiteralPath $path) {
        if (([IO.File]::GetAttributes($path) -band ([IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint)) -ne 0) { throw 'Unexpected navigation metadata link.' }
    }
}
function RequirePlainPath([string]$path) {
    $cursor = [IO.Path]::GetFullPath($path)
    $boundary = [IO.Path]::GetFullPath((Join-Path $AppRoot 'runtime'))
    if (-not $cursor.StartsWith(($boundary + '\'),[StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid navigation path.' }
    while ($cursor.Length -ge $boundary.Length) {
        if ([IO.Directory]::Exists($cursor) -and (([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'Unexpected navigation reparse point.' }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
}
function ValidateLibrary([string]$path,[string]$target) {
    RequirePlainFile $path
    $xml = New-Object Xml.XmlDocument
    $xml.XmlResolver = $null
    $xml.Load($path)
    $ns = New-Object Xml.XmlNamespaceManager($xml.NameTable)
    $ns.AddNamespace('l','http://schemas.microsoft.com/windows/2009/library')
    $locations = @($xml.SelectNodes('/l:libraryDescription/l:searchConnectorDescriptionList/l:searchConnectorDescription/l:simpleLocation/l:url',$ns))
    if ($locations.Count -ne 1 -or $locations[0].InnerText -ne $target) { throw 'Navigation library target changed.' }
}
function ValidateLegacyLink($entry) {
    $path = [IO.Path]::GetFullPath($entry.path)
    RequirePlainPath $path
    if (-not [IO.Directory]::Exists($path)) { return }
    foreach ($item in [IO.Directory]::GetFileSystemEntries($path)) {
        if ([IO.Path]::GetFileName($item) -notin @('desktop.ini','target.lnk')) { throw 'Navigation shortcut contains unrecognized content.' }
        RequirePlainFile $item
    }
    $link = Join-Path $path 'target.lnk'
    if ([IO.File]::Exists($link) -and $shortcuts.CreateShortcut($link).TargetPath -ne $entry.target) { throw 'Navigation shortcut target changed.' }
    $ini = Join-Path $path 'desktop.ini'
    if ([IO.File]::Exists($ini) -and -not [IO.File]::ReadAllText($ini).Contains('CLSID2={0AFACED1-E828-11D1-9187-B532F1E9575D}')) { throw 'Navigation metadata ownership mismatch.' }
}
try {
    try { $locked = $mutex.WaitOne(30000) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Account navigation is busy.' }
    $tasks = @((Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json).tasks | Where-Object { $_.id -ne $SkipTaskId })
    $navigation = Join-Path $AppRoot 'runtime/shell/accounts'
    $manifest = Join-Path $AppRoot 'runtime/shell/navigation.json'
    $previous = @()
    if (Test-Path -LiteralPath $manifest) { $previous = @((Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json).entries) }
    if (-not $IconPath) {
        $IconPath = Join-Path $AppRoot 'bin/TwoDriveTray/TwoDriveTray.exe'
        if (-not (Test-Path -LiteralPath $IconPath)) { $IconPath = Join-Path $AppRoot '115Drive.exe' }
    }
    # Folder entries have their own yellow-folder/115Drive-cloud artwork.
    $folderIconResource = $IconPath + ',0'
    $folderIcon = Join-Path $AppRoot 'tray/115Drive-folder.ico'
    if (Test-Path -LiteralPath $folderIcon -PathType Leaf) { $folderIconResource = $folderIcon + ',0' }
    $entries = @()
    $shortcuts = New-Object -ComObject WScript.Shell
    # Check legacy entries before creating replacements, so unexpected content
    # cannot leave a partially migrated account behind.
    foreach ($old in $previous) { if ($old.kind -eq 'link') { ValidateLegacyLink $old } }
    $accounts = @($tasks | Group-Object account | Sort-Object Name)
    foreach ($account in $accounts) {
        $parentId = ClassId ('account|' + $account.Name)
        $folder = Join-Path $navigation (Digest $account.Name).Substring(0,24)
        RequirePlainPath $folder
        [IO.Directory]::CreateDirectory($folder) | Out-Null
        $label = '115Drive'
        if ($accounts.Count -gt 1) { $label += ' - ' + $account.Name }
        RegisterFolder $parentId $label $folder $true
        $entries += [ordered]@{clsid=$parentId;path=$folder;kind='account';account=$account.Name}
        $labels = @((';115Drive.NavigationOwner=' + (Digest $AppRoot.ToLowerInvariant())),'[LocalizedFileNames]')
        $usedNames = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        foreach ($task in @($account.Group | Sort-Object id)) {
            if ($task.id -notmatch '^[a-f0-9]{32}$') { throw 'Invalid task identifier.' }
            $display = ([string]$task.name).Replace("`r",' ').Replace("`n",' ')
            # Use readable physical names too: a delegated Shell namespace can
            # ignore its backing directory's LocalizedFileNames customizations.
            $fileName = $display
            foreach ($invalid in [IO.Path]::GetInvalidFileNameChars()) { $fileName = $fileName.Replace([string]$invalid,'_') }
            $fileName = $fileName.Trim().TrimEnd('.')
            if ($fileName.Length -gt 100) { $fileName = $fileName.Substring(0,100).TrimEnd('.') }
            if (-not $fileName) { $fileName = '115Drive' }
            if ($fileName -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])($|\.)') { $fileName = '_' + $fileName }
            if (-not $usedNames.Add($fileName)) { $fileName += ' (' + $task.id + ')'; if (-not $usedNames.Add($fileName)) { throw 'Duplicate navigation filename.' } }
            $child = Join-Path $folder ($fileName + '.library-ms')
            RequirePlainPath (Split-Path $child)
            RequirePlainFile $child
            if ([IO.File]::Exists($child) -and $child -notin @($previous | ForEach-Object {$_.path})) { throw 'Unowned navigation library.' }
            $target = [IO.Path]::GetFullPath($task.local_path)
            $taskIconResource = $folderIconResource
            $taskIcon = Join-Path $AppRoot ('runtime/shell/folder-icons/' + $task.id + '.ico')
            RequirePlainPath (Split-Path $taskIcon)
            RequirePlainFile $taskIcon
            if (Test-Path -LiteralPath $taskIcon -PathType Leaf) { $taskIconResource = $taskIcon + ',0' }
            $labels += ($fileName + '.library-ms=' + $display)
            $escapedTarget = [Security.SecurityElement]::Escape($target)
            $escapedIcon = [Security.SecurityElement]::Escape($taskIconResource)
            $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<libraryDescription xmlns="http://schemas.microsoft.com/windows/2009/library">
  <version>6</version>
  <isLibraryPinned>false</isLibraryPinned>
  <iconReference>$escapedIcon</iconReference>
  <templateInfo><folderType>{5C4F28B5-F869-4E84-8E60-F11DB97C5CC7}</folderType></templateInfo>
  <searchConnectorDescriptionList><searchConnectorDescription>
    <isDefaultSaveLocation>true</isDefaultSaveLocation>
    <isSupported>true</isSupported>
    <simpleLocation><url>$escapedTarget</url></simpleLocation>
  </searchConnectorDescription></searchConnectorDescriptionList>
</libraryDescription>
"@
            # Explorer can add serialized location data when a library opens.
            # Keep it if its target and icon are still ours; no-op registration
            # must not continually rewrite the user's view metadata.
            $writeLibrary = $true
            if ([IO.File]::Exists($child)) {
                $oldLibrary = @($previous | Where-Object {$_.path -eq $child -and $_.kind -eq 'library'}) | Select-Object -First 1
                if (-not $oldLibrary) { throw 'Navigation library ownership mismatch.' }
                ValidateLibrary $child $oldLibrary.target
                [xml]$currentLibrary = [IO.File]::ReadAllText($child)
                $writeLibrary = $oldLibrary.target -ne $target -or $currentLibrary.libraryDescription.iconReference -ne $taskIconResource
            }
            if ($writeLibrary) { WriteTextIfChanged $child $xml }
            $entries += [ordered]@{path=$child;kind='library';task=$task.id;target=$target}
            # Keep the native registration and its identity; hide only its
            # separate top-level Shell shortcut after our child is available.
            $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
            $native = 'SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\SyncRootManager\115Drive!' + $sid + '!115-' + $task.id
            $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($native)
            if ($key) {
                try { $nativeId = $key.GetValue('NamespaceCLSID') } finally { $key.Dispose() }
                if ($nativeId -match '^\{[0-9a-fA-F-]{36}\}$') {
                    $bag = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Software\Classes\CLSID\$nativeId\Instance\InitPropertyBag")
                    if ($bag) {
                        try { $target = $bag.GetValue('TargetFolderPath') } finally { $bag.Dispose() }
                        if ($target -and [IO.Path]::GetFullPath($target).TrimEnd('\') -eq [IO.Path]::GetFullPath($task.local_path).TrimEnd('\')) {
                            SetValue "Software\Classes\CLSID\$nativeId" 'System.IsPinnedToNameSpaceTree' 0 DWord
                        }
                    }
                }
            }
        }
        $ini = Join-Path $folder 'desktop.ini'
        RequirePlainFile $ini
        if ([IO.File]::Exists($ini) -and -not [IO.File]::ReadAllText($ini).Contains($labels[0])) { throw 'Account display metadata ownership mismatch.' }
        WriteTextIfChanged $ini (($labels -join "`r`n") + "`r`n")
        [IO.File]::SetAttributes($ini,([IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::System))
        $folderAttributes = [IO.File]::GetAttributes($folder)
        if (($folderAttributes -band [IO.FileAttributes]::ReadOnly) -eq 0) { [IO.File]::SetAttributes($folder,($folderAttributes -bor [IO.FileAttributes]::ReadOnly)) }
    }
    foreach ($old in @($previous | Sort-Object { if ($_.kind -ne 'account') { 0 } else { 1 } })) {
        if ($old.path -in @($entries | ForEach-Object {$_.path})) { continue }
        $path = [IO.Path]::GetFullPath($old.path)
        $prefix = [IO.Path]::GetFullPath($navigation).TrimEnd('\') + '\'
        if (-not $path.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw 'Navigation path escapes its directory.' }
        if (Test-Path -LiteralPath $path) {
            RequirePlainPath $path
            if ($old.kind -eq 'library') {
                ValidateLibrary $path $old.target
                [IO.File]::Delete($path)
            } elseif ($old.kind -eq 'link') {
                ValidateLegacyLink $old
                $children = @([IO.Directory]::GetFileSystemEntries($path))
                foreach ($item in $children) { [IO.File]::SetAttributes($item,[IO.FileAttributes]::Normal); [IO.File]::Delete($item) }
                [IO.File]::SetAttributes($path,[IO.FileAttributes]::Normal)
                [IO.Directory]::Delete($path,$false)
            } elseif ($old.kind -eq 'account') {
                $ini = Join-Path $path 'desktop.ini'
                if ([IO.File]::Exists($ini)) {
                    RequirePlainFile $ini
                    if (-not [IO.File]::ReadAllText($ini).Contains(';115Drive.NavigationOwner=' + (Digest $AppRoot.ToLowerInvariant()))) { throw 'Account display metadata ownership mismatch.' }
                    [IO.File]::SetAttributes($ini,[IO.FileAttributes]::Normal)
                    [IO.File]::Delete($ini)
                }
                # Non-recursive deletion is intentional: never delete user content.
                [IO.File]::SetAttributes($path,[IO.FileAttributes]::Normal)
                [IO.Directory]::Delete($path,$false)
            } else {
                throw 'Unknown navigation entry type.'
            }
        }
        if ($old.clsid -and $old.clsid -notin @($entries | ForEach-Object {$_.clsid})) { RemoveOwnedClass $old.clsid }
    }
    $json = [ordered]@{version=3;owner=$AppRoot;entries=@($entries)} | ConvertTo-Json -Depth 5
    if (-not (Test-Path -LiteralPath $manifest) -or [IO.File]::ReadAllText($manifest) -cne $json) {
        $temp = $manifest + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
        [IO.File]::WriteAllText($temp,$json,[Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($manifest)) { [IO.File]::Replace($temp,$manifest,[NullString]::Value) } else { [IO.File]::Move($temp,$manifest) }
    }
    Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public static class AccountShellNotify{[DllImport("shell32.dll")]public static extern void SHChangeNotify(uint e,uint f,IntPtr a,IntPtr b);}' -ErrorAction SilentlyContinue
    [AccountShellNotify]::SHChangeNotify(0x08000000,0,[IntPtr]::Zero,[IntPtr]::Zero)
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
