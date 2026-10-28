"""T-0187：Windows 派发脚本的静态守卫。

这两个脚本只能在 Windows 上真跑——要窗口、要剪贴板、要按键，CI 和 FreeBSD 都没有，
所以这里**只读文本**做断言，钉住三类最容易被后续改动悄悄破坏的性质：

1. **默认 dry-run**：不加 `-Apply` 一个键都不许发。这条一破，跑一次脚本就往别人的
   chat 里灌一段指令，而且没人会注意到「只是少加了个开关」。
2. **按键必须在 `-Apply` 分支内**：不是「文件里出现过 `if ($Apply)`」，而是每一处
   发送语句都落在那段花括号里面（见 `test_key_sends_are_inside_apply_branch`）。
3. **不写死 agent 名与窗口标题**：这台机器上 agent 名和窗口标题是会变的（改名、换
   客户端、中文标题），写死一个就只对这一台机器成立，且失败方式是「静默粘错窗口」。

真跑的 dry-run 端到端输出不进 CI（要真窗口），写在任务 summary 里。

T-0192 追加第 7 节：三处「会静默出错」的脆点各有一组守卫 —— 剪贴板重试（退出码 5）、
AttachThreadInput 激活兜底（退出码 3 语义不变；实测还需一次空 Alt 解前台锁）、
以及发送后回读（命中 / 未命中退出码 6 / 未校验三态）。这些断言只读文本，不真发按键。
"""
from __future__ import annotations

import codecs
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WIN = ROOT / "windows"
PS1 = WIN / "dispatch-agent.ps1"
AHK = WIN / "dispatch-agent.ahk"
SAMPLE = WIN / "dispatch-agents.sample.json"

#: 卡面点名的六个 agent 名。实现区（去注释后）一个都不许出现。
AGENT_NAMES = (
    "trae-win",
    "codearts-win",
    "raccoon-win",
    "dumate-win",
    "doubao-win",
    "workbuddyai-win",
)


def _read(path: Path) -> str:
    """按 UTF-8 读；带 BOM 的文件用 utf-8-sig 剥掉，断言看到的都是正文。"""
    return path.read_text(encoding="utf-8-sig")


def _strip_comments(src: str) -> str:
    """去掉 PowerShell 注释（`<# ... #>` 与 `# ...`），留下实现区。

    前提：脚本里没有把 `#` 当字符串内容用（本文件里没有）。若将来真有，
    这条断言会偏严——偏严的方向是「少写注释」，不是「漏掉真回归」。
    """
    src = re.sub(r"<#.*?#>", "", src, flags=re.S)
    return re.sub(r"#[^\n]*", "", src)


def _block_after(src: str, brace_index: int) -> str:
    """从 `src[brace_index] == '{'` 起做花括号配对，返回块内文本（不含外层括号）。

    注释与字符串里不要出现不成对的花括号，否则配对会偏——本文件已满足。
    """
    assert src[brace_index] == "{", "起点必须是左花括号"
    depth = 0
    for i in range(brace_index, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace_index + 1:i]
    raise AssertionError("花括号不配对：脚本被截断或括号写错了")


# --- 1. CLI 契约 ----------------------------------------------------------- #

def test_ps1_calls_the_three_cli_contracts():
    """prompt / --format json / dispatch-mark 三处调用都得在。

    少任何一处，脚本就不成流水线：没 prompt 拿不到正文，没 json 解析不了载荷，
    没 dispatch-mark 派发完不记账（看板会一直显示「未派发」，人会重复派）。
    """
    src = _read(PS1)

    assert re.search(r"['\"]prompt['\"]", src), "缺 prompt 子命令调用"
    assert re.search(r"--format[\s,'\"]+json", src), "prompt 必须带 --format json"
    assert re.search(r"['\"]dispatch-mark['\"]", src), "缺 dispatch-mark 记账调用"


def test_prompt_payload_is_read_from_text_field():
    """正文取自 `payload.text`，而不是把 stdout 整段当正文。

    `--format json` 的载荷里 `text` 才是要粘贴的正文；直接粘 stdout 会把 JSON
    的键名和转义一起粘进 chat。
    """
    src = _read(PS1)

    assert "ConvertFrom-Json" in src, "必须真解析 JSON，不能靠字符串切"
    assert re.search(r"\$payload\.text", src), "正文必须取自 payload.text"


