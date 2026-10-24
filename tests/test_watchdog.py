"""看门狗测试：worker 租约心跳 + 自动回收死掉/孤儿/僵尸卡。

覆盖：
- dry-run 只报告、不改任何东西；
- --apply 把候选卡退回 ready、清空 owner、+retries、删 claim 分支；
- 健康卡（claim 新鲜、agent 已登记）不被误杀；
- 心跳 _refresh_lease 能把 lease_until 往前推（快进）；
- doctor 能报出孤儿/僵尸。
"""
from __future__ import annotations

import json

import hive
import pytest

from conftest import HIVE, sh


# --------------------------------------------------------------------------- #
# 测试夹具 / 辅助
# --------------------------------------------------------------------------- #
def _set_state(world, tid, state, body_prefix=""):
    """把一张卡改成给定 state（可选在 body 顶部塞一行，如 assigned_at）。"""
    h = hive.Hive(world.hive1)
    card = h.card(tid)
    card["state"] = state
    body = card.get("_body", "")
    if body_prefix:
        body = body_prefix + "\n" + body
    h.save_card(card, body)
    h.commit_and_push(f"test: {tid} -> {state}", [f"tasks/{tid}.md"])


def _push_claim(world, tid, agent, lease_iso=None):
    """直接往 origin/claim/<tid> 推一个 claims json（可指定过期租约）。"""
    parent = (f"origin/claim/{tid}"
              if world.refs(f"refs/heads/claim/{tid}") else "origin/main")
    lu = lease_iso or hive.iso(hive.now() + hive.timedelta(minutes=180))
    ca = hive.iso(hive.now() - hive.timedelta(minutes=5))
    payload = json.dumps({
        "task": tid, "agent": agent, "claimed_at": ca,
        "lease_until": lu, "host": "test",
    }, ensure_ascii=False, indent=2)
    hive.commit_file_to_ref(world.hive1, f"claims/{tid}.json", payload,
                           f"claim/{tid}", parent, f"claim {tid} by {agent}")


def _rm_agent(world, name):
    p = world.hive1 / "agents" / f"{name}.json"
    if p.exists():
        p.unlink()


def _claim_lease(world, tid):
    """从 claim 分支读 lease_until。"""
    info = hive.Hive(world.hive1).claim_info(tid)
    return info.get("lease_until") if isinstance(info, dict) else None


# --------------------------------------------------------------------------- #
# dry-run：只报告，零改动
# --------------------------------------------------------------------------- #
def test_watchdog_dry_run_no_mutation(world, fake_cmd):
    world.add_agent("w1", "exec", command=fake_cmd)
    world.add_task("T-0901", verify=["true"])
    world.hive("assign", "T-0901", "--agent", "w1")
    # claim 租约已过期的卡
    _push_claim(world, "T-0901", "w1",
                lease_iso=hive.iso(hive.now() - hive.timedelta(minutes=5)))

    p = world.hive("watchdog", "--minutes", "0")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "stale" in (p.stdout + p.stderr), p.stdout + p.stderr

    # 什么都没改：state 还是 assigned，claim 分支还在
    assert world.card("T-0901")["state"] == "assigned"
    assert world.refs("refs/heads/claim/T-0901") != []


def test_watchdog_json_lists_candidates(world, fake_cmd):
    world.add_agent("w1", "exec", command=fake_cmd)
    world.add_task("T-0901", verify=["true"])
    world.hive("assign", "T-0901", "--agent", "w1")
    _push_claim(world, "T-0901", "w1",
                lease_iso=hive.iso(hive.now() - hive.timedelta(minutes=5)))

    p = world.hive("watchdog", "--minutes", "0", "--json")
    data = json.loads(p.stdout)
    reasons = {c["reason"] for c in data["candidates"]}
    assert "stale" in reasons
    assert data["would_reclaim"] >= 1
    assert data["reclaimed"] == 0  # dry-run 不动手


