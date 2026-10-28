<#
.SYNOPSIS
  把一张任务卡的派发指令送进已打开的 agent 窗口：切窗口 -> 粘贴 -> 回车 -> 回 hive 记账。

.DESCRIPTION
  只自动化「派发」这一步，不碰「执行」：发完回车脚本就结束，不等 agent 回复、不读 chat。

  默认 dry-run：不加 -Apply 绝不发送任何按键，只打印目标窗口、正文前 5 行，
  以及将要执行的 dispatch-mark 命令行。任一步失败即中止，且不标记 dispatched。

  脚本里不含任何具体 agent 名与窗口标题：窗口从 -Config 指向的配置表读，
  配置表形态见同目录 dispatch-agents.sample.json。

.PARAMETER Task
  任务号，形如 T-0187。

.PARAMETER Agent
  配置表里的 agent 键名。省略时按配置顺序取第一个「窗口已打开」的 agent。

.PARAMETER Config
  配置表路径，默认 <脚本目录>\dispatch-agents.json。

.PARAMETER HiveDir
  hive 仓路径（作为 hive.py 的 --dir），默认脚本目录的上一级。

.PARAMETER Apply
  真正发送。不加就是 dry-run。

.PARAMETER SendDelayMs
  每次动作之间的等待毫秒数（等窗口真的到前台、等剪贴板真的写好）。

.NOTES
  依赖的 CLI 契约（由 T-0188 接线，本脚本按契约调用）：

    python hive.py --dir <hive> prompt <T-xxxx> --agent <agent> --format json
    python hive.py --dir <hive> dispatch-mark <T-xxxx> --agent <agent>

  契约不满足（子命令不存在 / JSON 里没有 text）时显式报错退出，不静默失败。
  解释器可用环境变量 HIVE_PYTHON 覆盖，默认取 PATH 上的 python。

  退出码：0 成功（含 dry-run）；1 参数/配置/hive.py 问题；2 prompt 契约未满足；
  3 窗口没找到或前台窗口不匹配；4 派发已发生但 dispatch-mark 失败。
#>

param(
  [Parameter(Mandatory=$true)][string]$Task,
  [string]$Agent,
  [string]$Config,
  [string]$HiveDir,
  [switch]$Apply,          # 不加就是 dry-run
  [int]$SendDelayMs = 800
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# 脚本目录相关的默认值不能写在 param 里：带 [Parameter(...)] 的 param 块会在
# $PSScriptRoot 赋值之前求值默认值，实测 5.1 下 `(Resolve-Path "$PSScriptRoot\..")`
# 会算成 `C:\`（$PSScriptRoot 是空串），脚本于是去 C:\ 找 hive.py。挪到这里才可靠。
$scriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptDir)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($Config)) {
    $Config = Join-Path $scriptDir 'dispatch-agents.json'
}
if ([string]::IsNullOrWhiteSpace($HiveDir)) {
    $HiveDir = (Resolve-Path -LiteralPath (Join-Path $scriptDir '..')).Path
}

# Windows PowerShell 5.1 的控制台默认是 OEM 代码页（cp936/cp437），中文提示会变问号。
# 钉成无 BOM 的 UTF-8：输出侧不乱码，同时不往管道里塞 BOM 字节。
try {
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
} catch {
    # 没有真实控制台（被计划任务/服务拉起）时设不上，不致命
}

# 子进程也钉成 UTF-8：python 默认按 locale（cp936）写管道，中文正文读回来就是乱码。
# 用进程级环境变量传递，而不是 ProcessStartInfo.EnvironmentVariables ——
# 后者在 5.1 下取到 null（实测「无法对 Null 数组进行索引」）。
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

function Write-Step([string]$Message) {
    Write-Host "[hive-dispatch] $Message"
}

function Fail([string]$Message, [int]$Code = 1) {
    # 不用 Write-Error：$ErrorActionPreference = 'Stop' 下它会把「报错」变成「抛异常」，
    # 异常栈对使用者是噪音，这里只要一句人话 + 非零退出码。
    [Console]::Error.WriteLine("[hive-dispatch] 中止：$Message")
    exit $Code
}

