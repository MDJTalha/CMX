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
$SrcPath = Join-Path $RepoRoot "src"

& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip." }

& $Python -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "Could not install CMX and development dependencies." }

Write-Host ""
Write-Host "Starting CMX locally at http://127.0.0.1:8000"
Write-Host "API documentation: http://127.0.0.1:8000/docs"
Write-Host "Press Ctrl+C to stop the server."
& $Python -m uvicorn cmx.main:app --app-dir $SrcPath --host 127.0.0.1 --port 8000