# --------------------------------------------------------------------------- #
# --apply：真正回收
# --------------------------------------------------------------------------- #
def test_watchdog_apply_reclaims_stale(world, fake_cmd):
    world.add_agent("w1", "exec", command=fake_cmd)
    world.add_task("T-0901", verify=["true"])
    world.hive("assign", "T-0901", "--agent", "w1")
    _push_claim(world, "T-0901", "w1",
                lease_iso=hive.iso(hive.now() - hive.timedelta(minutes=5)))

    world.hive("watchdog", "--minutes", "0", "--apply")

    card = world.card("T-0901")
    assert card["state"] == "ready", card
    assert not card.get("owner"), card.get("owner")
    assert int(card.get("retries") or 0) >= 1
    assert world.refs("refs/heads/claim/T-0901") == []  # 远程 claim 分支被删


def test_watchdog_orphan_running(world, fake_cmd):
    world.add_task("T-0901", verify=["true"])
    _set_state(world, "T-0901", "running")  # 跑着却没 claim 分支

    world.hive("watchdog", "--minutes", "0", "--apply")

    card = world.card("T-0901")
    assert card["state"] == "ready"
    assert not card.get("owner")


def test_watchdog_zombie(world, fake_cmd):
    world.add_agent("w1", "exec", command=fake_cmd)
    world.add_task("T-0901", verify=["true"])
    world.hive("assign", "T-0901", "--agent", "w1")
    world.hive("claim", "T-0901", "--agent", "w1")  # 合法 claim
    _rm_agent(world, "w1")  # 但 agent 登记被撤了 → 僵尸

    p = world.hive("watchdog", "--minutes", "0", "--apply")
    assert "zombie" in (p.stdout + p.stderr), p.stdout + p.stderr

    card = world.card("T-0901")
    assert card["state"] == "ready"
    assert not card.get("owner")


def test_watchdog_healthy_not_reclaimed(world, fake_cmd):
    world.add_agent("w1", "exec", command=fake_cmd)
    world.add_task("T-0901", verify=["true"])
    world.hive("assign", "T-0901", "--agent", "w1")
    world.hive("claim", "T-0901", "--agent", "w1")  # 新鲜 claim，agent 还在

    p = world.hive("watchdog", "--minutes", "0", "--apply")
    out = p.stdout + p.stderr
    assert "stale" not in out and "zombie" not in out and "orphan" not in out, out

    card = world.card("T-0901")
    assert card["state"] == "assigned"
    assert world.refs("refs/heads/claim/T-0901") != []


# --------------------------------------------------------------------------- #
# 租约心跳
# --------------------------------------------------------------------------- #
def test_heartbeat_refreshes_lease(world, fake_cmd):
    world.add_agent("w1", "exec", command=fake_cmd)
    world.add_task("T-0901", verify=["true"])
    world.hive("assign", "T-0901", "--agent", "w1")
    # 先放一个过期的 claim
    _push_claim(world, "T-0901", "w1",
                lease_iso=hive.iso(hive.now() - hive.timedelta(minutes=5)))

    before = _claim_lease(world, "T-0901")
    hive.Worker(world.hive1, "w1")._refresh_lease("T-0901")
    after = _claim_lease(world, "T-0901")

    assert after is not None
    # 心跳把租约续到了未来（fast-forward 成功）
    assert hive.parse_iso(after) > hive.now()
    # 顺带记了刷新时间
    info = hive.Hive(world.hive1).claim_info("T-0901")
    assert "refreshed_at" in info


# --------------------------------------------------------------------------- #
# doctor 增强
# --------------------------------------------------------------------------- #
def test_doctor_flags_orphan(world, fake_cmd):
    world.add_task("T-0901", verify=["true"])
    _set_state(world, "T-0901", "running")  # 孤儿

    p = world.hive("doctor", check=False)
    assert "孤儿" in p.stdout, p.stdout


def test_doctor_flags_zombie(world, fake_cmd):
    world.add_agent("w1", "exec", command=fake_cmd)
    world.add_task("T-0901", verify=["true"])
    world.hive("assign", "T-0901", "--agent", "w1")
    world.hive("claim", "T-0901", "--agent", "w1")
    _rm_agent(world, "w1")  # 僵尸

    p = world.hive("doctor", check=False)
    assert "僵尸" in p.stdout, p.stdout
