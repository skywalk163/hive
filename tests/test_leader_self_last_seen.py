"""leader 自身 last_seen 刷新：state.json 里的本机记录不能停在启动时刻。

背景见 tasks/T-0297.md：`_update_node_locked(..., touch=True)` 只在收到**对端**
心跳/快照时执行，leader 收不到自己的心跳，于是节点表里本机记录的
`last_seen`/`last_seen_epoch` 一直停在进程启动时刻。`/api/cluster/state` 因为
`_public_node_locked()` 对 self 回填 `now` 看着是好的，但 `state.json` 上那个
旧值会让看板与体检脚本（T-0288 的 `leader_record_stale`）误判主节点已死。

这些用例不起 HTTP/后台线程：直接构造 Cluster，手动驱动 `_maintenance_once()`，
结果完全确定。判据只有三条：

1. 维护一拍之后，本机记录的 `last_seen_epoch` 与当前时间差 < 1s；
2. 同一拍必须把新值落进 `state.json`（看板/体检读的是盘）；
3. 只刷新「自己」—— 对端记录的存活时间与状态判定一个字节都不能变。
"""
from __future__ import annotations

import json
import time

import pytest

import hive_cluster as hc

STALE_S = 7200.0        # 现场：last_seen 停在 11-08 12:08，进程已跑 31 小时


def make_cluster(tmp_path, name="leader", **kw):
    """构造一个不监听端口、不启线程的 Cluster。"""
    cfg = hc.Config(
        state_dir=kw.pop("state_dir", tmp_path / name),
        name=name,
        host=kw.pop("host", "127.0.0.1"),
        port=kw.pop("port", 0),
        advertise=kw.pop("advertise", "127.0.0.1"),
        discover=False,
        hive_dir=kw.pop("hive_dir", tmp_path / "repo"),
        election_min=kw.pop("election_min", 999.0),
        leader_lease=kw.pop("leader_lease", 5.0),
        **kw)
    return hc.Cluster(cfg)


def backend_self_age(c) -> float:
    """节点表里本机记录距现在多少秒（磁盘与 API 都源自这一份）。"""
    return time.time() - float(c.nodes[c.node_id]["last_seen_epoch"])


def seed_stale_self(c) -> float:
    """把本机记录摆成「启动后就没再刷新过」的现场状态。"""
    stale = time.time() - STALE_S
    with c.lock:
        rec = c.nodes[c.node_id]
        rec["last_seen"] = hc._iso(stale)
        rec["last_seen_epoch"] = stale
    return stale


def read_disk_state(c) -> dict:
    return json.loads(c.config.state_path.read_text(encoding="utf-8"))


def test_maintenance_refreshes_self_record(tmp_path):
    """leader 维护一拍后，本机记录不再陈旧，且仍被判为 alive/leader。"""
    c = make_cluster(tmp_path)
    seed_stale_self(c)
    with c.lock:
        c.role = "leader"
        c.leader_id = c.node_id
    assert backend_self_age(c) > STALE_S - 10

    c._maintenance_once()

    with c.lock:
        assert backend_self_age(c) < 1.0, "leader 自身 last_seen 没被刷新"
        rec = c.nodes[c.node_id]
        assert rec["last_seen"], "last_seen 字符串也没同步"
        assert abs(hc._epoch(rec["last_seen"])
                   - float(rec["last_seen_epoch"])) < 1.0
        # 状态判定语义不变：自己永远是 alive
        assert c._node_status_locked(rec) == "alive"


def test_snapshot_self_last_seen_is_fresh(tmp_path):
    """`/api/cluster/state` 的 snapshot 里，自身 last_seen 与当前时间差 <1s。"""
    c = make_cluster(tmp_path)
    seed_stale_self(c)
    with c.lock:
        c.role = "leader"
        c.leader_id = c.node_id
    c._maintenance_once()

    snap = c.snapshot()
    me = next(n for n in snap["nodes"] if str(n.get("id")) == c.node_id)
    assert time.time() - float(me["last_seen_epoch"]) < 1.0
    assert me["status"] == "alive"
    assert me["role"] == "leader"
    assert snap["self"]["id"] == c.node_id
    assert time.time() - float(snap["self"]["last_seen_epoch"]) < 1.0


