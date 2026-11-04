"""hive 的派发指令渲染：把一张任务卡变成一段可直接粘进 chat 的指令。

## 为什么要有这个模块

派发一直是人肉搬运：看板看卡 → 记在脑子里 → 切到 agent APP → 手打 prompt。
手打有三类稳定损耗：**任务号抄错、契约漏讲、工具名说错**。这三类都不该由人保证，
所以把「任务卡 → 派发指令」做成一段**无副作用的纯渲染**，让 CLI（`hive prompt`）、
看板接口、PowerShell 脚本共用同一份文本，谁调都一样。

## 边界

- 只吃入参，**不读 git、不读文件系统、不发网络请求**：body 由调用方读好后传进来
  （`parse_card` 已经返回正文，别在这层再拼 frontmatter）。
- 纯标准库，不 import `hive.py`（避免和 CLI 互相拖死，也方便单独测）。
- 不写 `print`、不加 CLI 参数：输出交给调用方决定去哪儿。

## 输出的四条硬约束（都是给「粘贴」这个动作服务的）

1. **确定性**：同一输入逐字符一致，不看时间、不看环境、不看随机数 —— 否则
   看板复制的和 CLI 打印的对不上，排障时没人信。
2. **纯文本语义**：不含 ANSI 转义、不含 markdown 表格行（`|` 开头的行在 chat 里
   会变成表格，粘贴后语义就歪了）；正文里的反引号一律剥掉。
3. **单行不超 `LINE_LIMIT`**：契约条目一多就换成缩进子项，不硬挤成一行长句。
4. **行尾 `\\n`**：Windows 的 `clip` 收到 `\\r\\n` 会在末尾多出空行。

## 两个刻意的取舍

1. **契约缺字段回退文案，但 `payload["contract"]` 保留原始空列表**。
   回退文案（「（验收卡，不改代码）」）是**渲染层**的措辞，不是契约数据；
   把措辞塞进 contract 会让「卡上到底写了什么」变得不可分辨。
2. **`max_chars` 按字符截断，不按字节**。Python 的 `str` 是码点序列，
   `text[:n]` 天然不会切坏 UTF-8，所以这里不需要手工处理代理对。
"""

from __future__ import annotations

import json
import re

MARKDOWN = "markdown"
JSON = "json"
FORMATS = (MARKDOWN, JSON)

# ---- 渲染模式（2026-11-03，看板「复制派发指令」按钮状态感知） ----
#
# 同一个按钮在不同状态的卡上复制出的文本不该是同一段话：
#   * claim    = 发给干活 agent 的领取指令（原有行为，向后兼容的默认值）
#   * merge    = 发给主控 agent 的合并指令（卡已完成待 land，再喊人领取就闹笑话了）
#   * abnormal = 领取指令 + 异常溯源块（重派前让新 agent 知道上一个 agent 栽在哪）
#   * shelved  = 已封存说明（绝不能给搁置卡生成领取指令，否则 agent 又去领了）
CLAIM = "claim"
MERGE = "merge"
ABNORMAL = "abnormal"
SHELVED = "shelved"
MODES = (CLAIM, MERGE, ABNORMAL, SHELVED)

#: 单行上限。超过就换缩进子项，不硬挤。
LINE_LIMIT = 200

#: 摘要默认长度上限（含标题）。
SUMMARY_CHARS = 200

_FALLBACK_PATHS = "（验收卡，不改代码）"
_FALLBACK_VERIFY = "见任务卡完成标准"
_FALLBACK_PLATFORM = "判据在本机跑不了就去 192.168.0.88 跑（详见 docs/判据与平台.md）"