function Resolve-PythonExe {
    $exe = $env:HIVE_PYTHON
    if ([string]::IsNullOrWhiteSpace($exe)) { $exe = 'python' }
    $cmd = Get-Command $exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Fail "找不到 Python 解释器「$exe」；用环境变量 HIVE_PYTHON 指定绝对路径"
    }
    return $cmd.Source
}

function ConvertTo-ArgumentString([string[]]$Arguments) {
    # 最小转义：含空白或引号的参数包双引号。路径里出现引号本身是异常，不在这里兜。
    ($Arguments | ForEach-Object {
        $a = [string]$_
        if ($a -eq '' -or $a -match '[\s"]') { '"' + $a + '"' } else { $a }
    }) -join ' '
}

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    # 显式用 Process 而不是 & 调用：stdout/stderr 的编码由我们指定，
    # 不跟着控制台代码页走（否则中文正文一过管道就成乱码）。
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = ConvertTo-ArgumentString $Arguments
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $decoder = [System.Text.UTF8Encoding]::new($false)
    $psi.StandardOutputEncoding = $decoder
    $psi.StandardErrorEncoding = $decoder

    $proc = [System.Diagnostics.Process]::Start($psi)
    $out = $proc.StandardOutput.ReadToEnd()
    $err = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    return [pscustomobject]@{ Code = $proc.ExitCode; Out = $out; Err = $err }
}

function Read-AgentConfig([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Fail @"
配置表不存在：$Path
按同目录 dispatch-agents.sample.json 的形态建一份（键是 agent 名，值是窗口标题片段 + hive 里的 agent 名）：
{ "<agent名>": { "window_title": "<窗口标题片段>", "hive_agent": "<hive agent 名>" } }
"@
    }
    $raw = [System.IO.File]::ReadAllText($Path, [System.Text.UTF8Encoding]::new($false))
    try {
        $obj = $raw | ConvertFrom-Json
    } catch {
        Fail "配置表不是合法 JSON：$Path`n$($_.Exception.Message)"
    }
    if ($null -eq $obj) { Fail "配置表是空的：$Path" }

    $entries = @()
    foreach ($prop in $obj.PSObject.Properties) {
        $title = [string]$prop.Value.window_title
        $hiveAgent = [string]$prop.Value.hive_agent
        if ([string]::IsNullOrWhiteSpace($title)) { Fail "配置项「$($prop.Name)」缺 window_title" }
        if ([string]::IsNullOrWhiteSpace($hiveAgent)) { Fail "配置项「$($prop.Name)」缺 hive_agent" }
        $entries += [pscustomobject]@{
            Name        = $prop.Name
            WindowTitle = $title
            HiveAgent   = $hiveAgent
        }
    }
    if ($entries.Count -eq 0) { Fail "配置表里没有任何 agent：$Path" }
    return $entries
}

