"""T-0295：投票闸门 store_behind 不再硬拒票；落后当选先 reconcile 再服务写。

背景
----
store 是可重建缓存、git 才是事实账（见 StoreReconciler）。原先 `handle_vote`
把「候选人 commit_seq 比本机小」当成拒票硬条件，可分区时各节点 commit_seq
**必然**不一致：于是全员互相否决，只能等 dead_timeout 把同伴判死、quorum
退化成 1 才「孤身称主」—— 无主窗口被拉长，还制造了一个更危险的主控。

本卡把默认改成：
  * 落后候选照样拿赞成票，但留痕 `vote_store_lag`（带 lag / candidate_seq / my_seq）；
  * 票里回带本机 `store_seq`，候选人当选后先 reconcile 到这个位点再服务写
    （未就绪期间 store 写返回 503 `E_RECONCILING`，读不受影响）；
  * 老的「宁可没有主控也不接受落后节点」改成严格模式：环境变量
    `HIVE_CLUSTER_STRICT_STORE_FRESHNESS=1` 或构造参数
    `strict_store_freshness=True`，默认关闭；命中时 emit `vote_store_behind`。
"""
from __future__ import annotations

import time

import pytest

import hive_cluster as hc
from hive_store import Store


def wait_until(fn, timeout: float = 6.0, interval: float = 0.05):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception:  # noqa: BLE001  轮询期间状态可能正好在变
            pass
        time.sleep(interval)
    return last


@pytest.fixture()
def cluster_factory(tmp_path):
    clusters: list[hc.Cluster] = []

    def make(name: str, seeds=None, **kw):
        cfg = hc.Config(
            state_dir=kw.pop("state_dir", tmp_path / name),
            name=name,
            host=kw.pop("host", "127.0.0.1"),
            port=kw.pop("port", 0),
            advertise=kw.pop("advertise", "127.0.0.1"),
            discover=kw.pop("discover", False),
            seeds=seeds or [],
            hive_dir=kw.pop("hive_dir", tmp_path / "repo"),
            heartbeat_interval=kw.pop("heartbeat_interval", 0.08),
            peer_timeout=kw.pop("peer_timeout", 0.5),
            dead_timeout=kw.pop("dead_timeout", 1.2),
            election_min=kw.pop("election_min", 0.3),
            election_jitter=kw.pop("election_jitter", 0.1),
            leader_lease=kw.pop("leader_lease", 0.7),
            lease_grace=kw.pop("lease_grace", 2.0),
            **kw,
        )
        c = hc.Cluster(cfg)
        c.start()
        clusters.append(c)
        return c

    yield make
    for c in clusters:
        try:
            c.stop()
        except Exception:  # noqa: BLE001
            pass


