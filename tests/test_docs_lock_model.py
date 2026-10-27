"""docs/锁与冲突模型.md 结构验收（升级 T-0164 强验收）。

T-0164 原本只用 `python3 -c` 检查小节存在。这里升级为 pytest，除必备小节之外
还校验：小节顺序、代码围栏配对、无占位标记、以及关键概念的落地——
"分支隔离/claim/paths/locks/land" 五件套都要出现，且 `locks` 必须明确标注
为"规划中、尚未合入"（T-0164 卡面明确要求，避免读者误以为已上线）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "锁与冲突模型.md"

REQUIRED_SECTIONS = [
    "## 当前模型",
    "## paths 字段",
    "## 目录前缀匹配",
    "## 文件级锁 locks（规划）",
    "## 与 land 的关系",
    "## 填写建议",
]

# 四层保障：分支隔离 + claim 租约 + paths 互斥 + land 串行。
LAYER_KEYWORDS = ["分支隔离", "租约", "paths", "land"]

# locks 必须显式标注为规划中，避免读者以为已上线。
LOCK_NOT_SHIPPED_MARKERS = ["规划中", "尚未合入"]

PLACEHOLDERS = ("TODO", "待补", "占位")

FENCE_RE = re.compile(r"^\s*>?\s*```", re.MULTILINE)


def _text() -> str:
    assert DOC.exists(), DOC
    return DOC.read_text(encoding="utf-8")


def test_doc_exists_and_nontrivial():
    text = _text()
    assert len(text) > 2000, f"文档过短（{len(text)} 字），可能被抽空"


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


def test_four_layer_model_covered():
    """四层保障的每一层关键词都得在文中出现。"""
    text = _text()
    for kw in LAYER_KEYWORDS:
        assert kw in text, f"四层保障缺关键词：{kw}"


def test_locks_field_marked_as_planned():
    """locks 字段必须显式标注为"规划中/尚未合入"，不能假装已上线。"""
    text = _text()
    section = text.split("## 文件级锁 locks（规划）", 1)[1].split("## ", 1)[0]
    # 至少一个规划标记必须出现在 locks 小节里，而不是散落全文。
    assert any(m in section for m in LOCK_NOT_SHIPPED_MARKERS), (
        "locks 小节未标注"规划中/尚未合入"，读者会误以为已上线"
    )
    assert "T-0163" in section, "locks 小节应引用 T-0163 作为来源"


def test_paths_overlap_example_present():
    """目录前缀匹配节应给出可核对的例子（src/ vs src/a.py 冲突）。"""
    text = _text()
    section = text.split("## 目录前缀匹配", 1)[1].split("## ", 1)[0]
    assert "src/" in section and "src/a.py" in section, (
        "目录前缀匹配节缺具体路径示例，无法核对算法"
    )
    assert "_path_overlap" in section or "_paths_conflict" in section, (
        "目录前缀匹配节未引用实际实现函数名"
    )


def test_land_relationship_callout():
    """与 land 一节必须强调"干活不冲突 ≠ 合流不冲突"这条铁律。"""
    text = _text()
    section = text.split("## 与 land 的关系", 1)[1]
    # 用 "依赖 depends_on" 兜住这个观点：真要改同一文件就靠 depends_on 串行。
    assert "depends_on" in section, (
        "与 land 一节未强调 depends_on 串行，读者会误以为 paths 能解决合流冲突"
    )
