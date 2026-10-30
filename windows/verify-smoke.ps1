#requires -Version 5.1
<#
.SYNOPSIS
    Official "verified on Windows" baseline for hive's verify mode.

.DESCRIPTION
    Builds a throw-away world (bare code repo + coordination repo + one
    verify-mode agent), runs real hive commands on this machine, and asserts
    the outcome. No network, no LLM, no MCP client needed.

    What it proves on a real Windows box:
      1. git/python toolchain works with the pinned UTF-8 pipeline
         (verify output with CJK characters round-trips through result JSON);
      2. verify-mode worker claims, runs verify, writes res/<tid>, and does
         NOT create a task/<tid> code branch (read-only verification);
      3. a red verify yields status=failed with the exit code recorded;
      4. lease fencing: a stale worker that lost its lease is rejected
         (LeaseFenced) instead of overwriting the current holder;
      5. close archives the result and cleans claim/res branches.

    Exit code: 0 = every check passed, 1 = at least one failed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File windows\verify-smoke.ps1
    powershell -ExecutionPolicy Bypass -File windows\verify-smoke.ps1 -Keep
#>
[CmdletBinding()]
param(
    [string]$Repo = "",
    [string]$Python = "python",
    [string]$Work = "",
    [switch]$Keep
)

# $PSScriptRoot is empty while a param() default is being bound in Windows
# PowerShell 5.1, so resolve the repo root here instead.
if (-not $Repo) {
    $Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

$ErrorActionPreference = "Stop"
$checks = New-Object System.Collections.ArrayList
$script:errFile = Join-Path $env:TEMP ("hive-smoke-err-" + [guid]::NewGuid().ToString("N") + ".log")
$script:nativeExit = 0

function Check([string]$name, [bool]$ok, [string]$detail) {
    $null = $checks.Add([pscustomobject]@{ Name = $name; Ok = $ok; Detail = $detail })
    $tag = if ($ok) { "PASS" } else { "FAIL" }
    Write-Host ("[{0}] {1}: {2}" -f $tag, $name, $detail)
}

function Invoke-Native {
    <#
      Windows PowerShell 5.1 turns a native program's *stderr* lines into
      ErrorRecords when piping `2>&1`; with $ErrorActionPreference="Stop"
      even a perfectly successful `git push` (progress goes to stderr) would
      throw NativeCommandError. So: redirect stderr to a temp file, drop to
      "Continue" only around the call, and decide by $LASTEXITCODE.

      NOTE: the trailing args are received as one array and re-expanded here.
      Do NOT name the trailing parameter $Args: that is a PowerShell automatic
      variable and binding into it silently swallows the arguments.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(ValueFromRemainingArguments = $true)]$NativeArgs
    )
    # ValueFromRemainingArguments 把"传进来的一整个数组"再套一层：需要拍平，
    # 否则 ['hive.py','-V'] 会被 [string] 化成 "hive.py -V" 当成单个参数。
    $flat = New-Object System.Collections.ArrayList
    foreach ($item in @($NativeArgs)) {
        foreach ($leaf in @($item)) { $null = $flat.Add([string]$leaf) }
    }
    [string[]]$argList = $flat.ToArray()
    if (Test-Path $script:errFile) { Remove-Item $script:errFile -Force -ErrorAction SilentlyContinue }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # @argList 展开成一个一个参数，而不是把整个数组当成一个字符串
        $out = & $Exe @argList 2> $script:errFile
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prev }
    $err = ""
    if (Test-Path $script:errFile) {
        $err = (Get-Content $script:errFile -Raw -ErrorAction SilentlyContinue)
        if ($null -eq $err) { $err = "" }
    }
    return [pscustomobject]@{
        Code = $code
        Out = (($out | Out-String).Trim())
        Err = ([string]$err).Trim()
    }
}

function Run-Hive([string]$dir, [string[]]$hiveArgs) {
    $all = @($hivePy, "--dir", $dir) + @($hiveArgs)
    return & Invoke-Native $Python @all
}

function Show-Ref([string]$gitDir, [string]$ref, [string]$path) {
    $r = & Invoke-Native git @("--git-dir", $gitDir, "show", ("{0}:{1}" -f $ref, $path))
    if ($r.Code -ne 0) { return $null }
    return $r.Out
}

$hivePy = Join-Path $Repo "hive.py"
Write-Host "=== hive Windows verify baseline ==="
Write-Host ("repo   : " + $Repo)
Write-Host ("python : " + $Python)
Write-Host ("host   : " + $env:COMPUTERNAME + "  os=" + [System.Environment]::OSVersion.VersionString)

