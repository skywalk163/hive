#requires -Version 5.1
<#
.SYNOPSIS
    Join a Windows 10 machine to the Hive multi-agent task bus.

.DESCRIPTION
    Checks python/git, clones the hive repo (plus any extra code repos given
    via -Repos), optionally registers this machine as an agent, and optionally
    installs a logon scheduled task that runs the worker.

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
    # 多机路由用的机器名；留空则取本机 hostname
    [string]$HiveHost = "",
    [ValidateSet("verify", "manual", "exec")]
    [string]$Mode = "verify",
    [string]$Command = "",
    [string]$Launch = "",
    # 额外代码仓（除 hive 本身外才需要）。hive 已从其它仓隔离，默认空；
    # 要登记额外代码仓时显式传 -Repos <name>。
    [string[]]$Repos = @(),
    [string[]]$Abilities = @(),
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
    if ($HiveHost -ne "") { $hiveArgs += @("--host", $HiveHost) }
    if ($Command -ne "") { $hiveArgs += @("--command", $Command) }
    if ($Abilities.Count -gt 0) { $hiveArgs += @("--abilities") + $Abilities }
    if ($Launch -ne "") { $hiveArgs += @("--launch", $Launch) }
    # hive 仓本身就是 $Root，直接登记它；不再 clone 一份到 $Src。
    # （light 等仓已从 hive 隔离，见 tasks/T-0133；要登记额外代码仓才显式
    #  传 -Repos，那些仓才会 clone 到 $Src\<name> 并登记。）
    $hiveArgs += @("--repo", ("hive=" + $Root))
    foreach ($repo in $Repos) {
        $hiveArgs += @("--repo", ($repo + "=" + (Join-Path $Src $repo)))
    }
    $hiveArgs += @("--worktrees", $Worktrees)
    Run $Python $hiveArgs
}

# 4.5 emit the MCP client config
# 这一步才是「告诉 agent 到 hive 报道」：把下面这段 JSON 粘进 agent 的 MCP
# 客户端配置（Claude Desktop / Cursor / VS Code / Codex CLI 都认这个格式），
# 那个 agent 就能 hive_whoami 看自己、hive_next 挑卡、hive_claim 领卡、
# 在返回的 worktree 里干活、hive_submit 提交。不需要 worker、不需要计划任务。
$mcp = [ordered]@{
    mcpServers = [ordered]@{
        ("hive-" + $Agent) = [ordered]@{
            command = $Python
            args = @((Join-Path $Root "hive.py"), "--dir", $Root,
                     "mcp", "--agent", $Agent)
        }
    }
}
$mcpJson = ($mcp | ConvertTo-Json -Depth 6)
$mcpFile = Join-Path $Root ("mcp-config-" + $Agent + ".json")
Say "---------------------------------------------------------------"
Say "把下面这段粘进 agent 的 MCP 配置（Claude/Cursor/VS Code/Codex 都认这个格式）："
Say ""
Write-Host $mcpJson
Say ""
Say ("已写入: " + $mcpFile)
Say "之后那个 agent 依次调用：hive_whoami -> hive_next -> hive_claim -> 干活 -> hive_submit"
Say "（Windows 上 agent 没有命令行的话，这条 MCP 路子和 manual worker 二选一，别同时用）"
Say "---------------------------------------------------------------"
if ($Apply) {
    Set-Content -Path $mcpFile -Value $mcpJson -Encoding UTF8
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
