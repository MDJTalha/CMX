$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    throw "Python 3.11 or later is required. Install Python and ensure it is available on PATH."
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

& $Python -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "Could not install CMX test dependencies." }

& $Python -m pytest
if ($LASTEXITCODE -ne 0) { throw "CMX tests failed." }

Write-Host "CMX tests passed."
