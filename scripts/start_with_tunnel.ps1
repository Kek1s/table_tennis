param(
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$cloudflaredPath = Join-Path $projectRoot ".tools\cloudflared.exe"
$envPath = Join-Path $projectRoot ".env"
$logsPath = Join-Path $projectRoot ".logs"
$originUrl = "http://127.0.0.1:$Port"
$tunnelAttempts = 3

function Set-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.AddRange([string[]][System.IO.File]::ReadAllLines($envPath))
    $prefix = "$Name="
    $updated = $false

    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index].StartsWith($prefix, [System.StringComparison]::Ordinal)) {
            $lines[$index] = "$prefix$Value"
            $updated = $true
            break
        }
    }

    if (-not $updated) {
        $lines.Add("$prefix$Value")
    }

    $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllLines($envPath, $lines, $utf8WithoutBom)
}

function Stop-ProcessTree {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    $children = Get-CimInstance Win32_Process `
        -Filter "ParentProcessId=$ProcessId" `
        -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project Python was not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath $cloudflaredPath)) {
    throw "cloudflared was not found: $cloudflaredPath"
}
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Settings file was not found: $envPath"
}
if ($Port -lt 1 -or $Port -gt 65535) {
    throw "Invalid port: $Port"
}

$existingListener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($existingListener) {
    throw "Port $Port is already in use. Stop the previous run with Ctrl+C."
}

New-Item -ItemType Directory -Path $logsPath -Force | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tunnelOut = $null
$tunnelErr = $null

# Some Windows environments contain both Path and PATH. Start-Process cannot
# pass that environment to a child process.
$pathKeys = @(
    [System.Environment]::GetEnvironmentVariables().Keys |
        Where-Object { $_ -ieq "Path" }
)
if ($pathKeys.Count -gt 1) {
    [System.Environment]::SetEnvironmentVariable(
        "PATH",
        $null,
        [System.EnvironmentVariableTarget]::Process
    )
}

$tunnelProcess = $null
$botProcess = $null

try {
    Write-Host "Creating a free HTTPS tunnel..."
    $publicUrl = $null
    $lastTunnelError = ""

    for ($attempt = 1; $attempt -le $tunnelAttempts; $attempt++) {
        # Windows occasionally returns only an unusable DNS record on the
        # first lookup. An explicit A lookup warms its resolver cache.
        Resolve-DnsName `
            -Name "api.trycloudflare.com" `
            -Type A `
            -DnsOnly `
            -ErrorAction SilentlyContinue | Out-Null

        $tunnelOut = Join-Path $logsPath "cloudflared-$timestamp-$attempt.stdout.log"
        $tunnelErr = Join-Path $logsPath "cloudflared-$timestamp-$attempt.stderr.log"
        $tunnelProcess = Start-Process `
            -FilePath $cloudflaredPath `
            -ArgumentList @(
                "tunnel",
                "--no-autoupdate",
                "--edge-ip-version",
                "4",
                "--protocol",
                "http2",
                "--retries",
                "3",
                "--url",
                $originUrl
            ) `
            -WorkingDirectory $projectRoot `
            -RedirectStandardOutput $tunnelOut `
            -RedirectStandardError $tunnelErr `
            -WindowStyle Hidden `
            -PassThru

        $deadline = [System.DateTime]::UtcNow.AddSeconds(20)
        while ([System.DateTime]::UtcNow -lt $deadline) {
            $tunnelProcess.Refresh()
            if ($tunnelProcess.HasExited) {
                break
            }

            $stdout = Get-Content -LiteralPath $tunnelOut -Raw -ErrorAction SilentlyContinue
            $stderr = Get-Content -LiteralPath $tunnelErr -Raw -ErrorAction SilentlyContinue
            $match = [regex]::Match(
                "$stdout`n$stderr",
                "https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com"
            )
            if ($match.Success) {
                $publicUrl = $match.Value
                break
            }
            Start-Sleep -Milliseconds 250
        }

        if ($publicUrl) {
            break
        }

        $lastTunnelError = Get-Content `
            -LiteralPath $tunnelErr `
            -Raw `
            -ErrorAction SilentlyContinue
        $tunnelProcess.Refresh()
        if (-not $tunnelProcess.HasExited) {
            Stop-ProcessTree -ProcessId $tunnelProcess.Id
        }
        $tunnelProcess = $null

        if ($attempt -lt $tunnelAttempts) {
            Write-Warning (
                "Tunnel attempt $attempt of $tunnelAttempts failed. " +
                "Retrying in 2 seconds..."
            )
            Start-Sleep -Seconds 2
        }
    }

    if (-not $publicUrl) {
        throw (
            "Cloudflare did not provide a public URL after " +
            "$tunnelAttempts attempts.`n$lastTunnelError"
        )
    }

    Set-DotEnvValue -Name "MINIAPP_URL" -Value $publicUrl
    Set-DotEnvValue -Name "WEB_HOST" -Value "127.0.0.1"
    Set-DotEnvValue -Name "WEB_PORT" -Value ([string]$Port)
    Set-DotEnvValue -Name "WEBAPP_DEV_MODE" -Value "0"

    Write-Host ""
    Write-Host "Mini App: $publicUrl" -ForegroundColor Green
    Write-Host "Database: $(Join-Path $projectRoot 'data\table_tennis.sqlite3')"
    Write-Host "Keep this window open. Press Ctrl+C to stop."
    Write-Host ""

    $botProcess = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @("-m", "table_tennis_bot.main") `
        -WorkingDirectory $projectRoot `
        -NoNewWindow `
        -PassThru

    while ($true) {
        $botProcess.Refresh()
        $tunnelProcess.Refresh()

        if ($botProcess.HasExited) {
            throw "The bot stopped with exit code $($botProcess.ExitCode)."
        }
        if ($tunnelProcess.HasExited) {
            $details = Get-Content -LiteralPath $tunnelErr -Raw -ErrorAction SilentlyContinue
            throw "The HTTPS tunnel stopped.`n$details"
        }
        Start-Sleep -Seconds 1
    }
}
finally {
    foreach ($process in @($botProcess, $tunnelProcess)) {
        if ($null -ne $process) {
            $process.Refresh()
            if (-not $process.HasExited) {
                Stop-ProcessTree -ProcessId $process.Id
            }
        }
    }
}
