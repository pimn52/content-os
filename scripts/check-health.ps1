[CmdletBinding()]
param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$Uri = "http://127.0.0.1:$Port/health"
$response = Invoke-RestMethod -Method Get -Uri "$Uri"

if ($response.status -ne "ok") {
    throw "Content OS API health check failed: status was '$($response.status)'"
}

$response | ConvertTo-Json -Compress