#: 「平台与判据」契约条目（2026-11-02 起）。
#:
#: 起因：T-0221 / T-0222 两张卡产物合格却被判 failed —— 卡上的判据是 POSIX 写法
#: （`head -1 x | grep -q y`、`! grep -q z`），而 Windows worker 用 cmd.exe + shell=True，
#: `head`/`grep` 不在 PATH、`!` 不是 cmd 语法，一律 exit 255。
#: 结论：判据不是产物的问题，是**执行平台**的问题，所以处置权交回 agent：
#: 别改判据（改了丢验收强度），换台机器跑。
_PLATFORM_HINT = [
    "Windows 上 head/grep/awk 与 ! / $() 不可用，POSIX 判据会 exit 255 假失败——先别怀疑产物",
    "确实跑不了就换 192.168.0.88（FreeBSD 15.1）跑：ssh workbuddy@192.168.0.88，"
    "密码取 .env 的 SSH_PASS_WORKBUDDY（尾部带空格，取值先 strip）",
]

#: 失败策略的后半句（前半句带分钟数，卡上没写分钟数时整句降级成 `_FALLBACK_FAILURE`）。
_FAILURE_TAIL = "失败时把卡住的位置写进 .hive/summary.md 并回 hive_submit，不要硬凑"
_FALLBACK_FAILURE = f"未完成即放弃；{_FAILURE_TAIL}"

#: body 里没写「不要做什么 / 红线」时的三条默认禁止行为。
DEFAULT_FORBIDDEN = (
    "只改 paths 内文件",
    "不动其它任务卡与 agents 配置",
    "不写入任何 token 或密钥",
)

_FORBIDDEN_SECTIONS = ("不要做什么", "红线")
_BACKGROUND_SECTION = "背景"

#: 只认 `#` / `##` 两级标题：`###` 当内容看（正文里常用 `### 1. xxx` 做条目子标题，
#: 当成边界会把一个条目切碎）。
_HEADING_RE = re.compile(r"^(#{1,2})\s+(.+?)\s*$")
#: 条目：`- x` / `* x` / `+ x` / `1. x` / `1) x`。
_ITEM_RE = re.compile(r"^(?:[-*+]|\d{1,2}[.)])\s+(.+)$")


# --------------------------------------------------------------------------- #
# 文本清洗
# --------------------------------------------------------------------------- #


def _clean(value) -> str:
    """规整一段文本：转字符串、剥反引号、压掉多余空白（含换行）。"""
    s = str(value if value is not None else "").replace("`", "")
    return " ".join(s.split())


def _is_ascii(ch: str) -> bool:
    return ord(ch) < 128


def _join_wrapped(lines: list[str]) -> str:
    """把被硬折行的同一段拼回来。

    CJK 之间直接拼（中文折行不留空格），只要有一侧是 ASCII 就补一个空格 ——
    `…让 agent` + `领任务…` 拼成 `让 agent 领任务`，而 `…损耗：` + `任务号抄错`
    拼成 `损耗：任务号抄错`。这条规则比「统一加空格」或「统一不加」都更接近原文。
    """
    out = ""
    for ln in lines:
        if not out:
            out = ln
        elif _is_ascii(out[-1]) or _is_ascii(ln[0]):
            out += " " + ln
        else:
            out += ln
    return out


def _str_list(value) -> list[str]:
    """把卡面字段规整成去空去重保序的字符串列表。

    frontmatter 里可能是 `None`（字段没写）、单条标量（`paths: a.py`）、
    或列表；三种都吃，都不抛异常。
    """
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    out: list[str] = []
    for it in items:
        s = _clean(it)
        if s and s not in out:
            out.append(s)
    return out


# --------------------------------------------------------------------------- #
# 正文小节解析
# --------------------------------------------------------------------------- #


def _sections(body: str) -> list[tuple[str, list[str]]]:
    """按一/二级标题把正文切开，返回 `[(标题, 小节正文行), ...]`（保持原文顺序）。"""
    out: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None
    for line in str(body or "").splitlines():
        m = _HEADING_RE.match(line)
        if m:
            cur = []
            out.append((m.group(2).strip(), cur))
            continue
        if cur is not None:
            cur.append(line)
    return out


def _section_matches(title: str, names) -> bool:
    return any(title == n or title.startswith(n) for n in names)


