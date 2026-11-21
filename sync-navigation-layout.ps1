param([string]$AppRoot = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($AppRoot).TrimEnd('\')
$manifest = Get-Content -LiteralPath (Join-Path $root 'runtime/shell/navigation.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$tasks = (Get-Content -LiteralPath (Join-Path $root 'runtime/private/sync-tasks.json') -Raw -Encoding UTF8 | ConvertFrom-Json).tasks
if ($manifest.owner -ne $root) { throw 'The navigation manifest belongs to another installation.' }
$libraries = @($manifest.entries | Where-Object kind -eq 'library')
if (!$libraries.Count) { return }
$accounts = [IO.Path]::GetFullPath((Join-Path $root 'runtime/shell/accounts'))

function Wait-Folder($window, [string]$path) {
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 100
        try { $ready = !$window.Busy -and $window.Document.Folder.Self.Path -eq $path } catch { $ready = $false }
        if ($ready) { return }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Explorer did not open $path"
}
function Read-Layout($view) {
    [pscustomobject]@{Mode=$view.CurrentViewMode; Size=$view.IconSize; Group=$view.GroupBy; Sort=$view.SortColumns}
}
function Open-Library($window, $library, $account) {
    $window.Navigate2(('shell:::' + $account.clsid))
    Wait-Folder $window $account.path
    $item = $window.Document.Folder.ParseName([IO.Path]::GetFileName($library.path))
    if (!$item -or !$item.IsFolder -or $item.Path -ne $library.path) { throw 'Unexpected account entry.' }
    $item.InvokeVerb('open')
    Wait-Folder $window $library.path
}
function Equal-Layout($left, $right) {
    $left.Mode -eq $right.Mode -and $left.Size -eq $right.Size -and $left.Group -ceq $right.Group -and $left.Sort -ceq $right.Sort
}

$shell = New-Object -ComObject Shell.Application
$existing = @($shell.Windows() | ForEach-Object { $_.HWND })
$seed = [IO.Directory]::CreateDirectory((Join-Path ([IO.Path]::GetTempPath()) ('115Drive-layout-' + [guid]::NewGuid().ToString('N')))).FullName
$window = $null
try {
    $shell.Explore($seed)
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 100
        $window = $shell.Windows() | Where-Object { $_.HWND -notin $existing -and $_.Document.Folder.Self.Path -eq $seed } | Select-Object -First 1
    } until ($window -or [DateTime]::UtcNow -ge $deadline)
    if (!$window) { throw 'Could not identify a separate Explorer window for copying folder views.' }
    foreach ($library in $libraries) {
        $path = [IO.Path]::GetFullPath($library.path)
        $parent = [IO.Path]::GetDirectoryName($path)
        $account = @($manifest.entries | Where-Object { $_.kind -eq 'account' -and $_.path -eq $parent })
        $task = @($tasks | Where-Object { $_.id -eq $library.task -and $_.local_path -eq $library.target })
        if ($task.Count -ne 1 -or $account.Count -ne 1 -or $account[0].clsid -notmatch '^\{[0-9a-fA-F-]{36}\}$' -or
            [IO.Path]::GetDirectoryName($parent) -ne $accounts -or [IO.Path]::GetExtension($path) -ne '.library-ms') {
            throw 'The navigation entry does not match a configured task and account.'
        }
        if (![IO.Directory]::Exists($library.target)) { Write-Warning "Folder unavailable: $($library.target)"; continue }
        $window.Navigate2($library.target)
        Wait-Folder $window $library.target
        $source = Read-Layout $window.Document
        Open-Library $window $library $account[0]
        $view = $window.Document
        if ($view.CurrentViewMode -ne $source.Mode) { $view.CurrentViewMode = $source.Mode }
        if ($view.IconSize -ne $source.Size) { $view.IconSize = $source.Size }
        if ($view.GroupBy -cne $source.Group) { $view.GroupBy = $source.Group }
        if ($view.SortColumns -cne $source.Sort) { $view.SortColumns = $source.Sort }
        # Navigating away commits the library's view to Explorer's per-user settings.
        Open-Library $window $library $account[0]
        if (!(Equal-Layout (Read-Layout $window.Document) $source)) { throw "Explorer did not retain the layout for $path" }
        [pscustomobject]@{Folder=$task[0].name; Mode=$source.Mode; IconSize=$source.Size; Sort=$source.Sort; Group=$source.Group}
    }
} finally {
    if ($window) { $window.Quit() }
    [IO.Directory]::Delete($seed, $false)
}
