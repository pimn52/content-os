[CmdletBinding()]
param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

Set-Location -LiteralPath $RepoRoot
& "$Python" -m uvicorn app.main:app --app-dir (Join-Path $RepoRoot "services\api") --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
