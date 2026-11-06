# join-agent.ps1 — 把一台 Windows 机器挂到 hive 主控上（一条命令）
#
# 用法（在 hive 仓根、用当前登录用户执行，不需要管理员）：
#   powershell -ExecutionPolicy Bypass -File scripts\join-agent.ps1 -Agent trae-win
#   powershell -ExecutionPolicy Bypass -File scripts\join-agent.ps1 -Agent workbuddyai-win `
#       -Launch "C:\Users\skywalk\AppData\Local\Programs\Trae\Trae.exe {worktree}"
#   powershell -ExecutionPolicy Bypass -File scripts\join-agent.ps1 -Agent codearts-win11 `
#       -Mode exec -Command "codebuddy -p --dangerously-skip-permissions -"
#   powershell -ExecutionPolicy Bypass -File scripts\join-agent.ps1 -Agent trae-win -Status
#   powershell -ExecutionPolicy Bypass -File scripts\join-agent.ps1 -Agent trae-win -Uninstall
#
# 它做四件事：
#   1. 校验 python / git
#   2. `hive agent add --dispatchable`：登记这台机器，并允许主控自动派卡
#   3. 生成 %USERPROFILE%\.hive\agents\<agent>\run-worker.cmd（带日志重定向）
#   4. 注册「登录时启动」的计划任务并立即拉起 worker
#
# 为什么用计划任务而不是服务：worker 要在**用户会话**里跑 —— GUI 模式要靠它
# 弹 IDE、写剪贴板，服务账户（SYSTEM）看不到用户的桌面，起来也没用。
#
# 两种模式的区别：
#   manual  GUI agent（Trae / WorkBuddy / CodeArts 窗口）。worker 建好 worktree、
#           写好 .hive/task.md、把 prompt 放进剪贴板、执行 -Launch 打开 IDE，
#           然后盯 worktree：出现提交或改动并稳定一段时间就自动收工。
#   exec    有 headless CLI 的 agent。prompt 从 stdin 进去，回答从 stdout 出来，
#           全程无人。这才是真·无人化，-Command 写那条命令行即可。
#
# 注意：manual 模式下主控仍然「派得了」，因为 agent 配置里写了 dispatchable。
# 不写的话 dispatch 会一律跳过 manual agent —— 这正是 2026-11-06 之前
# 所有 Windows 机器只能人工 copy prompt 的原因。

param(
    [Parameter(Mandatory = $true)][string]$Agent,
    [ValidateSet('manual', 'exec', 'verify')][string]$Mode = 'manual',
    [string]$Command = '',
    [string]$Launch = '',
    [string[]]$Repo = @(),
    [string]$Worktrees = '',
    [string]$HiveDir = '',
    [string]$Coord = '',
    [int]$Interval = 60,
    [int]$MaxConcurrency = 1,
    [switch]$NoDispatchable,
    [switch]$NoStart,
    [switch]$Status,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

# --- 路径 ---------------------------------------------------------------------
if (-not $HiveDir) {
    $HiveDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}
$HiveDir = (Resolve-Path $HiveDir).Path
$hivePy = Join-Path $HiveDir 'hive.py'
if (-not (Test-Path $hivePy)) {
    throw "找不到 $hivePy；用 -HiveDir 指定 hive 工具仓路径"
}
# 协调仓默认就是工具仓（hive 自举）。分开部署时用 -Coord 指过去。
if (-not $Coord) { $Coord = $HiveDir }
$Coord = (Resolve-Path $Coord).Path

$stateDir = Join-Path $env:USERPROFILE ".hive\agents\$Agent"
$logDir = Join-Path $stateDir 'logs'
$logFile = Join-Path $logDir 'worker.log'
$runCmd = Join-Path $stateDir 'run-worker.cmd'
$taskName = "hive-worker-$Agent"

# --- 工具函数 -----------------------------------------------------------------
function Resolve-Python {
    foreach ($n in @('python', 'python3')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    throw "找不到 python；先把 Python 装好并进 PATH"
}

function Get-PythonW([string]$py) {
    # pythonw 不弹黑窗口；找不到就退回 python（会有个控制台窗口）。
    $w = Join-Path (Split-Path $py) 'pythonw.exe'
    if (Test-Path $w) { return $w }
    return $py
}

$py = Resolve-Python
$pyw = Get-PythonW $py

function Invoke-Hive {
    param([string[]]$HiveArgs)
    & $py $hivePy --dir $Coord @HiveArgs
    if ($LASTEXITCODE -ne 0) {
        throw "hive 命令失败（exit $LASTEXITCODE）：$($HiveArgs -join ' ')"
    }
}

# --- 状态 / 卸载 ---------------------------------------------------------------
if ($Status) {
    $t = schtasks /Query /TN $taskName /FO LIST 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "计划任务 $taskName 已注册：" -ForegroundColor Green
        $t | Select-String -Pattern '状态|Status|下次运行|Next Run' | ForEach-Object { "  $_" }
    } else {
        Write-Host "计划任务 $taskName 未注册" -ForegroundColor Yellow
    }
    $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -and $_.CommandLine -match "work --agent $([regex]::Escape($Agent))" }
    if ($procs) {
        foreach ($p in $procs) { Write-Host "  worker 进程 PID $($p.ProcessId)" -ForegroundColor Green }
    } else {
        Write-Host "  worker 进程没在跑" -ForegroundColor Yellow
    }
    if (Test-Path $logFile) {
        Write-Host "`n--- 日志末尾（$logFile）---"
        Get-Content $logFile -Tail 8
    }
    return
}

