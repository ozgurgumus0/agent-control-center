<#
    Stops the services start_all.ps1 recorded.

    It kills only what is named in runtime\last-started-processes.json, and only
    after confirming the PID is STILL the process listening on that service's
    port. Windows reuses PIDs, and a stale record plus a blind Stop-Process is
    how a script like this ends up killing something unrelated.

    Process TREES, not single processes: the frontend is cmd.exe -> npm -> node,
    and the listener is the node at the bottom. Killing it alone leaves the
    shells orphaned and holding the log handles.
#>
param(
    [string]$Service,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$runtimeRoot = Join-Path $PSScriptRoot "runtime"
$recordPath = Join-Path $runtimeRoot "last-started-processes.json"

if (-not (Test-Path $recordPath)) {
    Write-Warning "no runtime\last-started-processes.json -- nothing recorded to stop."
    return
}

$record = Get-Content -Raw $recordPath | ConvertFrom-Json
$services = $record.services
if (-not $services) {
    Write-Warning "record has no services -- nothing to stop."
    return
}

function Get-ListenerPid([int]$Port) {
    try {
        $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($connection) { return [int]$connection.OwningProcess }
    } catch { }
    return $null
}

$names = $services.PSObject.Properties.Name
if ($Service) {
    if ($names -notcontains $Service) {
        Write-Warning "unknown service '$Service'. Known: $($names -join ', ')"
        return
    }
    $names = @($Service)
}

foreach ($name in $names) {
    $entry = $services.$name
    $recordedPid = $entry.pid
    $port = [int]$entry.port

    if (-not $recordedPid) {
        if (-not $Quiet) { Write-Output "$name : no pid recorded, skipped" }
        continue
    }

    $currentPid = Get-ListenerPid $port
    if ($null -eq $currentPid) {
        if (-not $Quiet) { Write-Output "$name : port $port already free, skipped" }
        continue
    }
    if ($currentPid -ne [int]$recordedPid) {
        Write-Warning "$name : port $port is held by PID $currentPid, not the recorded $recordedPid -- left alone."
        continue
    }

    # /T takes the children with it, /F because uvicorn and vite both ignore a
    # polite close when they have no console attached.
    taskkill /PID $recordedPid /T /F | Out-Null
    if ($LASTEXITCODE -eq 0) {
        if (-not $Quiet) { Write-Output "$name : stopped (PID $recordedPid, port $port)" }
    } else {
        Write-Warning "$name : taskkill failed for PID $recordedPid (exit $LASTEXITCODE)"
    }
}
