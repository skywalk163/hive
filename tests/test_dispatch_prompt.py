"""T-0184：`dispatch_prompt` 的渲染契约。

分三层测，坏在哪一层一眼能看出来：

1. **逐字**：五段骨架、四段契约与任务卡规定的格式逐字符对齐（`test_markdown_*`）；
2. **回退**：缺 `paths` / 缺 `verify` / 缺「不要做什么」时各自的默认文案，
   以及「缺任何字段都不抛异常」（`test_missing_*` / `test_contract_*`）；
3. **给粘贴服务的硬约束**：确定性、无 ANSI、无表格行、单行 ≤ 200、行尾 `\\n`
   （`test_paste_*` / `test_deterministic`）。

最后一条 `test_real_card_T0184` 拿仓里真实的 T-0184 卡跑一遍：格式对了不代表真卡能渲染，
body 里的 `## 背景` / `## 不要做什么` 是长句、带反引号、带句读的，只有真卡能证明清洗规则不歪。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dispatch_prompt as dp
from hive import parse_card

ROOT = Path(__file__).resolve().parents[1]

CARD = {
    "id": "T-0184",
    "title": "派发指令渲染",
    "paths": ["dispatch_prompt.py", "tests/test_dispatch_prompt.py"],
    "verify": ["python -m pytest -q tests/test_dispatch_prompt.py"],
    "timeout_minutes": 90,
}

BODY = """
## 背景

看板看卡 → 手打 prompt，三类稳定损耗。

## 不要做什么

- 不修改 `hive.py`。
- 不加 CLI 参数。
"""

EXPECTED = "\n".join([
    "请领取任务 T-0184。",
    "任务摘要：派发指令渲染。看板看卡 → 手打 prompt，三类稳定损耗。",
    "执行契约：",
    "- 允许修改路径：dispatch_prompt.py、tests/test_dispatch_prompt.py",
    "- 预期输出：python -m pytest -q tests/test_dispatch_prompt.py",
    "- 失败策略：90 分钟内未完成即放弃；失败时把卡住的位置写进 .hive/summary.md "
    "并回 hive_submit，不要硬凑",
    "- 禁止行为：不修改 hive.py；不加 CLI 参数",
    "领取方式：调用 MCP 工具 hive_claim，参数 task_id=T-0184",
    "完成后调用 hive_submit 提交。",
])


# --------------------------------------------------------------------------- #
# 1. 逐字：五段骨架 + 四段契约
# --------------------------------------------------------------------------- #


def test_markdown_matches_card_spec_verbatim():
    """完整卡：输出与任务卡里写死的格式逐字相等（含标点与空格）。"""
    assert dp.render_dispatch_prompt(CARD, body=BODY) == EXPECTED


def test_markdown_section_order_and_labels():
    """五段齐全且顺序固定：领取 / 摘要 / 契约 / 领取方式 / 完成后。"""
    lines = dp.render_dispatch_prompt(CARD, body=BODY).split("\n")
    assert lines[0] == "请领取任务 T-0184。"
    assert lines[1].startswith("任务摘要：")
    assert lines[2] == "执行契约："
    labels = [ln.split("：", 1)[0] for ln in lines[3:7]]
    assert labels == ["- 允许修改路径", "- 预期输出", "- 失败策略", "- 禁止行为"]
    assert lines[7] == "领取方式：调用 MCP 工具 hive_claim，参数 task_id=T-0184"
    assert lines[-1] == "完成后调用 hive_submit 提交。"


def test_missing_paths_uses_acceptance_fallback():
    """缺 `paths` → 「（验收卡，不改代码）」，且不因此漏掉别的段。"""
    card = {k: v for k, v in CARD.items() if k != "paths"}
    text = dp.render_dispatch_prompt(card, body=BODY)
    assert "- 允许修改路径：（验收卡，不改代码）" in text.split("\n")
    assert "- 预期输出：python -m pytest -q tests/test_dispatch_prompt.py" in text.split("\n")


def test_missing_verify_uses_card_standard_fallback():
    """缺 `verify` → 「见任务卡完成标准」。"""
    card = {k: v for k, v in CARD.items() if k != "verify"}
    assert "- 预期输出：见任务卡完成标准" in dp.render_dispatch_prompt(card, body=BODY).split("\n")


def test_empty_paths_list_behaves_like_missing():
    """`paths: []`（验收卡的典型写法）与字段缺失同一条回退路径。"""
    card = dict(CARD, paths=[])
    assert "- 允许修改路径：（验收卡，不改代码）" in dp.render_dispatch_prompt(card, body=BODY)


def test_forbidden_defaults_when_body_has_no_section():
    """body 没写「不要做什么 / 红线」→ 三条默认禁止行为，顺序固定。"""
    text = dp.render_dispatch_prompt(CARD, body="## 背景\n\n只有背景。\n")
    line = [ln for ln in text.split("\n") if ln.startswith("- 禁止行为：")][0]
    assert line == "- 禁止行为：" + "；".join(dp.DEFAULT_FORBIDDEN)


def test_redline_section_goes_into_contract():
    """body 有「## 红线」→ 条目进契约（与「不要做什么」合并，保持出现顺序）。"""
    body = BODY + "\n## 红线\n\n- 不碰 `agents/*.json`\n- 不提交 token\n"
    contract = dp.build_dispatch_contract(CARD, body)
    assert contract["forbidden"] == [
        "不修改 hive.py", "不加 CLI 参数", "不碰 agents/*.json", "不提交 token",
    ]
    assert "不碰 agents/*.json；不提交 token" in dp.render_dispatch_prompt(CARD, body=body)