def _first_paragraph(lines: list[str]) -> str:
    """小节里的第一段（遇到空行即止），顺带把硬折行拼回去、剥掉反引号。"""
    buf: list[str] = []
    for ln in lines:
        s = _clean(ln)
        if not s:
            if buf:
                break
            continue
        buf.append(s)
    return _join_wrapped(buf)


def _section_items(body: str, names) -> list[str]:
    """指定小节的条目（`- ` / `1. ` 开头），按正文出现顺序收集。

    同名小节可以出现多次（比如先「## 不要做什么」再「## 红线」），全部合并。
    条目末尾的句读（`。；，、`）在抽取时就剥掉：条目渲染时要拼成一行，
    留着会出现「…负责。；不加…」这种双标点。
    """
    items: list[str] = []
    for title, lines in _sections(body):
        if not _section_matches(title, names):
            continue
        for ln in lines:
            m = _ITEM_RE.match(ln.strip())
            if not m:
                continue
            s = _clean(m.group(1)).rstrip("。；;，,、.")
            if s and s not in items:
                items.append(s)
    return items


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #


def extract_task_summary(card: dict, body: str, *, max_chars: int = SUMMARY_CHARS) -> str:
    """任务摘要：`title` + body「## 背景」首段，超 `max_chars` 截断并以 `…` 结尾。

    截断是**按字符**的：`str` 切片不会切坏 UTF-8，也不会切出半个 emoji。
    `max_chars <= 0` 返回空串（调用方显式要空就给它空，不抛异常）。
    """
    card = card if isinstance(card, dict) else {}
    limit = int(max_chars)
    if limit <= 0:
        return ""

    title = _clean(card.get("title"))
    background = ""
    for name, lines in _sections(body):
        if _section_matches(name, (_BACKGROUND_SECTION,)):
            background = _first_paragraph(lines)
            break

    if not background:
        text = title
    elif not title:
        text = background
    elif title[-1] in "。！？.!?；;":
        text = title + background
    else:
        text = f"{title}。{background}"

    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def _timeout_minutes(card: dict) -> int | None:
    """`timeout_minutes` 规整成正整数；缺失 / 非数字 / 非正数一律当「没写」。"""
    raw = card.get("timeout_minutes")
    if raw is None:
        return None
    try:
        minutes = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return minutes if minutes > 0 else None


def _failure_policy(card: dict) -> list[str]:
    """失败策略：写进契约的是「多少分钟 + 放弃时怎么留痕」，不是一句空话。

    没写 `timeout_minutes` 时返回空列表（契约只报卡上写了什么），渲染层用
    `_FALLBACK_FAILURE` 兜底 —— 不替卡编一个「90 分钟」出来。
    """
    minutes = _timeout_minutes(card)
    if not minutes:
        return []
    return [f"{minutes} 分钟内未完成即放弃；{_FAILURE_TAIL}"]


def build_dispatch_contract(card: dict, body: str) -> dict[str, list[str]]:
    """从卡面 + 正文抽出四段契约，**原样**返回，缺什么就空什么。

    回退文案留给渲染层（见模块 docstring 的取舍 1）。任何字段缺失都不抛异常。
    """
    card = card if isinstance(card, dict) else {}
    return {
        "allowed_paths": _str_list(card.get("paths")),
        "expected_output": _str_list(card.get("verify")),
        "failure_policy": _failure_policy(card),
        "forbidden": _section_items(body, _FORBIDDEN_SECTIONS),
    }


def _contract_line(label: str, items: list[str], sep: str, fallback: str) -> str:
    """渲染一条契约行；条目拼起来超 `LINE_LIMIT` 就换成缩进子项。

    单条本身超长时不再切 —— 把一条命令切两行，粘出去就不是同一条命令了。
    """
    if not items:
        return f"- {label}：{fallback}"
    line = f"- {label}：{sep.join(items)}"
    if len(line) <= LINE_LIMIT:
        return line
    return "\n".join([f"- {label}："] + [f"    - {it}" for it in items])


