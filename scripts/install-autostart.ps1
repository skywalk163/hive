# 蜂巢 Hive 看板 —— Windows 常驻部分的唯一入口（计划任务 / 单实例守护 / 日志轮转）
#
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1            # 装登录自启计划任务，并启动看板
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Start     # 启动（已在跑就直接返回，不重复拉起）
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Stop      # 停止（按 pid 过滤，杀整棵子树）
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Status    # 只看状态，不动进程
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Restart
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Uninstall
#
# 为什么所有动作都收在这一个脚本里：托盘（windows\tray.ahk）和计划任务都调它。
# 一旦「启动」有两处实现，就会出现「谁又拉起了第二个看板」这种查不出肇事者的状态。
#
# 计划任务采用「用户登录时」触发（schtasks /sc onlogon）：
#   * 不需要管理员权限 —— 普通用户即可创建自己的登录任务。
#   * 若要「开机即启动、不登录也跑」，需管理员改用 /sc onstart /ru SYSTEM，
#     脚本 -RunAsSystem 参数走这条路（会申请提权）。
#
# 日志：%USERPROFILE%\.hive\logs\hive-<yyyyMMdd>.log（按天一份），追加写。
#   * 追加靠 cmd 的 >> 重定向，**不用** Start-Process 的 -RedirectStandardOutput ——
#     后者每次会把文件截断重写，按天追加就失效了（真机实测：先写 FIRST 再写 SECOND，
#     文件里只剩 SECOND）。
#   * 大小上限：当天日志超过 $MaxLogMB 就改名成 hive-<yyyyMMdd>.log.1、.2……，另开一份。
#     轮转只发生在**拉起看板之前**：拉起后那条 cmd 一直开着日志文件（>> 不共享写），
#     这时候改名会失败（真机实测 MoveFileInfoItemIOError）。
#   * 数量上限：超过 $LogDays 天（默认 7）的 hive-*.log* 在每次启动时清掉。
#   * 顺序讲究：启动行写在「拉起看板之前」、停止行写在「taskkill 之后」，两处都自带 try/catch。
#     进程活着时它占着日志文件，这时 Add-Content 会抛 IOException，而本脚本
#     $ErrorActionPreference='Stop' 会把异常升级成终止错误 —— 真机实测的后果是
#     「启动流程断在写日志那一步」「-Stop 更糟：异常发生在 taskkill 之前，进程根本没被杀掉」。
#
# 本脚本从不自动打开浏览器，也从不用可见窗口拉起子进程（-WindowStyle Hidden）：
# 常驻部分的「不打扰」= 不额外制造窗口（不等于去消灭别人已有的弹窗，那是 T-0206 的事）。

param(
    [int]$Port = 8787,
    [int]$Interval = 20,
    [int]$LogDays = 7,
    [int]$MaxLogMB = 5,
    [string]$Python = 'python',
    [switch]$Start,
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Status,
    [switch]$Uninstall,
    [switch]$RunAsSystem,
    [switch]$NoStart
)

# 端口/间隔来源：显式参数优先，其次环境变量，最后上面 param 的默认值。
# 与 hive.py / dashboard-service.cmd / *.sh 保持同一个默认值，别再各写各的。
if (-not $PSBoundParameters.ContainsKey('Port') -and $env:HIVE_DASHBOARD_PORT) {
    $Port = [int]$env:HIVE_DASHBOARD_PORT
}
if (-not $PSBoundParameters.ContainsKey('Interval') -and $env:INTERVAL) {
    $Interval = [int]$env:INTERVAL
}

$ErrorActionPreference = 'Stop'

# 控制台输出钉成 UTF-8：5.1 默认 OEM 代码页，中文提示会变问号，
# 而「看不出报错内容」正是这类脚本最常见的误诊来源。
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$SelfPath   = $MyInvocation.MyCommand.Path
$RepoDir    = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$DashDir    = Join-Path $RepoDir 'dashboard'
$ServePy    = Join-Path $DashDir 'serve.py'
$StatusFile = Join-Path $DashDir 'status.json'
$TaskName   = 'HiveDashboard'