# --------------------------------------------------------------------------- #
# 2. 回退与容错：缺字段不抛异常
# --------------------------------------------------------------------------- #


def test_contract_keeps_raw_empty_lists_for_bare_card():
    """契约层只报「卡上写了什么」：空卡四段全空，回退文案是渲染层的事。"""
    contract = dp.build_dispatch_contract({}, "")
    assert contract == {
        "allowed_paths": [], "expected_output": [], "failure_policy": [], "forbidden": [],
    }


def test_missing_fields_never_raise():
    """空卡 / 怪类型（`paths` 是字符串、`timeout_minutes` 是乱码）都只降级不抛异常。"""
    text = dp.render_dispatch_prompt({}, body="")
    assert text.split("\n")[0] == "请领取任务 。"
    assert "见任务卡完成标准" in text

    weird = {"id": "T-9999", "paths": "a.py", "verify": ("cmd1", "cmd2"),
             "timeout_minutes": "很久"}
    contract = dp.build_dispatch_contract(weird, "")
    assert contract["allowed_paths"] == ["a.py"]          # 标量当单条
    assert contract["expected_output"] == ["cmd1", "cmd2"]  # 元组也吃
    assert contract["failure_policy"] == []                # 乱码超时当没写


def test_timeout_missing_does_not_invent_number():
    """没写 `timeout_minutes` 就不编数字：策略句降级成「未完成即放弃」。"""
    card = {k: v for k, v in CARD.items() if k != "timeout_minutes"}
    line = [ln for ln in dp.render_dispatch_prompt(card, body=BODY).split("\n")
            if ln.startswith("- 失败策略：")][0]
    assert line.startswith("- 失败策略：未完成即放弃；")
    assert "分钟内" not in line


# --------------------------------------------------------------------------- #
# 3. 摘要：截断与折行还原
# --------------------------------------------------------------------------- #


def test_summary_truncated_to_max_chars_with_ellipsis():
    """长标题 + 长背景 → 恰好 `max_chars` 字符、以 `…` 结尾、UTF-8 往返无乱码。"""
    card = {"id": "T-0500", "title": "很长的标题" * 20}
    body = "## 背景\n\n" + "很长的背景描述" * 40 + "\n"
    summary = dp.extract_task_summary(card, body, max_chars=60)
    assert len(summary) == 60
    assert summary.endswith("…")
    assert summary.encode("utf-8").decode("utf-8") == summary   # 没切出半个字符
    assert "\ufffd" not in summary


def test_summary_short_input_untouched():
    """短输入不截断、不加省略号；`max_chars` 只是上限不是目标长度。"""
    summary = dp.extract_task_summary({"title": "短标题"}, "## 背景\n\n一句话。\n")
    assert summary == "短标题。一句话。"


def test_summary_joins_hard_wrapped_paragraph():
    """正文被硬折行的段落拼回来：CJK 之间不留空格，ASCII 一侧补空格，反引号剥掉。"""
    body = ("## 背景\n\n让 `agent` 领任务，\n损耗有三类：\n任务号抄错。\n\n第二段不要。\n")
    assert dp.extract_task_summary({"title": "t"}, body) == "t。让 agent 领任务，损耗有三类：任务号抄错。"


def test_summary_zero_max_chars_returns_empty():
    """`max_chars=0` 是调用方显式要空，不抛异常。"""
    assert dp.extract_task_summary(CARD, BODY, max_chars=0) == ""


# --------------------------------------------------------------------------- #
# 4. 给粘贴服务的硬约束
# --------------------------------------------------------------------------- #


def test_agent_line_injected_after_claim_line():
    """指定 `agent` → 在「领取方式」后插一行「领取身份」，其余逐字不变。"""
    text = dp.render_dispatch_prompt(CARD, agent="trae-win", body=BODY)
    lines = text.split("\n")
    idx = lines.index("领取方式：调用 MCP 工具 hive_claim，参数 task_id=T-0184")
    assert lines[idx + 1] == "领取身份：trae-win"
    assert lines[idx + 2] == "完成后调用 hive_submit 提交。"
    assert text.replace("领取身份：trae-win\n", "") == EXPECTED