def _render_markdown(task_id: str, summary: str, contract: dict, agent: str | None) -> str:
    lines = [
        f"请领取任务 {task_id}。",
        f"任务摘要：{summary}",
        "执行契约：",
        _contract_line("允许修改路径", contract["allowed_paths"], "、", _FALLBACK_PATHS),
        _contract_line("预期输出", contract["expected_output"], "；", _FALLBACK_VERIFY),
        # 平台提示不是「卡上写了什么」，而是每次派发都该带的口径，所以不进 contract 字典，
        # 直接由常量渲染 —— 契约层保持「只报卡面」的语义（见 test_contract_keeps_raw_*）。
        _contract_line("平台与判据", list(_PLATFORM_HINT), "；", _FALLBACK_PLATFORM),
        _contract_line("失败策略", contract["failure_policy"], "；", _FALLBACK_FAILURE),
        _contract_line("禁止行为", contract["forbidden"] or list(DEFAULT_FORBIDDEN), "；", ""),
        f"领取方式：调用 MCP 工具 hive_claim，参数 task_id={task_id}",
    ]
    if agent:
        lines.append(f"领取身份：{agent}")
    lines.append("完成后调用 hive_submit 提交。")
    return "\n".join(lines)


def render_dispatch_payload(card: dict, *, agent: str | None = None, body: str = "") -> dict:
    """渲染成可直接 `json.dumps` 的载荷（`text` 与 markdown 输出逐字一致）。

    键顺序固定：`task_id / agent / summary / contract / claim_tool / submit_tool / text`，
    方便脚本用 `json.tool` 肉眼比对，也让快照测试不会因为键序抖动。
    """
    card = card if isinstance(card, dict) else {}
    task_id = _clean(card.get("id"))
    summary = extract_task_summary(card, body)
    contract = build_dispatch_contract(card, body)
    return {
        "task_id": task_id,
        "agent": agent,
        "summary": summary,
        "contract": contract,
        "claim_tool": {"name": "hive_claim", "arguments": {"task_id": task_id}},
        "submit_tool": "hive_submit",
        "text": _render_markdown(task_id, summary, contract, agent),
    }


def render_dispatch_prompt(card: dict, *, agent: str | None = None,
                           output_format: str = MARKDOWN, body: str = "") -> str:
    """渲染派发指令：`markdown` 返正文，`json` 返载荷的 JSON 字符串。

    非法 `output_format` 抛 `ValueError`（调用方传错了是编程错误，不该静默降级成 markdown）。
    """
    if output_format not in FORMATS:
        raise ValueError(
            f"不支持的 output_format: {output_format!r}（可选：{'/'.join(FORMATS)}）"
        )
    payload = render_dispatch_payload(card, agent=agent, body=body)
    if output_format == MARKDOWN:
        return payload["text"]
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 状态感知渲染：merge（给主控）/ abnormal（带溯源）
# --------------------------------------------------------------------------- #


def detect_mode(record) -> str:
    """按状态快照里的运行时记录判定渲染模式，判定口径与看板 classify 对齐。

    record 来自 `hive status --json` 的单卡记录；没有记录（None / 空对象 /
    字段缺失）一律回 claim —— 和旧行为完全一致，快照坏了不影响复制按钮。

    * merge    = result_status=done 且还没 close（看板「待合并」列）
    * abnormal = result_status=failed / state=blocked / 租约过期（看板「异常」列）
    * shelved  = 已搁置（不进任何看板列，也不该被领取）
    * claim    = 其余所有状态（待领取 / 进行中 / 已关闭…）
    """
    if not isinstance(record, dict):
        return CLAIM
    state = str(record.get("state") or "")
    result_status = str(record.get("result_status") or "")
    # 搁置优先于一切：哪怕旧快照里还残留 done 结果或过期租约，也不能
    # 让「复制」按钮吐出一句「请领取任务」把 agent 又勾回来。
    if state == "shelved":
        return SHELVED
    if result_status == "done" and state not in ("done", "closed"):
        return MERGE
    if result_status == "failed" or state == "blocked" or record.get("stale"):
        return ABNORMAL
    return CLAIM


