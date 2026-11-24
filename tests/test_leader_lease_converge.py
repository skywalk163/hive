"""主控租约收敛不能把本人心跳续的租约拉回陈旧值（刷屏根因）。

2026-11-24 现场：`cluster.log` 里同一 term 内「采纳主控」刷了 44692 次、
约每 5 秒一条，而**一条 leader_lost 都没有**。原因是两条路径打架：

1. `_adopt_leader_locked(refresh_lease=True)` 收到主控本人心跳时，
   只把 `self.leader_lease_until` 续到 `now + leader_lease`，
   却**没有写回 `self.nodes[leader]["lease_until_epoch"]`**；
2. 维护循环里那段「防御性收敛」会用节点记录里的值把本机租约压回去：
       rec_lease = nodes[leader].lease_until_epoch
       if leader_lease_until > rec_lease: leader_lease_until = rec_lease
       if now > leader_lease_until: _clear_leader_locked("主节点租约过期")

于是每轮都是「心跳续租 → 收敛回旧值 → 判定过期 → 清空 leader_id →
下一拍再采纳」，日志刷屏但看不到清空记录（`_clear_leader_locked` 走
`_emit_locked`，只记事件、不写 cluster.log）。

覆盖：
- 节点记录里的陈旧租约不会把本人心跳续的租约拉回过去（不再反复清空）；
- 节点记录缺 `lease_until_epoch` 时不误判过期；
- 本人心跳确实把新租约写回节点记录（`_leader_record_locked` 对外一致）；
- **反向保护**：真正的死亡主控（记录陈旧、又没有本人心跳）仍会被判过期
  并清空 —— 修复不能把「主控死了也不选主」的老毛病带回来。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_cluster_module():
    spec = importlib.util.spec_from_file_location(
        "hive_cluster_for_lease", ROOT / "hive_cluster.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hive_cluster_for_lease"] = mod
    spec.loader.exec_module(mod)
    return mod


hc = _load_cluster_module()

LEADER_ID = "a11ce00000000000000000000000000000000000000000000000000000000001"
LEADER = {"id": LEADER_ID, "name": "L", "url": "http://127.0.0.1:18901",
          "incarnation": 1}
TERM = 30


@pytest.fixture()
def cluster(tmp_path):
    cfg = hc.Config(name="lease", port=18801, state_dir=str(tmp_path),
                    seeds=[], token="t", discovery_port=0,
                    heartbeat_interval=0.2, leader_lease=5.0)
    return hc.Cluster(cfg)


def _adopt(c, lease_epoch, refresh=True):
    rec = dict(LEADER)
    rec["lease_until_epoch"] = lease_epoch
    with c.lock:
        c._adopt_leader_locked(rec, TERM, refresh_lease=refresh)


def _maintain_lease_once(c):
    """复刻维护循环里那段租约收敛 + 过期判定，返回是否清空。"""
    with c.lock:
        if not c.leader_id:
            return False
        now = hc._now()
        rec_lease = float(
            (c.nodes.get(str(c.leader_id)) or {}).get("lease_until_epoch") or 0.0)
        if rec_lease and c.leader_lease_until > rec_lease:
            c.leader_lease_until = rec_lease
        if c.leader_lease_until and now > c.leader_lease_until:
            c._clear_leader_locked("主节点租约过期")
            return True
    return False


def test_stale_record_lease_does_not_clear_repeatedly(cluster):
    """节点记录里的陈旧租约 + 本人心跳续租 → 不得反复清空。"""
    cluster.term = TERM
    with cluster.lock:
        cluster._update_node_locked(dict(LEADER), touch=True)

    stale = hc._now() - 100          # 记录里是过去的旧值
    clears = 0
    for _ in range(8):
        _adopt(cluster, stale, refresh=True)     # 主控本人心跳
        if _maintain_lease_once(cluster):
            clears += 1

    assert clears == 0, (
        f"本人心跳续了租约却仍被判过期 {clears} 次 —— 正是刷屏根因")
    assert cluster.leader_id == LEADER_ID, "主控不应被清掉"


def test_missing_record_lease_is_not_treated_as_expired(cluster):
    """记录里没有 lease_until_epoch（=0）时不能误判过期。"""
    cluster.term = TERM
    with cluster.lock:
        cluster._update_node_locked(dict(LEADER), touch=True)

    clears = 0
    for _ in range(5):
        _adopt(cluster, 0, refresh=True)
        if _maintain_lease_once(cluster):
            clears += 1

    assert clears == 0
    assert cluster.leader_id == LEADER_ID


def test_heartbeat_writes_lease_back_to_node_record(cluster):
    """本人心跳必须把新租约写回节点记录，供 `_leader_record_locked` 转述。"""
    cluster.term = TERM
    with cluster.lock:
        cluster._update_node_locked(dict(LEADER), touch=True)

    _adopt(cluster, hc._now() - 100, refresh=True)

    rec = cluster.nodes[LEADER_ID]
    assert rec.get("lease_until_epoch") == pytest.approx(
        cluster.leader_lease_until), "节点记录里的租约应与本机一致"
    assert float(rec.get("lease_until_epoch") or 0) > hc._now(), \
        "写回的租约必须是未来时间"


def test_leader_record_exposes_fresh_lease(cluster):
    """对外转述的 leader 记录也要显示续过的新租约，而不是旧值。"""
    cluster.term = TERM
    with cluster.lock:
        cluster._update_node_locked(dict(LEADER), touch=True)
    _adopt(cluster, hc._now() - 100, refresh=True)

    with cluster.lock:
        pub = cluster._leader_record_locked()
    assert pub is not None
    assert float(pub.get("lease_until_epoch") or 0) > hc._now(), \
        f"对外租约仍是过去时间：{pub.get('lease_until_epoch')}"


def test_dead_leader_still_expires(cluster):
    """反向保护：真死的主控（没有本人心跳）必须仍能被判过期并清空。

    修复只允许「本人心跳」把租约往前推；转述路径（refresh_lease=False）
    仍只取 min(远端, now+lease)，所以陈旧记录依然会过期 —— 不能把
    「主控死了也不选主」的老毛病带回来。
    """
    cluster.term = TERM
    with cluster.lock:
        cluster._update_node_locked(dict(LEADER), touch=True)

    # 只有转述、没有本人心跳，且记录租约已是过去
    expired_once = False
    for _ in range(3):
        _adopt(cluster, hc._now() - 100, refresh=False)
        if _maintain_lease_once(cluster):
            expired_once = True
            break
    assert expired_once, "陈旧租约 + 无本人心跳 应当仍判定过期（否则集群会永久无主）"


def test_transcribe_does_not_exceed_now_plus_lease(cluster):
    """转述路径必须被 `min(远端, now+lease)` 夹住，不能无限往后推。

    语义要点：转述**可以**把本机租约抬到 now+lease（这是上限裁剪），
    但不允许超过它 —— 否则每一跳转发都把绝对时间再往未来推一格，
    主控真的死了租约也永不过期，集群永久无主（2026-11-11 的现场）。
    """
    cluster.term = TERM
    with cluster.lock:
        cluster._update_node_locked(dict(LEADER), touch=True)

    # 远端声称一个远到离谱的租约
    _adopt(cluster, hc._now() + 100000, refresh=False)
    ceiling = hc._now() + cluster.config.leader_lease

    assert cluster.leader_lease_until <= ceiling + 0.001, (
        f"转述把租约推到了 {cluster.leader_lease_until}，"
        f"超过上限 now+lease={ceiling}")
