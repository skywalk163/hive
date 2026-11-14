"""T-0351：控制面装配修复 + H-LSA T1 摘要下行。

全部使用注入的 fake transport / fake clock / 临时目录：
不真发 UDP 包、不 sleep、不碰 Git、不向生产组播组发包。
"""

from __future__ import annotations

import json
import queue
from types import SimpleNamespace

from hive_control.adapter import ClusterControlAdapter
from hive_control.config import (ControlConfig, Limits, resolve_multicast)
from hive_control.downlink import (LsaDigestReceiver, LsaDigestSender,
                                   DownlinkSender, encode_lsa_digest,
                                   lsa_digest_message)


# --------------------------------------------------------------------------
# 假对象
# --------------------------------------------------------------------------

class FakeTransport:
    def __init__(self):
        self.multicast: list[bytes] = []
        self.unicast: list[tuple[tuple[str, int], bytes]] = []
        self.callback = None
        self.started = False
        self.membership_ok = True

    def send_multicast(self, data: bytes) -> None:
        self.multicast.append(data)

    def send_unicast(self, addr, data: bytes) -> None:
        self.unicast.append((addr, data))

    def start_receiver(self, callback) -> None:
        self.callback = callback
        self.started = True

    def stop(self) -> None:
        self.started = False

    def feed(self, data: bytes, addr=("10.0.0.9", 50000), ttl: int = 1) -> None:
        assert self.callback is not None, "接收器尚未启动"
        self.callback(data, addr, ttl)


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeCluster:
    """足够让适配器装配出真实 ControlPlane 的最小 duck-typed cluster。"""

    def __init__(self, *, name="dev-a", advertise="127.0.0.1", port=0,
                 cluster_id="hive-dev"):
        self.nodes = {"self": {"id": name, "url": f"http://{advertise}:{port}"}}
        self._agents = [{"name": "trae-mac", "mode": "manual"}]
        self.config = SimpleNamespace(
            name=name, advertise=advertise, port=port, cluster_id=cluster_id)
        self.term = 0

    def _local_agents(self):
        return list(self._agents)


def make_adapter(tmp_path, *, cluster_id="hive-dev", transport=None,
                 provider=None, **kwargs):
    cluster = FakeCluster(cluster_id=cluster_id)
    limits = Limits(
        socket_path=str(tmp_path / "control.sock"),
        incarnation_path=str(tmp_path / "incarnation.json"),
        multicast_group="239.255.42.2",
        multicast_port=47900,
    )
    adapter = ClusterControlAdapter(
        cluster, limits=limits, transport=transport,
        start_threads=False, lsa_auto_tick=False, lsa_provider=provider,
        **kwargs)
    return adapter, cluster


def make_lsa_sender(*, clock, provider, transport, lsa_interval=1.0,
                    cluster_id="hive-dev", project_tag="proj"):
    config = ControlConfig(cluster_id=cluster_id, project_tag=project_tag,
                           lsa_interval=lsa_interval)
    limits = Limits(multicast_group="239.255.42.2",
                    multicast_port=47900)
    downlink = DownlinkSender(queue.Queue(), limits=limits,
                              transport=transport, clock=clock)
    sender = LsaDigestSender(downlink, limits=limits, config=config,
                             clock=clock, digest_provider=provider)
    return sender, downlink, config


def decode(packet: bytes) -> dict:
    return json.loads(packet.decode("utf-8").strip())


# --------------------------------------------------------------------------
# 装配：身份参数齐全 + 状态字段
# --------------------------------------------------------------------------

def test_adapter_assembly_fills_identity_and_status(tmp_path):
    adapter, cluster = make_adapter(tmp_path, transport=FakeTransport())
    adapter.start()
    try:
        assert adapter.plane_available is True
        assert adapter.plane is not None
        plane = adapter.plane
        assert plane.node == "dev-a"
        assert plane.address == "127.0.0.1"
        assert plane.listen_port == 0
        status = adapter.status()
        assert status["plane_available"] is True
        assert status["degraded"] is False
        assert status["multicast_mode"] == "multicast"
        assert status["lsa"]["tx"] and status["lsa"]["rx"]
        # 装配后的身份与 cluster 的 --name/--advertise/--port 一致
        assert plane.status()["node"] == "dev-a"
        assert plane.status()["address"] == "127.0.0.1"
    finally:
        adapter.stop()


