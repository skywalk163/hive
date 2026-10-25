"""文档结构回归守卫（T-0131）：操作手册的章节编号不许再乱。

以前手册里出现过 `### 0.`、`## 6.6` 重复、父章节 `## 6.` 排在子章节之后、
`## 8.1` 该是 `###` 却写成 `##`、以及四个 `## 6.x` 块夹在 §5 中间把 §5 拦腰截断。
编号一乱，人和 agent 都没法按「§N」定位。这个测试把规则钉死。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "docs" / "操作手册.md"

# 形如 "## 6. 标题" / "### 6.1 标题" / "### 6.1.1 标题"
HEADING = re.compile(r"^(#{2,3})\s+(\d+(?:\.\d+)*)\.?\s+(.*)$")


def _headings():
    """返回 [(层级, 编号字符串, 标题)]，按文档顺序。"""
    out = []
    for line in MANUAL.read_text(encoding="utf-8").split("\n"):
        m = HEADING.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2), m.group(3).strip()))
    return out


def test_manual_exists_and_has_headings():
    assert MANUAL.exists(), MANUAL
    assert len(_headings()) >= 10, "手册标题太少，解析是不是坏了？"


def test_top_level_numbers_strictly_increase_from_one():
    """顶层 `## N.` 必须从 1 开始、严格递增、无重复。"""
    nums = [int(num.split(".")[0]) for lvl, num, _ in _headings() if lvl == 2]
    assert nums, "没解析到任何顶层章节"
    assert nums[0] == 1, f"顶层编号应从 1 开始，实际 {nums[0]}"
    assert nums == sorted(nums), f"顶层编号不是递增的：{nums}"
    assert len(nums) == len(set(nums)), f"顶层编号有重复：{nums}"


def test_sub_sections_belong_to_current_top():
    """`###` 必须出现在某个 `##` 之后，且隶属于当前顶层编号。"""
    cur_top = None
    for lvl, num, _title in _headings():
        parts = [int(x) for x in num.split(".")]
        if lvl == 2:
            cur_top = parts[0]
            continue
        assert cur_top is not None, f"`### {num}` 出现在任何 `##` 之前"
        assert parts[0] == cur_top, f"`### {num}` 不属于当前顶层 §{cur_top}"


def test_sub_numbers_increase_without_duplicates():
    """同一顶层章节下的子编号要递增、不能重复（6.1.1 这种三级只看前两级）。"""
    cur_top = None
    seen: list[int] = []
    for lvl, num, _title in _headings():
        parts = [int(x) for x in num.split(".")]
        if lvl == 2:
            cur_top, seen = parts[0], []
            continue
        if len(parts) >= 3:
            # 三级（如 6.1.1 重试熔断）：只要求父级 N.M 已存在，不参与二级递增
            assert parts[1] in seen, f"§{num} 的父级 §{parts[0]}.{parts[1]} 不存在"
            continue
        sub = parts[1]
        assert sub not in seen, f"§{cur_top} 下子编号重复：{sub}"
        assert not seen or sub > seen[-1], (
            f"§{cur_top} 下子编号没有递增：{seen} -> {sub}")
        seen.append(sub)


def test_no_duplicate_full_numbers():
    seen: set[str] = set()
    for _lvl, num, _title in _headings():
        assert num not in seen, f"编号重复：{num}"
        seen.add(num)


def test_external_referenced_sections_still_mean_the_same():
    """README/CHANGELOG 引用了 §5.1（hive land）与 §8（已知坑 gc），编号不能漂。"""
    by_num = {num: title for _lvl, num, title in _headings()}
    assert "hive land" in by_num.get("5.1", ""), (
        f"§5.1 应该是 hive land，实际 {by_num.get('5.1')!r}")
    assert "gc" in by_num.get("8", ""), f"§8 应该是已知坑 gc，实际 {by_num.get('8')!r}"


def test_draft_template_points_agent_to_quickstart():
    """`hive draft new` 的骨架必须带「先读 AGENT快速上手.md」指引（T-0137）。

    新 agent 第一次接触 hive 往往就从一张 draft 卡开始；骨架里没有这句，
    阅读指引全靠口口相传，丢过一次（raccoon 的建议，别再丢第二次）。
    """
    hive_py = (ROOT / "hive.py").read_text(encoding="utf-8")
    m = re.search(r'DRAFT_TEMPLATE\s*=\s*"""(.*?)"""', hive_py, re.DOTALL)
    assert m, "hive.py 里找不到 DRAFT_TEMPLATE"
    assert "AGENT快速上手" in m.group(1), (
        "DRAFT_TEMPLATE 应提示第一次接 hive 的 agent 先读 docs/AGENT快速上手.md")