if ($Uninstall) {
    schtasks /Delete /TN $taskName /F 2>$null | Out-Null
    $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -and $_.CommandLine -match "work --agent $([regex]::Escape($Agent))" }
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "已卸载 $Agent 的计划任务与 worker 进程" -ForegroundColor Green
    Write-Host "（agent 登记仍在 agents/$Agent.json，要彻底删就 git rm 那个文件）"
    return
}

# --- 1. 校验 -------------------------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "找不到 git"
}
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# --- 2. 登记 agent -------------------------------------------------------------
$addArgs = @('agent', 'add', '--name', $Agent, '--mode', $Mode,
             '--worktrees', $(if ($Worktrees) { $Worktrees } else { Join-Path $env:USERPROFILE ".hive\wt\$Agent" }),
             '--max-concurrency', "$MaxConcurrency")
if ($Repo.Count -gt 0) {
    foreach ($r in $Repo) { $addArgs += @('--repo', $r) }
} else {
    $addArgs += @('--repo', "hive=$HiveDir")
}
if ($Command) { $addArgs += @('--command', $Command) }
if ($Launch) { $addArgs += @('--launch', $Launch) }
if (-not $NoDispatchable) { $addArgs += '--dispatchable' }

Write-Host "登记 agent $Agent（mode=$Mode）…" -ForegroundColor Cyan
Invoke-Hive -HiveArgs $addArgs

if ($Mode -eq 'exec' -and -not $Command) {
    Write-Warning "exec 模式没给 -Command，worker 启动会直接失败"
}
if ($Mode -eq 'manual' -and -not $Launch) {
    Write-Warning ("manual 模式没给 -Launch，worker 只会把 prompt 放进剪贴板，" +
                   "不会自动打开 IDE")
}

# --- 3. 生成启动包装 -----------------------------------------------------------
$workArgs = @("`"$hivePy`"", '--dir', "`"$Coord`"", 'work', '--agent', $Agent,
              '--interval', "$Interval")
$cmdBody = @(
    '@echo off',
    "rem 由 scripts\join-agent.ps1 生成，改这里不如重跑那个脚本",
    "cd /d `"$HiveDir`"",
    "`"$pyw`" $($workArgs -join ' ') >> `"$logFile`" 2>&1"
)
Set-Content -Path $runCmd -Value $cmdBody -Encoding OEM
Write-Host "已生成 $runCmd" -ForegroundColor Cyan

# --- 4. 计划任务（登录时启动） ---------------------------------------------------
schtasks /Delete /TN $taskName /F 2>$null | Out-Null
schtasks /Create /TN $taskName /TR "`"$runCmd`"" /SC ONLOGON /RL LIMITED /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "创建计划任务失败（exit $LASTEXITCODE）"
}
Write-Host "已注册计划任务 $taskName（登录时自动启动）" -ForegroundColor Green

if (-not $NoStart) {
    Start-Process -FilePath $runCmd -WindowStyle Hidden
    Start-Sleep -Seconds 3
    $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -and $_.CommandLine -match "work --agent $([regex]::Escape($Agent))" }
    if ($procs) {
        Write-Host "worker 已启动（PID $(($procs | ForEach-Object ProcessId) -join ', ')）" -ForegroundColor Green
    } else {
        Write-Warning "worker 似乎没起来，看日志：$logFile"
    }
}

Write-Host @"

下一步（在主控那台机器执行，确认它能看见这台机器）：
  python3 hive.py --dir <协调仓> agent list
  python3 hive.py --dir <协调仓> dispatch          # dry-run 看一眼会派给谁

本机排查：
  powershell -ExecutionPolicy Bypass -File scripts\join-agent.ps1 -Agent $Agent -Status
  日志：$logFile
"@ -ForegroundColor Gray