def _pick(record, *names: str) -> str:
    """从记录里取第一个非空字符串字段（claim_agent → owner → dispatched_to 这类回退）。"""
    for n in names:
        s = _clean((record or {}).get(n))
        if s:
            return s
    return ""


def _truncate(text, limit: int = SUMMARY_CHARS) -> str:
    """按字符截断（与 extract_task_summary 同口径），不切坏 UTF-8。"""
    s = _clean(text)
    if len(s) <= limit:
        return s
    if limit <= 1:
        return "…"
    return s[: limit - 1].rstrip() + "…"


def _result_summary(record) -> str:
    """结果摘要：result_summary 优先，summary 兜底；都按字符截断。"""
    return _truncate((record or {}).get("result_summary")
                     or (record or {}).get("summary"))


def render_merge_payload(card: dict, record: dict) -> dict:
    """渲染「给主控 agent 的合并指令」载荷（text 与 markdown 输出逐字一致）。

    收件人不是干活 agent 而是负责收尾的主控：所以正文不再讲领取契约，
    而是给出结果分支、head、执行 agent 这些 land 需要的溯源信息。
    """
    card = card if isinstance(card, dict) else {}
    record = record if isinstance(record, dict) else {}
    task_id = _clean(card.get("id"))
    agent = _pick(record, "claim_agent", "owner", "dispatched_to")
    head = _clean(record.get("head"))
    payload = {
        "mode": MERGE,
        "task_id": task_id,
        "agent": agent or None,
        "summary": extract_task_summary(card, ""),
        "repo": _clean(card.get("repo") or record.get("repo")),
        "branch": f"res/{task_id}",
        "head": head or None,
        "result_summary": _result_summary(record),
        "land_cmd": f"hive land {task_id} --apply",
        "text": _render_merge(task_id, card, record),
    }
    return payload


def _render_merge(task_id: str, card: dict, record: dict) -> str:
    agent = _pick(record, "claim_agent", "owner", "dispatched_to")
    head = _clean(record.get("head"))
    repo = _clean(card.get("repo") or record.get("repo"))
    lines = [
        f"任务 {task_id} 已完成，请合并（land）。",
        f"任务摘要：{extract_task_summary(card, '')}",
    ]
    if agent:
        lines.append(f"执行 agent：{agent}")
    if repo:
        lines.append(f"代码仓：{repo}")
    branch = f"res/{task_id}"
    lines.append(f"结果分支：{branch}" + (f"（head {head[:10]}）" if head else ""))
    rs = _result_summary(record)
    if rs:
        lines.append(f"结果摘要：{rs}")
    lines += [
        f"合并方式：在协调仓运行 CLI 命令 hive land {task_id} --apply"
        "（合入代码仓 main 并自动关闭任务卡）",
        "冲突处理：先解决冲突再重跑同一命令；解决不了就把冲突现场写进该任务的留言线程。",
    ]
    return "\n".join(lines)


def render_merge_prompt(card: dict, record: dict, *,
                        output_format: str = MARKDOWN) -> str:
    """渲染合并指令：markdown 返正文，json 返载荷的 JSON 字符串。"""
    if output_format not in FORMATS:
        raise ValueError(
            f"不支持的 output_format: {output_format!r}（可选：{'/'.join(FORMATS)}）"
        )
    payload = render_merge_payload(card, record)
    if output_format == MARKDOWN:
        return payload["text"]
    return json.dumps(payload, ensure_ascii=False)


def _abnormal_block(record: dict) -> list[str]:
    """异常溯源块：谁领的、什么时候、栽在哪 —— 重派前先让新 agent 知道前情。"""
    record = record if isinstance(record, dict) else {}
    agent = _pick(record, "claim_agent", "owner", "dispatched_to")
    claimed_at = _clean(record.get("claimed_at"))
    finished_at = _clean(record.get("finished_at"))
    result_status = _clean(record.get("result_status"))
    if not result_status and record.get("stale"):
        result_status = "租约过期"
    if not result_status:
        result_status = "异常"
    reason = _result_summary(record)
    lines = ["异常信息（供溯源）："]
    if agent:
        lines.append(f"- 此前领取 agent：{agent}")
    if claimed_at:
        lines.append(f"- 领取时间：{claimed_at}")
    if finished_at:
        lines.append(f"- 异常时间：{finished_at}")
    lines.append(f"- 异常状态：{result_status}")
    if reason:
        lines.append(f"- 异常摘要：{reason}")
    return lines