if (-not (Test-Path $hivePy)) {
    Write-Host "FAIL: hive.py not found at $hivePy"
    exit 1
}

if (-not $Work) {
    $Work = Join-Path $env:TEMP ("hive-win-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
}
New-Item -ItemType Directory -Force -Path $Work | Out-Null
Write-Host ("work   : " + $Work)

# --- isolated environment: no cluster, no store, local lease dir ------------
$env:HIVE_CLAIM_BACKEND = "git"
$env:HIVE_STORE = "0"
$env:HIVE_CLUSTER_AUTOSTART = "0"
$env:HIVE_LEASE_DIR = (Join-Path $Work "leases")
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
Remove-Item Env:\HIVE_STORE_PATH -ErrorAction SilentlyContinue
$env:GIT_AUTHOR_NAME = "t"; $env:GIT_AUTHOR_EMAIL = "t@t"
$env:GIT_COMMITTER_NAME = "t"; $env:GIT_COMMITTER_EMAIL = "t@t"

# Standalone check script: avoids the PS 5.1 embedded-double-quote mangling
# that `--verify "python -c \"...\""` hits, and keeps the UTF-8 case honest.
$checksPy = Join-Path $Work "vcheck.py"
$checksSrc = @'
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "utf8"
if mode == "utf8":
    # CJK must survive verify -> result JSON -> git show on a cp936 console box
    print("cjk-ok 中文验收")
elif mode == "win32":
    if sys.platform != "win32":
        print("not windows: " + sys.platform)
        sys.exit(1)
    print("win32 ok")
elif mode == "fail3":
    print("intentional failure")
    sys.exit(3)
else:
    print("unknown mode")
    sys.exit(2)
'@
[System.IO.File]::WriteAllText($checksPy, $checksSrc, (New-Object System.Text.UTF8Encoding($false)))

try {
    # 1. toolchain ---------------------------------------------------------
    $pyVer = & Invoke-Native $Python @("-V")
    Check "python" ($pyVer.Code -eq 0 -and $pyVer.Out -like "Python 3.*") $pyVer.Out
    $gitVer = & Invoke-Native git @("--version")
    Check "git" ($gitVer.Code -eq 0 -and $gitVer.Out -like "git version*") $gitVer.Out
    $hiveVer = & Invoke-Native $Python @($hivePy, "-V")
    Check "hive" ($hiveVer.Code -eq 0 -and $hiveVer.Out -match "^\d") $hiveVer.Out

    # 2. world -------------------------------------------------------------
    $codeOrigin = Join-Path $Work "light.git"
    $seed = Join-Path $Work "light-seed"
    $null = & Invoke-Native git @("init", "--bare", "-b", "main", $codeOrigin)
    $null = & Invoke-Native git @("init", "-b", "main", $seed)
    Set-Content -Path (Join-Path $seed "README.md") -Value "# light" -Encoding ASCII
    $null = & Invoke-Native git @("-C", $seed, "add", "-A")
    $null = & Invoke-Native git @("-C", $seed, "commit", "-m", "init")
    $null = & Invoke-Native git @("-C", $seed, "remote", "add", "origin", $codeOrigin)
    $null = & Invoke-Native git @("-C", $seed, "push", "-u", "origin", "main")
    Check "world" (Test-Path (Join-Path $codeOrigin "HEAD")) "bare code repo + seed pushed"

    $hiveOrigin = Join-Path $Work "hive.git"
    $hive1 = Join-Path $Work "hive-a1"
    $hive2 = Join-Path $Work "hive-a2"
    $null = & Invoke-Native git @("init", "--bare", "-b", "main", $hiveOrigin)
    $r = Run-Hive $hive1 @("init", "--remote", $hiveOrigin)
    Check "hive init" ($r.Code -eq 0) (($r.Out -split "`n")[-1])
    $null = & Invoke-Native git @("clone", "-q", $hiveOrigin, $hive2)

    $code = Join-Path $Work "light-a3"
    $null = & Invoke-Native git @("clone", "-q", $codeOrigin, $code)
    $r = Run-Hive $hive1 @("agent", "add", "--name", "win-verify", "--mode", "verify",
                           "--abilities", "verify", "--capabilities", "windows",
                           "--host", $env:COMPUTERNAME, "--repo", ("light=" + $code))
    Check "agent add (verify)" ($r.Code -eq 0) ("win-verify / mode=verify / host=" + $env:COMPUTERNAME)

    # 3. green verify card --------------------------------------------------
    # 故意混用 POSIX 惯用法与 Windows 命令：`true` 与 `test -f` 在 cmd.exe 里不存在，
    # 由 hive_compat 垫片转义；这证明「同一张卡跨平台可验」，而不是各写各的。
    $r = Run-Hive $hive1 @("task", "new", "--id", "T-9001", "--title", "windows verify smoke",
                           "--repo", "light",
                           "--verify", ($Python + " `"$checksPy`" utf8"),
                           "--verify", ($Python + " `"$checksPy`" win32"),
                           "--verify", "cmd.exe /c ver",
                           "--verify", "true",
                           "--verify", "test -f README.md",
                           "--body", "Verify-mode baseline card; read-only.")
    Check "task new" ($r.Code -eq 0) ("T-9001 with 5 verify commands" + $(if ($r.Code) { " -> " + $r.Err } else { "" }))
    $r = Run-Hive $hive1 @("assign", "T-9001", "--agent", "win-verify")
    Check "assign" ($r.Code -eq 0) ("T-9001 -> win-verify" + $(if ($r.Code) { " -> " + $r.Err } else { "" }))

    $r = Run-Hive $hive1 @("work", "--agent", "win-verify", "--once",
                           "--state-dir", (Join-Path $Work "state-win-verify"))
    Check "work --once (verify)" ($r.Code -eq 0) (($r.Out -split "`n")[-1])

    $resRaw = Show-Ref $hiveOrigin "res/T-9001" "results/T-9001.json"
    $okRes = $false; $detail = "no res branch"
    if ($resRaw) {
        $res = $resRaw | ConvertFrom-Json
        $codes = (($res.verify) | ForEach-Object { $_.code }) -join ","
        $okRes = ($res.status -eq "done") -and ($res.mode -eq "verify") -and ($codes -eq "0,0,0,0,0")
        $detail = "status=$($res.status) mode=$($res.mode) codes=$codes duration=$($res.duration_s)s"
    }
    Check "verify result done" $okRes $detail

    # UTF-8: CJK must survive `git show` of the result JSON
    $utf8Ok = $false
    if ($resRaw) {
        $needle = [string][char]0x4E2D + [string][char]0x6587
        $utf8Ok = $resRaw.Contains($needle)
    }
    Check "UTF-8 round-trip" $utf8Ok "result tail contains 中文"

    $tb = & Invoke-Native git @("--git-dir", $codeOrigin, "for-each-ref",
                                "--format=%(refname)", "refs/heads/task/T-9001")
    Check "no code branch" ($tb.Out -eq "") "verify mode is read-only"

    $rowJson = (& Invoke-Native $Python @($hivePy, "--dir", $hive1, "status", "--json")).Out
    $row = ($rowJson | ConvertFrom-Json | Where-Object { $_.id -eq "T-9001" })
    Check "status row" ($row.result -eq "done" -and [int]$row.fence -ge 1) `
        ("result=" + $row.result + " fence=" + $row.fence + " result_fence=" + $row.result_fence)

    # 4. red verify card: failure captured, not swallowed -------------------
    $r = Run-Hive $hive1 @("task", "new", "--id", "T-9002", "--title", "windows red verify",
                           "--repo", "light",
                           "--verify", ($Python + " `"$checksPy`" fail3"),
                           "--body", "Must fail with code 3.")
    $null = Run-Hive $hive1 @("assign", "T-9002", "--agent", "win-verify")
    $null = Run-Hive $hive1 @("work", "--agent", "win-verify", "--once",
                              "--state-dir", (Join-Path $Work "state-win-verify"))
    $redRaw = Show-Ref $hiveOrigin "res/T-9002" "results/T-9002.json"
    $redOk = $false; $redDetail = "no res branch"
    if ($redRaw) {
        $red = $redRaw | ConvertFrom-Json
        $redOk = ($red.status -eq "failed") -and ([int]$red.verify[0].code -eq 3)
        $redDetail = "status=$($red.status) code=$($red.verify[0].code) error=$($red.error)"
    }
    Check "red verify captured" $redOk $redDetail

    # 5. lease fencing on a real machine ------------------------------------
    $null = Run-Hive $hive1 @("agent", "add", "--name", "win-other", "--mode", "verify",
                              "--repo", ("light=" + $code), "--host", $env:COMPUTERNAME)
    $r = Run-Hive $hive1 @("task", "new", "--id", "T-9003", "--title", "fencing smoke",
                           "--repo", "light", "--verify", "cmd.exe /c exit 0",
                           "--body", "Claim, take over, then stale write must be refused.")
    $null = Run-Hive $hive1 @("assign", "T-9003", "--agent", "win-verify")
    $null = Run-Hive $hive1 @("claim", "T-9003", "--agent", "win-verify")
    $leaseList = (& Invoke-Native $Python @($hivePy, "--dir", $hive1, "lease", "list", "--json")).Out
    $staleLease = $null
    if ($leaseList) {
        try { $staleLease = ($leaseList | ConvertFrom-Json)."T-9003" } catch { $staleLease = $null }
    }
    Check "local lease kept" ($null -ne $staleLease -and $staleLease.lease_id) `
        "lease_id/fence persisted under HIVE_LEASE_DIR"

    $null = Run-Hive $hive1 @("unassign", "T-9003", "--reason", "smoke takeover")
    $null = Run-Hive $hive1 @("retry", "T-9003", "--agent", "win-other")
    $null = Run-Hive $hive1 @("claim", "T-9003", "--agent", "win-other")

    $fenceProbe = @'
import json, sys
src, repo, tid, stale_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, src)
import hive
stale = json.load(open(stale_path, encoding="utf-8"))
w = hive.Worker(repo, "win-verify")
w._lease[tid] = stale
hive.save_local_lease(tid, stale)
try:
    w._write_result(tid, {"task": tid, "status": "done", "agent": "win-verify", "verify": []})
except hive.LeaseFenced as exc:
    print("FENCED " + str(exc))
    sys.exit(0)
print("NOT FENCED")
sys.exit(1)
'@
    $probePath = Join-Path $Work "fence_probe.py"
    [System.IO.File]::WriteAllText($probePath, $fenceProbe, (New-Object System.Text.UTF8Encoding($false)))
    # 走临时文件而不是命令行参数：PS 5.1 用双引号包参数时会把内层引号吃掉，
    # JSON 变成 {task:...} 就解析失败（NativeCommandError 只是噪音）。
    $staleJsonPath = Join-Path $Work "stale_lease.json"
    [System.IO.File]::WriteAllText($staleJsonPath, ($staleLease | ConvertTo-Json -Depth 6),
                                   (New-Object System.Text.UTF8Encoding($false)))
    $probe = & Invoke-Native $Python @($probePath, $Repo, $hive1, "T-9003", $staleJsonPath)
    $probeBits = if ($probe.Out) { $probe.Out } else { $probe.Err }
    Check "stale write fenced" ($probe.Code -eq 0 -and $probe.Out.StartsWith("FENCED")) $probeBits

    # 6. close + archive ----------------------------------------------------
    $r = Run-Hive $hive1 @("close", "T-9001", "--note", "windows baseline ok")
    $archived = Show-Ref $hiveOrigin "main" "results/T-9001.json"
    $closeOk = $false; $closeDetail = "no archived result"
    if ($archived) {
        $a = $archived | ConvertFrom-Json
        $closeOk = ($a.status -eq "done")
        $closeDetail = "archived status=$($a.status)"
    }
    $lr = & Invoke-Native git @("--git-dir", $hiveOrigin, "for-each-ref",
                                "--format=%(refname)", "refs/heads/claim/T-9001",
                                "refs/heads/res/T-9001")
    Check "close + archive" ($r.Code -eq 0 -and $closeOk -and $lr.Out -eq "") `
        ($closeDetail + $(if ($lr.Out) { " left=" + $lr.Out } else { "" }))
}
catch {
    Check "unexpected error" $false ($_.Exception.Message + " @ line " + $_.InvocationInfo.ScriptLineNumber)
}
finally {
    if (-not $Keep) { Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue }
    else { Write-Host ("kept: " + $Work) }
    Remove-Item $script:errFile -Force -ErrorAction SilentlyContinue
}

$failed = @($checks | Where-Object { -not $_.Ok })
Write-Host ""
Write-Host ("=== summary: {0}/{1} passed ===" -f ($checks.Count - $failed.Count), $checks.Count)
if ($failed.Count -gt 0) {
    foreach ($f in $failed) { Write-Host ("FAILED " + $f.Name + " -> " + $f.Detail) }
    exit 1
}
Write-Host "PASS: hive verify mode is verified on this Windows machine."
exit 0
