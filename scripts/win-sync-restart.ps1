<#
  在 Windows 节点上「同步 hive 代码 + 重启集群服务」。

  默认只体检、不改任何东西；加 -Apply 才真的执行。
  加 -EnableRemote 顺手开 OpenSSH Server（只放行内网），之后 Linux 主控
  就能直接 ssh 过来同步/重启，不用每次到这台机器上点。

  用法（PowerShell，普通用户即可；-EnableRemote 需要管理员）：
    powershell -ExecutionPolicy Bypass -File .\win-sync-restart.ps1
    powershell -ExecutionPolicy Bypass -File .\win-sync-restart.ps1 -Apply
    powershell -ExecutionPolicy Bypass -File .\win-sync-restart.ps1 -Apply -EnableRemote

  找不到路径时用 -HiveDir / -CoordDir 手工指定：
    ... -Apply -HiveDir C:\path\to\hive -CoordDir C:\path\to\coord-repo
#>
param(
    [switch]$Apply,
    [switch]$EnableRemote,
    [string]$HiveDir = "",
    [string]$CoordDir = ""
)

$ErrorActionPreference = 'Stop'

function Find-NodeProcess {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and $_.CommandLine -match 'hive(_cluster)?\.py' -and
            $_.CommandLine -match 'cluster'
        } | Select-Object -First 1
}

function Resolve-HivePy($proc, $hint) {
    if ($hint) {
        $p = Join-Path $hint 'hive.py'
        if (Test-Path $p) { return (Resolve-Path $p).Path }
    }
    if ($proc) {
        $m = [regex]::Match($proc.CommandLine, '(?i)([A-Za-z]:\\[^"]*?hive\.py)')
        if ($m.Success -and (Test-Path $m.Groups[1].Value)) {
            return $m.Groups[1].Value
        }
    }
    # 兜底：在用户目录下浅层找 hive.py（深度 4，别扫全盘）
    $roots = @($env:USERPROFILE, 'C:\dswork', 'D:\dswork') | Where-Object { $_ -and (Test-Path $_) }
    foreach ($r in $roots) {
        $hit = Get-ChildItem -Path $r -Filter 'hive.py' -File -Recurse -Depth 4 -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return ""
}

function Resolve-CoordDir($proc, $hint) {
    if ($hint) { return $hint }
    if ($proc) {
        $m = [regex]::Match($proc.CommandLine, '(?i)--dir\s+"?([^"\s]+)"?')
        if ($m.Success) { return $m.Groups[1].Value }
    }
    if ($env:HIVE_DIR) { return $env:HIVE_DIR }
    return (Get-Location).Path
}

# --- 体检 -------------------------------------------------------------------
$proc = Find-NodeProcess
$py = ""
if ($proc -and $proc.ExecutablePath) { $py = $proc.ExecutablePath }
if (-not $py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
$hivePy = Resolve-HivePy $proc $HiveDir
$coord = Resolve-CoordDir $proc $CoordDir
$repo = if ($hivePy) { Split-Path $hivePy -Parent } else { "" }

Write-Host "=== hive Windows 节点体检 ===" -ForegroundColor Cyan
Write-Host ("  节点进程 : " + $(if ($proc) { "PID $($proc.ProcessId)" } else { "未找到（可能已停）" }))
Write-Host ("  python   : " + $(if ($py) { $py } else { "未找到" }))
Write-Host ("  hive.py  : " + $(if ($hivePy) { $hivePy } else { "未找到" }))
Write-Host ("  协调仓   : " + $coord)
if ($repo) {
    $head = (git -C $repo rev-parse --short HEAD 2>$null)
    $dirty = (git -C $repo status --porcelain 2>$null)
    Write-Host ("  当前 HEAD: " + $head + $(if ($dirty) { "（工作区有改动）" } else { "（干净）" }))
}
try {
    $h = Invoke-RestMethod 'http://127.0.0.1:8788/api/cluster/health' -TimeoutSec 3
    Write-Host ("  服务状态 : role=$($h.role) term=$($h.term) leader=$($h.leader.name) peers=$($h.peers)")
} catch {
    Write-Warning "本机 8788 探活失败：$($_.Exception.Message)"
}

if (-not $Apply) {
    Write-Host "`n只体检，未改动。确认上面路径无误后加 -Apply 执行。" -ForegroundColor Yellow
    return
}
if (-not $py -or -not $hivePy) {
    throw "没定位到 python 或 hive.py，请用 -HiveDir / -CoordDir 手工指定后重跑"
}
if ($dirty) {
    throw "hive 仓库工作区有未提交改动，先处理（git status）再同步，避免 pull 撞车"
}

# --- 1) 同步代码 ------------------------------------------------------------
Write-Host "`n=== 1) 同步 hive 代码 ===" -ForegroundColor Cyan
$before = (git -C $repo rev-parse --short HEAD)
git -C $repo fetch --all --quiet
if ($LASTEXITCODE -ne 0) { throw "git fetch 失败（检查远端/网络）" }
git -C $repo pull --ff-only
if ($LASTEXITCODE -ne 0) { throw "git pull --ff-only 失败（本地有分叉？先 git status 看）" }
$after = (git -C $repo rev-parse --short HEAD)
Write-Host "  $before -> $after"

# --- 2) 重启节点（用官方 restart：读 config.json、detached 拉起、自检健康）----
Write-Host "`n=== 2) 重启集群服务 ===" -ForegroundColor Cyan
& $py $hivePy '--dir' $coord 'cluster' 'restart'
if ($LASTEXITCODE -ne 0) { throw "cluster restart 返回 $LASTEXITCODE，看 $env:USERPROFILE\.hive\cluster\ 下的日志" }

Write-Host "`n=== 3) 探活 ===" -ForegroundColor Cyan
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        $h = Invoke-RestMethod 'http://127.0.0.1:8788/api/cluster/health' -TimeoutSec 3
        if ($h.ok) {
            Write-Host ("  已就绪：role=$($h.role) term=$($h.term) leader=$($h.leader.name) peers=$($h.peers)") -ForegroundColor Green
            $ok = $true
            break
        }
    } catch { }
    Start-Sleep -Seconds 1
}
if (-not $ok) { Write-Warning "20 秒内没探活成功，请看日志：hive cluster status" }

# --- 4) 可选：开远程通道 -----------------------------------------------------
if ($EnableRemote) {
    Write-Host "`n=== 4) 开启 OpenSSH Server（仅放行 192.168.0.0/24）===" -ForegroundColor Cyan
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "-EnableRemote 需要「以管理员身份运行」PowerShell"
    }

    $installed = Get-WindowsCapability -Online -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -like 'OpenSSH.Server*' -and $_.State -eq 'Installed' }
    if (-not $installed) {
        Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
    }
    Set-Service -Name sshd -StartupType Automatic
    Start-Service sshd
    if (-not (Get-NetFirewallRule -Name 'hive-sshd' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'hive-sshd' -DisplayName 'hive sshd (LAN only)' `
            -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow `
            -RemoteAddress '192.168.0.0/24' | Out-Null
    }
    $keys = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
    Write-Host "  sshd 已启动，22 端口只对内网开放。" -ForegroundColor Green
    Write-Host "  最后一步：把主控的公钥追加到 $keys" -ForegroundColor Yellow
    Write-Host "  之后主控即可：ssh skywalk@192.168.0.104 'cd <hive> && git pull && python hive.py cluster restart'"
}

Write-Host "`n完成。" -ForegroundColor Green
