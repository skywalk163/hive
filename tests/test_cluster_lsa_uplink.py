"""T-0352：旧 HTTP 心跳面增量 LSA 上行回归。

覆盖卡面 §6 的新增用例（6 项 0.16.03/0.16.05 回归仍在 test_cluster.py 复跑）：

* 心跳 last_seen / 节点触碰不产生 LSA；
* 实质变更产生且仅产生 1 条字段级 diff（不是整卡快照）；
* 200ms 合并窗口内 N 次变更合成 1 条；
* LSA 邻居摘要不同时，心跳响应只回增量 diff，不带 progress/leases 全量；
* 空闲 5s 节拍不被 LSA 打破，空闲 tick 不出 change 包；
* 旧节点（不带 sd/lsa）兼容路径仍回全量；
* 300s 可见性 SLA 在 fake clock 下成立；
* 租约 claim 产生 LSA，纯续签（只推 lease_until）不产生第二条。
"""

from __future__ import annotations

import time

import hive_cluster as hc
import hive_lsa as lsa_mod


def _offline(tmp_path, name: str = "lsa", **kw) -> hc.Cluster:
    """只要内存状态、不启 HTTP/后台循环，方便确定性地摆弄 LSA 核心。"""
    cfg = hc.Config(
        state_dir=tmp_path / name, name=name,
        host="127.0.0.1", port=0, advertise="127.0.0.1",
        discover=False, hive_dir=tmp_path / "repo",
        election_min=kw.pop("election_min", 999.0),
        leader_lease=kw.pop("leader_lease", 5.0), **kw)
    return hc.Cluster(cfg)


def _fake_core(c: hc.Cluster, clock, rng=lambda: 0.5, epoch: int = 1) -> lsa_mod.LSACore:
    """注入自带 fake clock / fake rng 的核心，替换集群自建的那份。"""
    core = lsa_mod.LSACore(
        originator=c.node_id,
        area=lsa_mod.make_area("hive", "hive-dev", "hive-dev"),
        project_tag="hive-dev", leader_epoch=epoch,
        clock=clock, rng=rng, start_at=0.0)
    c._lsa = core
    return core


def _peer(nid: str = "p1") -> dict:
    return {"id": nid, "name": nid, "url": "http://127.0.0.1:9999",
            "incarnation": 1}


def _change_lsas(packets: list[dict]) -> list[dict]:
    out: list[dict] = []
    for pkt in packets:
        if pkt.get("type") == "lsa":
            out.extend(pkt.get("lsas") or [])
    return out


# --------------------------------------------------------------------------- #
# 1. last_seen / 触碰不产生 LSA
# --------------------------------------------------------------------------- #

def test_touch_last_seen_emits_no_lsa(tmp_path):
    c = _offline(tmp_path)
    core = _fake_core(c, lambda: 0.0)
    with c.lock:
        c._touch_self_last_seen_locked()
        c._update_node_locked(_peer("p1"), touch=True)
        core.tick(0.0)
        status = core.status()
    assert status["pending"] == 0, "last_seen 触碰不该登记变更"
    assert status["outbox"] == 0, "last_seen 触碰不该进待发队列"


# --------------------------------------------------------------------------- #
# 2/3. 实质变更：1 条字段级 diff；窗口内 N 次变更合并
# --------------------------------------------------------------------------- #

def test_progress_change_emits_one_field_level_diff(tmp_path):
    c = _offline(tmp_path)
    clock = [0.0]
    core = _fake_core(c, lambda: clock[0])
    with c.lock:
        c.publish_progress({"task": "T-1", "agent": "a", "state": "running",
                            "progress": 30, "step": "写测试"})
        # 合并窗口内第二次推进进度
        c.publish_progress({"task": "T-1", "agent": "a", "state": "running",
                            "progress": 55, "step": "写测试"})
        clock[0] = 0.3  # 越过 200ms 合并窗口
        out = core.tick(0.3)
    lsas = _change_lsas(out["packets"])
    assert len(lsas) == 1, "窗口内两次变更应合成 1 条 change LSA"
    diff = lsas[0]["diff"]
    assert diff["progress"] == 55
    assert diff["state"] == "running"
    # 字段级 diff：不带整卡快照，也不带本地记账元数据
    assert "gen" not in diff and "term" not in diff
    assert "updated_epoch" not in diff
    assert lsas[0]["key"] == ["hive", "T-1"]
    assert lsas[0]["kind"] == lsa_mod.CHANGE


def test_three_changes_within_window_merge_to_one(tmp_path):
    c = _offline(tmp_path)
    clock = [0.0]
    core = _fake_core(c, lambda: clock[0])
    with c.lock:
        for progress in (10, 20, 30):
            c.publish_progress({"task": "T-2", "agent": "b",
                                "state": "running", "progress": progress})
        clock[0] = 0.25
        out = core.tick(0.25)
    lsas = _change_lsas(out["packets"])
    assert len(lsas) == 1
    assert lsas[0]["diff"]["progress"] == 30


# --------------------------------------------------------------------------- #
# 4. LSA 心跳：摘要一致空载荷；不一致只回增量，绝不回全量
# --------------------------------------------------------------------------- #

def test_lsa_matching_digest_returns_empty_payload(tmp_path):
    c = _offline(tmp_path)
    core = _fake_core(c, lambda: 0.0)
    with c.lock:
        dig = core.build_digest()
    body = {"node": _peer(), "term": c.term, "leader": False,
            "lsa": {"seq": dig["seq"], "digest": dig["digest"],
                    "epoch": dig["leader_epoch"], "area": dig["area"]}}
    _, resp = c.handle_heartbeat(body, peer_ip="127.0.0.1")
    assert resp["progress"] == {}
    assert resp["leases"] == {}
    assert resp["lsa_diffs"] == []


