# Build everything connect-doom needs on Windows (x64 or Arm).
#   setup.cmd            install what's missing and build the game (about ten minutes)
#   setup.cmd -Game      only rebuild the game from a changed patch (no downloads)
# setup.cmd runs this with the execution policy bypassed: Windows blocks .ps1 scripts by default.
# The compiler and SDL libraries come from MSYS2, which this installs with winget if needed.
param([switch]$Game)
# Not 'Stop': Windows PowerShell 5.1 turns a native program's stderr into an error. Each step
# checks $LASTEXITCODE itself.
$ErrorActionPreference = 'Continue'
Push-Location $PSScriptRoot

function Stop-Setup($msg) { Write-Host $msg -ForegroundColor Red; Pop-Location; exit 1 }

# autotools and libtool break on a source path with a space, and user folders often have one.
if ($PSScriptRoot -match ' ') {
    Stop-Setup "The game can't build in a folder whose path has a space ($PSScriptRoot). Move the repo to one without, like C:\connect-ai-doom, and run setup.cmd there."
}

# Python 3.9 or newer, for the bridge, the panel server and the level builder.
$python = $null
foreach ($c in 'python', 'py') {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { $python = $cmd.Source; $pyName = $c; break }
}
if (-not $python) { Stop-Setup "Python 3.9 or newer is missing: install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run setup.cmd again." }
& $python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
if ($LASTEXITCODE) { Stop-Setup "Python 3.9 or newer is needed; found $(& $python --version)." }

# MSYS2: x64 Windows gets the MinGW64 environment, Arm Windows the native Clang one.
if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { $msystem = 'CLANGARM64' } else { $msystem = 'MINGW64' }
$msys = @('C:\msys64', "$env:USERPROFILE\msys64") | Where-Object { Test-Path "$_\usr\bin\bash.exe" } | Select-Object -First 1
if (-not $msys) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Stop-Setup "MSYS2 is missing and winget isn't available: install MSYS2 from https://www.msys2.org, then run setup.cmd again."
    }
    Write-Host 'Installing MSYS2 (the compiler and SDL libraries)...'
    winget install --id MSYS2.MSYS2 -e --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE) { Stop-Setup 'Installing MSYS2 failed.' }
    $msys = 'C:\msys64'
    if (-not (Test-Path "$msys\usr\bin\bash.exe")) { Stop-Setup "MSYS2 isn't at C:\msys64 after installing: run setup.cmd again." }
}
$bash = "$msys\usr\bin\bash.exe"

# MSYSTEM picks the environment; CHERE_INVOKING keeps this folder as the working directory.
# Both are put back afterwards, so later MSYS2 or Git Bash shells from this window aren't affected.
$saved = @{ MSYSTEM = $env:MSYSTEM; CHERE_INVOKING = $env:CHERE_INVOKING }
function Invoke-Bash {
    $env:MSYSTEM = $msystem; $env:CHERE_INVOKING = '1'
    try { & $bash @args | Out-Host }
    finally { $env:MSYSTEM = $saved.MSYSTEM; $env:CHERE_INVOKING = $saved.CHERE_INVOKING }
}

if (-not $Game) {
    # Bring MSYS2 up to date: an old package database points at versions the mirrors have
    # dropped. The first pass may update only pacman and MSYS2's core, so run it twice.
    Write-Host 'Updating MSYS2...'
    foreach ($pass in 1, 2) {
        Invoke-Bash -lc 'pacman -Syu --noconfirm'
        if ($LASTEXITCODE) { Stop-Setup 'Updating MSYS2 failed: open "MSYS2 MSYS" from the Start menu, run  pacman -Syu  and then setup.cmd again.' }
    }
}

Write-Host "Building in MSYS2 ($msystem)..."
Invoke-Bash -l tools/setup-win.sh $(if ($Game) { '--game' })
if ($LASTEXITCODE) { Stop-Setup 'The build failed: see vendor\build.log.' }
if ($Game) { Pop-Location; exit 0 }

# Pillow draws the panel's sprites and the level's signs.
& $python -c 'import PIL' 2>$null
if ($LASTEXITCODE) {
    & $python -m pip install --user pillow
    if ($LASTEXITCODE) { Stop-Setup 'Installing Pillow failed: run  pip install pillow  and then setup.cmd again.' }
}
& $python tools/make_sprites.py wads/freedoom2.wad panel/sprites assets/brand
if ($LASTEXITCODE) { Stop-Setup 'Making the panel sprites failed.' }
Pop-Location
Write-Host "Done. Next: $pyName raid.py offline  (sample data, no account needed)"
