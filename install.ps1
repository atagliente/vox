<#
.SYNOPSIS
    VOX preflight installer for Windows.

.DESCRIPTION
    Verifies Python 3.11+, installs VOX (pipx when available, otherwise a
    dedicated virtual environment), puts a `vox` launcher on the user PATH and
    runs the system check. Never needs administrator rights: only the *user*
    PATH is touched.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Yes
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$NoPath,
    [switch]$Uninstall,
    [string]$Prefix
)

$ErrorActionPreference = 'Stop'

$RepoUrl  = 'https://github.com/atagliente/vox'
$VoxHome  = if ($env:VOX_HOME) { $env:VOX_HOME } else { Join-Path $env:USERPROFILE '.vox' }
$VenvDir  = Join-Path $VoxHome 'venv'
$BinDir   = if ($Prefix) { $Prefix } else { Join-Path $env:LOCALAPPDATA 'Programs\vox\bin' }

function Write-Ok    ($m) { Write-Host "[ OK ] $m"  -ForegroundColor Green }
function Write-Warn  ($m) { Write-Host "[WARN] $m"  -ForegroundColor Yellow }
function Write-Fail  ($m) { Write-Host "[FAIL] $m"  -ForegroundColor Red }
function Stop-Install($m) { Write-Fail $m; exit 1 }

function Write-Banner {
    Write-Host ''
    Write-Host '+----------------------------------------------------------+'
    Write-Host '|  V O X   -  W.O.P.R. TERMINAL  -  PREFLIGHT INSTALLER     |'
    Write-Host '+----------------------------------------------------------+'
    Write-Host ''
}

function Confirm-Step($question) {
    if ($Yes) { return $true }
    $answer = Read-Host "$question [y/N]"
    return $answer -match '^(y|yes)$'
}

function Find-Python {
    # Prefer the launcher, then any python on PATH; require 3.11 or newer.
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $candidates += ,@('py', @('-3.13'))
        $candidates += ,@('py', @('-3.12'))
        $candidates += ,@('py', @('-3.11'))
        $candidates += ,@('py', @('-3'))
    }
    foreach ($name in @('python3.12', 'python3.11', 'python3', 'python')) {
        if (Get-Command $name -ErrorAction SilentlyContinue) { $candidates += ,@($name, @()) }
    }
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]; $prefixArgs = $candidate[1]
        $probe = @($prefixArgs) + @('-c', 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)')
        try { & $exe @probe 2>$null } catch { continue }
        if ($LASTEXITCODE -eq 0) { return [pscustomobject]@{ Exe = $exe; Args = $prefixArgs } }
    }
    return $null
}

function Invoke-Uninstall {
    $removed = $false
    if (Get-Command pipx -ErrorAction SilentlyContinue) {
        pipx uninstall vox *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok 'pipx package removed'; $removed = $true }
    }
    if (Test-Path $VenvDir) {
        Remove-Item -Recurse -Force $VenvDir
        Write-Ok "venv removed: $VenvDir"; $removed = $true
    }
    $launcher = Join-Path $BinDir 'vox.cmd'
    if (Test-Path $launcher) {
        Remove-Item -Force $launcher
        Write-Ok "launcher removed: $launcher"; $removed = $true
    }
    if (-not $removed) { Write-Warn 'nothing to remove' }
    Write-Host ''
    Write-Host "Your settings and sessions are untouched in $VoxHome" -ForegroundColor DarkGray
    Write-Host "Delete them with: Remove-Item -Recurse -Force '$VoxHome'" -ForegroundColor DarkGray
    exit 0
}

Write-Banner
Write-Host "PLATFORM: Windows $([Environment]::OSVersion.Version)"

if ($Uninstall) { Invoke-Uninstall }

