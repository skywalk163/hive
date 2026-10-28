"""Tests for dispatch_tracking module."""

from datetime import datetime, timezone, timedelta

import dispatch_tracking as dt


# ---- board_stage：九条分支 ---------------------------------------------

def test_board_stage_done():
    card = {"state": "done"}
    assert dt.board_stage(card) == dt.STAGE_DONE


def test_board_stage_closed_counts_as_done():
    card = {"state": "closed"}
    assert dt.board_stage(card) == dt.STAGE_DONE


def test_board_stage_blocked():
    card = {"state": "blocked"}
    assert dt.board_stage(card) == dt.STAGE_BLOCKED


def test_board_stage_conflict():
    card = {"state": "conflict"}
    assert dt.board_stage(card) == dt.STAGE_CONFLICT


def test_board_stage_failed():
    card = {"state": "failed"}
    assert dt.board_stage(card) == dt.STAGE_FAILED


def test_board_stage_running():
    card = {"state": "running"}
    assert dt.board_stage(card) == dt.STAGE_RUNNING


def test_board_stage_claimed_state():
    card = {"state": "claimed"}
    assert dt.board_stage(card) == dt.STAGE_CLAIMED


def test_board_stage_assigned_state():
    card = {"state": "assigned"}
    assert dt.board_stage(card) == dt.STAGE_CLAIMED


def test_board_stage_ready_with_owner():
    card = {"state": "ready", "owner": "trae-win"}
    assert dt.board_stage(card) == dt.STAGE_CLAIMED


def test_board_stage_ready_with_dispatched_to():
    card = {"state": "ready", "dispatched_to": "fb88-dsh"}
    assert dt.board_stage(card) == dt.STAGE_DISPATCHED


def test_board_stage_ready_plain():
    card = {"state": "ready"}
    assert dt.board_stage(card) == dt.STAGE_PENDING


def test_board_stage_unknown_state_falls_back_to_pending():
    card = {"state": "weird_new_state"}
    assert dt.board_stage(card) == dt.STAGE_PENDING


# ---- mark_dispatched ----------------------------------------------------

def test_mark_dispatched_returns_new_dict_and_does_not_mutate_input():
    card = {"state": "ready", "title": "test"}
    result = dt.mark_dispatched(card, "agent-x")
    assert result is not card
    assert "dispatched_to" not in card  # 原卡没被改
    assert result["dispatched_to"] == "agent-x"
    assert result["title"] == "test"  # 其它字段保留


def test_mark_dispatched_preserves_owner_on_claimed_card():
    card = {"state": "ready", "owner": "fb88-dsh"}
    result = dt.mark_dispatched(card, "trae-win")
    assert result["owner"] == "fb88-dsh"  # owner 保留
    assert result["dispatched_to"] == "trae-win"


def test_mark_dispatched_empty_agent_raises_value_error():
    card = {"state": "ready"}
    try:
        dt.mark_dispatched(card, "")
        assert False, "应当抛 ValueError"
    except ValueError:
        pass


def test_mark_dispatched_whitespace_agent_raises_value_error():
    card = {"state": "ready"}
    try:
        dt.mark_dispatched(card, "   ")
        assert False, "应当抛 ValueError"
    except ValueError:
        pass


def test_mark_dispatched_with_explicit_when():
    card = {"state": "ready"}
    when = "2026-10-28T10:00:00+08:00"
    result = dt.mark_dispatched(card, "agent-y", when=when)
    assert result["dispatched_at"] == when


# ---- clear_dispatched ---------------------------------------------------

def test_clear_dispatched_sets_none_not_deletes_keys():
    card = {"state": "ready", "dispatched_to": "agent-x", "dispatched_at": "2026-10-28T10:00:00+08:00"}
    result = dt.clear_dispatched(card)
    assert "dispatched_to" in result
    assert "dispatched_at" in result
    assert result["dispatched_to"] is None
    assert result["dispatched_at"] is None


