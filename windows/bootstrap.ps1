#requires -Version 5.1
<#
.SYNOPSIS
    Join a Windows 10 machine to the Hive multi-agent task bus.

.DESCRIPTION
    Checks python/git, clones the hive repo and the code repos, optionally
    registers this machine as an agent, and optionally installs a logon
    scheduled task that runs the worker.

    Without -Apply it only prints what it would do (dry run).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\hive\windows\bootstrap.ps1 -Apply

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\hive\windows\bootstrap.ps1 `
        -Apply -Register -InstallTask -Mode verify -Agent win10
#>
[CmdletBinding()]
param(
    [string]$Root = "C:\hive",
    [string]$Src = "C:\src",
    [string]$GiteaBase = "http://192.168.1.5:3000/skywalk",
    [string]$Agent = "win10",
    [ValidateSet("verify", "manual", "exec")]
    [string]$Mode = "verify",
    [string]$Command = "",
    [string]$Launch = "",
    [string[]]$Repos = @("light"),
    [string]$Python = "python",
    [string]$Worktrees = "C:\hive-wt",
    [switch]$Register,
    [switch]$InstallTask,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

function Say([string]$m) { Write-Host ("[hive] " + $m) }
function Die([string]$m) { throw $m }
function Have([string]$name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}
function Run([string]$exe, [string[]]$argv) {
    Say ("run: " + $exe + " " + ($argv -join " "))
    if (-not $Apply) { return }
    & $exe @argv
    if ($LASTEXITCODE -ne 0) {
        Die ("failed with exit " + $LASTEXITCODE + ": " + $exe + " " + ($argv -join " "))
    }
}

Say "root=$Root src=$Src agent=$Agent mode=$Mode apply=$Apply"

if (-not (Have "git")) {
    Die "git not found. Install it first: winget install --id Git.Git -e"
}
if (-not (Have $Python)) {
    Die ("python not found: " + $Python + ". Install Python 3.10+ and tick 'Add to PATH'.")
}
if ($Mode -eq "exec" -and $Command -eq "") {
    Die "mode=exec requires -Command, e.g. -Command 'claude -p'"
}
if ($InstallTask -and -not $Apply) {
    Say "NOTE: -InstallTask without -Apply only prints the task command."
}

& $Python --version
& git --version

# 1. git settings that keep Windows from polluting diffs
Run "git" @("config", "--global", "core.autocrlf", "false")
Run "git" @("config", "--global", "core.longpaths", "true")

# 2. hive repo
$hiveDirGit = Join-Path $Root ".git"
if (Test-Path $hiveDirGit) {
    Run "git" @("-C", $Root, "pull", "--ff-only")
}
else {
    Run "git" @("clone", ($GiteaBase + "/hive.git"), $Root)
}

# 3. code repos
foreach ($repo in $Repos) {
    $dst = Join-Path $Src $repo
    if (Test-Path (Join-Path $dst ".git")) {
        Run "git" @("-C", $dst, "pull", "--ff-only")
    }
    else {
        Run "git" @("clone", ($GiteaBase + "/" + $repo + ".git"), $dst)
    }
}

# 4. register this machine as an agent (needs Gitea write access)
if ($Register) {
    $hivePy = Join-Path $Root "hive.py"
    $hiveArgs = @($hivePy, "--dir", $Root, "agent", "add", "--name", $Agent, "--mode", $Mode)
    if ($Command -ne "") { $hiveArgs += @("--command", $Command) }
    if ($Launch -ne "") { $hiveArgs += @("--launch", $Launch) }
    foreach ($repo in $Repos) {
        $hiveArgs += @("--repo", ($repo + "=" + (Join-Path $Src $repo)))
    }
    $hiveArgs += @("--worktrees", $Worktrees)
    Run $Python $hiveArgs
}

# 5. scheduled task at logon
if ($InstallTask) {
    $worker = Join-Path $Root "windows\run-worker.ps1"
    $taskArg = '-NoProfile -ExecutionPolicy Bypass -File "' + $worker + '"' +
               ' -Root "' + $Root + '" -Agent "' + $Agent + '"'
    Say ("task: powershell.exe " + $taskArg)
    if ($Apply) {
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskArg
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask `
            -TaskName ("HiveWorker-" + $Agent) `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Description ("Hive worker for " + $Agent) `
            -Force | Out-Null
        Say "scheduled task registered: HiveWorker-$Agent"
    }
}

# 6. Defender exclusions (best effort, needs admin)
if ($Apply) {
    try {
        Add-MpPreference -ExclusionPath @($Src, $Worktrees) -ErrorAction Stop
        Say "Defender exclusions added for $Src and $Worktrees"
    }
    catch {
        Say ("skip Defender exclusion (run an elevated shell if you want it): " + $_.Exception.Message)
    }
}

Say "done."
if (-not $Apply) {
    Say "dry run only. Re-run with -Apply (add -Register -InstallTask as needed)."
}
else {
    Say ("next: check log " + (Join-Path $Root ("logs\worker-" + $Agent + ".log")))
    Say "hint: enable auto-logon for this VM so the task starts after a reboot."
}