$python = Find-Python
if (-not $python) {
    Write-Fail 'Python 3.11 or newer not found.'
    Write-Host '  Install it from https://python.org/downloads or with: winget install Python.Python.3.12'
    exit 1
}
$pyVersion = & $python.Exe @($python.Args + @('-c', 'import platform; print(platform.python_version())'))
Write-Ok "python: $($python.Exe) $($python.Args -join ' ') ($pyVersion)"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($scriptDir -and (Test-Path (Join-Path $scriptDir 'pyproject.toml'))) {
    $target = $scriptDir
    Write-Ok "source: local checkout $scriptDir"
} else {
    $target = "git+$RepoUrl"
    Write-Ok "source: $RepoUrl"
}

function Install-WithPipx {
    if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) { return $false }
    Write-Host 'installing with pipx...' -ForegroundColor DarkGray
    pipx install --force $target *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    Write-Ok 'installed with pipx'
    return $true
}

function Install-WithVenv {
    Write-Host "installing into $VenvDir ..." -ForegroundColor DarkGray
    if (-not (Test-Path $VenvDir)) {
        & $python.Exe @($python.Args + @('-m', 'venv', $VenvDir))
        if ($LASTEXITCODE -ne 0) { Stop-Install 'cannot create the virtual environment' }
    }
    $venvPython = Join-Path $VenvDir 'Scripts\python.exe'
    & $venvPython -m pip install --upgrade pip *> $null
    & $venvPython -m pip install --upgrade $target
    if ($LASTEXITCODE -ne 0) { Stop-Install 'pip install failed' }

    if (-not (Test-Path $BinDir)) { New-Item -ItemType Directory -Force -Path $BinDir | Out-Null }
    $launcher = Join-Path $BinDir 'vox.cmd'
    $content = "@echo off`r`nrem VOX launcher - generated by install.ps1`r`n`"$venvPython`" -m vox_chat %*`r`n"
    Set-Content -Path $launcher -Value $content -Encoding ascii
    Write-Ok "installed in $VenvDir"
    Write-Ok "launcher: $launcher"
    return $true
}

$usedPipx = Install-WithPipx
if (-not $usedPipx) {
    Write-Warn 'pipx not usable, falling back to a dedicated virtual environment'
    Install-WithVenv | Out-Null
}

# --------------------------------------------------------------------- PATH

# pipx installs its own launcher elsewhere, so ask it where rather than
# assuming; either way `vox` has to be reachable from any directory.
if ($usedPipx) {
    $pipxBin = (pipx environment --value PIPX_BIN_DIR 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $pipxBin) {
        $pipxBin = Join-Path $env:USERPROFILE '.local\bin'
    }
    $BinDir = $pipxBin.Trim()
}

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$entries = @()
if ($userPath) { $entries = $userPath.Split(';') | Where-Object { $_ } }
if ($entries -contains $BinDir) {
    Write-Ok "PATH already contains $BinDir"
} elseif ($NoPath) {
    Write-Warn "$BinDir is not in PATH (-NoPath given, add it yourself)"
} else {
    Write-Host ''
    Write-Host "$BinDir is not in your user PATH."
    if (Confirm-Step 'Add it now, so vox works from any directory?') {
        $newPath = if ($userPath) { "$userPath;$BinDir" } else { $BinDir }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        $env:Path = "$env:Path;$BinDir"
        Write-Ok 'user PATH updated - open a new terminal and just type: vox'
    } else {
        Write-Warn "add it manually: $BinDir"
    }
}

# -------------------------------------------------------------- final check

Write-Host ''
$launcher = Join-Path $BinDir 'vox.cmd'
if (Test-Path $launcher) {
    $voxCmd = $launcher
} elseif (Get-Command vox -ErrorAction SilentlyContinue) {
    $voxCmd = 'vox'
} else {
    Stop-Install 'installation finished but the vox command was not found'
}

& $voxCmd doctor --plain --timeout 5
$status = $LASTEXITCODE

Write-Host ''
switch ($status) {
    0 { Write-Ok 'VOX IS READY - run: vox' }
    1 {
        Write-Warn 'VOX is installed; the provider is unreachable.'
        Write-Host "config file: $VoxHome\config.json" -ForegroundColor DarkGray
    }
    default { Stop-Install 'system check failed - see the report above' }
}
exit 0