# ---- is_dispatch_candidate ----------------------------------------------

def test_is_candidate_ready_no_dispatched_to():
    card = {"state": "ready"}
    assert dt.is_dispatch_candidate(card) is True


def test_is_candidate_not_ready_returns_false():
    card = {"state": "running"}
    assert dt.is_dispatch_candidate(card) is False


def test_is_candidate_with_owner_returns_false():
    card = {"state": "ready", "owner": "fb88-dsh"}
    assert dt.is_dispatch_candidate(card) is False


def test_is_candidate_within_ttl_returns_false():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=10)).isoformat()
    card = {"state": "ready", "dispatched_to": "agent-x", "dispatched_at": recent}
    assert dt.is_dispatch_candidate(card, dispatched_ttl_minutes=60, now=now) is False


def test_is_candidate_expired_ttl_returns_true():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=120)).isoformat()
    card = {"state": "ready", "dispatched_to": "agent-x", "dispatched_at": old}
    assert dt.is_dispatch_candidate(card, dispatched_ttl_minutes=60, now=now) is True


def test_is_candidate_invalid_dispatched_at_returns_true():
    card = {"state": "ready", "dispatched_to": "agent-x", "dispatched_at": "not-a-date"}
    assert dt.is_dispatch_candidate(card) is True


def test_is_candidate_missing_dispatched_at_returns_true():
    card = {"state": "ready", "dispatched_to": "agent-x"}
    assert dt.is_dispatch_candidate(card) is True


# ---- summarize_board ----------------------------------------------------

def test_summarize_board_counts_and_sorting():
    cards = {
        "T-0180": {"state": "done"},
        "T-0181": {"state": "running"},
        "T-0182": {"state": "ready", "owner": "fb88-dsh"},
        "T-0183": {"state": "ready", "dispatched_to": "trae-win"},
        "T-0184": {"state": "ready"},
        "T-0185": {"state": "ready"},
    }
    result = dt.summarize_board(cards)
    assert result["total"] == 6
    assert result["counts"]["done"] == 1
    assert result["counts"]["running"] == 1
    assert result["counts"]["claimed"] == 1
    assert result["counts"]["dispatched"] == 1
    assert result["counts"]["pending"] == 2
    # 零值阶段不出现
    assert "blocked" not in result["counts"]
    assert "conflict" not in result["counts"]
    assert "failed" not in result["counts"]
    # pending 列表按号排序
    assert result["pending"] == ["T-0184", "T-0185"]


def test_summarize_board_by_agent():
    cards = {
        "T-0183": {"state": "ready", "dispatched_to": "trae-win"},
        "T-0187": {"state": "ready", "owner": "fb88-dsh"},
    }
    result = dt.summarize_board(cards)
    assert "by_agent" in result
    assert result["by_agent"]["trae-win"] == ["T-0183"]
    assert result["by_agent"]["fb88-dsh"] == ["T-0187"]


def test_summarize_board_tasks_list_form():
    cards_list = {
        "tasks": [
            {"id": "T-0180", "state": "done"},
            {"id": "T-0181", "state": "ready"},
        ]
    }
    result = dt.summarize_board(cards_list)
    assert result["total"] == 2
    assert result["counts"]["done"] == 1
    assert result["counts"]["pending"] == 1
    assert result["pending"] == ["T-0181"]


def test_summarize_board_two_input_forms_equivalent():
    dict_form = {
        "T-0180": {"state": "done"},
        "T-0181": {"state": "ready", "dispatched_to": "agent-x"},
    }
    list_form = {
        "tasks": [
            {"id": "T-0180", "state": "done"},
            {"id": "T-0181", "state": "ready", "dispatched_to": "agent-x"},
        ]
    }
    r1 = dt.summarize_board(dict_form)
    r2 = dt.summarize_board(list_form)
    assert r1 == r2
