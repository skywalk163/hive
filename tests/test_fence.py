"""租约 fencing token + 本地留底（T-0206）。

覆盖三件事：
1. 领取时写到 claim 分支的 claim 记录带 lease_id / fence；
2. 旧 worker 复活后写结果，必须被 LeaseFenced 拦住（不覆盖别人的结果）；
3. 卡面 fence 被回收/重派推进后，旧结果在 land 处被识别为过期。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import hive  # noqa: E402
from conftest import World, sh  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_lease_dir(monkeypatch, tmp_path):
    """本机留底目录指向临时目录，别污染开发机 ~/.hive/leases。"""
    monkeypatch.setenv("HIVE_LEASE_DIR", str(tmp_path / "leases"))


def _claim_payload(world: World, tid: str) -> dict:
    out = sh(["git", "--git-dir", world.hive_origin, "show",
              f"claim/{tid}:claims/{tid}.json"]).stdout
    return json.loads(out)


def test_claim_records_lease_id_and_fence(world, fake_cmd):
    world.add_agent("a1", "exec", command=fake_cmd)
    world.add_task("T-0001")
    world.hive("assign", "T-0001", "--agent", "a1")
    world.run_worker("a1")
    payload = _claim_payload(world, "T-0001")
    assert payload["lease_id"], payload
    assert int(payload["fence"]) >= 1, payload
    assert payload["agent"] == "a1"
    # 结果也带上了同一版租约的 fencing 信息
    res = world.hive_json("result", "T-0001")
    assert res["lease_id"] == payload["lease_id"]
    assert int(res["fence"]) == int(payload["fence"])


def test_stale_worker_write_is_fenced(world, fake_cmd):
    """模拟：旧 worker 抢到租约 → 租约被 gc 删掉并被别人接管 → 旧 worker 写结果。

    结果必须被 LeaseFenced 拒绝，远端 res 分支保持不存在（不覆盖新持有者）。
    """
    world.add_agent("a1", "exec", command=fake_cmd)
    world.add_agent("a2", "exec", command=fake_cmd)
    world.add_task("T-0002")
    world.hive("assign", "T-0002", "--agent", "a1")
    # 只 claim，不跑任务：模拟「旧 worker 拿卡后卡住」
    world.hive("claim", "T-0002", "--agent", "a1")

    # 旧 worker 进程内存里的租约副本（fencing 拿它和远端对账）
    stale = json.loads(sh(["git", "--git-dir", world.hive_origin, "show",
                           "claim/T-0002:claims/T-0002.json"]).stdout)
    # 回收：删 claim 分支，卡面 fence +1，然后 a2 重新领取
    world.hive("unassign", "T-0002", "--reason", "测试回收")
    world.hive("retry", "T-0002", "--agent", "a2")
    world.hive("claim", "T-0002", "--agent", "a2")

    w = hive.Worker(str(world.hive1), "a1")
    w._lease["T-0002"] = stale
    hive.save_local_lease("T-0002", stale)
    with pytest.raises(hive.LeaseFenced):
        w._write_result("T-0002", {"task": "T-0002", "status": "done",
                                   "agent": "a1", "verify": []})
    # res 分支没被旧 worker 写出来
    assert world.result_refs("T-0002") == []


def test_reclaim_bumps_fence_and_land_rejects_old_result(world, fake_cmd):
    """卡面 fence 推进后，旧 fence 的 done 结果不能 land。"""
    world.add_agent("a1", "exec", command=fake_cmd)
    world.add_task("T-0003", title="x", verify=["true"])
    world.hive("assign", "T-0003", "--agent", "a1")
    world.run_worker("a1")
    res = world.hive_json("result", "T-0003")
    assert int(res["fence"]) >= 1

    # 人工回收一次 → 卡面 fence 比旧结果大一格
    world.hive("unassign", "T-0003", "--reason", "人工回收")
    card = world.card("T-0003")
    assert int(card.get("fence") or 0) > int(res["fence"])

    # 旧结果的 res 分支被回收时删掉了；手动造一条带旧 fence 的结果回来
    sh(["git", "--git-dir", world.hive_origin, "update-ref", "-d",
        "refs/heads/res/T-0003"], check=False)
    world.hive("retry", "T-0003", "--agent", "a1")
    # retry 又推进一次；把旧结果按原样塞回 res 分支
    import hive as _h  # noqa: F401
    h = hive.Hive(str(world.hive1))
    h.fetch()
    hive.commit_file_to_ref(
        h.dir, "results/T-0003.json",
        json.dumps({**res, "status": "done"}, ensure_ascii=False),
        "res/T-0003", "origin/main", "old result", force=True)
    plan = h.land_one("T-0003", apply=False)
    assert plan["status"] == "fenced", plan
    assert any("过期租约" in p for p in plan["problems"]), plan


def test_lease_cli_lists_and_clears(world, fake_cmd, tmp_path):
    world.add_agent("a1", "exec", command=fake_cmd)
    world.add_task("T-0004")
    world.hive("assign", "T-0004", "--agent", "a1")
    world.hive("claim", "T-0004", "--agent", "a1")
    data = world.hive_json("lease", "list")
    assert "T-0004" in data, data
    assert data["T-0004"]["lease_id"]
    world.hive("lease", "clear", "T-0004")
    assert world.hive_json("lease", "list") == {}


def test_local_lease_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_LEASE_DIR", str(tmp_path / "l"))
    rec = {"task": "T-0009", "agent": "x", "lease_id": "abc", "fence": 3}
    assert hive.save_local_lease("T-0009", rec) is True
    got = hive.load_local_lease("T-0009")
    assert got["lease_id"] == "abc" and got["fence"] == 3
    assert "saved_at" in got
    hive.clear_local_lease("T-0009")
    assert hive.load_local_lease("T-0009") is None
