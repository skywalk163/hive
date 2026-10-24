"""重试熔断（T-0130）：自动回收达 max_retries 后转 blocked，杜绝无限重派。"""
from __future__ import annotations

import json

import hive


def _setup_orphan(world, tid="T-0001", retries=0, max_retries=3):
    """造一张「判死待回收」的卡：state=running 但 claim 分支不存在（orphan_running）。"""
    world.add_task(tid, verify=["true"])
    h = hive.Hive(world.hive1)
    card = h.card(tid)
    card["state"] = "running"
    card["retries"] = retries
    card["max_retries"] = max_retries
    h.save_card(card, card.get("_body", ""))
    h.commit_and_push(f"test: setup {tid}", [f"tasks/{tid}.md"])
    world.sync()


def _after_watchdog(world, tid="T-0001"):
    world.hive("watchdog", "--apply")
    world.sync()
    return world.card(tid)


# -- 额度内：正常回收回 ready ------------------------------------------------ #
def test_reclaim_within_cap_returns_to_ready(world):
    _setup_orphan(world, retries=0, max_retries=3)
    card = _after_watchdog(world)
    assert card["state"] == "ready", card
    assert card["retries"] == 1, card


def test_reclaim_consumes_cap_one_by_one(world):
    """每次回收消耗一次额度，用尽前都回 ready。"""
    _setup_orphan(world, retries=2, max_retries=3)
    card = _after_watchdog(world)
    assert card["state"] == "ready", card
    assert card["retries"] == 3, card


# -- 额度用尽：转 blocked，不再回 ready -------------------------------------- #
def test_reclaim_at_cap_blocks_instead_of_ready(world):
    _setup_orphan(world, retries=3, max_retries=3)
    card = _after_watchdog(world)
    assert card["state"] == "blocked", card
    assert card["state"] != "ready", "额度用尽还回 ready 就是无限重派"


def test_blocked_card_does_not_keep_counting_retries(world):
    """转 blocked 不该继续 +retries（否则数字会一直涨，看不出已经停了）。"""
    _setup_orphan(world, retries=3, max_retries=3)
    card = _after_watchdog(world)
    assert card["retries"] == 3, card


def test_max_retries_zero_blocks_on_first_chance(world):
    """max_retries=0 表示一次都不许自动重派，不是「不限」。"""
    _setup_orphan(world, retries=0, max_retries=0)
    card = _after_watchdog(world)
    assert card["state"] == "blocked", card


# -- 真的退出流水线 ---------------------------------------------------------- #
def test_blocked_card_is_skipped_by_dispatch(world):
    world.add_agent("fb", "exec", command="true")  # dispatch 要求至少登记一个 agent
    _setup_orphan(world, retries=3, max_retries=3)
    _after_watchdog(world)
    world.hive("dispatch", "--apply")
    world.sync()
    card = world.card("T-0001")
    assert card["state"] == "blocked", card
    assert not card.get("owner"), card


def test_watchdog_json_marks_block_action(world):
    _setup_orphan(world, retries=3, max_retries=3)
    data = world.hive_json("watchdog", "--json")
    acts = {c["id"]: c["action"] for c in data["candidates"]}
    assert acts.get("T-0001") == "block", data
    assert data["would_block"] == 1, data
    assert data["would_reclaim"] == 0, data


def test_doctor_names_exhausted_card(world):
    _setup_orphan(world, retries=3, max_retries=3)
    _after_watchdog(world)
    p = world.hive("doctor", check=False)
    assert "重试额度烧光" in (p.stdout + p.stderr), p.stdout + p.stderr


# -- 人工显式 retry 不受熔断限制 --------------------------------------------- #
def test_explicit_retry_still_allowed_past_cap(world):
    """人在明确要求重跑时照做（只是会提醒），熔断只管自动路径。"""
    _setup_orphan(world, retries=3, max_retries=3)
    _after_watchdog(world)
    assert world.card("T-0001")["state"] == "blocked"

    world.hive("retry", "T-0001")
    world.sync()
    card = world.card("T-0001")
    assert card["state"] == "assigned", card
