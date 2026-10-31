#Requires AutoHotkey v2.0
#SingleInstance Force

; ============================================================================
; 蜂巢 Hive 看板 —— Windows 系统托盘（T-0208）
;
; 常驻视图，不占窗口：只在托盘放一个图标 + 一个菜单。「状态」那一项是活的：
;   状态：运行中 · 主节点 <leader> · 在跑 <n> 张
;   状态：未运行
; 数据全部来自看板自己的 HTTP 接口（只读，不发任何会改状态的东西）：
;   * GET /api/cluster/health —— 取 leader.name（主节点），取不到就退到 node.name
;   * GET /api/status         —— 数 tasks 里 state == "running" 的条数
; 两个接口都取不到时显示「未运行」：不弹错误框、不写日志，托盘只报状态。
;
; 启动 / 停止一律委托给 scripts\install-autostart.ps1（单实例入口）——
; 托盘这层绝不自己杀进程、绝不自己拉第二个看板。两处实现一旦分叉，
; 「为什么同时有两个看板」就没人查得出来是谁干的。
;
; 前提：AutoHotkey v2（winget install AutoHotkey.AutoHotkey）。
;       本文件是 UTF-8 带 BOM：AHK v2 对无 BOM 的非 ASCII 源文件按 ANSI 读，中文会乱码。
; ============================================================================

Persistent(true)

AppName := "Hive 看板"

Port := EnvGet("HIVE_DASHBOARD_PORT")
if (Port = "")
    Port := "8787"

BaseUrl := "http://127.0.0.1:" Port "/"
StatusUrl := BaseUrl "api/status"
ClusterUrl := BaseUrl "api/cluster/health"

; 仓库根 = 本脚本所在目录的上一级（脚本就在 windows\ 下）
RepoDir := RegExReplace(A_ScriptDir, "\\windows$", "")
InstallPs1 := RepoDir "\scripts\install-autostart.ps1"

UserProfile := EnvGet("USERPROFILE")
LogDir := UserProfile "\.hive\logs"
LogFile := LogDir "\hive-" FormatTime(A_Now, "yyyyMMdd") ".log"

; 托盘图标悬停提示
A_IconTip := AppName

; 上次渲染出来的状态文案：变了才重建菜单，避免每 5 秒闪一次
LastStatusText := ""

RefreshTray()
SetTimer(RefreshTray, 5000)

; --- 状态采集（只读） ------------------------------------------------------ #

HttpGet(url, timeout := 1500) {
    ; 用系统自带的 WinHTTP，不引任何第三方组件；任何异常都吞掉并返回空串。
    try {
        whr := ComObject("WinHttp.WinHttpRequest.5.1")
        whr.Open("GET", url, false)
        whr.SetTimeouts(timeout, timeout, timeout, timeout)
        whr.Send()
        if (whr.Status = 200)
            return whr.ResponseText
    } catch {
        return ""
    }
    return ""
}

CountMatches(hay, pattern) {
    n := 0
    pos := 1
    while (found := RegExMatch(hay, pattern, &m, pos)) {
        n += 1
        pos := found + m.Len
    }
    return n
}

BuildStatusText() {
    global StatusUrl, ClusterUrl

    health := HttpGet(ClusterUrl)
    status := HttpGet(StatusUrl)

    ; 两个都取不到 = 看板没在跑（或还没启动完）。不报错，就报「未运行」。
    if (health = "" && status = "")
        return "未运行"

    ; 主节点：优先 leader.name；没选出 leader（leader 为 null）就退到 node.name
    leaderName := ""
    if (health != "") {
        if (RegExMatch(health, '"leader"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]*)"', &m))
            leaderName := m[1]
        else if (RegExMatch(health, '"node"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]*)"', &m))
            leaderName := m[1]
    }
    if (leaderName = "")
        leaderName := "-"

    running := CountMatches(status, '"state"\s*:\s*"running"')
    return "运行中 · 主节点 " leaderName " · 在跑 " running " 张"
}

; --- 托盘菜单 -------------------------------------------------------------- #

RefreshTray() {
    global LastStatusText

    text := BuildStatusText()
    if (text = LastStatusText)
        return
    LastStatusText := text
    RebuildMenu(text)
}

RebuildMenu(text) {
    A_TrayMenu.Delete()
    A_TrayMenu.Add("状态：" . text, NoOp)
    A_TrayMenu.Add()
    A_TrayMenu.Add("启动", MenuStart)
    A_TrayMenu.Add("停止", MenuStop)
    A_TrayMenu.Add("打开看板", MenuOpenBoard)
    A_TrayMenu.Add("打开日志", MenuOpenLog)
    A_TrayMenu.Add()
    A_TrayMenu.Add("退出", MenuExit)
    ; 双击托盘图标 = 打开看板（最常用的那个动作）
    A_TrayMenu.Default := "打开看板"
}

NoOp(*) {
}

RunInstallPs1(mode) {
    global InstallPs1
    ps := A_WinDir . "\System32\WindowsPowerShell\v1.0\powershell.exe"
    try {
        ; "Hide"：隐藏窗口拉起，别在托盘交互时再闪一个黑框（本卡「不打扰」的底线）
        RunWait('"' ps '" -NoProfile -ExecutionPolicy Bypass -File "' InstallPs1 '" ' mode, , "Hide")
    } catch {
        TrayTip("调用 install-autostart.ps1 失败：" InstallPs1)
    }
    RefreshTray()
}

MenuStart(*) {
    RunInstallPs1("-Start")
}

MenuStop(*) {
    RunInstallPs1("-Stop")
}

MenuOpenBoard(*) {
    global BaseUrl
    Run(BaseUrl)
}

MenuOpenLog(*) {
    global LogFile, LogDir
    if FileExist(LogFile)
        Run('notepad.exe "' LogFile '"')
    else if FileExist(LogDir)
        Run('explorer.exe "' LogDir '"')
    else
        TrayTip("还没有日志目录：" LogDir)
}

MenuExit(*) {
    ExitApp()
}