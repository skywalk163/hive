"""docs/AGENT快速上手.md + docs/操作手册.md 结构验收（升级 T-0166 强验收）。

T-0166 原本只用 `python3 -c` 检查两篇文档末尾是否多了 FAQ/术语表。这里升级为
pytest，除必备小节之外还校验：小节顺序、代码围栏配对、无占位标记，以及
FAQ 里真的落了卡面点名的关键问题（claimable / verify 手跑 / block vs abort 等）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_DOC = ROOT / "docs" / "AGENT快速上手.md"
MANUAL_DOC = ROOT / "docs" / "操作手册.md"

AGENT_REQUIRED_SECTIONS = ["## FAQ", "## 术语表"]
MANUAL_REQUIRED_SECTIONS = ["## FAQ"]

AGENT_FAQ_TOPICS = [
    "claimable",
    "verify",
    "git commit",
    "--dir",
    "block",
    "abort",
    "paths",
]

GLOSSARY_TERMS = [
    "task",
    "inbox",
    "claim",
    "worktree",
    "lease",
    "dispatch",
    "land",
    "watchdog",
]

MANUAL_FAQ_TOPICS = [
    "verify",
    "并行度",
    "land",
    "watchdog",
]

PLACEHOLDERS = ("TODO", "待补", "占位")

FENCE_RE = re.compile(r"^\s*>?\s*```", re.MULTILINE)


def _agent_text() -> str:
    assert AGENT_DOC.exists(), AGENT_DOC
    return AGENT_DOC.read_text(encoding="utf-8")


def _manual_text() -> str:
    assert MANUAL_DOC.exists(), MANUAL_DOC
    return MANUAL_DOC.read_text(encoding="utf-8")


def extract_section(text: str, name: str) -> str:
    """提取 `## name` 小节正文，直到下一个 `## ` 标题（忽略 `### ` 子标题）。"""
    m = re.search(r"^## " + re.escape(name) + r"\s*$(.*?)(?=^## |\Z)",
                  text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


# -- AGENT 快速上手 ----------------------------------------------------------- #

def test_agent_doc_has_required_sections():
    text = _agent_text()
    for section in AGENT_REQUIRED_SECTIONS:
        assert section in text, f"AGENT快速上手 缺必备小节：{section}"


def test_agent_doc_required_sections_in_order():
    """术语表必须排在 FAQ 之后（作为最后一节）。"""
    text = _agent_text()
    pos_faq = text.index("## FAQ")
    pos_glossary = text.index("## 术语表")
    assert pos_faq < pos_glossary, "术语表应排在 FAQ 之后"


def test_agent_doc_no_placeholder_markers():
    text = _agent_text()
    for marker in PLACEHOLDERS:
        assert marker not in text, f"AGENT快速上手 含未完成标记：{marker}"


def test_agent_doc_code_fences_balanced():
    text = _agent_text()
    fences = FENCE_RE.findall(text)
    assert len(fences) >= 2
    assert len(fences) % 2 == 0, (
        f"AGENT快速上手 代码围栏不配对（{len(fences)} 个）"
    )


def test_agent_faq_covers_required_topics():
    """FAQ 必须覆盖卡面点名的每一个高频疑问。"""
    text = _agent_text()
    faq = extract_section(text, "FAQ")
    for topic in AGENT_FAQ_TOPICS:
        assert topic in faq, (
            f"AGENT FAQ 未覆盖高频疑问：{topic}"
        )


def test_agent_glossary_has_core_terms():
    """术语表要覆盖核心术语，不能只有几个字。"""
    text = _agent_text()
    glossary = extract_section(text, "术语表")
    for term in GLOSSARY_TERMS:
        assert term in glossary, f"术语表缺核心术语：{term}"


# -- 操作手册 ----------------------------------------------------------------- #

def test_manual_doc_has_required_sections():
    text = _manual_text()
    for section in MANUAL_REQUIRED_SECTIONS:
        assert section in text, f"操作手册 缺必备小节：{section}"


def test_manual_doc_no_placeholder_markers():
    text = _manual_text()
    for marker in PLACEHOLDERS:
        assert marker not in text, f"操作手册 含未完成标记：{marker}"


def test_manual_doc_code_fences_balanced():
    text = _manual_text()
    fences = FENCE_RE.findall(text)
    assert len(fences) >= 2
    assert len(fences) % 2 == 0, (
        f"操作手册 代码围栏不配对（{len(fences)} 个）"
    )


def test_manual_faq_covers_required_topics():
    """操作手册 FAQ 面向人视角，必须覆盖卡面点名的每一项。"""
    text = _manual_text()
    faq = extract_section(text, "FAQ")
    for topic in MANUAL_FAQ_TOPICS:
        assert topic in faq, f"操作手册 FAQ 未覆盖：{topic}"


def test_manual_faq_uses_qn_format():
    """FAQ 每条应以 Q1/Q2 或 Q1/Q2. 编号开头，方便跳转。"""
    text = _manual_text()
    faq = extract_section(text, "FAQ")
    qs = re.findall(r"Q\d", faq)
    assert len(qs) >= 4, (
        f"操作手册 FAQ 只解析到 {len(qs)} 个 Q，应有至少 4 条"
    )
