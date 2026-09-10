[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$NoWorker,
    [string]$HostAddress = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

$Worker = Join-Path $RepoRoot ".venv\Scripts\content-os-worker.exe"
$ApiProcess = $null
$WorkerProcess = $null
$RendererDir = Join-Path $RepoRoot "apps\renderer"
$RendererCompositorDir = Join-Path $RendererDir "node_modules\@remotion\compositor-win32-x64-msvc"
$WebDist = Join-Path $RepoRoot "apps\web\dist"

function Resolve-ExecutablePath {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$OverrideVariable
    )

    if ($OverrideVariable) {
        $override = [Environment]::GetEnvironmentVariable($OverrideVariable)
        if (-not [string]::IsNullOrWhiteSpace($override)) {
            return $override
        }
    }
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    return $null
}

function Get-VersionLine {
    param(
        [string]$CommandPath,
        [string[]]$Arguments
    )

    if ([string]::IsNullOrWhiteSpace($CommandPath)) {
        return "missing"
    }
    try {
        $line = & $CommandPath @Arguments 2>&1 | Select-Object -First 1
        if ($line) {
            return ([string]$line).Trim()
        }
        return "version unavailable"
    } catch {
        return "unavailable"
    }
}

function Test-ExecutableAvailable {
    param([string]$CommandPath)

    if ([string]::IsNullOrWhiteSpace($CommandPath)) {
        return $false
    }
    if (Test-Path -LiteralPath $CommandPath -PathType Leaf) {
        return $true
    }
    return $null -ne (Get-Command $CommandPath -ErrorAction SilentlyContinue)
}

function Test-RendererInstall {
    return (Test-Path -LiteralPath (Join-Path $RendererDir "package.json")) -and
        (Test-Path -LiteralPath (Join-Path $RendererDir "package-lock.json")) -and
        (Test-Path -LiteralPath (Join-Path $RendererDir "node_modules\@remotion\cli\package.json"))
}

function Test-WebInstall {
    return (Test-Path -LiteralPath (Join-Path $WebDist "index.html")) -and
        (Test-Path -LiteralPath (Join-Path $WebDist "assets"))
}

function Test-AsrConfigured {
    $provider = $env:CONTENT_OS_ASR_PROVIDER
    if ($provider -and $provider.Trim().ToLowerInvariant() -eq "local") {
        return $true
    }
    if (-not $provider -or $provider.Trim().ToLowerInvariant() -eq "openai-compatible") {
        return (-not [string]::IsNullOrWhiteSpace($env:CONTENT_OS_ASR_API_KEY)) -or
            (-not [string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY))
    }
    return $false
}

function Show-RuntimePreflight {
    param([switch]$WorkerRequired)

    $node = Resolve-ExecutablePath -Name "node"
    $npm = Resolve-ExecutablePath -Name "npm" -OverrideVariable "CONTENT_OS_NPM"
    $ffmpeg = Resolve-ExecutablePath -Name "ffmpeg" -OverrideVariable "CONTENT_OS_FFMPEG"
    $ffprobe = Resolve-ExecutablePath -Name "ffprobe" -OverrideVariable "CONTENT_OS_FFPROBE"
    if (-not $ffmpeg) {
        $bundled = Join-Path $RendererCompositorDir "ffmpeg.exe"
        if (Test-Path -LiteralPath $bundled) { $ffmpeg = $bundled }
    }
    if (-not $ffprobe) {
        $bundled = Join-Path $RendererCompositorDir "ffprobe.exe"
        if (Test-Path -LiteralPath $bundled) { $ffprobe = $bundled }
    }

    $checks = @(
        [pscustomobject]@{ Name = "Python"; Path = $Python; Version = Get-VersionLine $Python @("--version"); Required = $true; Available = Test-Path -LiteralPath $Python },
        [pscustomobject]@{ Name = "Node"; Path = $node; Version = Get-VersionLine $node @("--version"); Required = $WorkerRequired; Available = Test-ExecutableAvailable $node },
        [pscustomobject]@{ Name = "npm"; Path = $npm; Version = Get-VersionLine $npm @("--version"); Required = $WorkerRequired; Available = Test-ExecutableAvailable $npm },
        [pscustomobject]@{ Name = "FFmpeg"; Path = $ffmpeg; Version = Get-VersionLine $ffmpeg @("-version"); Required = $WorkerRequired; Available = Test-ExecutableAvailable $ffmpeg },
        [pscustomobject]@{ Name = "ffprobe"; Path = $ffprobe; Version = Get-VersionLine $ffprobe @("-version"); Required = $WorkerRequired; Available = Test-ExecutableAvailable $ffprobe },
        [pscustomobject]@{ Name = "Remotion renderer"; Path = $RendererDir; Version = "4.0.522 package lock"; Required = $WorkerRequired; Available = Test-RendererInstall },
        [pscustomobject]@{ Name = "Formal Web app"; Path = $WebDist; Version = "apps/web/dist React/Vite build"; Required = $WorkerRequired; Available = Test-WebInstall }
    )

    Write-Host "Content OS local runtime preflight:"
    foreach ($check in $checks) {
        $status = if ($check.Available) { "OK" } else { "MISSING" }
        $color = if ($check.Available) { "Green" } else { "Yellow" }
        Write-Host ("  [{0}] {1}: {2}" -f $status, $check.Name, $check.Version) -ForegroundColor $color
    }

    $missing = @($checks | Where-Object { $_.Required -and -not $_.Available } | ForEach-Object { $_.Name })
    if ($missing.Count -gt 0) {
        throw ("Runtime preflight failed: {0}. Recovery: run 'npm --prefix apps/web run build' for the Formal Web app, install other missing local prerequisites, then rerun; use -NoWorker for API-only diagnostics." -f ($missing -join ", "))
    }
}

