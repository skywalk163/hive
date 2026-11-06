<#
  Windows agent「定时领取」—— 不需要 OpenSSH、不需要远程运维通道。

  hive 的 worker 本来就是拉取式（见 hive.py 文件头）：只要能 git fetch/push
  协调仓，就能领卡、交结果。主控不需要 SSH 进这台机器。所以让计划任务每
  半小时 / 一小时把 worker 叫醒一次即可，不必常驻进程，也不必开 22 端口。

  每轮做两件事：
    1. git pull --ff-only 同步 **hive 工具仓**（顺带把代码更新也解决掉）
    2. hive work --once --no-cluster 领一轮卡就退出（纯 git，不拉起集群服务）

  用法（PowerShell 5.1，普通用户即可，不需要管理员）：
    powershell -ExecutionPolicy Bypass -File .\win-scheduled-work.ps1 -Agent trae-win
    powershell -ExecutionPolicy Bypass -File .\win-scheduled-work.ps1 -Agent trae-win -Minutes 60 -Apply
    powershell -ExecutionPolicy Bypass -File .\win-scheduled-work.ps1 -Agent trae-win -Status
    powershell -ExecutionPolicy Bypass -File .\win-scheduled-work.ps1 -Agent trae-win -RunNow
    powershell -ExecutionPolicy Bypass -File .\win-scheduled-work.ps1 -Agent trae-win -Uninstall

  不带 -Apply 只体检，不改任何东西。
#>
param(
    [string]$Agent = '',
    [ValidateSet(15, 30, 60, 120)][int]$Minutes = 30,
    [string]$HiveDir = '',
    [string]$Coord = '',
    [switch]$Apply,
    [switch]$RunNow,
    [switch]$Status,
    [switch]$Uninstall,
    [switch]$NoPull,
    [switch]$NoOnce
)

$ErrorActionPreference = 'Stop'

# --- 路径 ---------------------------------------------------------------------
if (-not $HiveDir) {
    $HiveDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}
$HiveDir = (Resolve-Path $HiveDir).Path
$hivePy = Join-Path $HiveDir 'hive.py'
if (-not (Test-Path $hivePy)) { throw "找不到 $hivePy；用 -HiveDir 指定 hive 工具仓" }
if (-not $Coord) { $Coord = $HiveDir }