# 绑定地址：默认 0.0.0.0（局域网可访问）。收回本机：set HIVE_DASHBOARD_HOST=127.0.0.1
$HostBind = $env:HIVE_DASHBOARD_HOST
if (-not $HostBind) { $HostBind = '0.0.0.0' }

$StateDir = Join-Path $env:USERPROFILE '.hive'
$RunDir   = Join-Path $StateDir 'run'
$LogDir   = Join-Path $StateDir 'logs'
$PidFile  = Join-Path $RunDir 'dashboard.pid'

# --- 日志轮转 -------------------------------------------------------------- #

function Get-HiveLogPath {
    # 一天一份：文件名里带日期，天然按天切分，不用去解析文件内容。
    return (Join-Path $LogDir ('hive-' + (Get-Date -Format 'yyyyMMdd') + '.log'))
}

function Rotate-HiveLog {
    # 大小轮转：当天日志超过上限就改名带序号，另开一份，避免单文件无限膨胀。
    # 必须在**拉起看板之前**调用：看板那条 cmd 用 >> 把日志一直开着，
    # 文件被占用时 Move-Item 改不了名（真机实测 MoveFileInfoItemIOError）。
    param([string]$LogPath)
    if (-not (Test-Path -LiteralPath $LogPath)) { return }
    $size = (Get-Item -LiteralPath $LogPath).Length
    if ($size -lt (1MB * $MaxLogMB)) { return }
    $n = 1
    while (Test-Path -LiteralPath ("$LogPath.$n")) { $n += 1 }
    try {
        Move-Item -LiteralPath $LogPath -Destination ("$LogPath.$n") -Force -ErrorAction Stop
    } catch {
        # 改不了名（上一次的看板还没退干净、文件仍被占用）不能让轮转把启动流程带崩：
        # 记一句警告就好，下次启动再轮。
        Write-Warning ("日志轮转跳过：" + $_.Exception.Message)
    }
}

function Write-HiveLog {
    param([string]$Line)
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }
    $log = Get-HiveLogPath
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
# 追加而不是覆盖；只动本脚本自己的 hive-*.log，不碰同目录里 worker-*.log 等别人家的日志。
    # 必须显式 -Encoding UTF8：PS 5.1 的 Add-Content 默认按 ANSI（简体中文机器上是 GBK）落盘，
    # 而看板 stdout 经 cmd >> 进来的是 UTF-8，不钉编码本脚本写下的中文行就是乱码（真机实测）。
    try {
        Add-Content -LiteralPath $log -Value ("[$stamp] " + $Line) -Encoding UTF8 -ErrorAction Stop
    } catch {
        # 看板进程正开着这个文件（不共享写），追加会失败 —— 看板自己的 stdout 已经写进里面了。
        # 这里吞掉异常、不中断流程：写日志失败不该把启动 / 停止带停。
        Write-Warning ("日志追加跳过：" + $_.Exception.Message)
    }
}

function Remove-OldHiveLogs {
    # 数量上限：按最后写入时间清掉超过 $LogDays 天的 hive-*.log*（含大小轮转出来的 .1/.2）。
    if (-not (Test-Path $LogDir)) { return }
    $cutoff = (Get-Date).AddDays(-1 * $LogDays)
    Get-ChildItem -Path $LogDir -Filter 'hive-*.log*' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
}

# --- 进程与实例状态 -------------------------------------------------------- #

function Get-HiveProcId {
    if (-not (Test-Path $PidFile)) { return 0 }
    $raw = Get-Content -LiteralPath $PidFile -TotalCount 1 -ErrorAction SilentlyContinue
    if (-not $raw) { return 0 }
    $id = 0
    if ([int]::TryParse(("$raw").Trim(), [ref]$id) -and $id -gt 0) { return $id }
    return 0
}

function Test-HiveProcAlive {
    param([int]$ProcId)
    if ($ProcId -le 0) { return $false }
    # 按 PID 精确问，不按映像名问（同机可能有别的 python 在跑，按名字会误判）。
    $out = & tasklist /FI "PID eq $ProcId" /NH 2>$null
    return ("$out" -match ("\b" + $ProcId + "\b"))
}