function Find-WindowByTitle([string]$Fragment) {
    Get-Process | Where-Object {
        $_.MainWindowTitle -and
        $_.MainWindowTitle.IndexOf($Fragment, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    } | Select-Object -First 1
}

function Initialize-WindowApi {
    if ('HiveDispatch.Win32' -as [type]) { return $true }
    $code = @'
using System;
using System.Text;
using System.Runtime.InteropServices;

namespace HiveDispatch {
    public static class Win32 {
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int maxCount);

        public static string ForegroundTitle() {
            StringBuilder sb = new StringBuilder(1024);
            GetWindowText(GetForegroundWindow(), sb, sb.Capacity);
            return sb.ToString();
        }
    }
}
'@
    try {
        Add-Type -TypeDefinition $code -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Set-WindowForeground($WindowProcess) {
    if (Initialize-WindowApi) {
        $handle = $WindowProcess.MainWindowHandle
        if ($handle -eq [IntPtr]::Zero) { return $false }
        # 9 = SW_RESTORE：最小化的窗口先还原，否则 SetForegroundWindow 只亮任务栏
        [void][HiveDispatch.Win32]::ShowWindow($handle, 9)
        return [bool][HiveDispatch.Win32]::SetForegroundWindow($handle)
    }
    # 兜底：没有 Win32 时用 AppActivate（返回是否成功），复核交给调用方
    $shell = New-Object -ComObject WScript.Shell
    return [bool]$shell.AppActivate($WindowProcess.Id)
}

function Get-ForegroundWindowTitle {
    if (Initialize-WindowApi) { return [HiveDispatch.Win32]::ForegroundTitle() }
    # 拿不到前台标题就返回空串：调用方的匹配必然失败 -> 中止发送。
    # 「复核不了」和「复核不通过」在这里是同一个结论：不发。
    return ''
}

# --------------------------------------------------------------------------- #
# 1. 任务号形态
# --------------------------------------------------------------------------- #

if ($Task -notmatch '^T-\d{4}$') {
    Fail "任务号形态不对：$Task（应形如 T-0187）"
}

$HiveDir = (Resolve-Path -LiteralPath $HiveDir).Path
$hivePy = Join-Path $HiveDir 'hive.py'
if (-not (Test-Path -LiteralPath $hivePy -PathType Leaf)) {
    Fail "找不到 hive.py：$hivePy（用 -HiveDir 指定 hive 仓路径）"
}

# --------------------------------------------------------------------------- #
# 2. 配置表 + 目标 agent
# --------------------------------------------------------------------------- #

$entries = Read-AgentConfig -Path $Config

if ($Agent) {
    $target = $entries | Where-Object { $_.Name -eq $Agent } | Select-Object -First 1
    if (-not $target) {
        $names = ($entries | ForEach-Object { $_.Name }) -join ', '
        Fail "配置表里没有 agent「$Agent」；现有：$names"
    }
} else {
    $target = $null
    foreach ($entry in $entries) {
        if (Find-WindowByTitle -Fragment $entry.WindowTitle) { $target = $entry; break }
    }
    if (-not $target) {
        $candidates = ($entries | ForEach-Object { "$($_.Name)（$($_.WindowTitle)）" }) -join ', '
        Fail "没有找到任何已打开的 agent 窗口。候选：$candidates`n先把对应 agent 的窗口打开，或用 -Agent 显式指定" -Code 3
    }
}

$window = Find-WindowByTitle -Fragment $target.WindowTitle

# --------------------------------------------------------------------------- #
# 3. 取派发正文（CLI 契约：prompt --format json）
# --------------------------------------------------------------------------- #

$python = Resolve-PythonExe
$promptArgs = @(
    $hivePy,
    '--dir', $HiveDir,
    'prompt', $Task,
    '--agent', $target.HiveAgent,
    '--format', 'json'
)

Write-Step "取派发正文：$Task -> $($target.HiveAgent)"
$result = Invoke-Native -Exe $python -Arguments $promptArgs
if ($result.Code -ne 0) {
    [Console]::Error.WriteLine(@"
[hive-dispatch] 中止：hive prompt 失败（退出码 $($result.Code)）—— CLI 契约未满足。
[hive-dispatch] 命令：$python $($promptArgs -join ' ')
[hive-dispatch] stderr：
$($result.Err.Trim())
[hive-dispatch] stdout：
$($result.Out.Trim())
[hive-dispatch] 这条子命令由 T-0188 接线；未合入前派发流水线只能停在 dry-run。
"@)
    exit 2
}

$payload = $null
try {
    $payload = $result.Out | ConvertFrom-Json
} catch {
    Fail "prompt 的输出不是合法 JSON：`n$($result.Out)"
}
if ($null -eq $payload -or -not $payload.PSObject.Properties['text']) {
    Fail "prompt 的 JSON 里没有 text 字段：`n$($result.Out)"
}

$bodyText = [string]$payload.text
if ([string]::IsNullOrWhiteSpace($bodyText)) {
    Fail "prompt 返回的正文是空的，拒绝派发空指令"
}

$lines = $bodyText -split "\r?\n"
$preview = @($lines | Select-Object -First 5)

$markArgs = @(
    $hivePy,
    '--dir', $HiveDir,
    'dispatch-mark', $Task,
    '--agent', $target.HiveAgent
)
$markCommandLine = "$python $($markArgs -join ' ')"

# --------------------------------------------------------------------------- #
# 4. dry-run（默认路径，到此为止）
# --------------------------------------------------------------------------- #

if (-not $Apply) {
    Write-Host "[dry-run] 任务：$Task"
    Write-Host "[dry-run] 目标 agent：$($target.Name)（hive_agent=$($target.HiveAgent)）"
    Write-Host "[dry-run] 目标窗口标题片段：$($target.WindowTitle)"
    if ($window) {
        Write-Host "[dry-run] 已匹配窗口：PID $($window.Id) / $($window.ProcessName) / $($window.MainWindowTitle)"
    } else {
        Write-Host "[dry-run] 注意：当前没检测到该窗口（dry-run 不发送，故不中止）"
    }
    Write-Host "[dry-run] 正文前 5 行（共 $($lines.Count) 行）："
    $n = 0
    foreach ($line in $preview) {
        $n++
        Write-Host ("  {0}| {1}" -f $n, $line)
    }
    Write-Host "[dry-run] 将要执行的记账命令：$markCommandLine"
    Write-Host "[dry-run] 未加 -Apply，不发送任何按键。"
    exit 0
}

# --------------------------------------------------------------------------- #
# 5~7. 真发：激活 -> 复核前台 -> 粘贴 -> 回车 -> 记账
# --------------------------------------------------------------------------- #

if ($Apply) {
    if (-not $window) {
        Fail "agent「$($target.Name)」的窗口没打开（配置里的标题片段：$($target.WindowTitle)）" -Code 3
    }

    # 5. 激活窗口
    $activated = Set-WindowForeground -WindowProcess $window
    if (-not $activated) {
        Fail "激活窗口失败：PID $($window.Id)（$($window.MainWindowTitle)）" -Code 3
    }
    Start-Sleep -Milliseconds $SendDelayMs

    # 5'. 复核当前前台窗口 —— 这一步是防「粘错窗口」的唯一屏障，不能省
    $foregroundTitle = Get-ForegroundWindowTitle
    if ($foregroundTitle.IndexOf($target.WindowTitle, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
        Fail "前台窗口是「$foregroundTitle」，与配置「$($target.WindowTitle)」不匹配，不发送按键" -Code 3
    }
    Write-Step "已激活窗口：$foregroundTitle"

    # 6. 正文进剪贴板 -> 粘贴 -> 回车
    Set-Clipboard -Value $bodyText
    Start-Sleep -Milliseconds $SendDelayMs
    $keyboard = New-Object -ComObject WScript.Shell
    $keyboard.SendKeys('^v')
    Start-Sleep -Milliseconds $SendDelayMs
    $keyboard.SendKeys('{ENTER}')
    Write-Step "已发送：粘贴 + 回车（正文 $($lines.Count) 行）"

    # 7. 记账。派发已经发生，记账失败必须让人看见，不能悄悄吞掉。
    $mark = Invoke-Native -Exe $python -Arguments $markArgs
    if ($mark.Code -ne 0) {
        Write-Host "[hive-dispatch] 警告：派发已发生，但 dispatch-mark 失败（退出码 $($mark.Code)）——账没记上，请人工补。" -ForegroundColor Yellow
        Write-Host "[hive-dispatch] 命令：$markCommandLine"
        Write-Host $mark.Err.Trim()
        exit 4
    }
    Write-Step "已标记 dispatched：$Task -> $($target.HiveAgent)"
    exit 0
}

exit 0
