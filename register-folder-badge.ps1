param(
    [ValidateSet('Apply','Remove')][string]$Action='Apply',
    [string]$AppRoot=$PSScriptRoot,
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{32}$')][string]$TaskId,
    [string]$ContextBase64
)
$ErrorActionPreference='Stop'
$AppRoot=[IO.Path]::GetFullPath($AppRoot).TrimEnd('\')
$Root=[IO.Path]::GetFullPath($Root).TrimEnd('\')
if($Root -notmatch '^[a-zA-Z]:\\.+' -or -not [IO.Directory]::Exists($Root)){throw 'A local task folder is required.'}
$directory=Join-Path $AppRoot 'runtime/shell/folder-icons'
$icon=Join-Path $directory ($TaskId+'.ico')
$manifest=Join-Path $directory ($TaskId+'.json')
function PlainFile([string]$path){
    if(Test-Path -LiteralPath $path){
        if(([IO.File]::GetAttributes($path) -band ([IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint)) -ne 0){throw 'Unexpected badge metadata link.'}
    }
}
function HashBytes([byte[]]$bytes){
    $sha=[Security.Cryptography.SHA256]::Create()
    try{return [Convert]::ToBase64String($sha.ComputeHash($bytes))}finally{$sha.Dispose()}
}
function SaveState($state){
    PlainFile $manifest
    $temporary=$manifest+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
    [IO.File]::WriteAllText($temporary,($state | ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
    if([IO.File]::Exists($manifest)){[IO.File]::Replace($temporary,$manifest,[NullString]::Value)}else{[IO.File]::Move($temporary,$manifest)}
}
$ini=Join-Path $Root 'desktop.ini'
function ReadMetadata {
    PlainFile $ini
    if([IO.File]::Exists($ini)){
        if((Get-Item -LiteralPath $ini -Force).Length -gt 1048576){throw 'Unexpectedly large folder metadata.'}
        $bytes=[IO.File]::ReadAllBytes($ini)
        return [pscustomobject]@{exists=$true;base64=[Convert]::ToBase64String($bytes);hash=(HashBytes $bytes);attributes=[int][IO.File]::GetAttributes($ini);text=[IO.File]::ReadAllText($ini,[Text.Encoding]::Default)}
    }
    return [pscustomobject]@{exists=$false;base64=$null;hash=$null;attributes=0;text=''}
}
function OtherSettings([string]$text){
    $section='';$rows=@(foreach($line in [regex]::Split($text,"\r\n|\n|\r")){
        if($line -match '^\s*\[([^]]+)\]\s*$'){$section=$Matches[1];continue}
        if(-not $line.Trim() -or $line -match '^\s*[;#]'){continue}
        if($section -ieq '.ShellClassInfo' -and $line -match '^\s*(IconResource|IconFile|IconIndex)\s*='){continue}
        $section+':'+$line
    });return ($rows -join "`n")
}
function OwnIcon([string]$text){
    return [regex]::IsMatch($text,('(?im)^\s*IconResource\s*=\s*'+[regex]::Escape($icon)+',\s*0\s*$'))
}
function RestoreMetadata($state){
    PlainFile $ini
    if($state.original_exists){
        $bytes=[Convert]::FromBase64String($state.original_base64)
        if([IO.File]::Exists($ini)){
            $attrs=[IO.File]::GetAttributes($ini)
            [IO.File]::SetAttributes($ini,($attrs -band (-bnot [IO.FileAttributes]::ReadOnly)))
            $stream=[IO.File]::Open($ini,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::Read)
        }else{$stream=[IO.File]::Open($ini,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)}
        try{$stream.Write($bytes,0,$bytes.Length);$stream.SetLength($bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
        [IO.File]::SetAttributes($ini,[IO.FileAttributes]$state.original_attributes)
    }elseif([IO.File]::Exists($ini)){
        # Delete only the exact metadata this task created, after ownership/hash checks.
        [IO.File]::SetAttributes($ini,[IO.FileAttributes]::Normal);[IO.File]::Delete($ini)
    }
    $attrs=[IO.File]::GetAttributes($Root)
    if($state.original_readonly){$attrs=$attrs -bor [IO.FileAttributes]::ReadOnly}else{$attrs=$attrs -band (-bnot [IO.FileAttributes]::ReadOnly)}
    [IO.File]::SetAttributes($Root,$attrs)
    [FolderBadgeAssets]::Notify($Root)
}
$cursor=$directory
while($cursor.Length -ge $AppRoot.Length){
    if(Test-Path -LiteralPath $cursor){
        if(-not [IO.Directory]::Exists($cursor) -or ([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint)){throw 'Unexpected badge storage path.'}
    }
    if($cursor -eq $AppRoot){break};$cursor=[IO.Path]::GetDirectoryName($cursor)
}
$mutex=New-Object Threading.Mutex($false,('Local\115Drive.FolderBadge.'+(HashBytes ([Text.Encoding]::UTF8.GetBytes($AppRoot.ToLowerInvariant()))).Replace('/','_')))
$locked=$false
try{
    try{$locked=$mutex.WaitOne(15000)}catch [Threading.AbandonedMutexException]{$locked=$true}
    if(-not $locked){throw 'Folder badge is busy.'}
    if($Action -eq 'Remove' -and -not [IO.File]::Exists($manifest)){return}
    if(-not ('FolderBadgeAssets' -as [type])){Add-Type -Path (Join-Path $AppRoot 'tray/FolderBadge.dll')}
    $identity=[FolderBadgeAssets]::RootIdentity($Root)
    if($Action -eq 'Apply'){
        if(-not $ContextBase64){throw 'Missing sync-root ownership context.'}
        $context=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($ContextBase64)) | ConvertFrom-Json
        if($context.provider -cne 'Twodrive' -or $context.task -cne $TaskId -or [IO.Path]::GetFullPath($context.root).TrimEnd('\') -ine $Root){throw 'Task ownership context mismatch.'}
        $identity=[FolderBadgeAssets]::VerifyRoot($Root,$ContextBase64)
    }
    PlainFile $manifest;PlainFile $icon
    $current=ReadMetadata
    $state=$null
    if([IO.File]::Exists($manifest)){
        $state=Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
        if($state.version -ne 1 -or $state.owner -ine $AppRoot -or $state.root -ine $Root -or $state.task -cne $TaskId -or $state.root_identity -cne $identity){throw 'Folder badge ownership mismatch.'}
    }
    if($Action -eq 'Remove'){
        if($state.phase -eq 'removed'){return}
        if($current.hash -cne $state.applied_hash -or -not (OwnIcon $current.text)){
            Write-Warning 'Folder appearance was changed by the user; retained as-is.';return
        }
        RestoreMetadata $state
        $state.phase='removed';SaveState $state;return
    }
    if($state -and $state.phase -eq 'applied'){
        if($current.hash -cne $state.applied_hash){Write-Warning 'Folder appearance was changed by the user; retained as-is.';return}
        if(-not [IO.File]::Exists($icon) -or (HashBytes ([IO.File]::ReadAllBytes($icon))) -cne $state.icon_hash){throw 'Managed folder icon is missing or changed.'}
        Write-Output $icon;return
    }
    if($state -and $state.phase -eq 'prepared' -and (OwnIcon $current.text)){
        if((OtherSettings $current.text) -cne $state.original_settings -or (HashBytes ([IO.File]::ReadAllBytes($icon))) -cne $state.icon_hash){throw 'Interrupted folder appearance changed.'}
        $state.phase='applied';$state.applied_hash=$current.hash;SaveState $state
        [FolderBadgeAssets]::Apply($Root,$icon);Write-Output $icon;return
    }
    if($state -and $current.hash -cne $state.original_hash -and -not ($state.phase -eq 'prepared' -and $current.text -ceq $state.original_text)){throw 'Original folder appearance changed.'}
    if($current.text.IndexOf($directory,[StringComparison]::OrdinalIgnoreCase) -ge 0){throw 'Existing managed icon needs its original backup; refusing to add a second badge.'}
    if($current.attributes -band [IO.FileAttributes]::ReadOnly){throw 'Folder metadata is read-only.'}
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $temporary=$icon+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
    [FolderBadgeAssets]::Build($Root,$temporary)
    if([IO.File]::Exists($icon)){[IO.File]::Replace($temporary,$icon,[NullString]::Value)}else{[IO.File]::Move($temporary,$icon)}
    if(-not $state){
        $state=[pscustomobject][ordered]@{version=1;owner=$AppRoot;task=$TaskId;root=$Root;root_identity=$identity;phase='prepared';original_exists=$current.exists;original_base64=$current.base64;original_hash=$current.hash;original_attributes=$current.attributes;original_readonly=([bool]([IO.File]::GetAttributes($Root) -band [IO.FileAttributes]::ReadOnly));original_text=$current.text;original_settings=(OtherSettings $current.text);applied_hash=$null;icon_hash=$null}
    }
    $state.phase='prepared';$state.icon_hash=HashBytes ([IO.File]::ReadAllBytes($icon))
    SaveState $state
    if((ReadMetadata).hash -cne $current.hash){throw 'Folder appearance changed during icon generation.'}
    $metadataChanged=$false
    try{
        # Native Shell APIs otherwise create ANSI desktop.ini on an empty root.
        # Keep its original bytes in the journal and write Unicode before applying.
        $mode=if($current.exists){[IO.FileMode]::Open}else{[IO.FileMode]::CreateNew}
        $stream=[IO.File]::Open($ini,$mode,[IO.FileAccess]::Write,[IO.FileShare]::Read)
        $metadataChanged=$true
        try{
            [byte[]]$unicode=[Text.Encoding]::Unicode.GetPreamble()+[Text.Encoding]::Unicode.GetBytes($current.text)
            $stream.Write($unicode,0,$unicode.Length);$stream.SetLength($unicode.Length);$stream.Flush($true)
        }finally{$stream.Dispose()}
        [IO.File]::SetAttributes($Root,([IO.File]::GetAttributes($Root) -bor [IO.FileAttributes]::ReadOnly))
        [FolderBadgeAssets]::Apply($Root,$icon)
        [IO.File]::SetAttributes($ini,([IO.File]::GetAttributes($ini) -bor [IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::System))
        $updated=ReadMetadata
        if(-not (OwnIcon $updated.text) -or (OtherSettings $updated.text) -cne $state.original_settings){throw 'Folder icon readback mismatch.'}
        $state.applied_hash=$updated.hash;$state.phase='applied';SaveState $state
    }catch{
        if($metadataChanged){RestoreMetadata $state}
        throw
    }
    Write-Output $icon
}finally{
    if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()
}