function Test-HivePortBusy {
    param([int]$Port)
    # 先看端口上有没有人在听 —— 有人在听就是「有别的东西占着这个端口」，不能重复拉起。
    # 不能只看「HTTP 返回 200」：看板刚起来、状态文件还没生成时会返 503（真机实测），
    # 那时它其实已经在服务了，只看 200 会误判成「没在跑」而拉出第二个实例。
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect('127.0.0.1', $Port)
        $busy = $tcp.Connected
        $tcp.Close()
        if ($busy) { return $true }
    } catch {
        # 连不上就当没人在听，往下走 HTTP 兜底。
    }
    # 端口连不上也不代表没看板（可能只绑在别的地址上）：再问一次只读接口兜底。
    # 免 token 的接口，只读探一下；任何异常都当「没在服务」。
    try {
        $resp = Invoke-WebRequest -Uri ("http://127.0.0.1:$Port/api/status") -UseBasicParsing -TimeoutSec 2
        return ($resp.StatusCode -ge 200)
    } catch {
        return $false
    }
}

function Get-HiveInstanceState {
    # running   本脚本拉起的实例还活着（pid 文件有效且进程在）
    # port-busy 端口上有人在听 / 在服务，但不是本脚本拉起的（pid 文件无效）—— 同样不能重复拉起
    # stopped   端口空着，可以启动
    $procId = Get-HiveProcId
    if ($procId -gt 0 -and (Test-HiveProcAlive $procId)) { return 'running' }
    if (Test-HivePortBusy $Port) { return 'port-busy' }
    return 'stopped'
}

# --- 启动 / 停止 ----------------------------------------------------------- #

