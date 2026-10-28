# 蜂巢 Hive 看板 —— Windows 开机自启安装器（计划任务）
#
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Port 8787
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Uninstall
#
# 采用方式：计划任务「用户登录时」触发（schtasks /sc onlogon）。
#   * 不需要管理员权限 —— 普通用户即可创建自己的登录任务。
#   * 若要「开机即启动、不登录也跑」，需管理员改用 /sc onstart /ru SYSTEM，
#     脚本 -RunAsSystem 参数走这条路（会申请提权）。

param(
    [int]$Port = 8787,
    [int]$Interval = 20,
    [switch]$Uninstall,
    [switch]$RunAsSystem,
    [switch]$NoStart
)

# 端口来源：-Port 优先，其次 $env:HIVE_DASHBOARD_PORT，最后上面 param 的默认 8787。
# 三者与 hive.py / dashboard-service.cmd / *.sh 保持同一个默认值，别再各写各的。
if (-not $PSBoundParameters.ContainsKey('Port') -and $env:HIVE_DASHBOARD_PORT) {
    $Port = [int]$env:HIVE_DASHBOARD_PORT
}

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir   = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$Service   = Join-Path $ScriptDir 'dashboard-service.cmd'
$TaskName  = 'HiveDashboard'

if (-not (Test-Path $Service)) {
    Write-Error "找不到 $Service"
    exit 1
}

function Remove-HiveTask {
    $existing = & schtasks /query /tn $TaskName 2>$null
    if ($LASTEXITCODE -eq 0) {
        & schtasks /delete /tn $TaskName /f
        Write-Host "已删除计划任务：$TaskName"
    } else {
        Write-Host "计划任务 $TaskName 不存在，无需删除"
    }
}

if ($Uninstall) {
    # 先停掉正在跑的实例，再删任务
    & $Service stop | Out-Host
    Remove-HiveTask
    Write-Host "卸载完成。"
    exit 0
}

Remove-HiveTask

# 计划任务执行的命令：隐藏窗口拉起看板
$tr = "cmd.exe /c `"$Service`" start $Port"

if ($RunAsSystem) {
    # 开机即启动（未登录也跑），需要管理员
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Write-Error "-RunAsSystem 需要管理员权限，请以管理员身份运行 PowerShell。"
        exit 1
    }
    & schtasks /create /tn $TaskName /tr $tr /sc onstart /ru SYSTEM /rl HIGHEST /f
} else {
    & schtasks /create /tn $TaskName /tr $tr /sc onlogon /f
}

if ($LASTEXITCODE -ne 0) {
    Write-Error "创建计划任务失败（schtasks 退出码 $LASTEXITCODE）"
    exit 1
}
Write-Host "已创建计划任务：$TaskName（触发：$(
    if ($RunAsSystem) { '开机 onstart' } else { '用户登录 onlogon' })）"

if (-not $NoStart) {
    $env:PORT = "$Port"
    $env:INTERVAL = "$Interval"
    & $Service start $Port
}

Write-Host ""
Write-Host "访问：http://127.0.0.1:$Port/"
Write-Host "管理：scripts\dashboard-service.cmd {start|stop|restart|status}"
Write-Host "卸载：powershell -File scripts\install-autostart.ps1 -Uninstall"
