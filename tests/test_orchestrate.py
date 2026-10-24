"""总控进程测试：hive orchestrate（看门狗→分派→合入→体检 循环）+ 看门狗 orphan_grace。

覆盖：
- orchestrate --once 能单独跑「分派」阶段，把 ready 卡派给 exec agent；
- orchestrate --once 能单独跑「合入」阶段，把 done 卡 land 并 close；
- orchestrate --once 能单独跑「看门狗」阶段，回收僵尸卡；
- orchestrate 在 orphan_grace 内不会误杀刚分派的卡；
- orchestrate --iterations N 跑满 N 轮后退，不无限循环；
- watchdog 的 orphan_assigned 在 grace 内不回收、超过才回收（分派时间从 inbox 读）。
"""
from __future__ import annotations

import re

import pytest

from conftest import HIVE, sh


def _rm_agent(world, name):
    p = world.hive1 / "agents" / f"{name}.json"
    if p.exists():
        p.unlink()


def _rewrite_inbox_assigned_at(world, tid, owner, iso_str):
    p = world.hive1 / "inbox" / owner / f"{tid}.md"
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"^assigned_at:\s*\S+", f"assigned_at: {iso_str}", text,
                  flags=re.MULTILINE)
    p.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 分派阶段
# --------------------------------------------------------------------------- #
def test_orchestrate_once_dispatches(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb")})
    world.add_task("T-0001", repo="hive")

    p = world.hive("orchestrate", "--once",
                   "--no-land", "--no-watchdog", "--no-doctor")
    assert p.returncode == 0, p.stdout + p.stderr

    card = world.card("T-0001")
    assert card["owner"] == "fb", card
    assert (world.hive1 / "inbox" / "fb" / "T-0001.md").exists()


# --------------------------------------------------------------------------- #
# 合入阶段
# --------------------------------------------------------------------------- #
def test_orchestrate_once_lands_done(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")  # 跑出一张 done 的卡
    row = next(r for r in world.hive_json("status") if r["id"] == "T-0001")
    assert row["result"] == "done"

    p = world.hive("orchestrate", "--once",
                   "--no-dispatch", "--no-watchdog", "--no-doctor")
    assert p.returncode == 0, p.stdout + p.stderr

    # 卡被 land + close
    assert world.card("T-0001")["state"] == "done"
    blob = sh(["git", "--git-dir", world.code_origin, "show", "main:feature.txt"]).stdout
    assert "made by fb for T-0001" in blob


# --------------------------------------------------------------------------- #
# 看门狗阶段
# --------------------------------------------------------------------------- #
def test_orchestrate_once_reclaims_via_watchdog(world, fake_cmd):
    world.add_agent("w1", "exec", command=fake_cmd)
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "w1")
    world.hive("claim", "T-0001", "--agent", "w1")
    _rm_agent(world, "w1")  # agent 登记被撤 → 僵尸

    p = world.hive("orchestrate", "--once",
                   "--no-dispatch", "--no-land", "--no-doctor")
    assert p.returncode == 0, p.stdout + p.stderr

    card = world.card("T-0001")
    assert card["state"] == "ready"
    assert not card.get("owner")
    assert world.refs("refs/heads/claim/T-0001") == []


# --------------------------------------------------------------------------- #
# orphan_grace：刚分派的卡不被误杀
# --------------------------------------------------------------------------- #
def test_orchestrate_skips_fresh_assigned(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb")})
    world.add_task("T-0001", repo="hive")

    # 跑两轮：第一轮分派，第二轮看门狗（orphan_grace 默认 45 分钟）不该回收
    p = world.hive("orchestrate", "--iterations", "2", "--interval", "1",
                   "--no-land", "--no-doctor")
    assert p.returncode == 0, p.stdout + p.stderr

    card = world.card("T-0001")
    assert card["owner"] == "fb", card
    assert card["state"] == "assigned"


def test_watchdog_orphan_grace(world, fake_cmd):
    world.add_agent("w1", "exec", command=fake_cmd)
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "w1")  # inbox 写入 assigned_at=now

    # grace 内（默认 30 分钟）：刚分派，不该回收
    p = world.hive("watchdog", "--orphan-grace", "30", "--apply")
    assert p.returncode == 0, p.stdout + p.stderr
    assert world.card("T-0001")["state"] == "assigned"

    # 把 inbox 里的分派时间改成很久以前：应被回收
    _rewrite_inbox_assigned_at(world, "T-0001", "w1", "2000-01-01T00:00:00+00:00")
    p = world.hive("watchdog", "--orphan-grace", "30", "--apply")
    assert p.returncode == 0, p.stdout + p.stderr
    card = world.card("T-0001")
    assert card["state"] == "ready"
    assert not card.get("owner")


# --------------------------------------------------------------------------- #
# 循环退出条件
# --------------------------------------------------------------------------- #
def test_orchestrate_iterations(world, fake_cmd):
    # 所有阶段都关掉，纯验证循环会按 --iterations 退（不无限挂起）
    p = world.hive("orchestrate", "--iterations", "2", "--interval", "1",
                   "--json", "--no-dispatch", "--no-land",
                   "--no-watchdog", "--no-doctor")
    assert p.returncode == 0, p.stdout + p.stderr
    ticks = [l for l in p.stdout.splitlines() if l.strip()]
    assert len(ticks) == 2, ticks
