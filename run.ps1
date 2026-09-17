# Anime Viewer launcher for PowerShell (ASCII-only messages: safe for PS 5.1 codepage)
$root = $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }
Set-Location $root

$env:PYTHONIOENCODING = 'utf-8'

$main = Join-Path $root 'main.py'
$py = Join-Path $root 'runtime\python.exe'
$pyw = Join-Path $root 'runtime\pythonw.exe'

# --detach: start without a console window, so closing the terminal
# cannot kill the application
if ($args -contains '--detach') {
    if (Test-Path $pyw) {
        # кавычки внутри -ArgumentList обязательны: путь содержит пробелы и скобки
        Start-Process -FilePath $pyw -ArgumentList "`"$main`"" -WorkingDirectory $root | Out-Null
        Write-Host 'Anime Viewer started (detached, no console).'
        exit 0
    }
}

if (Test-Path $py) {
    & $py $main @args
    exit $LASTEXITCODE
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $main @args
    exit $LASTEXITCODE
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    & python $main @args
    exit $LASTEXITCODE
}

Write-Host 'Python 3.10+ not found. Keep the "runtime" folder next to main.py.'
exit 1
