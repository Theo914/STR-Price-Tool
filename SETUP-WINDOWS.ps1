$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 -m venv .venv
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python -m venv .venv
} else {
    throw "Python 3.11 or newer is required and was not found on PATH."
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.11 or newer is required." }

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

& $python -m pip install -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "The regression suite failed." }

Write-Host ""
Write-Host "Setup complete. Run .\RUN-APP.ps1 to launch Parity Desk."
Write-Host "Expected regression result: 376 passed."

