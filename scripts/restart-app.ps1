# Restart the Wiki Video Pipeline dashboard.
# Usage:
#   .\scripts\restart-app.ps1              # stop anything on the port, start in this window
#   .\scripts\restart-app.ps1 -Background  # stop + start detached
#   .\scripts\restart-app.ps1 -RegisterStartup
#   .\scripts\restart-app.ps1 -UnregisterStartup
# Requires PowerShell 5.1+

[CmdletBinding()]
param(
    [switch]$Background,
    [switch]$RegisterStartup,
    [switch]$UnregisterStartup,
    [switch]$StopOnly
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$TaskName = "WikiVideoPipeline"
$StartupCmdName = "WikiVideoPipeline.cmd"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $RepoRoot "output"
$LogFile = Join-Path $LogDir "app.log"
$DefaultPort = 8082

function Get-StartupCmdPath {
    return (Join-Path ([Environment]::GetFolderPath("Startup")) $StartupCmdName)
}

function Get-AppPort {
    $yaml = Join-Path $RepoRoot "config\pipeline.yaml"
    if (Test-Path $yaml) {
        $match = Select-String -Path $yaml -Pattern '^\s*port:\s*(\d+)' | Select-Object -First 1
        if ($match) {
            return [int]$match.Matches[0].Groups[1].Value
        }
    }
    return $DefaultPort
}

function Get-ListeningPids([int]$Port) {
    $pids = @()
    $lines = netstat -ano | Select-String ":$Port\s+.*LISTENING"
    foreach ($line in $lines) {
        if ($line.Line -match '\s(\d+)\s*$') {
            $pids += [int]$Matches[1]
        }
    }
    return $pids | Select-Object -Unique
}

function Get-MainPids {
    $pids = @()
    $marker = [regex]::Escape($RepoRoot)
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            ($_.CommandLine -match 'src\.main') -and
            ($_.CommandLine -match $marker -or $_.ExecutablePath -match $marker)
        } |
        ForEach-Object { $pids += [int]$_.ProcessId }
    return $pids | Select-Object -Unique
}

function Stop-App {
    param([int]$Port)

    $toStop = @()
    $toStop += Get-ListeningPids -Port $Port
    $toStop += Get-MainPids
    $toStop = $toStop | Select-Object -Unique

    if (-not $toStop) {
        Write-Host "No running app found on port $Port."
        return
    }

    foreach ($procId in $toStop) {
        try {
            Write-Host "Stopping PID $procId..."
            Stop-Process -Id $procId -Force -ErrorAction Stop
        } catch {
            Write-Host "Could not stop PID ${procId}: $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    Start-Sleep -Seconds 1
    $left = Get-ListeningPids -Port $Port
    if ($left) {
        throw "Port $Port still in use by PID(s): $($left -join ', ')"
    }
    Write-Host "Stopped."
}

function Start-AppForeground {
    if (-not (Test-Path $PythonExe)) {
        throw "Virtual environment not found. Run scripts\setup-windows.ps1 first."
    }
    Write-Host "Starting Wiki Video Pipeline..."
    Write-Host "Dashboard: http://127.0.0.1:$(Get-AppPort)"
    & $PythonExe -m src.main
}

function Start-AppBackground {
    if (-not (Test-Path $PythonExe)) {
        throw "Virtual environment not found. Run scripts\setup-windows.ps1 first."
    }
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $port = Get-AppPort
    Write-Host "Starting Wiki Video Pipeline in background..."
    Write-Host "Dashboard: http://127.0.0.1:$port"
    Write-Host "Log: $LogFile"

    $argList = @(
        "-NoProfile"
        "-ExecutionPolicy", "Bypass"
        "-Command"
        @"
Set-Location '$RepoRoot'
`$ErrorActionPreference = 'Continue'
& '$PythonExe' -m src.main *>> '$LogFile'
"@
    )
    Start-Process -FilePath "powershell.exe" -ArgumentList $argList -WorkingDirectory $RepoRoot -WindowStyle Hidden | Out-Null

    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (Get-ListeningPids -Port $port) {
            Write-Host "App is listening on port $port."
            return
        }
    }
    Write-Host "Started process, but port $port not confirmed yet. Check $LogFile" -ForegroundColor Yellow
}

function Register-StartupTask {
    $scriptPath = Join-Path $PSScriptRoot "restart-app.ps1"
    $startupCmd = Get-StartupCmdPath
    $cmd = @(
        "@echo off"
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Background"
    ) -join "`r`n"
    Set-Content -Path $startupCmd -Value $cmd -Encoding ASCII
    Write-Host "Registered Windows Startup entry:" -ForegroundColor Green
    Write-Host "  $startupCmd"

    try {
        $action = New-ScheduledTaskAction `
            -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Background" `
            -WorkingDirectory $RepoRoot
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Principal $principal `
            -Force `
            -ErrorAction Stop | Out-Null
        Write-Host "Also registered scheduled task '$TaskName'." -ForegroundColor Green
    } catch {
        Write-Host "Scheduled task skipped (Startup folder entry is enough): $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Unregister-StartupTask {
    $startupCmd = Get-StartupCmdPath
    if (Test-Path $startupCmd) {
        Remove-Item $startupCmd -Force
        Write-Host "Removed Startup entry: $startupCmd" -ForegroundColor Green
    } else {
        Write-Host "No Startup entry found."
    }

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed startup task '$TaskName'." -ForegroundColor Green
    }
}

if ($RegisterStartup) {
    Register-StartupTask
    exit 0
}

if ($UnregisterStartup) {
    Unregister-StartupTask
    exit 0
}

$port = Get-AppPort
Stop-App -Port $port

if ($StopOnly) {
    exit 0
}

if ($Background) {
    Start-AppBackground
} else {
    Start-AppForeground
}
