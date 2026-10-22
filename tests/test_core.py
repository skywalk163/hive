"""hive 核心行为测试：分派、体检、统计、领取、验收、归档。"""
from __future__ import annotations

import json
import subprocess
import sys

import hive
import pytest

from conftest import HIVE, sh


def show(world, ref: str) -> str:
    return sh(["git", "--git-dir", world.hive_origin, "show", ref]).stdout


# --------------------------------------------------------------------------- #
# 分派
# --------------------------------------------------------------------------- #

def test_dispatch_auto_plans_and_applies(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    world.add_agent("win", "verify", caps=["windows"])
    world.add_task("T-0001", needs=["freebsd"], paths=["src/"])
    world.add_task("T-0002", needs=["windows"])
    world.add_task("T-0003", needs=["windows"], paths=["src/"])   # 验收机不改代码
    world.add_task("T-0004", depends=["T-0001"])                  # 依赖未完成

    out = world.hive_json("dispatch")
    plan = {p["task"]: p["agent"] for p in out["plan"]}
    assert plan == {"T-0001": "fb", "T-0002": "win"}, out
    reasons = {s["task"]: s["reason"] for s in out["skipped"]}
    assert "T-0003" in reasons and "T-0004" in reasons

    # 默认 dry-run，不写
    assert world.card("T-0001")["owner"] is None

    world.hive("dispatch", "--apply")
    assert world.card("T-0001")["owner"] == "fb"
    assert world.card("T-0002")["owner"] == "win"
    assert (world.hive1 / "inbox" / "fb" / "T-0001.md").exists()

    # 都分完了，第二轮没有新计划
    assert world.hive_json("dispatch")["plan"] == []


def test_dispatch_respects_max_concurrency(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    for i in range(3):
        world.add_task(f"T-000{i + 1}")
    out = world.hive_json("dispatch")
    assert len(out["plan"]) == 1
    assert {s["task"] for s in out["skipped"]} == {"T-0002", "T-0003"}


# --------------------------------------------------------------------------- #
# 体检
# --------------------------------------------------------------------------- #

def test_doctor_ok(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    world.add_task("T-0001")
    p = world.hive("doctor")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "[ OK ]" in p.stdout


def test_doctor_detects_broken_card(world):
    (world.hive1 / "tasks" / "T-9999.md").write_text("不是任务卡\n", encoding="utf-8")
    p = world.hive("doctor", check=False)
    assert p.returncode != 0
    assert "[ERR ]" in p.stdout


# --------------------------------------------------------------------------- #
# 领取 / 验收 / 统计 / 归档 / 回收
# --------------------------------------------------------------------------- #

def test_claim_is_atomic(world, fake_cmd):
    world.add_agent("a1", "exec", command=fake_cmd)
    world.add_agent("a2", "exec", command=fake_cmd)
    world.add_task("T-0001")
    procs = [
        subprocess.Popen(
            [sys.executable, str(HIVE),
             "--dir", str(world.hive1 if name == "a1" else world.hive2),
             "claim", "T-0001", "--agent", name],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for name in ("a1", "a2")
    ]
    codes = [p.wait() for p in procs]
    assert sorted(codes) == [0, 1], codes
    claims = world.refs("refs/heads/claim/T-0001")
    assert len(claims) == 1
    data = json.loads(show(world, "claim/T-0001:claims/T-0001.json"))
    assert data["agent"] in ("a1", "a2")


def test_verify_mode_stats_and_close(world):
    world.add_agent("win", "verify", caps=["windows"])
    world.add_task("T-0001", needs=["windows"], verify=["true"])
    world.hive("assign", "T-0001", "--agent", "win")
    p = world.run_worker("win")
    assert p.returncode == 0, p.stdout + p.stderr

    res = json.loads(show(world, "res/T-0001:results/T-0001.json"))
    assert res["status"] == "done" and res["mode"] == "verify", res
    assert world.refs("refs/heads/task/T-0001") == []

    stats = json.loads(world.hive("stats", "--json").stdout)
    assert stats["summary"]["done"] == 1
    assert stats["summary"]["rate"] == 100.0

    world.hive("close", "T-0001", "--note", "ok")
    archived = json.loads(show(world, "main:results/T-0001.json"))
    assert archived["status"] == "done"
    assert world.refs("refs/heads/claim/T-0001") == []
    assert world.refs("refs/heads/res/T-0001") == []
    status = json.loads(world.hive("status", "--json").stdout)
    assert status[0]["result"] == "done"


def test_gc_reclaims_expired_claim(world, fake_cmd):
    world.add_agent("a1", "exec", command=fake_cmd)
    world.add_task("T-0001")
    world.hive("claim", "T-0001", "--agent", "a1")
    assert world.refs("refs/heads/claim/T-0001")
    world.hive("gc", "--minutes", "0")
    assert world.refs("refs/heads/claim/T-0001") == []


def test_retry_fresh_clears_archive(world):
    world.add_agent("win", "verify", caps=["windows"])
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "win")
    world.run_worker("win")
    world.hive("close", "T-0001")
    assert json.loads(show(world, "main:results/T-0001.json"))["status"] == "done"

    world.hive("retry", "T-0001", "--fresh")
    assert world.hive("status", "--json").returncode == 0
    # 归档已从 main 撤掉
    check = subprocess.run(
        ["git", "--git-dir", str(world.hive_origin), "show", "main:results/T-0001.json"],
        text=True, capture_output=True)
    assert check.returncode != 0
    assert world.card("T-0001")["fresh"] is True


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #

def test_parse_card_roundtrip(tmp_path):
    path = tmp_path / "T-0001.md"
    data = {
        "id": "T-0001", "title": "标题: 带冒号", "repo": "light", "base": "main",
        "state": "ready", "owner": None, "priority": "P1", "needs": ["windows"],
        "paths": ["src/"], "verify": ["true"], "depends_on": [],
        "lease_minutes": 30, "timeout_minutes": 10, "created": "2026-10-23T00:00:00+08:00",
    }
    hive.write_card(path, data, "# 正文\n")
    got, body = hive.parse_card(path)
    assert got["title"] == "标题: 带冒号"
    assert got["needs"] == ["windows"]
    assert got["lease_minutes"] == 30
    assert body == "# 正文\n"


def test_parse_card_rejects_bad_list(tmp_path):
    path = tmp_path / "T-0001.md"
    path.write_text("---\nid: T-0001\n  - 孤儿列表项\n---\n", encoding="utf-8")
    with pytest.raises(hive.HiveError):
        hive.parse_card(path)
