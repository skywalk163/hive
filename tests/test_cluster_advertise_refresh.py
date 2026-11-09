"""对外地址（advertise）自校准：IP 漂移后节点表必须跟着走，且不能刷屏。

背景见 tasks/T-0293.md：`advertise_host` 只在进程启动时算一次，DHCP 换址后
本机仍通告旧地址，同伴按旧地址发包全部超时，于是把活着的 leader 判死，
进入 1/3 → 1/2 → 1/1 的选举风暴。

这些用例不起 HTTP 服务：直接构造 Cluster，在锁内驱动 `_refresh_advertise_locked`
或 `_maintenance_once`，探测函数用 monkeypatch 注入，结果完全确定。
"""
from __future__ import annotations

import pytest

import hive_cluster as hc


def make_cluster(tmp_path, **kw):
    """构造一个不监听端口的 Cluster（不 start，不起线程）。"""
    cfg = hc.Config(
        state_dir=kw.pop("state_dir", tmp_path / "state"),
        name=kw.pop("name", "n"),
        host=kw.pop("host", "0.0.0.0"),
        port=kw.pop("port", 8788),
        advertise=kw.pop("advertise", ""),
        discover=False,
        hive_dir=kw.pop("hive_dir", tmp_path / "repo"),
        **kw,
    )
    return hc.Cluster(cfg)


def seed_host(cluster, host: str) -> None:
    """把节点表里的「当前地址」摆成指定值（模拟启动时猜到的旧地址）。"""
    with cluster.lock:
        cluster.advertise_host = host
        cluster._put_self_node_locked()


def advertise_events(cluster):
    return [e for e in cluster.events if e.get("type") == "advertise_changed"]


def test_ip_change_updates_url_after_two_consistent_probes(tmp_path):
    c = make_cluster(tmp_path)
    seed_host(c, "192.168.0.103")
    assert c.url == "http://192.168.0.103:8788"
    assert c.nodes[c.node_id]["url"] == "http://192.168.0.103:8788"

    # 第一次只记账（去抖），地址不动
    with c.lock:
        assert c._refresh_advertise_locked("192.168.0.110", 1000.0) is False
    assert c.url == "http://192.168.0.103:8788"
    assert not advertise_events(c)

    # 第二次探测一致才切换，并同步节点表与事件
    with c.lock:
        assert c._refresh_advertise_locked("192.168.0.110", 1030.0) is True
    assert c.url == "http://192.168.0.110:8788"
    assert c.nodes[c.node_id]["url"] == "http://192.168.0.110:8788"
    assert c.nodes[c.node_id]["host"] == "192.168.0.110"
    evs = advertise_events(c)
    assert len(evs) == 1
    assert "192.168.0.103" in evs[0]["message"]
    assert "192.168.0.110" in evs[0]["message"]


def test_advertise_changed_event_is_emitted_only_once(tmp_path):
    c = make_cluster(tmp_path)
    seed_host(c, "192.168.0.103")
    with c.lock:
        c._refresh_advertise_locked("192.168.0.110", 1000.0)
        c._refresh_advertise_locked("192.168.0.110", 1010.0)
        # 同一地址继续探测：不得再发事件，也不得来回改
        for i in range(5):
            assert c._refresh_advertise_locked("192.168.0.110", 1020.0 + i) is False
    assert len(advertise_events(c)) == 1
    assert c.url == "http://192.168.0.110:8788"


def test_flapping_probe_never_switches(tmp_path):
    c = make_cluster(tmp_path)
    seed_host(c, "10.0.0.9")
    with c.lock:
        # A → B → A 不是「连续两次一致」，必须原地不动
        assert c._refresh_advertise_locked("10.0.0.20", 1.0) is False
        assert c._refresh_advertise_locked("10.0.0.21", 2.0) is False
        assert c._refresh_advertise_locked("10.0.0.20", 3.0) is False
    assert c.url == "http://10.0.0.9:8788"
    assert not advertise_events(c)


def test_explicit_advertise_is_never_overridden(tmp_path):
    c = make_cluster(tmp_path, advertise="10.1.1.1")
    seed_host(c, "10.1.1.1")
    assert c.config.advertise_explicit is True
    with c.lock:
        for i in range(5):
            assert c._refresh_advertise_locked("192.168.0.110", float(i)) is False
    assert c.url == "http://10.1.1.1:8788"
    assert not advertise_events(c)


def test_loopback_probe_does_not_replace_reachable_address(tmp_path):
    """断网时猜出 127.0.0.1，不能把可达的局域网地址换掉。"""
    c = make_cluster(tmp_path)
    seed_host(c, "192.168.0.103")
    with c.lock:
        for i in range(3):
            assert c._refresh_advertise_locked("127.0.0.1", float(i)) is False
    assert c.url == "http://192.168.0.103:8788"
    assert not advertise_events(c)


def test_maintenance_loop_refreshes_advertise(tmp_path, monkeypatch):
    c = make_cluster(tmp_path, advertise_refresh=0.0)
    seed_host(c, "192.168.0.103")
    c.role = "leader"
    monkeypatch.setattr(hc, "_guess_lan_ip", lambda: "192.168.0.110")

    # 维护循环每轮都到期（refresh=0），但仍要连续两次一致才切换
    c._maintenance_once()
    assert c.url == "http://192.168.0.103:8788"
    c._maintenance_once()
    assert c.url == "http://192.168.0.110:8788"
    c._maintenance_once()
    assert len(advertise_events(c)) == 1


def test_refresh_is_skipped_when_explicit_advertise(tmp_path, monkeypatch):
    c = make_cluster(tmp_path, advertise="10.1.1.1", advertise_refresh=0.0)
    seed_host(c, "10.1.1.1")
    monkeypatch.setattr(hc, "_guess_lan_ip", lambda: "192.168.0.110")
    for _ in range(3):
        c._maintenance_once()
    assert c.url == "http://10.1.1.1:8788"
    assert not advertise_events(c)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