def _two_nodes(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader"), "单节点应自动当选"
    b = cluster_factory("b", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id), "b 应跟随 a"
    return a, b


def _candidate(seq: int) -> dict:
    """合成一个候选人自述：投票闸只看 id/incarnation/last_store_seq。"""
    return {"id": "peer-x", "name": "peer-x", "incarnation": 1,
            "last_store_seq": seq, "electable": True, "priority": 1}


def _store_at(path, seq: int) -> Store:
    """造一个 commit_seq=seq 的 store（内容为空也无所谓，比的是位点）。"""
    s = Store(path, "fixture")
    s.import_snapshot({"commit_seq": seq, "last_seq": seq,
                       "compacted_through": seq, "tasks": {}, "claims": {},
                       "results": {}, "summaries": {}})
    return s


class _FakeStore:
    def __init__(self, seq: int):
        self._seq = seq

    def commit_seq(self) -> int:
        return self._seq


def _events(c: hc.Cluster, etype: str) -> list[dict]:
    return [e for e in c.snapshot()["events"] if e.get("type") == etype]


# --------------------------------------------------------------------------- #
# 默认：落后候选照样拿票，只要留痕
# --------------------------------------------------------------------------- #

def test_default_mode_grants_lagging_candidate_and_records_lag(cluster_factory):
    """分区里各节点 seq 不一致是常态 —— 不能因此互相拒票。"""
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    a.store = _FakeStore(99)
    try:
        code, body = a.handle_vote({"term": a.term + 5,
                                    "candidate": _candidate(1)})
        assert code == 200
        assert body.get("granted") is True, body
        # 票里回带本机位点：候选人当选后要追到这里再服务写
        assert body.get("store_seq") == 99

        lag = _events(a, "vote_store_lag")
        assert lag, "默认放行必须留下 vote_store_lag 审计事件"
        data = lag[-1]["data"]
        assert data["candidate_seq"] == 1 and data["my_seq"] == 99
        assert data["lag"] == 98
    finally:
        a.store = None


def test_lagging_candidate_wins_election_without_force(cluster_factory):
    """没有 force 也能当选：闸门不再把「数据新鲜度」当「选举资格」。

    T-0299 起「不抢占在位主控」是硬规则（`_start_election` 看到有效主控租约
    就直接返回 False，发 CLAIM 前还有 `_carrier_sense()` 询问成员）。所以这里
    先让在位主控正常下野，模拟一次正常切主，再让落后的 b 竞选；a 仍是成员、
    仍是活着的投票人，默认模式（非严格）下会给 b 投赞成票并留 `vote_store_lag`
    —— 这样既守住不抢占规则，又仍然钉住「store 落后不是选举资格」。
    """
    a, b = _two_nodes(cluster_factory)
    a.store = _FakeStore(99)          # 在位主控领先很多
    b.store = None                    # 候选人 last_store_seq=0，明显落后
    try:
        # 让 a 下野后不再自己参选（priority=0 是 OSPF 的弃权硬资格），
        # 否则 a 会抢在 b 前面重新当选，这条用例就测不到 b 了。
        a.config.leader_priority = 0
        code, _ = a.handle_resign({"node_id": a.node_id})
        assert code == 200
        # 等 b 自己把「主控租约」放掉（正常切主的第一步）
        assert wait_until(lambda: b.leader_id is None), "b 仍视 a 为在位主控"
        won = b._start_election(force=False)
        assert won is True, "落后候选默认应能当选，而不是等到 quorum 退化"
        assert wait_until(lambda: b.role == "leader")
        assert a.role == "follower", "原主控必须下野，不能留双主"
    finally:
        a.store = None


# --------------------------------------------------------------------------- #
# 严格模式：显式选择「宁可没有主控，也不接受落后节点」
# --------------------------------------------------------------------------- #

def test_strict_mode_rejects_and_emits_reason(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    a.config.strict_store_freshness = True
    a.store = _FakeStore(99)
    try:
        code, body = a.handle_vote({"term": a.term + 5,
                                    "candidate": _candidate(1)})
        assert code == 200
        assert body.get("granted") is False
        assert body.get("reason") == "store_behind"
        assert body.get("strict") is True
        assert not _events(a, "vote_store_lag"), "严格模式不放行，就不该有放行痕迹"

        behind = _events(a, "vote_store_behind")
        assert behind, "严格模式命中必须 emit 明确原因，否则只看到「未获多数」"
        assert behind[-1]["data"]["lag"] == 98
        assert behind[-1]["data"]["strict"] is True
    finally:
        a.store = None


def test_force_still_overrides_in_strict_mode(cluster_factory):
    """人工夺权通道优先于严格模式：管理员点名时仍可强行上任。"""
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    a.config.strict_store_freshness = True
    a.store = _FakeStore(99)
    try:
        code, body = a.handle_vote({"term": a.term + 5,
                                    "candidate": _candidate(1),
                                    "force": True})
        assert body.get("granted") is True, body
        assert _events(a, "leader_forced"), "人工放行必须留 leader_forced"
    finally:
        a.store = None


def test_strict_mode_defaults_off_and_can_be_switched_by_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HIVE_CLUSTER_STRICT_STORE_FRESHNESS", raising=False)
    default = hc.Config(state_dir=tmp_path / "c1", name="x")
    assert default.strict_store_freshness is False, "默认必须是「允许落后当选」"

    monkeypatch.setenv("HIVE_CLUSTER_STRICT_STORE_FRESHNESS", "1")
    strict = hc.Config(state_dir=tmp_path / "c2", name="x")
    assert strict.strict_store_freshness is True

    kw = hc.Config(state_dir=tmp_path / "c3", name="x",
                   strict_store_freshness=True)
    assert kw.strict_store_freshness is True


# --------------------------------------------------------------------------- #
# 当选后：先 reconcile 再服务写
# --------------------------------------------------------------------------- #

def test_store_writes_rejected_while_reconciling(cluster_factory, tmp_path):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    a.store = _store_at(tmp_path / "a.db", 0)
    try:
        with a.lock:
            a._store_serving_ready = False
            a._store_lag_target = 7
        code, body = a.handle_store("claim", {
            "task": "T-0001", "agent": "x", "lease_minutes": 5})
        assert code == 503, body
        assert body.get("code") == "E_RECONCILING", \
            "写闸要给出结构化 code，让 worker 退避重试而不是当成「被抢走」"
        assert body.get("store_lag_target") == 7

        # 读接口不受影响
        code, body = a.handle_store_state()
        assert code == 200 and body.get("ok") is True

        with a.lock:
            a._store_serving_ready = True
        code, _body = a.handle_store("claim", {
            "task": "T-0001", "agent": "x", "lease_minutes": 5})
        assert code != 503, "对齐完成后不能继续拿 E_RECONCILING 挡写"
    finally:
        a.store = None


def test_behind_winner_reconciles_commit_seq_before_serving(cluster_factory,
                                                           tmp_path,
                                                           monkeypatch):
    """当选后从同伴拉 snapshot/op，把 commit_seq 追平再开放写。"""
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    a.store = _store_at(tmp_path / "a.db", 0)
    src = _store_at(tmp_path / "src.db", 7)
    # 只用一张「同伴」记录提供 URL；不真起第二台，免得心跳把本用例带偏
    with a.lock:
        a.nodes["ghost-peer"] = {"id": "ghost-peer", "name": "ghost",
                                 "url": "http://127.0.0.1:9", "status": "alive",
                                 "last_seen_epoch": hc._now()}

    def _fake_http(url, method="GET", payload=None, timeout=1.5, token=""):
        if url.endswith("/api/cluster/store/snapshot"):
            return 200, {"ok": True, "snapshot": src.snapshot()}
        if "/api/cluster/op/since" in url:
            return 200, {"ok": True, "ops": []}
        # 心跳/投票等其它请求一律当作网络不通，不影响本用例
        return None, None

    monkeypatch.setattr(hc, "http_json", _fake_http)
    try:
        with a.lock:
            a._store_lag_target = 7
            started = a._arm_store_reconcile_locked()
        assert started is True
        assert a._store_serving_ready is False, "追平之前必须先关写闸"
        assert _events(a, "store_reconcile_started")

        assert wait_until(lambda: a._store_serving_ready), "追平后必须自动开闸"
        assert a.store.commit_seq() == 7, "当选后 commit_seq 要对齐到投票人位点"
        assert _events(a, "store_reconciled"), "对齐完成要留痕"
    finally:
        a.store = None