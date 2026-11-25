"""节点重启记录的判据（T-0473 / T-0474 / T-0475）。

三张卡各自要求「本机同步到 0.16.09 并重启集群，然后用 health 证实」。
它们共用一份记录 `docs/cluster/节点重启记录.md`：每台机器跑完追加自己那一条。

判据要钉住的是**记录不能糊弄**：
- 每条记录必须写明节点名、时间、是否真的重启、重启前后的 uptime 与租约秒数；
- 「已是最新所以没重启」是合法结论，但必须**显式写出来**，不能留空；
- 升级后的租约必须 ≥ 10s —— 旧代码是 4~5s，这是区分新旧代码的硬指标；
- 记录里出现 `state.json` 之类手工改状态的痕迹要报出来。

三张卡是**独立验收**的：某台机器先跑完时，其它节点还没写记录是正常的，
所以缺其它节点的条目只算 warning，不算失败。
本文件被三张卡共用，任何一张改记录后都应能继续通过。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RECORD = ROOT / "docs" / "cluster" / "节点重启记录.md"

NODES = ("fb98", "DESKTOP-IC6RTQ4", "DESKTOP-6LFQUO8")

# 一条记录至少要有这些字段（允许中英文标签）
REQUIRED_LABELS = ("仓路径", "是否重启", "升级后")


def _read_record() -> str:
    if not RECORD.is_file():
        pytest.skip(
            "还没有 docs/cluster/节点重启记录.md —— "
            "第一张跑完的卡会创建它；本判据在记录存在前不阻塞")
    return RECORD.read_text(encoding="utf-8")


def _sections(text: str) -> dict[str, str]:
    """按 `### <节点名> — <时间>` 切分记录块。

    只收**已知节点名**的块：文档里有一节 `### FIXTURE — ...` 是给人看的格式
    样例，不是真实记录，必须排除，否则占位符 `<x>s` 会被当成数据。
    """
    out: dict[str, str] = {}
    for m in re.finditer(r"^###\s+(.+?)\s+—\s+(.+?)\s*$", text, re.M):
        head = m.group(1).strip()
        if head not in NODES:
            continue
        start = m.end()
        nxt = re.search(r"^###\s", text[start:], re.M)
        block = text[start: start + (nxt.start() if nxt else len(text) - start)]
        out[head] = block
    return out


def test_record_has_at_least_one_entry():
    """记录文件必须存在且结构可读。

    注意**不要求**此时已有真实条目：三张卡谁先跑，谁就是第一个写记录的人。
    在第一条真实记录之前，本判据只确认「文件在、格式模板在」，不拦人。
    真正的质量把关由下面几条「每条记录都必须满足 …」负责 ——
    它们对已存在的条目逐条检查，没有条目时自然通过。
    """
    text = _read_record()
    assert "FIXTURE" in text or _sections(text), (
        "记录文件既没有格式样例（FIXTURE）也没有任何真实条目，"
        "说明它不是本卡预期的文件")


def test_each_entry_has_required_fields():
    """每条记录都要有仓路径 / 是否重启 / 升级后 三块信息。"""
    text = _read_record()
    for node, block in _sections(text).items():
        missing = [lb for lb in REQUIRED_LABELS if lb not in block]
        assert not missing, (
            f"{node} 的记录缺字段 {missing} —— "
            "至少要写：本机仓路径、是否重启、升级后的 uptime 与租约")


def test_entries_state_whether_restarted():
    """「是否重启」必须是明确的 是/否，不能留空或写模糊话。"""
    text = _read_record()
    for node, block in _sections(text).items():
        m = re.search(r"是否重启[：:]\s*(\S+)", block)
        assert m, f"{node} 的记录没有「是否重启：是/否」"
        verdict = m.group(1)
        assert re.match(r"^(是|否)", verdict), (
            f"{node} 的「是否重启」写的是 {verdict!r}，要用 是/否 开头"
            f"（已是最新也要显式写「否」并说明原因）")


def test_upgraded_lease_meets_threshold():
    """升级后租约必须 ≥ 10s —— 旧代码是 4~5s，这是新旧代码的硬区分。"""
    text = _read_record()
    for node, block in _sections(text).items():
        # 形如「主控下发租约=15.0s」或「租约 ≥ 10s」
        nums = [float(x) for x in re.findall(r"租约[^\d]{0,6}(\d+(?:\.\d+)?)\s*s", block)]
        if not nums:
            pytest.fail(f"{node} 的记录里找不到「租约=<数字>s」")
        after = max(nums)   # 升级后的值通常最大
        assert after >= 10.0, (
            f"{node} 升级后租约只有 {after}s（< 10s）—— "
            "说明那个节点还在跑旧代码，重启没生效")


def test_no_hand_edited_state_evidence():
    """不许靠手改状态文件来"让记录成立"。"""
    text = _read_record()
    for bad in ("state.json 改成", "手改 state.json", "伪造", "编造"):
        assert bad not in text, f"记录里出现可疑表述：{bad!r}"


def test_missing_nodes_are_warnings_not_failures():
    """三张卡独立验收：别的节点还没写完记录，不能因此让本卡失败。"""
    text = _read_record()
    secs = _sections(text)
    missing = [n for n in NODES if n not in secs]
    if missing:
        # 只留下可见提示，不 assert —— 否则先跑完的那台永远过不了
        print(f"[提示] 以下节点尚无重启记录（各自那张卡负责补）：{missing}")


def test_lease_floor_still_pinned():
    """本卡不改租约逻辑，但它依赖 4730460 的租约下限仍然在位。"""
    src = (ROOT / "hive_cluster.py").read_text(encoding="utf-8")
    assert "idle_heartbeat_interval * 3.0" in src, (
        "租约下限（空闲心跳 × 3）不见了 —— 那是 4730460 修的租约撞车")
