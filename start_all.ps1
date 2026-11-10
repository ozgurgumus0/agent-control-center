param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"

$controlRoot = $PSScriptRoot
$workspaceRoot = Split-Path $controlRoot -Parent
# The planner (allocation tool) lives in a sibling folder unless PLANNER_ROOT says otherwise.
$plannerRoot = if ($env:PLANNER_ROOT) { $env:PLANNER_ROOT } else { Join-Path $workspaceRoot "planner" }
$runtimeRoot = Join-Path $controlRoot "runtime"
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

function Test-LocalPort([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $attempt = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $attempt.AsyncWaitHandle.WaitOne(250)) { return $false }
        $client.EndConnect($attempt)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

# Which process is actually serving a port, whoever started it.
#
# The old record only held what THIS run spawned, so a second run -- when the
# services were already up -- wrote an empty object and threw away the PIDs of
# everything still running, leaving no way to stop them. Reading the listener
# back makes the record describe what is running now rather than what this
# particular invocation happened to do.
function Get-ListenerPid([int]$Port) {
    try {
        $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($connection) { return [int]$connection.OwningProcess }
    } catch {
        # Get-NetTCPConnection is absent on older hosts; the port check above
        # still tells us the service is up, we just cannot name its process.
    }
    return $null
}

function Start-HiddenProcess($Name, $FilePath, $Arguments, $WorkingDirectory) {
    $stdout = Join-Path $runtimeRoot "$Name.log"
    $stderr = Join-Path $runtimeRoot "$Name.err.log"
    $process = Start-Process -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden -PassThru
    return $process.Id
}

$plannerPython = Join-Path $plannerRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $plannerPython)) { $plannerPython = "python" }

$services = @(
    @{
        name      = "planner-backend"
        port      = 8000
        file      = $plannerPython
        arguments = @("-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8000")
        directory = $plannerRoot
    },
    @{
        name      = "planner-frontend"
        port      = 5173
        file      = "cmd.exe"
        arguments = @("/c", "npm run dev -- --host 127.0.0.1")
        directory = (Join-Path $plannerRoot "frontend")
    },
    @{
        name      = "control-center"
        port      = 8765
        file      = "python"
        arguments = @("server.py")
        directory = $controlRoot
    }
)

# Start what is missing, wait, then try once more for anything that did not come
# up. The frontend has been seen to die on a transient `spawn EPERM` while vite
# probes for network drives -- an environment hiccup rather than a code fault,
# and it succeeds on a second attempt. One retry costs a few seconds and turns
# that into a non-event; retrying forever would only hide a real breakage.
foreach ($attempt in 1..2) {
    $missing = @($services | Where-Object { -not (Test-LocalPort $_.port) })
    if ($missing.Count -eq 0) { break }

    if ($attempt -eq 2) {
        Write-Warning ("retrying: " + ($missing.name -join ", "))
    }

    foreach ($service in $missing) {
        Start-HiddenProcess $service.name $service.file $service.arguments $service.directory | Out-Null
    }

    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if (-not ($services | Where-Object { -not (Test-LocalPort $_.port) })) { break }
        Start-Sleep -Milliseconds 500
    }
}

$record = [ordered]@{}
foreach ($service in $services) {
    $listening = Test-LocalPort $service.port
    $record[$service.name] = [ordered]@{
        port      = $service.port
        listening = $listening
        pid       = if ($listening) { Get-ListenerPid $service.port } else { $null }
        log       = "runtime/$($service.name).log"
        error_log = "runtime/$($service.name).err.log"
    }
}

# UTF8 without BOM: the record is read back by stop_all.ps1 and by anything else
# that parses JSON, and a BOM breaks several of those.
$json = [ordered]@{
    written_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    services       = $record
} | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText((Join-Path $runtimeRoot "last-started-processes.json"), $json)

$down = @($record.Keys | Where-Object { -not $record[$_].listening })
if ($down.Count -gt 0) {
    Write-Warning ("not listening: " + ($down -join ", ") + " -- see runtime\<name>.err.log")
}

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:8765"
}
