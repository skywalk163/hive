#requires -Version 5.1
<#
.SYNOPSIS
    Run the Hive worker forever, restarting it if it crashes.

.DESCRIPTION
    Started by the "HiveWorker-<agent>" scheduled task at logon.
    All output is appended to <Root>\logs\worker-<agent>.log.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\hive\windows\run-worker.ps1 -Root C:\hive -Agent win10
#>
[CmdletBinding()]
param(
    [string]$Root = "C:\hive",
    [string]$Agent = "win10",
    [int]$Interval = 60,
    [string]$Python = "python",
    [int]$MaxRestarts = 50
)

$ErrorActionPreference = "Continue"

$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("worker-" + $Agent + ".log")
$hivePy = Join-Path $Root "hive.py"

function Note([string]$m) {
    $line = (Get-Date -Format o) + " " + $m
    Add-Content -Path $log -Value $line -Encoding UTF8
}

if (-not (Test-Path $hivePy)) {
    Note ("ERROR: hive.py not found at " + $hivePy)
    exit 2
}

Note ("run-worker start: agent=" + $Agent + " root=" + $Root + " interval=" + $Interval)
$restarts = 0
while ($restarts -lt $MaxRestarts) {
    Note ("starting worker " + $Agent)
    & $Python $hivePy --dir $Root work --agent $Agent --interval $Interval *>> $log
    $code = $LASTEXITCODE
    Note ("worker exited with code " + $code)
    $restarts = $restarts + 1
    $wait = [Math]::Min(60, 5 * $restarts)
    Note ("restart " + $restarts + "/" + $MaxRestarts + " in " + $wait + "s")
    Start-Sleep -Seconds $wait
}

Note "max restarts reached; worker stops. Check the log and run manually."
exit 1
