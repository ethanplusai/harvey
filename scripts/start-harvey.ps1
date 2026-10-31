# Single-instance launcher for Harvey's heartbeat.
#
# Why: on 2026-10-26 `Stop-ScheduledTask` and `schtasks /end` both reported
# success but left `harvey.exe run` alive in session 0 (access denied to kill
# it from a normal shell). With the task's MultipleInstances = IgnoreNew and no
# tracked instance, the next repetition could have started a SECOND heartbeat
# writing to the same SQLite database.
#
# This launcher makes the start idempotent:
#   * no harvey.exe alive  -> run `harvey.exe run` in the foreground
#   * one already alive    -> adopt it: watch until it exits, then start fresh
#                             (keeps the task in "Running" state, which is also
#                              what the ops health checks look at)
#
# Task "Harvey Heartbeat" action:
#   cmd.exe /c "powershell -NoProfile -ExecutionPolicy Bypass -File
#     C:\Users\gaurk\harvey\scripts\start-harvey.ps1 >> C:\Users\gaurk\harvey\heartbeat-launch.log 2>&1"
#
#   -DryRun   report what would happen and exit (never starts or watches)

param([switch]$DryRun)

$ErrorActionPreference = 'Stop'

$harveyExe = 'C:\Users\gaurk\harvey\.venv\Scripts\harvey.exe'
$logPath   = 'C:\Users\gaurk\harvey\heartbeat-run.log'
$guardLog  = 'C:\Users\gaurk\harvey\heartbeat-guard.log'

function Write-GuardLog([string]$message) {
    $line = "{0} [start-harvey] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    try { Add-Content -LiteralPath $guardLog -Value $line -Encoding UTF8 } catch {}
    Write-Host $line
}

# Anything named harvey.exe that is still alive means a loop already owns the
# database. Name-based on purpose: in session 0 the CommandLine and
# ExecutablePath fields are unreadable to a non-admin caller.
function Get-HarveyProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='harvey.exe'")
}

$running = @(Get-HarveyProcesses)

if ($running.Count -gt 0) {
    $ids = ($running | ForEach-Object { $_.ProcessId }) -join ','
    Write-GuardLog "already running (pid $ids) - watching it, not starting a second heartbeat"
    if ($DryRun) {
        Write-GuardLog 'dry run: would have adopted that process and waited for it to exit'
        exit 0
    }
    $lastWarn = Get-Date
    while ($true) {
        Start-Sleep -Seconds 60
        if (@(Get-HarveyProcesses).Count -eq 0) {
            Write-GuardLog 'previous heartbeat exited - starting a fresh one'
            break
        }
        # Surface a hung loop: the cycle log should advance at least every 15 min.
        if (((Get-Date) - $lastWarn).TotalMinutes -ge 60) {
            $lastWarn = Get-Date
            try {
                $age = ((Get-Date) - (Get-Item -LiteralPath $logPath).LastWriteTime).TotalMinutes
                if ($age -gt 45) {
                    Write-GuardLog ("WARNING: cycle log has not moved for {0:N0} min" -f $age)
                }
            } catch {}
        }
    }
}

if ($DryRun) {
    Write-GuardLog "dry run: no harvey.exe alive, would start $harveyExe run"
    exit 0
}

Write-GuardLog "no heartbeat running - starting harvey.exe run"
$env:PYTHONUTF8 = '1'
# Run the loop through cmd.exe so its logging never becomes a PowerShell
# error record: Harvey logs to stderr, and under $ErrorActionPreference =
# 'Stop' plus the task-level `2>&1` redirect, PS 5.1 turns the first stderr
# line into a terminating NativeCommandError that kills this launcher and
# the loop with it (seen 2026-10-29: every fresh loop died on its banner
# line, task result 1). No path here contains spaces, so no inner quoting.
if ($harveyExe -match '\s' -or $logPath -match '\s') {
    Write-GuardLog "FATAL: path with spaces not supported: $harveyExe / $logPath"
    exit 4
}
& cmd.exe /s /c "$harveyExe run >> $logPath 2>&1"
exit $LASTEXITCODE
