"""dispatch_tracking — 派发流水线的 dispatched_to 状态机内核。

纯标准库模块，不 import hive.py，无副作用。
把「待分配 / 已推送 / 已被领走 / 跑到一半」统一成一套可判定的阶段。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone, timedelta
from typing import Any

# ---- 阶段常量 ----------------------------------------------------------

STAGE_PENDING = "pending"
STAGE_DISPATCHED = "dispatched"
STAGE_CLAIMED = "claimed"
STAGE_RUNNING = "running"
STAGE_DONE = "done"
STAGE_FAILED = "failed"
STAGE_CONFLICT = "conflict"
STAGE_BLOCKED = "blocked"

# ---- 内部工具 ----------------------------------------------------------

def _now_iso_local() -> str:
    """返回当前本地时区时间的 ISO8601 秒级字符串（带偏移量）。"""
    # datetime.now(timezone.utc) 拿到 UTC，再转本地偏移
    utc_now = datetime.now(timezone.utc)
    # 计算本地与 UTC 的偏移
    local_now = datetime.now()
    offset = local_now.replace(tzinfo=None) - utc_now.replace(tzinfo=None).replace(tzinfo=None)
    # 简化：直接用本地时间 + 固定偏移
    # 更可靠的方式：用 astimezone
    tz_aware = utc_now.astimezone()
    return tz_aware.isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime | None:
    """宽松解析 ISO8601 时间字符串，失败返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        # Python 3.11+ fromisoformat 支持带 Z 的格式；手动兼容
        v = value.strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return datetime.fromisoformat(v)
    except (ValueError, TypeError):
        return None


# ---- board_stage -------------------------------------------------------

def board_stage(card: dict[str, Any]) -> str:
    """根据任务卡字段判定其在看板上的阶段（先命中先返回）。"""
    state = (card.get("state") or "").lower()
    owner = card.get("owner")
    dispatched_to = card.get("dispatched_to")

    # 1. done / closed
    if state in ("done", "closed"):
        return STAGE_DONE
    # 2. blocked
    if state == "blocked":
        return STAGE_BLOCKED
    # 3. conflict
    if state == "conflict":
        return STAGE_CONFLICT
    # 4. failed
    if state == "failed":
        return STAGE_FAILED
    # 5. running
    if state == "running":
        return STAGE_RUNNING
    # 6. claimed / assigned 或有 owner
    if state in ("claimed", "assigned") or (owner is not None and str(owner).strip() != ""):
        return STAGE_CLAIMED
    # 7. ready 且已推送
    if state == "ready" and dispatched_to not in (None, ""):
        return STAGE_DISPATCHED
    # 8. 其它 ready
    if state == "ready":
        return STAGE_PENDING
    # 9. 兜底
    return STAGE_PENDING


# ---- mark_dispatched / clear_dispatched --------------------------------

def mark_dispatched(
    card: dict[str, Any],
    agent_name: str,
    *,
    when: str | None = None,
) -> dict[str, Any]:
    """标记该卡已推送给某 agent，返回新 dict（deepcopy 语义）。"""
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise ValueError("agent_name 不能为空或仅含空白")

    new_card = deepcopy(card)
    new_card["dispatched_to"] = agent_name.strip()

    if when is not None:
        new_card["dispatched_at"] = when
    else:
        new_card["dispatched_at"] = _now_iso_local()

    return new_card


def clear_dispatched(card: dict[str, Any]) -> dict[str, Any]:
    """清除推送标记，字段置 None（不删键），返回新 dict。"""
    new_card = deepcopy(card)
    new_card["dispatched_to"] = None
    new_card["dispatched_at"] = None
    return new_card


# ---- is_dispatch_candidate ---------------------------------------------

def is_dispatch_candidate(
    card: dict[str, Any],
    *,
    dispatched_ttl_minutes: int = 60,
    now: datetime | None = None,
) -> bool:
    """判断该卡是否适合再推送给某个 agent。"""
    state = (card.get("state") or "").lower()
    owner = card.get("owner")

    # 必须是 ready 且无 owner
    if state != "ready":
        return False
    if owner is not None and str(owner).strip() != "":
        return False

    dispatched_to = card.get("dispatched_to")
    # 没推过 → 候选
    if dispatched_to in (None, ""):
        return True

    # 推过，看 TTL
    dispatched_at_raw = card.get("dispatched_at")
    dt = _parse_iso(dispatched_at_raw if isinstance(dispatched_at_raw, str) else "")
    if dt is None:
        # 时间缺失或无法解析 → 视为过期，允许再推
        return True

    # 统一为 aware datetime 做比较
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if dt.tzinfo is None:
        # naive 时间按 UTC 处理
        dt = dt.replace(tzinfo=timezone.utc)

    age = now - dt
    ttl_delta = timedelta(minutes=dispatched_ttl_minutes)
    if age < ttl_delta:
        # 还在 TTL 内，刚推过，别重复推
        return False
    # 超过 TTL，推了没领，允许再推
    return True


# ---- summarize_board ----------------------------------------------------

def summarize_board(cards: Any) -> dict[str, Any]:
    """对全量任务卡做看板汇总。

    支持两种输入形态：
      - {tid: card_dict} 字典
      - {"tasks": [card_dict, ...]} 含 tasks 键的字典（按 card["id"] 取号）
    """
    # 统一成 {tid: card}
    normalized: dict[str, dict[str, Any]] = {}
    if isinstance(cards, dict) and "tasks" in cards:
        for c in cards["tasks"]:
            if isinstance(c, dict) and "id" in c:
                normalized[str(c["id"])] = c
    elif isinstance(cards, dict):
        for k, v in cards.items():
            if isinstance(v, dict):
                normalized[str(k)] = v

    total = len(normalized)

    # 按阶段分桶
    stage_buckets: dict[str, list[str]] = {}
    by_agent: dict[str, list[str]] = {}

    for tid in sorted(normalized.keys()):
        card = normalized[tid]
        stage = board_stage(card)
        stage_buckets.setdefault(stage, []).append(tid)

        # by_agent：已推送或已领取的卡，按 agent 归类
        agent = card.get("dispatched_to") or card.get("owner")
        if agent:
            by_agent.setdefault(str(agent), []).append(tid)

    # counts 只含非零阶段
    counts = {s: len(tids) for s, tids in stage_buckets.items() if len(tids) > 0}

    result: dict[str, Any] = {
        "total": total,
        "counts": counts,
    }
    # 各阶段的 tid 列表（按号排序）
    for stage, tids in stage_buckets.items():
        result[stage] = sorted(tids)

    # by_agent 也按 agent 名排序
    if by_agent:
        result["by_agent"] = {a: sorted(tids) for a, tids in sorted(by_agent.items())}

    return result