if (@("127.0.0.1", "localhost", "::1") -notcontains $HostAddress -and [string]::IsNullOrWhiteSpace($env:CONTENT_OS_ACCESS_TOKEN)) {
    throw "LAN/private-network binding requires CONTENT_OS_ACCESS_TOKEN; keep HostAddress at 127.0.0.1 for local-only use"
}

try {
    Set-Location -LiteralPath $RepoRoot
    Show-RuntimePreflight -WorkerRequired:(-not $NoWorker)
    $ApiDirArgument = '"' + (Join-Path $RepoRoot "services\api") + '"'
    $ApiProcess = Start-Process -FilePath $Python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--app-dir", $ApiDirArgument, "--host", $HostAddress, "--port", "$Port") `
        -WorkingDirectory $RepoRoot -PassThru -NoNewWindow

    $healthUri = "http://127.0.0.1:$Port/health"
    $healthHeaders = @{}
    if (-not [string]::IsNullOrWhiteSpace($env:CONTENT_OS_ACCESS_TOKEN)) {
        $healthHeaders["Authorization"] = "Bearer " + $env:CONTENT_OS_ACCESS_TOKEN
    }
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if ($ApiProcess.HasExited) {
            throw "Content OS API exited before becoming healthy (code $($ApiProcess.ExitCode))"
        }
        try {
            $health = Invoke-RestMethod -Method Get -Uri $healthUri -Headers $healthHeaders -TimeoutSec 2
            if ($health.status -eq "ok") {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $ready) {
        throw "Content OS API did not become healthy at $healthUri"
    }

    $asrConfigured = $false
    if (-not $NoWorker) {
        if (-not (Test-Path -LiteralPath $Worker)) {
            throw "content-os-worker is not installed; run the documented editable install first"
        }
        $workerArguments = @("--job-type", "analyze_asset", "--job-type", "render")
        $asrConfigured = Test-AsrConfigured
        if ($asrConfigured) {
            $workerArguments += @("--job-type", "transcribe_audio")
        }
        $WorkerProcess = Start-Process -FilePath $Worker `
            -ArgumentList $workerArguments `
            -WorkingDirectory $RepoRoot -PassThru -NoNewWindow
    }

    $workerText = if ($NoWorker) { "worker disabled" } else { "API + local analyze/render worker" }
    if (-not $NoWorker -and $asrConfigured) {
        $workerText += " + configured ASR"
    }
    Write-Host "Content OS running at http://$HostAddress`:$Port ($workerText). Press Ctrl+C to stop."
    while (-not $ApiProcess.HasExited) {
        if ($WorkerProcess -and $WorkerProcess.HasExited) {
            throw "Content OS worker exited (code $($WorkerProcess.ExitCode))"
        }
        Start-Sleep -Seconds 1
    }
    exit $ApiProcess.ExitCode
} finally {
    if ($WorkerProcess -and -not $WorkerProcess.HasExited) {
        Stop-Process -Id $WorkerProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($ApiProcess -and -not $ApiProcess.HasExited) {
        Stop-Process -Id $ApiProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
