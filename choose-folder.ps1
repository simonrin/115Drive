$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Add-Type -AssemblyName System.Windows.Forms
$settings = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择新的本地同步文件夹（空文件夹）；确认后还需在设置页检查并开始切换。'
$dialog.SelectedPath = $settings.local_path
$dialog.ShowNewFolderButton = $true
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.Show()
try {
    $selected = $null
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { $selected = $dialog.SelectedPath }
    @{ path = $selected } | ConvertTo-Json -Compress
} finally {
    $dialog.Dispose()
    $owner.Close()
    $owner.Dispose()
}
