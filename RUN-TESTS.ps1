$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Virtual environment not found. Run .\SETUP-WINDOWS.ps1 first."
}

& $python -m pytest -q
exit $LASTEXITCODE

