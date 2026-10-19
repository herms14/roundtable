# Installs / updates the Roundtable Companion desktop app on this PC.
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Code is copied to %LOCALAPPDATA%\RoundtableCompanion (node_modules stays out of OneDrive), Electron is installed
# with npm, and a Start Menu shortcut is created. Re-run after changing main.js / preload.js.
#
# Auth: Google blocks SSO inside desktop apps, so the app talks to the portal directly on the LAN with a device token.
# %APPDATA%\roundtable-companion\config.json = {"url": "http://192.168.40.16:8088/", "token": "<COMPANION_TOKEN>"}
# The token lives in ~/.olympus_env (COMPANION_TOKEN) on codex-agent-lxc; never commit it.
# First-time config: set $env:ROUNDTABLE_COMPANION_TOKEN before running and config.json is written for you
# (an existing config.json is never overwritten).
$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot
$dst = Join-Path $env:LOCALAPPDATA 'RoundtableCompanion'
$cfgDir = Join-Path $env:APPDATA 'roundtable-companion'
$cfg = Join-Path $cfgDir 'config.json'
if (-not (Test-Path $cfg) -and $env:ROUNDTABLE_COMPANION_TOKEN) {
    New-Item -ItemType Directory -Force $cfgDir | Out-Null
    @{ url = 'http://192.168.40.16:8088/'; token = $env:ROUNDTABLE_COMPANION_TOKEN } | ConvertTo-Json | Set-Content -Encoding UTF8 $cfg
    Write-Host "Wrote $cfg"
}
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item (Join-Path $src 'package.json'), (Join-Path $src 'main.js'), (Join-Path $src 'preload.js') $dst -Force
# tray/toast icon: Gideon's portrait from the repo's avatars folder
$icon = Join-Path $dst 'icon.png'
$local = Join-Path $src '..\avatars\boss-demo.png'
if (Test-Path $local) { Copy-Item $local $icon -Force }
Push-Location $dst
try { npm install --no-audit --no-fund --loglevel=error; if ($LASTEXITCODE) { throw "npm install failed" } } finally { Pop-Location }
$exe = Join-Path $dst 'node_modules\electron\dist\electron.exe'
if (-not (Test-Path $exe)) {
    # electron's own extractor silently fails on some Node versions: unpack the downloaded zip ourselves
    $zip = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'electron\Cache') -Recurse -Filter 'electron-v*-win32-x64.zip' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($zip) {
        $dist = Split-Path $exe
        Remove-Item -Recurse -Force $dist -ErrorAction SilentlyContinue
        Expand-Archive $zip.FullName -DestinationPath $dist -Force
        Set-Content -NoNewline -Path (Join-Path $dst 'node_modules\electron\path.txt') -Value 'electron.exe'
    }
}
if (-not (Test-Path $exe)) { throw "electron.exe missing: $exe" }
$lnk = Join-Path ([Environment]::GetFolderPath('Programs')) 'Roundtable Companion.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = $exe
$s.Arguments = "`"$dst`""
$s.WorkingDirectory = $dst
$s.Description = 'Hermes Roundtable desktop companion'
$s.Save()
Write-Host "Installed to $dst"
Write-Host "Start Menu: Roundtable Companion  (tray icon -> 'Start with Windows' to launch at sign-in)"
if (-not (Test-Path $cfg)) { Write-Warning "No $cfg yet: without the LAN url + COMPANION_TOKEN the app falls back to Authentik sign-in, which Google blocks in desktop apps." }