def test_prompt_failure_is_explicit_not_silent():
    """prompt 退出码非 0 时必须显式报错退出，不许静默继续。

    T-0188 没合入时 `prompt` 子命令根本不存在，脚本如果只看「输出空」就继续，
    会往窗口里粘一段空指令——这比报错难查得多。
    """
    src = _read(PS1)

    # 非零退出码的分支里必须有 exit 2（prompt 契约未满足的专用退出码）
    assert re.search(r"\$result\.Code\s*-ne\s*0", src), "必须判 prompt 的退出码"
    assert re.search(r"契约未满足", src), "报错文案要点明是契约问题，不是「输出为空」"
    assert re.search(r"exit\s+2", src), "契约未满足必须用专用退出码退出"


# --- 2. 默认 dry-run ------------------------------------------------------- #

def _param_block(src: str) -> str:
    """取 `param( ... )` 里的参数声明。

    PowerShell 的 param 用圆括号不用花括号，所以这里做圆括号配对——用花括号配对会
    滑到后面第一个 `{`（try 块）上去，断言就变成对着一段无关代码做，白绿。
    """
    start = src.index("param(") + len("param(")
    depth = 1
    for i in range(start, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[start:i]
    raise AssertionError("param( 的圆括号不配对")


def test_apply_is_a_switch_without_default():
    """`$Apply` 是 switch 且不带默认值 —— 不加就是 dry-run。

    断言「没写 `= $true`」而不是「写了什么」：默认值一旦被改成 true，
    这条会红，而 dry-run 分支还在文件里（所以不能只断言 dry-run 分支存在）。
    """
    block = _param_block(_read(PS1))

    assert re.search(r"\[switch\]\s*\$Apply", block), "$Apply 必须是 [switch]"
    assert not re.search(r"\$Apply\s*=\s*\$?[Tt]rue", block), "$Apply 不许带默认 true"


def test_dry_run_branch_exists_and_prints_the_five_line_preview():
    """dry-run 要打印目标窗口、正文前 5 行、将要执行的记账命令。

    这三样是「人能不能在按键之前判断这次派发对不对」的全部依据。
    """
    src = _read(PS1)

    assert re.search(r"if\s*\(\s*-not\s+\$Apply\s*\)", src), "缺 dry-run 分支"
    assert re.search(r"\[dry-run\]", src), "dry-run 输出要有 [dry-run] 前缀，便于肉眼区分"
    assert re.search(r"Select-Object\s+-First\s+5", src), "正文预览必须是前 5 行"
    assert re.search(r"\$markCommandLine", src), "dry-run 要打印将要执行的记账命令"


# --- 3. 按键守卫 ----------------------------------------------------------- #

@pytest.mark.parametrize("statement", ["SendKeys", "Set-Clipboard"])
def test_key_sends_are_inside_apply_branch(statement: str):
    """每一处发送语句都必须落在 `if ($Apply) { ... }` 的花括号里面。

    不是「前面出现过 if ($Apply)」——那样把发送语句挪到 if 块之后仍然过。
    这里做的是真配对：块内出现的次数必须等于全文出现的次数。
    """
    src = _read(PS1)

    guard = src.index("if ($Apply)")
    body = _block_after(src, src.index("{", guard))

    assert statement in body, f"{statement} 应该只在 -Apply 分支里出现"
    assert src.count(statement) == body.count(statement), (
        f"{statement} 在 -Apply 分支外还有出现：dry-run 路径绝不能发送"
    )


def test_apply_branch_rechecks_foreground_window():
    """激活之后必须复核前台窗口标题，不匹配就中止（防粘错窗口）。

    只激活不复核，等于把「窗口切换的时序」当成了保证：Windows 前台锁、
    目标应用自己的启动页、切窗口失败都会让按键落到别的窗口里。
    """
    src = _read(PS1)

    assert "GetForegroundWindow" in src, "要取真正的前台窗口句柄，而不是「我激活过谁」"
    assert re.search(r"ForegroundTitle", src), "要读前台窗口标题"
    assert re.search(r"IndexOf\([^)]*WindowTitle", src), "要拿前台标题与配置比对"
    assert re.search(r"不匹配[^\n]*不发送", src), "不匹配时必须中止且不发送"


def test_abort_codes_are_distinct():
    """不同失败点给不同退出码，AHK 的气泡才能说清「发没发出去」。

    退出码 4 与其它码的区别是**已经发出去了**：这条语义错了，人就会以为没派发
    而重派一次，agent 收到两遍同样的指令。
    """
    src = _read(PS1)

    # 四个码各有出处：1 = 参数/配置（Fail 默认），2 = prompt 契约，3 = 窗口，4 = 记账
    assert re.search(r"\[int\]\$Code\s*=\s*1", src), "Fail 的默认退出码必须是 1"
    assert re.search(r"exit\s+2\b", src), "prompt 契约未满足要用 2"
    assert re.search(r"-Code\s+3", src), "窗口找不到 / 前台不匹配要用 3"
    assert re.search(r"exit\s+4\b", src), "记账失败要用 4"
    assert re.search(r"派发已发生[^\n]*dispatch-mark\s*失败", src), (
        "记账失败必须明说「派发已发生」，否则人会重派"
    )


# --- 4. 不写死 agent 名 / 窗口标题 ----------------------------------------- #

def test_no_agent_name_literal_in_implementation():
    """实现区（去掉注释后）不许出现任何具体 agent 名。

    卡面允许示例配置与注释里出现（那是文档），实现区出现就意味着「只对这一台机器成立」。
    """
    impl = _strip_comments(_read(PS1))

    hits = [name for name in AGENT_NAMES if name in impl]
    assert not hits, f"实现区写死了 agent 名：{hits}"


def test_missing_config_points_at_the_sample_file():
    """缺配置表时要指向 sample，而不是内置一份默认窗口标题。

    内置默认标题 = 在别人的机器上「碰巧匹配到某个窗口」然后粘进去，这是最坏的失败方式。
    """
    src = _read(PS1)

    assert "dispatch-agents.sample.json" in src, "缺配置的报错要指向 sample 文件"
    assert re.search(r"Test-Path[^\n]*\$Path", src), "要真检查配置文件存在"


def test_sample_config_is_valid_and_uses_placeholders():
    """示例配置可解析、每项含 window_title/hive_agent、且标题是占位符。

    占位符（`<...>`）是这条的牙齿：真窗口标题一旦被提交进来，这里会红。
    """
    data = json.loads(_read(SAMPLE))

    assert data, "示例配置不能是空的"
    for name, item in data.items():
        assert set(item) == {"window_title", "hive_agent"}, f"{name} 的字段不对：{sorted(item)}"
        assert item["window_title"].startswith("<") and item["window_title"].endswith(">"), (
            f"{name}.window_title 必须是占位符，不能是真窗口标题"
        )
        assert item["hive_agent"], f"{name}.hive_agent 不能为空"


# --- 5. AHK 不重复实现逻辑 -------------------------------------------------- #

def test_ahk_does_not_reimplement_the_pipeline():
    """AHK 只做快捷键 + 气泡：正文渲染、窗口匹配、记账一律不许出现在这层。

    两处实现一旦分叉，「粘错窗口」这类 bug 就没人查得出来是谁干的。
    """
    src = _read(AHK)

    for forbidden in ("dispatch-mark", "hive.py prompt", "ConvertFrom-Json", "--format"):
        assert forbidden not in src, f"AHK 里不该出现 {forbidden!r}（逻辑必须在 ps1）"

    assert re.search(r"#Requires\s+AutoHotkey\s+v2", src), "必须声明 AHK v2"
    assert "dispatch-agent.ps1" in src, "AHK 要调的就是 ps1"
    assert re.search(r"-Apply", src), "AHK 走的是真发路径"
    assert "TrayTip" in src, "失败要有气泡提示"


# --- 6. 编码 --------------------------------------------------------------- #

@pytest.mark.parametrize("path", [PS1, AHK], ids=lambda p: p.name)
def test_scripts_are_utf8_with_bom(path: Path):
    """脚本必须是**带 BOM** 的 UTF-8。

    这不是洁癖，是实测出来的：Windows PowerShell 5.1 对无 BOM 的文件按 ANSI
    代码页（本机 cp936）解码，UTF-8 的中文字节会被拆成乱码，其中 `）` 这类字符
    的尾字节正好破坏字符串终止 —— 脚本连解析都过不去（实测报
    「表达式或语句中包含意外的标记 }」/「字符串缺少终止符」）。
    AHK v2 对无 BOM 的非 ASCII 源文件同样按 ANSI 读，理由相同。
    """
    raw = path.read_bytes()

    assert raw.startswith(codecs.BOM_UTF8), f"{path.name} 缺 UTF-8 BOM，5.1 会按 ANSI 解码"
    text = raw[len(codecs.BOM_UTF8):].decode("utf-8")
    assert any(ord(ch) > 127 for ch in text), f"{path.name} 里没有中文，这条断言是空转"


def test_sample_json_is_utf8_without_bom():
    """示例配置必须是**无 BOM** 的 UTF-8。

    与脚本相反：`json.loads` 见到 U+FEFF 直接抛「Expecting value」，
    而这份文件是要被 Python 读的（测试与 T-0188 都会读它）。
    """
    raw = SAMPLE.read_bytes()

    assert not raw.startswith(codecs.BOM_UTF8), "示例配置不能带 BOM，json.loads 会炸"
    assert json.loads(raw.decode("utf-8")), "示例配置要能按 UTF-8 解出内容"


def test_ps1_pins_console_encoding():
    """ps1 必须显式把控制台输出编码钉成 UTF-8。

    5.1 默认 OEM 代码页，中文提示变问号；而「看不出报错内容」正是这类脚本
    最常见的误诊来源。
    """
    src = _read(PS1)

    assert re.search(r"\[Console\]::OutputEncoding\s*=", src), "必须显式设 OutputEncoding"
    assert re.search(r"UTF8Encoding", src), "必须是 UTF-8（无 BOM）"


# --- 7. T-0192：三处静默失败的硬化 ---------------------------------------- #

def _apply_body(src: str) -> str:
    """取 `if ($Apply) { ... }` 的块内文本（真配对，不是「后面出现过」）。"""
    guard = src.index("if ($Apply)")
    return _block_after(src, src.index("{", guard))


def _function_body(src: str, name: str) -> str:
    """取 `function <name>(...) { ... }` 的块内文本。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\b[^{]*\{", src)
    assert m, f"找不到函数 {name} 的定义"
    return _block_after(src, m.end() - 1)


def test_clipboard_write_has_retry_loop_and_exit_code_5():
    """剪贴板写入必须自带重试，且全失败走专用退出码 5。

    背景：Clipboard 是全局单例，别的进程开着时 `Set-Clipboard` 会抛
    ExternalException；`$ErrorActionPreference='Stop'` 会让脚本直接终止并只留一坨
    .NET 堆栈。没有重试 = 偶发一次占用就丢一次派发。
    """
    src = _read(PS1)
    body = _function_body(src, "Set-ClipboardText")

    assert re.search(r"Set-Clipboard\s+-Value\s+\$Text", body), (
        "重试函数里必须真的写剪贴板（写的是入参 $Text）"
    )
    assert re.search(r"for\s*\(.*-le\s+5", body), "必须有最多 5 次的重试循环"
    assert re.search(r"150\s*\*\s*\$attempt", body), "退避必须是 150ms × 第几次（不能写死等）"
    assert re.search(r"catch\s*\{", body) and re.search(r"\.Exception\.Message", body), (
        "每次失败要记下最后一条异常，最终报错才有信息量"
    )
    assert re.search(r"Fail[^\n]*-Code\s+5", body), "全部失败必须用专用退出码 5 退出"
    assert re.search(r"已重试\s*5\s*次", body), "报错要明说已经重试过 5 次"


def test_clipboard_call_site_replaced_and_failure_blocks_sending():
    """调用点必须换成 Set-ClipboardText，且失败路径在按键之前终止。

    旧调用留着（或新函数只在定义里、调用点没换）时，这条会红：前者说明还有一条
    不受重试保护的写剪贴板路径，后者说明改了函数但没人用。
    """
    src = _read(PS1)
    apply_body = _apply_body(src)

    assert "Set-ClipboardText $bodyText" in apply_body, "第 6 步必须调用重试版写剪贴板"
    assert "Set-Clipboard -Value $bodyText" not in src, "旧的裸调用不许留在文件里"
    assert src.count("Set-Clipboard -Value") == 1, (
        "裸写剪贴板只允许出现在重试函数内部（多一处就是绕过重试）"
    )

    # 失败即终止，不能先发按键：-Code 5 必须出现在粘贴之前
    fail5 = apply_body.index("-Code 5")
    paste = apply_body.index("SendKeys('^v')")
    assert fail5 < paste, "剪贴板失败分支必须在粘贴之前（带着旧剪贴板粘贴比不发更糟）"
    assert re.search(r"一个键都不发|绝不发送|带着旧剪贴板", src), (
        "注释要写清「失败不发」的理由，避免后人把它改成「尽力而为」"
    )


def test_activation_falls_back_to_attach_thread_input():
    """激活失败要能用 AttachThreadInput 兜底，且判定看 GetForegroundWindow。

    实测：调用进程本身不是前台时 SetForegroundWindow 返回 false（连续三次 exit 3），
    但窗口其实能被顶上来；反过来它也可能在窗口已到前台时仍返回 false。所以判定标准
    只能是「前台句柄 == 目标句柄」。
    """
    src = _read(PS1)
    body = _function_body(src, "Set-WindowForeground")

    assert re.search(r"ShowWindow\s*\([^)]*,\s*9\s*\)", body), "先 SW_RESTORE，最小化窗口才能被顶上来"
    assert "AttachThreadInput" in body, "必须有 AttachThreadInput 兜底，否则换触发方式就退化"
    assert re.search(r"AttachThreadInput\([^)]*,\s*\$?true\s*\)", body), "attach 必须真的 attach"
    assert re.search(r"AttachThreadInput\([^)]*,\s*\$?false\s*\)", body), (
        "attach 是有状态的，用完必须在 finally 里解开"
    )
    assert re.search(r"GetForegroundWindow\(\)\s*-eq\s*\$handle", body), (
        "判定标准是前台句柄，不是 SetForegroundWindow 的返回值"
    )
    assert not re.search(r"return\s+\[bool\]\[HiveDispatch\.Win32\]::SetForegroundWindow", body), (
        "不许回到「拿返回值当结论」的老写法"
    )
    # 真机实测（Win10 19045）：只 attach 仍被前台锁拒绝，需要一次空 Alt 解锁。
    # 这条钉住「要解锁」这个事实，避免后来人以为 attach 单独就够而把它删掉。
    assert "keybd_event" in body, (
        "attach 之后要发一次空 Alt 解前台锁（实测否则仍 exit 3）；只按 Alt 不输入字符"
    )
    first_attach = body.index("AttachThreadInput")
    last_attach = body.rindex("AttachThreadInput")
    unlock = body.index("keybd_event")
    assert first_attach < unlock < last_attach, (
        "解锁必须夹在 attach / detach 之间（顺序：attach -> 空 Alt -> SetForegroundWindow -> detach）"
    )
    # 真机踩过：GetWindowThreadProcessId 的线程号是返回值、out 参数是 PID。
    # 把 PID 当线程号传进 AttachThreadInput 会静默失败（attach=false），必须钉住。
    assert re.search(
        r"\$foregroundThread\s*=\s*\[HiveDispatch\.Win32\]::GetWindowThreadProcessId", body
    ), "线程号必须取返回值；out 参数是 PID，不能拿来 attach"


def test_confirm_paste_landed_has_three_states():
    """回读必须是三态：命中 / 未命中（退出码 6）/ 读不了（警告但记账）。

    两态（成功/失败）会把「UIA 读不了」误判成「没发出去」，导致重复派发；
    而把「读到文本但没命中」当成成功，就是假绿 —— 账上说发了，窗口里没有。
    """
    src = _read(PS1)
    body = _function_body(src, "Confirm-PasteLanded")

    assert "'landed'" in body and "'missing'" in body and "'unknown'" in body, (
        "必须显式返回 landed / missing / unknown 三态"
    )
    assert re.search(r"IndexOf\([^)]*Sentinel", body), "命中判定是「文本里含 sentinel」"
    assert re.search(r"return\s+'unknown'", body), "读不了必须是 unknown，不能当失败"
    apply_body = _apply_body(src)
    assert re.search(r"-eq\s+'missing'[\s\S]{0,400}?-Code\s+6", apply_body), (
        "未命中必须用专用退出码 6 退出，且不记账"
    )
    assert re.search(r"未校验[^\n]*照常记账", apply_body), "读不了时要明确警告「未校验」并照常记账"


def test_confirm_uses_readonly_uia_only():
    """回读只读：不加第三方依赖、不写目标窗口、不切焦点。

    UIA 的写接口（SetValue）会改目标窗口内容，SetFocus 会抢焦点 —— 两者都会把
    「回读」变成「再来一次副作用」。
    """
    src = _read(PS1)
    body = _function_body(src, "Confirm-PasteLanded") + _function_body(src, "Read-ControlText")

    assert "UIAutomationClient" in src, "UIAutomation 是 .NET Framework 自带程序集，不许引第三方包"
    assert "TryGetCurrentPattern" in body, "读文本必须走 TryGetCurrentPattern（不支持的控件要能跳过）"
    assert "ValuePattern" in body and "TextPattern" in body, (
        "两种模式都要尝试：ValuePattern 读输入框，TextPattern 读文档"
    )
    assert "SetValue" not in body, "不许向目标控件写值"
    assert "SetFocus" not in body, "不许抢焦点"


def test_confirm_runs_after_enter_before_dispatch_mark():
    """顺序：回车 -> 回读 -> 记账。顺序错了回读就是空转。

    回读在回车之前会读到「还没粘进去」的旧内容；记账在回读之前则意味着未命中也会
    先记一笔，回读就失去了「不记账」的意义。
    """
    src = _read(PS1)

    enter = src.index("SendKeys('{ENTER}')")
    confirm = re.search(r"\$landed\s*=\s*Confirm-PasteLanded", src)
    assert confirm, "必须把回读结果接进变量再判断（不能只调用不看）"
    mark = src.index("$mark = Invoke-Native")

    assert enter < confirm.start() < mark, (
        f"顺序应为 回车({enter}) < 回读({confirm.start()}) < 记账({mark})"
    )
    assert re.search(r"Confirm-PasteLanded[^\n]*-Sentinel\s+\$Task", src), (
        "sentinel 取任务号（正文里必然出现），不额外往正文塞标记"
    )


def test_readback_name_fallback_is_narrowed():
    """记事本这类只经 MSAA 桥暴露正文的窗口要能回读；但 Name 兜底必须收窄。

    真机实测（Win10 19045）：记事本的 Edit 控件 ValuePattern / TextPattern 都不支持，
    全文只出现在 UIA 的 Name 上。不兜底 -> 记事本永远「未校验」，回读形同虚设；
    兜底不收窄 -> 按钮文字、窗口标题会被当成正文，把「已落进窗口」误判成「没落进」，
    这是假阴性，人会重复派发。
    """
    src = _read(PS1)
    body = _function_body(src, "Read-ControlText")
    gate = _function_body(src, "Test-TextContainer")

    assert "Current.Name" in body, "必须有 Name 兜底，否则记事本这类窗口永远读不到正文"
    assert re.search(r"Test-TextContainer\s+\$Element", body), "Name 兜底必须先过容器判定"
    assert re.search(r"ClassName", gate), "容器判定要按类名收窄"
    assert re.search(r"(?i)edit", gate), "类名里带 edit 的（Edit/RichEdit/TextBox）才算容器"
    assert "ControlType.Document" in gate, "文档类控件也要算容器"


def test_notes_exit_code_table_lists_5_and_6():
    """头部 .NOTES 的退出码表必须写上 5 与 6。

    退出码表是 AHK 气泡与人的唯一对照表；表里没有 5/6，失败时就只剩「退出码 5」，
    人得回来读代码才知道发生了什么。
    """
    src = _read(PS1)
    m = re.search(r"\.NOTES([\s\S]*?)#>", src)
    assert m, "脚本头部必须有 .NOTES 段"
    notes = m.group(1)

    assert re.search(r"5\s*剪贴板", notes), ".NOTES 的退出码表缺 5（剪贴板）"
    assert re.search(r"6\s*回读", notes), ".NOTES 的退出码表缺 6（回读未命中）"