def test_json_payload_structure_and_text_equality():
    """json 载荷结构完整，`payload["text"]` 与 markdown 渲染逐字相等。"""
    payload = dp.render_dispatch_payload(CARD, agent="doubao-win", body=BODY)
    assert list(payload) == ["task_id", "agent", "summary", "contract",
                             "claim_tool", "submit_tool", "text"]
    assert payload["task_id"] == "T-0184"
    assert payload["agent"] == "doubao-win"
    assert payload["claim_tool"] == {"name": "hive_claim", "arguments": {"task_id": "T-0184"}}
    assert payload["submit_tool"] == "hive_submit"
    assert payload["text"] == dp.render_dispatch_prompt(
        CARD, agent="doubao-win", body=BODY)
    # 载荷本身能 json.dumps 且往返无损（脚本要 json.loads 直接吃）
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_json_output_format_is_single_json_line():
    """`output_format="json"` 返回一行 JSON 字符串，不是 dict、不是多行。"""
    raw = dp.render_dispatch_prompt(CARD, output_format="json", body=BODY)
    assert isinstance(raw, str)
    assert "\n" not in raw
    assert json.loads(raw)["text"] == EXPECTED


def test_bad_output_format_raises_value_error():
    """非法 format 抛 `ValueError`（传错是编程错误，不静默降级）。"""
    with pytest.raises(ValueError):
        dp.render_dispatch_prompt(CARD, output_format="yaml", body=BODY)


def test_paste_safety_no_ansi_no_table_no_crlf():
    """粘贴安全：无 ANSI、无表格行、无 `\\r`、单行 ≤ 200。"""
    body = BODY + "\n## 不要做什么\n\n| a | b |\n|---|---|\n- 别写表格\n"
    text = dp.render_dispatch_prompt(CARD, agent="trae-win", body=body)
    assert "\x1b" not in text
    assert "\r" not in text
    for line in text.split("\n"):
        assert not line.strip().startswith("|"), f"表格行混进指令: {line!r}"
        assert len(line) <= dp.LINE_LIMIT, f"单行超长({len(line)}): {line!r}"


def test_long_contract_wraps_into_indented_subitems():
    """契约条目拼起来超 200 字符 → 换缩进子项，一条命令一行，谁都没被丢。"""
    card = dict(CARD, verify=[f"python -m pytest -q tests/test_case_{i}.py" for i in range(8)])
    lines = dp.render_dispatch_prompt(card, body=BODY).split("\n")
    head = lines.index("- 预期输出：")
    assert all(len(ln) <= dp.LINE_LIMIT for ln in lines)
    subitems = [ln for ln in lines[head + 1:] if ln.startswith("    - ")]
    assert subitems == [f"    - {cmd}" for cmd in card["verify"]]


def test_deterministic_across_calls():
    """连调两次逐字符相等（不看时间 / 环境 / 随机数）。"""
    first = dp.render_dispatch_prompt(CARD, agent="trae-win", body=BODY)
    second = dp.render_dispatch_prompt(CARD, agent="trae-win", body=BODY)
    assert first == second
    assert dp.render_dispatch_payload(CARD, body=BODY) == dp.render_dispatch_payload(CARD, body=BODY)


# --------------------------------------------------------------------------- #
# 5. 真卡回归：格式对了不代表真卡能渲染
# --------------------------------------------------------------------------- #


def test_real_card_T0184():
    """拿仓里真实的 T-0184 卡渲染：五段齐全、契约来自卡面、行宽不超限。"""
    card, body = parse_card(ROOT / "tasks" / "T-0184.md")
    text = dp.render_dispatch_prompt(card, body=body)
    lines = text.split("\n")

    assert lines[0] == "请领取任务 T-0184。"
    assert "- 允许修改路径：dispatch_prompt.py、tests/test_dispatch_prompt.py" in lines
    assert "- 预期输出：python -m pytest -q tests/test_dispatch_prompt.py" in lines
    assert "- 失败策略：90 分钟内未完成即放弃；" in text
    assert all(len(ln) <= dp.LINE_LIMIT for ln in lines)
    # 真卡有「## 不要做什么」，四条条目进契约、句读已剥掉
    contract = dp.build_dispatch_contract(card, body)
    assert len(contract["forbidden"]) == 4
    assert all(not it.endswith("。") for it in contract["forbidden"])
    assert contract["forbidden"][0].startswith("不修改 hive.py、mcp_server.py、dashboard/")
    assert "不动既有测试与 conflict.py" in text