def test_state_json_self_last_seen_is_persisted(tmp_path):
    """落盘值同样被刷新：看板/体检读 state.json 不能继续看到旧时间。"""
    c = make_cluster(tmp_path)
    stale = seed_stale_self(c)
    # 先把「旧值」真的写到盘上，证明修复前盘上就是陈旧的
    with c.lock:
        c._dirty = True
    c._persist(force=True)
    disk = read_disk_state(c)
    assert abs(float(disk["nodes"][c.node_id]["last_seen_epoch"]) - stale) < 1.0

    # 清掉 _persist 的 1s 节流，让维护循环这一拍真的落盘
    c._last_persist = 0.0
    c._maintenance_once()

    disk = read_disk_state(c)
    disk_age = time.time() - float(disk["nodes"][c.node_id]["last_seen_epoch"])
    assert disk_age < 1.0, "state.json 里本机 last_seen 仍是旧值"
    # 盘上的 snapshot 与 nodes 两处不得分叉
    snap_self = disk["snapshot"]["self"]
    snap_age = time.time() - float(snap_self["last_seen_epoch"])
    assert snap_age < 1.0
    assert abs(snap_age - disk_age) < 1.0


def test_self_refresh_never_touches_peers(tmp_path):
    """只刷新自己：对端记录的时间戳与 dead/suspect 判定一概不变。"""
    c = make_cluster(tmp_path)
    seed_stale_self(c)
    stale_peer = time.time() - STALE_S
    with c.lock:
        c.role = "leader"
        c._update_node_locked({
            "id": "peer-101", "name": "peer", "incarnation": 1,
            "url": "http://192.168.0.101:8788",
            "last_seen": hc._iso(stale_peer), "last_seen_epoch": stale_peer,
        }, touch=False, trust_remote_seen=True)
        before = dict(c.nodes["peer-101"])
        peer_status_before = c._node_status_locked(c.nodes["peer-101"])

    c._maintenance_once()

    with c.lock:
        after = c.nodes["peer-101"]
        assert float(after["last_seen_epoch"]) == float(before["last_seen_epoch"])
        assert after["last_seen"] == before["last_seen"]
        assert c._node_status_locked(after) == peer_status_before
        assert c._node_status_locked(after) == "dead", \
            "对端长期无心跳必须仍判 dead（不能因为刷新自己而抬高别人）"
        # 自己确实被刷新了，证明上面不是「整拍没生效」
        assert backend_self_age(c) < 1.0


def test_refresh_does_not_bump_generation(tmp_path):
    """刷新时间戳不是状态变化：不能推进 generation（长轮询不该被空转唤醒）。"""
    c = make_cluster(tmp_path)
    seed_stale_self(c)
    with c.lock:
        c.role = "leader"
        gen_before = c.generation
    c._maintenance_once()
    with c.lock:
        assert c.generation == gen_before, "维护刷新把 generation 推进了"
        assert c.updated_at  # 时间戳字段仍在
    # 长轮询语义：没有真实变化就不该有新 generation
    c._maintenance_once()
    with c.lock:
        assert c.generation == gen_before


def test_follower_own_record_is_refreshed_too(tmp_path):
    """非 leader 进程的**自身**记录同样刷新（自己恒 alive，口径要一致）。

    对端（含 leader）的记录不受影响，见上一条用例。
    """
    c = make_cluster(tmp_path, name="follower")
    seed_stale_self(c)
    leader_stale = time.time() - STALE_S
    with c.lock:
        c.role = "follower"
        c.leader_id = "leader-9"
        c._update_node_locked({
            "id": "leader-9", "name": "leader", "incarnation": 1,
            "url": "http://192.168.0.9:8788",
            "last_seen": hc._iso(leader_stale),
            "last_seen_epoch": leader_stale,
        }, touch=False, trust_remote_seen=True)
        leader_before = float(c.nodes["leader-9"]["last_seen_epoch"])

    c._maintenance_once()

    with c.lock:
        assert backend_self_age(c) < 1.0
        assert float(c.nodes["leader-9"]["last_seen_epoch"]) == leader_before


def test_heartbeat_body_carries_last_seen(tmp_path):
    """自述记录本身带上 last_seen：心跳 body 不再缺这个公开字段。"""
    c = make_cluster(tmp_path)
    node = c._self_node()
    assert node["id"] == c.node_id
    assert node["last_seen"]
    assert time.time() - float(node["last_seen_epoch"]) < 1.0
    assert abs(hc._epoch(node["last_seen"])
               - float(node["last_seen_epoch"])) < 1.0


def test_put_self_node_uses_single_clock_source(tmp_path):
    """`_put_self_node_locked()` 落进节点表的时间戳来自同一份记录（不分叉）。"""
    c = make_cluster(tmp_path)
    with c.lock:
        c._put_self_node_locked()
        rec = c.nodes[c.node_id]
    assert time.time() - float(rec["last_seen_epoch"]) < 1.0
    assert abs(hc._epoch(rec["last_seen"])
               - float(rec["last_seen_epoch"])) < 1.0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))