def test_lsa_mismatch_response_carries_no_full_state(tmp_path):
    c = _offline(tmp_path)
    clock = [0.0]
    core = _fake_core(c, lambda: clock[0])
    with c.lock:
        c.publish_progress({"task": "T-1", "agent": "a",
                            "state": "running", "progress": 40})
        clock[0] = 0.3
        # 先把本机这条变更装进 LSDB（摘要水位推进）
        core.tick(0.3)
    # 对端是一台空 LSDB 的新节点：摘要必然不一致。
    peer = lsa_mod.LSACore(
        originator="peer", area=core.area, project_tag="hive-dev",
        leader_epoch=1, clock=lambda: 0.0, rng=lambda: 0.5)
    peer_dig = peer.build_digest()
    body = {"node": _peer(), "term": c.term, "leader": False,
            "lsa": {"seq": peer_dig["seq"], "digest": peer_dig["digest"],
                    "epoch": peer_dig["leader_epoch"], "area": peer_dig["area"]}}
    _, resp = c.handle_heartbeat(body, peer_ip="127.0.0.1")
    # 增量路径：响应里绝不带整份 progress / leases。
    assert resp["progress"] == {}
    assert resp["leases"] == {}
    # 摘要块本身也不允许夹带状态体字段。
    summary = resp["lsa"]
    for forbidden in lsa_mod.FORBIDDEN_IN_DIGEST:
        assert forbidden not in summary


def test_pending_change_is_served_as_incremental_diff(tmp_path):
    c = _offline(tmp_path)
    clock = [0.0]
    core = _fake_core(c, lambda: clock[0])
    with c.lock:
        c.publish_progress({"task": "T-9", "agent": "a",
                            "state": "running", "progress": 12})
        clock[0] = 0.3
        diffs = c._lsa_tick_locked()
    assert len(diffs) == 1
    assert diffs[0]["kind"] == lsa_mod.CHANGE
    assert diffs[0]["diff"]["progress"] == 12
    # 增量报文里没有整份节点表 / 全量状态键。
    for forbidden in ("nodes", "agents", "full_state", "progress", "leases"):
        assert forbidden not in diffs[0]


# --------------------------------------------------------------------------- #
# 5. 空闲节拍不被 LSA 打破
# --------------------------------------------------------------------------- #

def test_idle_heartbeat_interval_not_broken_by_lsa(tmp_path):
    c = _offline(tmp_path)
    _fake_core(c, lambda: 0.0)
    assert c._has_active_work_locked() is False
    assert c._heartbeat_interval_now() == c.config.idle_heartbeat_interval
    with c.lock:
        assert c._lsa_tick_locked() == [], "空闲期不该放出 change 包"


# --------------------------------------------------------------------------- #
# 6. 旧节点兼容：不带 sd/lsa 仍回全量（滚动升级不丢状态）
# --------------------------------------------------------------------------- #

def test_legacy_peer_without_digest_gets_full_state(tmp_path):
    c = _offline(tmp_path)
    with c.lock:
        c.progress["T-1"] = {"task": "T-1", "state": "running", "gen": 1,
                             "updated_epoch": time.time()}
        c.leases["T-1"] = {"task": "T-1", "gen": 1,
                           "lease_until_epoch": time.time() + 60,
                           "updated_epoch": time.time()}
    body = {"node": _peer(), "term": c.term, "leader": False}
    _, resp = c.handle_heartbeat(body, peer_ip="127.0.0.1")
    assert resp["progress"] and resp["leases"], "旧节点必须仍拿到全量"


# --------------------------------------------------------------------------- #
# 7. 300s 可见性 SLA（fake clock）
# --------------------------------------------------------------------------- #

def test_change_visible_within_sla_fake_clock(tmp_path):
    c = _offline(tmp_path)
    clock = [0.0]
    core = _fake_core(c, lambda: clock[0])
    with c.lock:
        c.publish_progress({"task": "T-1", "agent": "a", "state": "running"})
    # 设计目标 ≤10s；硬上限 SLA_VISIBILITY_S=300s。fake clock 推进到 10s 内
    # 必须放出 change LSA，不能被合并窗口 / 退避压住。
    clock[0] = 10.0
    out = core.tick(10.0)
    lsas = _change_lsas(out["packets"])
    assert lsas, "10s 内必须放出 change LSA"
    assert core.cfg.sla_visibility_s == lsa_mod.SLA_VISIBILITY_S == 300.0


# --------------------------------------------------------------------------- #
# 8. 租约 claim 产生 LSA；纯续签不产生第二条
# --------------------------------------------------------------------------- #

def test_lease_claim_notes_lsa_but_renewal_does_not(tmp_path):
    c = _offline(tmp_path)
    clock = [0.0]
    core = _fake_core(c, lambda: clock[0])
    with c.lock:
        c.publish_lease({"task": "T-1", "agent": "a", "host": "win10"})
        clock[0] = 0.3
        first = core.tick(0.3)
    claimed = _change_lsas(first["packets"])
    assert len(claimed) == 1
    assert claimed[0]["diff"]["agent"] == "a"

    with c.lock:
        # 纯续签：agent/host 不变，只把 lease_until 往后推。
        c.publish_lease({"task": "T-1", "agent": "a", "host": "win10",
                         "lease_until_epoch": time.time() + 9000})
        clock[0] = 6.0  # 同时越过 MinLSInterval，排除节流干扰
        second = core.tick(6.0)
    renewed = _change_lsas(second["packets"])
    assert renewed == [], "纯续签不该产生第二条 LSA"
