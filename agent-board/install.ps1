# Agent Board installer for Windows: downloads the Win98 assets and creates shortcuts
# (Desktop + autostart). Run: powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = 'Stop'
$app = Split-Path -Parent $MyInvocation.MyCommand.Path

# Resolve a real interpreter: `python` in PATH is often the Microsoft Store stub.
$python = $null
foreach ($cmd in @(@('py', '-3'), @('python'))) {
    if (Get-Command $cmd[0] -ErrorAction SilentlyContinue) {
        $exe = & $cmd[0] @($cmd[1..9] | Where-Object { $_ }) -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { $python = $exe.Trim(); break }
    }
}
if (-not $python) { throw 'Python 3 not found: install it from python.org' }
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

& $python (Join-Path $app 'fetch_assets.py')
if ($LASTEXITCODE -ne 0) { throw 'fetch_assets.py failed' }

# Frameless WebView2 window; without it launch.pyw falls back to a browser app window.
& $python -m pip install --user --quiet --disable-pip-version-check --no-warn-script-location pywebview
if ($LASTEXITCODE -ne 0) { Write-Warning 'pywebview not installed: the window will open in a browser' }

$shell = New-Object -ComObject WScript.Shell
function New-Shortcut($path, $arguments) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $pythonw
    $s.Arguments = "`"$(Join-Path $app 'launch.pyw')`" $arguments"
    $s.WorkingDirectory = $app
    $s.IconLocation = Join-Path $app 'web\icons\app.ico'
    $s.Description = 'Agent Board'
    $s.Save()
}
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Agent Board.lnk') ''
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Startup')) 'Agent Board (background).lnk') '--background'
Write-Host 'Agent Board installed: Desktop shortcut + autostart.'
