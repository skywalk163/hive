"""docs/架构与排障全景.md 结构验收（升级 T-0165 强验收）。

T-0165 原本只用 `python3 -c` 检查小节存在。这里升级为 pytest，除必备小节之外
还校验：小节顺序、代码围栏配对、无占位标记，以及"排障表"里真的落了
可检索的条目（不能只有表头），"四根支柱"关键词齐全。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "架构与排障全景.md"

REQUIRED_SECTIONS = [
    "## 架构总览",
    "## dispatch",
    "## worker",
    "## claim 与租约",
    "## land 与合流",
    "## watchdog 与 gc",
    "## 排障表",
]

# 架构总览里明确点名的四根支柱。
PILLARS = ["中心仓", "claim", "worktree", "land"]

# 排障表必须覆盖的典型现象（卡面点名的六类）。
TROUBLESHOOTING_TOPICS = [
    "dispatch",          # 不派
    "land",              # land 相关故障
    "assigned",          # 卡一直 assigned
    "verify",            # 验收恒过
    "paths",             # 两台机改同文件 / paths 越界
]

PLACEHOLDERS = ("TODO", "待补", "占位")

FENCE_RE = re.compile(r"^\s*>?\s*```", re.MULTILINE)


def _text() -> str:
    assert DOC.exists(), DOC
    return DOC.read_text(encoding="utf-8")


def test_doc_exists_and_nontrivial():
    text = _text()
    assert len(text) > 4000, f"文档过短（{len(text)} 字），可能被抽空"


def test_all_required_sections_present():
    text = _text()
    for section in REQUIRED_SECTIONS:
        assert section in text, f"缺少必备小节：{section}"


def test_required_sections_appear_in_expected_order():
    text = _text()
    positions = [text.index(s) for s in REQUIRED_SECTIONS]
    assert positions == sorted(positions), (
        f"必备小节顺序错乱：{list(zip(REQUIRED_SECTIONS, positions))}"
    )


def test_code_fences_balanced():
    text = _text()
    fences = FENCE_RE.findall(text)
    assert len(fences) >= 2, "文档没有任何代码块，围栏检查失去意义"
    assert len(fences) % 2 == 0, (
        f"代码围栏不配对（共 {len(fences)} 个），文档渲染会崩"
    )


def test_no_placeholder_markers_in_body():
    text = _text()
    for marker in PLACEHOLDERS:
        assert marker not in text, f"文档仍含未完成标记：{marker}"


def test_four_pillars_all_mentioned():
    """架构总览必须点名四根支柱。"""
    text = _text()
    overview = text.split("## 架构总览", 1)[1].split("## ", 1)[0]
    for pillar in PILLARS:
        assert pillar in overview, f"架构总览缺支柱：{pillar}"


def test_troubleshooting_table_has_content():
    """排障表不能只有表头——至少要覆盖 dispatch / land / verify 等典型现象。"""
    text = _text()
    tail = text.split("## 排障表", 1)[1]
    for topic in TROUBLESHOOTING_TOPICS:
        assert topic in tail, (
            f"排障表未覆盖关键现象：{topic}（卡面点名要覆盖）"
        )
    # 表格至少要有几行数据，不能只有一个空表头。
    rows = [ln for ln in tail.splitlines() if ln.strip().startswith("|")]
    assert len(rows) >= 8, (
        f"排障表只有 {len(rows)} 行，覆盖不足（应至少 8 行）"
    )


def test_three_git_refs_distinguished():
    """文档应区分三套 git ref：claim/<tid> / res/<tid> / task/<tid>。"""
    text = _text()
    for ref in ("claim/", "res/", "task/"):
        assert ref in text, f"文档未区分 git ref：{ref}"


def test_land_never_force_push():
    """合流章节必须明确"不自动解冲突、不 force push"这条红线。"""
    text = _text()
    land_section = text.split("## land 与合流", 1)[1].split("## ", 1)[0]
    assert "不 force push" in land_section or "不自动解" in land_section, (
        "land 节未强调"不自动解冲突、不 force push"的红线"
    )
