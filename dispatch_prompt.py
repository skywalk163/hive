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

#: 单行上限。超过就换缩进子项，不硬挤。
LINE_LIMIT = 200

#: 摘要默认长度上限（含标题）。
SUMMARY_CHARS = 200

_FALLBACK_PATHS = "（验收卡，不改代码）"
_FALLBACK_VERIFY = "见任务卡完成标准"

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
