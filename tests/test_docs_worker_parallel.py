"""docs/design-worker-parallel.md 结构验收（升级 T-0161 强验收）。

T-0161 原本的 verify 只检查小节是否存在；这里把它升级为真正的 pytest：
除了必备小节齐全之外，还校验小节顺序、代码围栏配对、正文无占位标记，
以及关键结论（线程池 + 每任务心跳 + max_concurrency）真的写进去了——
防止文档缩水成"只留标题的骨架"。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "design-worker-parallel.md"

# 卡面 needs 里点名的必备小节（顺序即期望顺序）。
REQUIRED_SECTIONS = [
    "## 模型",
    "## 租约与心跳",
    "## 隔离保证",
    "## 错误隔离",
    "## 与 dispatch 的关系",
    "## 落地建议",
]

# 设计结论里必须出现的关键短语（缺一即说明文档没写到底）。
KEY_PHRASES = [
    "ThreadPoolExecutor",      # 推荐线程池
    "max_concurrency",         # 并发上限的语义来源
    "Semaphore",               # 槽位节流，防 worker 占光 inbox
    "worktree",                # 物理隔离的载体
]

PLACEHOLDERS = ("TODO", "待补")

# 代码围栏：一行以（可选空白 + 可选引用前缀 >）开头紧跟三个反引号。
# 兼容 blockquote 内的 ```sh / ``` 块（AGENT 文档里见过这种）。
FENCE_RE = re.compile(r"^\s*>?\s*```", re.MULTILINE)


def _text() -> str:
    assert DOC.exists(), DOC
    return DOC.read_text(encoding="utf-8")


def test_doc_exists_and_nontrivial():
    text = _text()
    # 少于 1000 字说明内容被抽空，强验收没意义。
    assert len(text) > 3000, f"文档过短（{len(text)} 字），可能被抽空"


def test_all_required_sections_present():
    text = _text()
    for section in REQUIRED_SECTIONS:
        assert section in text, f"缺少必备小节：{section}"


def test_required_sections_appear_in_expected_order():
    """必备小节应按需顺序出现，防止小节被搬错位置。"""
    text = _text()
    positions = [text.index(s) for s in REQUIRED_SECTIONS]
    assert positions == sorted(positions), (
        f"必备小节顺序错乱：{list(zip(REQUIRED_SECTIONS, positions))}"
    )


def test_code_fences_balanced():
    """``` 代码围栏必须成对，否则 Markdown 渲染会崩。"""
    text = _text()
    fences = FENCE_RE.findall(text)
    assert len(fences) >= 2, "文档没有任何代码块，围栏检查失去意义"
    assert len(fences) % 2 == 0, (
        f"代码围栏不配对（共 {len(fences)} 个），文档渲染会崩"
    )


def test_no_placeholder_markers_in_body():
    """正文不允许留 TODO/待补/占位 这类未完成标记。"""
    text = _text()
    for marker in PLACEHOLDERS:
        assert marker not in text, f"文档仍含未完成标记：{marker}"


def test_recommends_thread_pool_executor():
    """推荐方案必须点명 ThreadPoolExecutor，不能停在"罗列选项"。"""
    text = _text()
    for phrase in KEY_PHRASES:
        assert phrase in text, f"结论缺关键短语：{phrase}"


def test_discusses_heartbeat_per_task():
    """核心改动：每任务一条心跳线程（不再靠 contextmanager 包整轮）。"""
    text = _text()
    assert (
        "start_heartbeat" in text or "每任务一条心跳" in text
    ), "文档没交代每任务一条心跳的设计"


def test_mentions_risk_register():
    """落地建议应包含风险登记表（本卡明确要求标出风险点）。"""
    text = _text()
    assert "风险" in text, "文档没有风险登记内容"
