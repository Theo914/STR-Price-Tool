$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$source = Join-Path $PSScriptRoot ".parity\app"
if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw "No local app evidence exists. Run a search first, then stop the app."
}

$destination = Join-Path ([Environment]::GetFolderPath("Desktop")) "parity-discovery-diagnostic.zip"
Compress-Archive -LiteralPath $source -DestinationPath $destination -Force
Write-Host "Diagnostic archive created: $destination"