if (-not $Agent) { throw "必须给 -Agent <名字>；只想看脚本用法就不带参数运行看注释" }
$stateDir = Join-Path $env:USERPROFILE ".hive\agents\$Agent"
$logDir = Join-Path $stateDir 'logs'
$logFile = Join-Path $logDir 'scheduled-work.log'
$runCmd = Join-Path $stateDir 'run-scheduled-work.cmd'
$taskName = "hive-scheduled-$Agent"
$mutexName = "Local\hive-scheduled-$Agent"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-Log([string]$msg) {
    $line = ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Resolve-Python {
    foreach ($n in @('python', 'python3')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    throw "找不到 python；先把 Python 装好并进 PATH"
}

$py = Resolve-Python

# --- 状态 / 卸载 ---------------------------------------------------------------
if ($Status) {
    $t = schtasks /Query /TN $taskName /FO LIST 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "计划任务 $taskName 已注册：" -ForegroundColor Green
        $t | Select-String -Pattern '状态|Status|下次运行|Next Run|上次运行|Last Run' | ForEach-Object { "  $_" }
    } else {
        Write-Host "计划任务 $taskName 未注册" -ForegroundColor Yellow
    }
    Write-Host "日志：$logFile"
    if (Test-Path $logFile) {
        Write-Host "`n--- 最近 15 行 ---"
        Get-Content $logFile -Tail 15
    }
    return
}

if ($Uninstall) {
    schtasks /Delete /TN $taskName /F 2>$null | Out-Null
    Write-Host "已删除计划任务 $taskName（agent 登记与日志保留）" -ForegroundColor Green
    return
}

# --- 立即跑一轮（也可由计划任务调用）-------------------------------------------
if ($RunNow) {
    # 计划任务可能在上一轮还没跑完时又触发。命名 Mutex 保证同一 agent 只有一个
    # 领取进程；拿不到锁就直接退出，不排队（下一轮还会来，不丢任务）。
    $created = $false
    $mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$created)
    if (-not $created) {
        Write-Log "上一轮还在跑，跳过本次触发"
        return
    }
    try {
        Write-Log "=== 开始领取（agent=$Agent）==="
        if (-not $NoPull) {
            $dirty = (git -C $HiveDir status --porcelain 2>$null)
            if ($dirty) {
                Write-Log "跳过代码同步：hive 工具仓有本地改动（避免 pull 撞车）"
            } else {
                git -C $HiveDir fetch --all --quiet
                git -C $HiveDir pull --ff-only
                if ($LASTEXITCODE -ne 0) {
                    Write-Log "警告：git pull --ff-only 失败，沿用现有代码继续领卡"
                } else {
                    Write-Log ("代码已同步到 " + (git -C $HiveDir rev-parse --short HEAD))
                }
            }
        }
        if (-not $NoOnce) {
            & $py $hivePy '--dir' $Coord 'work' '--agent' $Agent '--once' '--no-cluster' 2>&1 |
                ForEach-Object { Write-Log ([string]$_) }
            Write-Log ("work --once 退出码 " + $LASTEXITCODE)
        }
        Write-Log "=== 本轮结束 ==="
    } finally {
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
    return
}

# --- 体检 ----------------------------------------------------------------------
Write-Host "=== hive 定时领取体检 ===" -ForegroundColor Cyan
Write-Host ("  agent    : " + $Agent)
Write-Host ("  间隔     : 每 " + $Minutes + " 分钟")
Write-Host ("  python   : " + $py)
Write-Host ("  hive.py  : " + $hivePy)
Write-Host ("  协调仓   : " + $Coord)
$head = (git -C $HiveDir rev-parse --short HEAD 2>$null)
$dirty = (git -C $HiveDir status --porcelain 2>$null)
Write-Host ("  当前 HEAD: " + $head + $(if ($dirty) { "（工作区有改动，将跳过 pull）" } else { "（干净）" }))
Write-Host ("  计划任务 : " + $taskName)
Write-Host ("  日志     : " + $logFile)

# agent 是否登记、是否可派：定时领取也救不了「主控不给你派卡」
$agentJson = Join-Path $Coord "agents\$Agent.json"
if (Test-Path $agentJson) {
    try {
        $cfg = Get-Content $agentJson -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Host ("  agent 配置: mode=" + $cfg.mode + " command=" + $(if ($cfg.command) { "已配" } else { "空" }) +
                    " dispatchable=" + $(if ($cfg.dispatchable) { "是" } else { "否" }))
        if (-not $cfg.dispatchable) {
            Write-Warning "该 agent 未标 dispatchable，主控 dispatch 不会自动派卡给它。定时领取只能捡已派到 inbox 的卡。"
        }
        if ($cfg.mode -eq 'manual' -or -not $cfg.command) {
            Write-Warning "manual 模式 / command 为空：领到卡只会建工作区并把 prompt 放进剪贴板，仍需人在 IDE 里点。"
        }
    } catch {
        Write-Warning ("agent 配置读取失败：" + $_.Exception.Message)
    }
} else {
    Write-Warning "协调仓里没有 agents\$Agent.json；先在主控执行 hive agent add 登记这台机器"
}

if (-not $Apply) {
    Write-Host "`n只体检，未改动。确认无误后加 -Apply 注册计划任务。" -ForegroundColor Yellow
    Write-Host "想立刻验证一轮：加 -RunNow（不需要先 -Apply）" -ForegroundColor Yellow
    return
}

# --- 注册 ----------------------------------------------------------------------
# 执行体写成 .cmd：计划任务的 /TR 参数带空格时引号极易被拆错，包一层最稳。
# 用 pythonw 避免每半小时弹一个黑窗口；日志走文件。
$pyw = Join-Path (Split-Path $py -Parent) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }
$psExe = (Get-Command powershell.exe).Source
$cmdBody = @(
    '@echo off',
    "rem generated by win-scheduled-work.ps1 - do not edit",
    "`"$psExe`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Agent $Agent -HiveDir `"$HiveDir`" -Coord `"$Coord`" -RunNow $(if ($NoPull) { '-NoPull' } else { '' })"
)
Set-Content -Path $runCmd -Value $cmdBody -Encoding OEM
Write-Host "已生成 $runCmd" -ForegroundColor Cyan

schtasks /Delete /TN $taskName /F 2>$null | Out-Null
# /SC MINUTE /MO N：每 N 分钟一次；/IT 保证在用户登录的会话里跑（GUI agent 需要桌面）。
$sch = schtasks /Create /TN $taskName /TR "`"$runCmd`"" /SC MINUTE /MO $Minutes /IT /RL LIMITED /F 2>&1
if ($LASTEXITCODE -ne 0) { throw ("创建计划任务失败：" + ($sch | Out-String)) }
Write-Host ("已注册 $taskName：每 $Minutes 分钟领一轮卡") -ForegroundColor Green

Write-Host "`n下一步：" -ForegroundColor Cyan
Write-Host "  立刻验证一轮 : powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Agent $Agent -RunNow"
Write-Host "  看状态/日志  : ... -Agent $Agent -Status"
Write-Host "  卸载         : ... -Agent $Agent -Uninstall"