def test_dev_cluster_uses_isolated_multicast_group(tmp_path):
    assert resolve_multicast("hive") == ("239.255.42.1", 47899)
    assert resolve_multicast("hive-dev") == ("239.255.42.2", 47900)
    assert resolve_multicast("hive", group="239.0.0.9", port=5000) == (
        "239.0.0.9", 5000)
    adapter, _ = make_adapter(tmp_path, transport=FakeTransport())
    adapter.start()
    try:
        limits = adapter.plane.limits
        assert (limits.multicast_group, limits.multicast_port) == (
            "239.255.42.2", 47900)
    finally:
        adapter.stop()


def test_assembly_failure_degrades_without_raising(tmp_path):
    def broken_factory(_adapter):
        raise RuntimeError("装配失败")

    adapter, _ = make_adapter(tmp_path, plane_factory=broken_factory)
    adapter.start()
    try:
        assert adapter.plane_available is False
        status = adapter.status()
        assert status["degraded"] is True
        assert status["multicast_mode"] == "off"
    finally:
        adapter.stop()


# --------------------------------------------------------------------------
# T1 摘要：字段、尺寸、只有 Leader 发、切主抑制
# --------------------------------------------------------------------------

def _provider():
    return {"digest": "sha256:abc", "head_sha": "deadbeef",
            "n_changes": 3, "area": "cards"}


def test_digest_packet_fields_and_size_limit():
    clock = FakeClock()
    transport = FakeTransport()
    sender, _, config = make_lsa_sender(clock=clock, provider=_provider,
                                        transport=transport)
    assert sender.tick(is_leader=True, epoch=1) == 0      # 切主静默窗口
    clock.advance(4.1)
    assert sender.tick(is_leader=True, epoch=1) == 1
    assert len(transport.multicast) == 1
    packet = transport.multicast[0]
    assert len(packet) <= 1400

    message = decode(packet)
    for field in ("leader_epoch", "seq", "digest", "head_sha", "n_changes",
                  "area", "project_tag", "cluster_id", "sent_at", "hmac"):
        assert field in message
    assert message["project_tag"] == "proj"
    assert message["cluster_id"] == "hive-dev"
    # 摘要包绝不能夹带状态体
    for forbidden in ("entries", "progress", "leases", "cards", "state"):
        assert forbidden not in message


def test_only_leader_sends_digest():
    clock = FakeClock()
    transport = FakeTransport()
    sender, _, _ = make_lsa_sender(clock=clock, provider=_provider,
                                   transport=transport)
    clock.advance(100)
    for _ in range(5):
        assert sender.tick(is_leader=False, epoch=7) == 0
        clock.advance(60)
    assert transport.multicast == []


def test_leader_switch_suppresses_flood_then_resumes():
    clock = FakeClock()
    transport = FakeTransport()
    sender, _, _ = make_lsa_sender(clock=clock, provider=_provider,
                                   transport=transport)
    assert sender.suppress_window >= 2 * 2.0

    assert sender.tick(is_leader=True, epoch=1) == 0
    clock.advance(4.1)
    assert sender.tick(is_leader=True, epoch=1) == 1      # 窗口后首拍
    clock.advance(0.2)
    assert sender.tick(is_leader=True, epoch=1) == 0      # T1 节奏未到

    # 切主：新 epoch 进入抑制窗口，窗口内不重复泛洪
    clock.advance(0.1)
    assert sender.tick(is_leader=True, epoch=2) == 0
    before = len(transport.multicast)
    clock.advance(3.9)
    assert sender.tick(is_leader=True, epoch=2) == 0
    assert len(transport.multicast) == before
    clock.advance(0.2)
    assert sender.tick(is_leader=True, epoch=2) == 1
    epochs = [decode(p)["leader_epoch"] for p in transport.multicast]
    assert epochs == [1, 2]


