"""T-0220：派发中间态 —— TTL 对 dispatch 生效 + 「派发未领取」快速回收。

现场（T-0196）：卡从 blocked 退回 ready 后，orchestrate（180s 一轮）3 分钟内
连派 3 次给同一个 agent，每次只写 owner/state，不建 claim，agent 也没来领，
卡停在「已分派无人领」——面板显示进行中，实际没人在跑。

覆盖四条判据（每条都反向钉住旧实现的错误）：
1. 派发后卡面必须有 dispatched_to + dispatched_at；
2. 派发后 unassign → TTL 内 is_dispatch_candidate() == False（不再立刻重派）；
3. assigned 且无 claim、assigned_at 11 分钟前 → 回收成 ready、dispatch_miss == 1、
   inbox 残留被清掉；
4. dispatch_miss == 2 → 转 blocked，且不再出现在 dispatch 计划里。
外加：store 里 state 与卡面不一致的卡不派（claim 注定失败）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import dispatch_tracking as dt
import hive
import hive_store


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _add_hive_agent(world, name: str = "fb"):
    """登记一个能接 hive 仓的 exec agent（dispatch 只看登记信息，不真跑）。"""
    world.add_agent(name, "exec", repos={"hive": world.code_clone(name)})


def _rewrite_inbox_assigned_at(world, tid: str, owner: str, when: datetime) -> None:
    p = world.hive1 / "inbox" / owner / f"{tid}.md"
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"^assigned_at:\s*\S+",
                  f"assigned_at: {when.isoformat(timespec='seconds')}",
                  text, flags=re.MULTILINE)
    p.write_text(text, encoding="utf-8")


def _minutes_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=n)


def _set_card_field(world, tid: str, key: str, value) -> None:
    """直接改卡面字段（模拟历史遗留卡）。dispatch 只 fetch 不 reset，工作区改动有效。"""
    path = world.hive1 / "tasks" / f"{tid}.md"
    data, body = hive.parse_card(path)
    data[key] = value
    hive.write_card(path, data, body)


# --------------------------------------------------------------------------- #
# 判据 1：派发写 dispatched_to / dispatched_at
# --------------------------------------------------------------------------- #
def test_dispatch_writes_pack_markers(world):
    _add_hive_agent(world)
    world.add_task("T-0001", repo="hive")

    p = world.hive("dispatch", "--apply")
    assert p.returncode == 0, p.stdout + p.stderr

    card = world.card("T-0001")
    assert card["state"] == "assigned"
    assert card["owner"] == "fb"
    # 旧 _assign_local 只写 owner/state，这两个字段必须补上，否则 TTL 全是空转
    assert card["dispatched_to"] == "fb"
    assert card["dispatched_at"], card


# --------------------------------------------------------------------------- #
# 判据 2：unassign 之后 TTL 内不重复派（T-0196 的拉锯正犯在这里）
# --------------------------------------------------------------------------- #
def test_unassign_keeps_ttl_guard(world):
    _add_hive_agent(world)
    world.add_task("T-0001", repo="hive")
    world.hive("dispatch", "--apply")

    # 人工退回：unassign 语义不变，必须无条件执行
    p = world.hive("unassign", "T-0001", "--reason", "派错人了")
    assert p.returncode == 0, p.stdout + p.stderr

    card = world.card("T-0001")
    assert card["state"] == "ready"
    assert not card.get("owner")
    # dispatched_to 保留 = TTL 继续生效；退回旧实现这里会是 True
    assert card["dispatched_to"] == "fb"
    assert dt.is_dispatch_candidate(card) is False

    out = world.hive_json("dispatch", "--json")
    assert [x["task"] for x in out["plan"]] == []
    assert any(x["task"] == "T-0001" and "TTL" in x["reason"]
               for x in out["skipped"]), out["skipped"]


# --------------------------------------------------------------------------- #
# 判据 3：派发未领取的快速回收（默认 10 分钟）
# --------------------------------------------------------------------------- #
def test_watchdog_reclaims_unclaimed_and_clears_inbox(world):
    _add_hive_agent(world, "w1")
    world.add_task("T-0001", repo="hive")
    world.hive("assign", "T-0001", "--agent", "w1")
    assert (world.hive1 / "inbox" / "w1" / "T-0001.md").exists()

    _rewrite_inbox_assigned_at(world, "T-0001", "w1", _minutes_ago(11))
    p = world.hive("watchdog", "--apply")
    assert p.returncode == 0, p.stdout + p.stderr

    card = world.card("T-0001")
    assert card["state"] == "ready", card
    assert not card.get("owner")
    assert card["dispatch_miss"] == 1
    assert "无人领取" in card["_body"]
    # 收件箱残留必须撕掉，否则 agent 下一轮还会照着旧信号来领
    assert not (world.hive1 / "inbox" / "w1" / "T-0001.md").exists()


def test_unclaimed_threshold_is_configurable(world):
    _add_hive_agent(world, "w1")
    world.add_task("T-0001", repo="hive")
    world.hive("assign", "T-0001", "--agent", "w1")
    _rewrite_inbox_assigned_at(world, "T-0001", "w1", _minutes_ago(3))

    # 默认 10 分钟：3 分钟还没到，不回收（刚派出去，留给 agent 反应）
    world.hive("watchdog", "--apply")
    assert world.card("T-0001")["state"] == "assigned"

    # 把阈值调到 1 分钟：同一张卡应被快速回收
    world.hive("watchdog", "--apply",
               env={"HIVE_DISPATCH_UNCLAIMED_MINUTES": "1"})
    card = world.card("T-0001")
    assert card["state"] == "ready"
    assert card["dispatch_miss"] == 1


# --------------------------------------------------------------------------- #
# 判据 4：连续 2 次无人领 → 转人工，不再自动派
# --------------------------------------------------------------------------- #
def test_second_miss_blocks_manual_review(world):
    _add_hive_agent(world, "w1")
    world.add_task("T-0001", repo="hive")

    for expected in (1, 2):
        world.hive("assign", "T-0001", "--agent", "w1")
        _rewrite_inbox_assigned_at(world, "T-0001", "w1", _minutes_ago(11))
        world.hive("watchdog", "--apply")
        card = world.card("T-0001")
        assert card["dispatch_miss"] == expected, card
        if expected == 1:
            # 第一次只退回 ready，不判死
            assert card["state"] == "ready"

    card = world.card("T-0001")
    assert card["state"] == "blocked"
    assert "转人工" in card["_body"]


def test_ready_card_with_two_misses_never_planned(world):
    _add_hive_agent(world)
    world.add_task("T-0001", repo="hive")
    _set_card_field(world, "T-0001", "dispatch_miss", 2)

    out = world.hive_json("dispatch", "--json")
    assert [x["task"] for x in out["plan"]] == []
    assert any(x["task"] == "T-0001" and "转人工" in x["reason"]
               for x in out["skipped"]), out["skipped"]
    # dry-run 不改卡：仍然停在 ready，等人工决定 block / 关闭
    assert world.card("T-0001")["state"] == "ready"


# --------------------------------------------------------------------------- #
# store 模式：卡面 ready、store 还是 blocked → 不派（claim 的 CAS 注定失败）
# --------------------------------------------------------------------------- #
def test_store_state_mismatch_skips_dispatch(world, tmp_path):
    _add_hive_agent(world)
    world.add_task("T-0001", repo="hive")

    db = tmp_path / "hive-store.db"
    st = hive_store.Store(db, node_id="test")
    st.apply_op({"op_id": "seed-blocked", "type": "assign", "task_id": "T-0001",
                 "term": 0,
                 "payload": {"task": {"id": "T-0001", "state": "blocked"}}})
    st.close_conn()

    env = {"HIVE_CLAIM_BACKEND": "store", "HIVE_STORE_PATH": str(db)}
    p = world.hive("dispatch", "--json", env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    out = json.loads(p.stdout)
    assert [x["task"] for x in out["plan"]] == []
    assert any(x["task"] == "T-0001" and "store" in x["reason"]
               for x in out["skipped"]), out["skipped"]
