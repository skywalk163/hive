"""任务时间预算与异常卡定位的回归测试（2026-11-11）。

两件事：
1. 出题时一刀切给固定 timeout，复杂任务跑到一半就被 watchdog / 看板判异常；
   现在按 paths / verify / depends / needs / 正文长度估复杂度并给预算。
2. 看板「异常」列过去只有状态徽标，看不出该找哪台机器；现在带责任 agent。
   同时钉住 normalize() 白名单——上一轮 status 加了字段但前端没透传，
   导致 land_ready 分类修复实际没生效。
"""
from __future__ import annotations

import re
from pathlib import Path

import hive


def _app_js() -> str:
    return (Path(__file__).parents[1] / "dashboard" / "app.js").read_text(
        encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. 复杂度 → 时间预算
# --------------------------------------------------------------------------- #

def test_simple_task_gets_small_budget():
    b = hive.task_budget({"paths": [], "verify": [], "needs": [],
                          "depends_on": [], "priority": "P2"})
    assert b["level"] == "low"
    assert b["timeout_minutes"] == 60
    assert b["lease_minutes"] == 180


def test_complex_task_gets_more_time():
    b = hive.task_budget({
        "paths": [f"src/f{i}.py" for i in range(8)],
        "verify": ["pytest -q a", "pytest -q b", "pytest -q c"],
        "needs": ["code", "verify"],
        "depends_on": ["T-0001", "T-0002", "T-0003"],
        "priority": "P0",
    }, "x" * 5000)
    assert b["timeout_minutes"] >= 180
    # 租约必须覆盖整条交付链路，不能比执行超时还短
    assert b["lease_minutes"] >= int(b["timeout_minutes"] * 1.5) + 30


def test_explicit_complexity_overrides_auto_score():
    b = hive.task_budget({"complexity": "xl"})
    assert b["level"] == "xl"
    assert b["timeout_minutes"] == 240


def test_task_new_fills_budget_automatically(world):
    world.hive("task", "new", "--id", "T-9101", "--title", "改一行文档",
               "--repo", "light")
    simple = world.card("T-9101")
    assert simple["timeout_minutes"] == 60
    assert simple["lease_minutes"] == 180

    world.hive("task", "new", "--id", "T-9102", "--title", "大批改动",
               "--repo", "light", "--paths", "a.py", "b.py", "c.py", "d.py",
               "e.py", "f.py", "--verify", "pytest -q a",
               "--verify", "pytest -q b", "--needs", "code",
               "--body", "x" * 5000)
    complex_ = world.card("T-9102")
    assert complex_["timeout_minutes"] > simple["timeout_minutes"]
    assert complex_["lease_minutes"] > simple["lease_minutes"]


def test_task_new_explicit_timeout_wins(world):
    world.hive("task", "new", "--id", "T-9103", "--title", "人工指定",
               "--repo", "light", "--timeout", "300", "--lease", "400")
    c = world.card("T-9103")
    assert c["timeout_minutes"] == 300
    assert c["lease_minutes"] == 400


def test_retime_only_raises_never_lowers(world):
    world.hive("task", "new", "--id", "T-9104", "--title", "给短了",
               "--repo", "light", "--timeout", "10", "--lease", "20")
    world.hive("task", "new", "--id", "T-9105", "--title", "给宽裕了",
               "--repo", "light", "--timeout", "600", "--lease", "900")

    preview = world.hive("task", "retime").stdout
    assert "T-9104" in preview and "T-9105" not in preview
    assert "dry-run" in preview
    assert world.card("T-9104")["timeout_minutes"] == 10  # 预演不写盘

    world.hive("task", "retime", "--apply")
    assert world.card("T-9104")["timeout_minutes"] >= 60
    assert world.card("T-9104")["lease_minutes"] >= 180
    assert world.card("T-9105")["timeout_minutes"] == 600
    assert world.card("T-9105")["lease_minutes"] == 900


# --------------------------------------------------------------------------- #
# 2. 异常卡的责任 agent / 前端字段透传
# --------------------------------------------------------------------------- #

def test_status_exposes_agent_and_budget_fields(world):
    world.add_agent("a1")
    world.add_task("T-9106", title="字段检查")
    row = next(r for r in world.hive_json("status") if r["id"] == "T-9106")
    for key in ("result_agent", "abnormal_agent", "stale_suspected",
                "timeout_minutes", "lease_minutes", "budget_score",
                "budget_timeout", "budget_lease", "budget_note",
                "land_ready", "local_stale"):
        assert key in row, f"status 缺字段 {key}"


def test_normalize_whitelist_keeps_all_new_fields():
    """normalize() 是白名单；漏一个字段，看板就永远拿不到。"""
    m = re.search(r"function normalize\(t\) \{(.*?)\n  \}", _app_js(), re.S)
    assert m, "没找到 normalize()"
    body = m.group(1)
    for key in ("land_ready", "land_block", "local_stale", "stale_suspected",
                "result_agent", "abnormal_agent", "timeout_minutes",
                "lease_minutes", "budget_score", "budget_timeout",
                "budget_lease", "budget_note"):
        assert key in body, f"normalize() 没透传 {key}"


def test_abnormal_card_renders_agent_and_tight_budget():
    text = _app_js()
    assert "责任 agent" in text
    assert "超时偏紧" in text
    assert "时间预算" in text


def test_is_stale_respects_recent_progress():
    """还在上报进度的卡，不该因为租约刚过期就进异常列。"""
    text = _app_js()
    m = re.search(r"function isStale\(t\) \{(.*?)\n  \}", text, re.S)
    assert m
    assert "pushed_at" in m.group(1) and "15 * 60 * 1000" in m.group(1)