# --------------------------------------------------------------------------
# 接收端：epoch 单调、project_tag / cluster_id / HMAC 校验
# --------------------------------------------------------------------------

def _valid_message(**overrides):
    base = dict(leader_epoch=5, seq=3, digest="sha256:abc", head_sha="dead",
                n_changes=1, area="cards", project_tag="proj",
                cluster_id="hive-dev", sent_at=1,
                hmac_key="hive-control:hive-dev")
    base.update(overrides)
    return lsa_digest_message(**base)


def test_receiver_accepts_valid_and_drops_stale_epoch():
    receiver = LsaDigestReceiver(project_tag="proj", cluster_id="hive-dev",
                                 hmac_key="hive-control:hive-dev")
    assert receiver.handle(_valid_message(leader_epoch=5, seq=3)) is True
    # 旧主 / 幽灵 Leader：epoch 回退
    assert receiver.handle(_valid_message(leader_epoch=4, seq=9)) is False
    # 同一 epoch 内重复
    assert receiver.handle(_valid_message(leader_epoch=5, seq=3)) is False
    status = receiver.status()
    assert status["accepted"] == 1
    assert status["stale_epoch"] == 1
    assert status["duplicate"] == 1


def test_receiver_rejects_tag_cluster_and_hmac_mismatch():
    receiver = LsaDigestReceiver(project_tag="proj", cluster_id="hive-dev",
                                 hmac_key="hive-control:hive-dev")
    assert receiver.handle(_valid_message(cluster_id="hive")) is False
    assert receiver.handle(_valid_message(project_tag="other")) is False

    tampered = _valid_message(leader_epoch=6, seq=1)
    tampered["digest"] = "sha256:tampered"
    assert receiver.handle(tampered) is False
    status = receiver.status()
    assert status["bad_cluster"] == 1
    assert status["bad_project"] == 1
    assert status["bad_hmac"] == 1
    assert status["accepted"] == 0


def test_receiver_is_wired_into_plane_downlink(tmp_path):
    transport = FakeTransport()
    adapter, _ = make_adapter(tmp_path, transport=transport,
                              provider=_provider)
    adapter.start()
    try:
        config = adapter.control_config()
        good = _valid_message(leader_epoch=1, seq=1,
                              project_tag=config.project_tag,
                              cluster_id=config.cluster_id,
                              hmac_key=config.hmac_key)
        transport.feed(encode_lsa_digest(good))
        assert adapter.plane.downlink_receiver.status()["lsa_digests"] == 1
        assert adapter.status()["counters"]["lsa_accepted"] == 1

        transport.feed(encode_lsa_digest(_valid_message(
            leader_epoch=0, seq=99, project_tag=config.project_tag,
            cluster_id=config.cluster_id, hmac_key=config.hmac_key)))
        assert adapter.status()["counters"]["lsa_stale_epoch"] == 1
    finally:
        adapter.stop()


# --------------------------------------------------------------------------
# fallback 语义不变
# --------------------------------------------------------------------------

def test_digest_respects_fallback_unicast_semantics():
    clock = FakeClock()
    transport = FakeTransport()
    sender, downlink, _ = make_lsa_sender(clock=clock, provider=_provider,
                                          transport=transport)
    downlink.set_peers([("10.0.0.2", 40000), ("10.0.0.3", 40000)])
    downlink.set_fallback(True)

    assert sender.tick(is_leader=True, epoch=1) == 0
    clock.advance(4.1)
    assert sender.tick(is_leader=True, epoch=1) == 2      # 两个节点各一份
    assert transport.multicast == []
    assert len(transport.unicast) == 2
    assert downlink.multicast_mode == "fallback"
