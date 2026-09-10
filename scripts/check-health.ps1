[CmdletBinding()]
param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$Uri = "http://127.0.0.1:$Port/health"
$headers = @{}
if (-not [string]::IsNullOrWhiteSpace($env:CONTENT_OS_ACCESS_TOKEN)) {
    $headers["Authorization"] = "Bearer " + $env:CONTENT_OS_ACCESS_TOKEN
}
$response = Invoke-RestMethod -Method Get -Uri "$Uri" -Headers $headers

if ($response.status -ne "ok") {
    throw "Content OS API health check failed: status was '$($response.status)'"
}

$response | ConvertTo-Json -Compress