def render_shelved_prompt(card: dict, record) -> str:
    """搁置卡复制出来的文本：不是派发指令，是一张「别动它」的说明。

    搁置卡已经退出派发池，如果复制按钮还吐出「请领取任务」，agent 会照着做，
    于是又把它领走 —— 正好是搁置要防的事。这里把原因和三个出口写清楚，
    方便人把这段贴到留言/工单里，日后按情况处置。
    """
    tid = _clean((card or {}).get("id")) or _clean((record or {}).get("id")) or "?"
    title = _clean((card or {}).get("title")) or _clean((record or {}).get("title")) or "（无标题）"
    at = (_clean((record or {}).get("shelved_at"))
          or _clean((card or {}).get("shelved_at")) or "未知")
    reason = (_clean((record or {}).get("shelve_reason"))
              or _clean((card or {}).get("shelve_reason")) or "（未填原因）")
    return "\n".join([
        f"# {tid} 已搁置 —— 不要领取、不要重派",
        "",
        f"- 标题：{title}",
        f"- 搁置时间：{at}",
        f"- 原因：{reason}",
        "",
        "这张卡当前做不了，已从派发池移出；租约 / 结果 / 进度 / 收件箱信号均已释放。",
        "它不属于异常，也不算挂账，agent 平时不用管它。",
        "**请不要执行 `hive_claim` / `hive take`，也不要重派。**",
        "",
        "日后由人按情况处置：",
        f"  - 放回待领取：`hive unshelve {tid}`",
        f"  - 另立新卡：`hive unshelve {tid} --spawn-new --title \"新标题\"`",
        f"  - 废弃：`hive close {tid} --note \"原因\"`",
    ])


def render_status_prompt(card: dict, record, *, agent: str | None = None,
                         output_format: str = MARKDOWN, body: str = "") -> str:
    """按运行时状态选渲染模式，是看板「复制」按钮的总入口。

    * merge    → render_merge_prompt（发给主控）
    * abnormal → 派发指令 + 异常溯源块（重派时新 agent 知道前情）
    * shelved  → 搁置说明（明确告诉 agent 别领）
    * claim    → render_dispatch_prompt（原有行为，逐字不变）
    """
    mode = detect_mode(record)
    if mode == SHELVED:
        text = render_shelved_prompt(card, record)
        if output_format == JSON:
            # 别复用 render_dispatch_payload：它带 claim_tool=hive_claim，
            # 脚本照着执行就等于把搁置卡又领走了。
            return json.dumps({"task_id": tid, "mode": SHELVED,
                               "text": text, "claimable": False},
                              ensure_ascii=False)
        return text
    if mode == MERGE:
        return render_merge_prompt(card, record, output_format=output_format)
    if mode == ABNORMAL:
        text = render_dispatch_prompt(card, agent=agent,
                                      output_format=MARKDOWN, body=body)
        text += "\n" + "\n".join(_abnormal_block(record))
        if output_format == JSON:
            payload = render_dispatch_payload(card, agent=agent, body=body)
            payload["mode"] = ABNORMAL
            payload["abnormal"] = {
                "agent": _pick(record, "claim_agent", "owner", "dispatched_to") or None,
                "claimed_at": _clean((record or {}).get("claimed_at")) or None,
                "finished_at": _clean((record or {}).get("finished_at")) or None,
                "result_status": _clean((record or {}).get("result_status")) or None,
                "summary": _result_summary(record),
            }
            payload["text"] = text
            return json.dumps(payload, ensure_ascii=False)
        return text
    return render_dispatch_prompt(card, agent=agent,
                                  output_format=output_format, body=body)
