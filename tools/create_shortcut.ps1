# Creates the "Anime Viewer" shortcut with the application icon.
$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root 'run-detached.bat'
$icon = Join-Path $root 'assets\app_icon.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'Anime Viewer.lnk'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $launcher
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'Anime Viewer - anime and manga'
$shortcut.Save()

Write-Host ''
Write-Host "Shortcut created: $lnk"
Write-Host "It starts: $launcher"
