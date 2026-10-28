#Requires AutoHotkey v2.0
#SingleInstance Force
;
; T-0187 派发快捷键：选中任务号 -> ^!d -> 切窗口、粘贴、回车、记账。
;
; 这一层刻意只做两件事：
;   1) 抓一个任务号（选中文本 -> 输入框 -> 手输），拼成命令行交给 dispatch-agent.ps1 -Apply；
;   2) 用气泡把结果告诉人。
; 正文渲染、窗口匹配、前台复核、记账全部在 ps1 里 —— 这里不重复实现，
; 否则两处逻辑一旦分叉，粘错窗口这种事没人查得出来。
;
; 说明：SetForegroundWindow 受 Windows 前台锁限制，由热键触发时通常放行；
; 若某次激活失败，ps1 会以退出码 3 中止（宁可不发，也不粘错窗口），气泡里能看到。

SendMode "Input"
SetWorkingDir A_ScriptDir

global PS1_PATH := A_ScriptDir "\dispatch-agent.ps1"
global LOG_PATH := A_ScriptDir "\dispatch-agent.log"
global PS_EXE := A_WinDir "\System32\WindowsPowerShell\v1.0\powershell.exe"

^!d::DispatchSelected()

DispatchSelected() {
    global PS1_PATH, LOG_PATH, PS_EXE
    if !FileExist(PS1_PATH) {
        TrayTip("派发脚本缺失", PS1_PATH, 2)
        return
    }

    taskId := AskTaskId()
    if (taskId = "") {
        TrayTip("没有任务号", "先选中形如 T-0187 的文本，再按 ^!d", 1)
        return
    }

    ; 输出统一进日志：失败时人能顺着气泡去翻，而不是对着闪一下的控制台猜。
    command := A_ComSpec ' /c ' PS_EXE ' -NoProfile -ExecutionPolicy Bypass -File "'
             . PS1_PATH '" -Task ' taskId ' -Apply >> "' LOG_PATH '" 2>&1'
    exitCode := RunWait(command, , "Hide")

    if (exitCode = 0) {
        TrayTip("已派发 " taskId, "窗口已粘贴并回车，记账成功", 1)
    } else if (exitCode = 2) {
        TrayTip("派发失败（2）", "hive prompt 契约未满足，见日志：" LOG_PATH, 2)
    } else if (exitCode = 3) {
        TrayTip("派发失败（3）", "窗口没找到或前台不匹配，未发送，见日志：" LOG_PATH, 2)
    } else if (exitCode = 4) {
        TrayTip("已发送但没记账（4）", "派发已发生，回填状态失败，需人工补记，见日志：" LOG_PATH, 2)
    } else {
        TrayTip("派发失败（" exitCode "）", "见日志：" LOG_PATH, 2)
    }
}

; 任务号的三个来源，按「离手最近」排序：选中的文本 -> 当前输入框 -> 手输。
AskTaskId() {
    text := SelectedText()
    if RegExMatch(text, "T-\d{4}", &hit)
        return hit[0]

    box := InputBox("没从选中文本里找到任务号，手输一个（形如 T-0187）：",
                    "派发到 agent", "w340 h140")
    if (box.Result != "OK")
        return ""
    if RegExMatch(box.Value, "T-\d{4}", &hit)
        return hit[0]
    return ""
}

; 复制选区再还原剪贴板：热键不能把用户剪贴板里的东西弄丢。
SelectedText() {
    saved := ClipboardAll()
    A_Clipboard := ""
    Send("^c")
    if !ClipWait(0.5) {
        A_Clipboard := saved
        return ""
    }
    text := A_Clipboard
    A_Clipboard := saved
    return text
}