function Start-HiveDashboard {
    if (-not (Test-Path $ServePy)) {
        Write-Host "找不到 $ServePy"
        return 1
    }
    if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir -Force | Out-Null }
    Remove-OldHiveLogs

    $state = Get-HiveInstanceState
    if ($state -eq 'running') {
        Write-Host ("看板已在运行（pid=" + (Get-HiveProcId) + "，端口 " + $Port + "）—— 单实例守护：不重复启动，直接返回。")
        return 0
    }
    if ($state -eq 'port-busy') {
        Write-Host ("端口 " + $Port + " 上已有看板在提供接口（不是本脚本拉起的）—— 单实例守护：不重复启动。")
        return 0
    }

    $logPath = Get-HiveLogPath
    # 顺序很关键：先轮转、先写自己的启动行，**然后**才拉起看板。
    # 拉起之后那条 cmd 就一直开着日志文件（>> 不共享写），此后再 Move-Item / Add-Content 都会失败。
    Rotate-HiveLog $logPath
    Write-HiveLog ("启动看板 端口=" + $Port + " 日志=" + $logPath + "（pid 见 run\dashboard.pid）")

    # 追加 + 按天：必须走 cmd 的 >>。Start-Process -RedirectStandardOutput 会把文件截断。
    # call "python" 前缀是为了让 cmd 正确解析带引号的可执行路径。
    $inner = 'call "' + $Python + '" -u "' + $ServePy + '" --status "' + $StatusFile + '"' +
             ' --port ' + $Port + ' --host ' + $HostBind + ' --auto-refresh --refresh-interval ' + $Interval +
             ' >> "' + $logPath + '" 2>&1'
    $proc = Start-Process -FilePath $env:ComSpec -ArgumentList @('/s', '/c', ('"' + $inner + '"')) `
        -WorkingDirectory $DashDir -WindowStyle Hidden -PassThru

    Set-Content -LiteralPath $PidFile -Value $proc.Id
    Start-Sleep -Milliseconds 900

    if ((Get-HiveInstanceState) -eq 'running') {
        Write-Host ("看板已启动（pid=" + $proc.Id + "），端口 " + $Port)
        return 0
    }
    Write-Host ("看板启动后未能提供服务（pid=" + $proc.Id + "），请看日志：" + $logPath)
    return 1
}

function Stop-HiveDashboard {
    $procId = Get-HiveProcId
    if ($procId -le 0) {
        Write-Host "看板未在运行（没有 pid 文件）"
        return 0
    }
    if (-not (Test-HiveProcAlive $procId)) {
        Write-Host ("看板未在运行（pid 文件残留，进程 " + $procId + " 已退出）")
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return 0
    }
    # 先亮清单再动手：让人能在进程消失之前核对「要杀的是不是这一个」。
    Write-Host "将停止以下进程（按 pid 过滤，含子树）："
& tasklist /FI "PID eq $procId"
    # 只按 PID 杀，并且杀整棵子树（cmd -> python 链），不用 /IM 按映像名批量杀。
    & taskkill /PID $procId /T /F | Out-Null
    Start-Sleep -Milliseconds 400
    # 杀完再写日志：进程活着的时候它占着日志文件，这时候 Add-Content 会撞上「文件被占用」并抛异常，
    # 异常一旦发生在 taskkill 之前，进程就根本没被杀掉（真机实测）。
    Write-HiveLog ("停止看板 pid=" + $procId)
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host ("看板已停止（pid=" + $procId + "）")
    return 0
}

# --- 计划任务 -------------------------------------------------------------- #

function Remove-HiveTask {
    $existing = & schtasks /query /tn $TaskName 2>$null
    if ($LASTEXITCODE -eq 0) {
        & schtasks /delete /tn $TaskName /f | Out-Null
        Write-Host "已删除计划任务：$TaskName"
    } else {
        Write-Host "计划任务 $TaskName 不存在，无需删除"
    }
}

function Install-HiveTask {
    Remove-HiveTask
    if ($SelfPath -match '\s') {
        Write-Host "警告：脚本路径含空格，schtasks 的 -tr 可能解析不了，建议把仓库放到无空格路径。"
    }
    # 刻意不嵌引号：PS 5.1 给原生程序传含引号的参数会被转义坏（真机实测 exit=1）。
    $tr = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ' + $SelfPath +
          ' -Start -Port ' + $Port + ' -Interval ' + $Interval

    if ($RunAsSystem) {
        # 开机即启动（未登录也跑），需要管理员
        $isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) {
            Write-Host "-RunAsSystem 需要管理员权限，请以管理员身份运行 PowerShell。"
            return 1
        }
        & schtasks /create /tn $TaskName /tr $tr /sc onstart /ru SYSTEM /rl HIGHEST /f | Out-Null
    } else {
        & schtasks /create /tn $TaskName /tr $tr /sc onlogon /f | Out-Null
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Host "创建计划任务失败（schtasks 退出码 $LASTEXITCODE）"
        return 1
    }
    Write-Host "已创建计划任务：$TaskName（触发：$(
        if ($RunAsSystem) { '开机 onstart' } else { '用户登录 onlogon' })，命令：$tr）"
    return 0
}

# --- 入口分派 -------------------------------------------------------------- #

if ($Uninstall) {
    Stop-HiveDashboard | Out-Null
    Remove-HiveTask
    Write-Host "卸载完成。"
    exit 0
}

if ($Status) {
    switch (Get-HiveInstanceState) {
        'running'   { Write-Host ("运行中（pid=" + (Get-HiveProcId) + "），端口 " + $Port + "，日志 " + (Get-HiveLogPath)) }
        'port-busy' { Write-Host ("端口 " + $Port + " 被占用，但 pid 文件指向的进程不在了") }
        'stopped'   { Write-Host "未运行" }
    }
    exit 0
}

if ($Stop) {
    Stop-HiveDashboard | Out-Null
    exit 0
}

if ($Restart) {
    Stop-HiveDashboard | Out-Null
    Start-Sleep -Milliseconds 500
    exit (Start-HiveDashboard)
}

if ($Start) {
    exit (Start-HiveDashboard)
}

# 默认路径 = 装计划任务 + 启动。
if ((Install-HiveTask) -ne 0) { exit 1 }

if (-not $NoStart) {
    Start-HiveDashboard | Out-Null
}

Write-Host ""
Write-Host "访问：http://127.0.0.1:$Port/"
Write-Host "日志：$(Get-HiveLogPath)（按天切分，保留 $LogDays 天，单文件上限 $MaxLogMB MB）"
Write-Host "管理：scripts\install-autostart.ps1 {-Start|-Stop|-Restart|-Status}"
Write-Host "卸载：powershell -File scripts\install-autostart.ps1 -Uninstall"