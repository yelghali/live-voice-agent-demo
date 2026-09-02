[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$requirements = Join-Path $repoRoot 'requirements.txt'
$envExample = Join-Path $repoRoot '.env.example'
$envFile = Join-Path $repoRoot '.env'

Push-Location $repoRoot
try {
    if (-not (Test-Path $venvPython)) {
        # The demo targets 3.12+; `py -3` alone can select an older interpreter.
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if (-not $launcher) { throw 'Python launcher (py) not found. Install Python 3.12 or later.' }

        $version = (& py -3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
        if ($LASTEXITCODE -ne 0) { throw 'Could not determine the Python version.' }
        if ([version]$version -lt [version]'3.12') {
            throw "Python $version found, but 3.12 or later is required. Install it, then rerun."
        }

        Write-Host "Creating .venv with Python $version..."
        py -3 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed.' }
    }

    if (-not $SkipInstall) {
        Write-Host 'Installing Python dependencies...'
        & $venvPython -m pip install -r $requirements
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    }

    if (-not (Test-Path $envFile)) {
        Copy-Item $envExample $envFile
        Write-Host 'Created .env from .env.example.'
    } else {
        Write-Host '.env already exists; leaving it unchanged.'
    }

    Write-Host ''
    Write-Host 'Bootstrap complete.'
    Write-Host 'Next: edit .env, run az login, then follow "Run the demos" in README.md.'
}
finally {
    Pop-Location
}