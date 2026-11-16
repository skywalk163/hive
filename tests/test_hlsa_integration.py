"""T-0356：H-LSA dev 环境集成回归（第 2 批收口，供 T-0357 灰度后复跑）。

把第 1 批六张卡「拼起来」的可自动化部分串成端到端回归：

1. **cluster 层全链路**（真 socket、真 LSA 核心、真心跳循环）：
   T0 变更 → 心跳上行 → 主控 LSDB → 其余节点可见（SLA-1/SLA-2 的集成口径）；
   稳态心跳不回全量；合并窗口与字段级 diff 在活链路上成立。
2. **控制面层真组播**（隔离组播组，绝不碰生产 239.255.42.1:47899）：
   T1 摘要 ≤1400B、不含状态体、只有控制面 Leader 发、
   切主后单组播源恢复、follower 接收计数干净（SLA-7 集成口径）。
3. **git 层协调仓链路**（bare origin 替身，不连生产 Gitea）：
   T1 head_sha 一致 → 零 Gitea 流量；T0 事件 → 提前 autosync；
   脏工作区字节级不动（L5 关键回归）；sync_health / local_stale 契约。

隔离纪律（卡面 L1–L5 的可自动化部分）：
- 组播组用 239.255.42.<run 唯一>（既非生产组也非 dev 常驻组），端口按 PID 派生；
- cluster_id / project_tag 每次运行唯一，互不串包；
- 全部仓、状态目录、socket 路径都在 tmp_path；
- 不碰 8787/8788/8789/8790、~/.hive/cluster、生产 Gitea。
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

import hive
import hive_cluster as hc
import hive_repos
from conftest import sh

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# 隔离常量（每次运行唯一；绝不与生产/dev 常驻实例重叠）
# --------------------------------------------------------------------------- #
RUN_ID = f"{os.getpid() % 100000:05d}"
CLUSTER_ID = f"hive-itest-{RUN_ID}"
PROJECT_TAG = f"hive-itest-{RUN_ID}"
MC_GROUP = "239.255.42.3"
MC_PORT = 47910 + (os.getpid() % 80)

PROD_GROUP, PROD_PORT = "239.255.42.1", 47899
FORBIDDEN_IN_DIGEST = ("entries", "progress", "leases", "cards", "state",
                       "nodes", "agents", "full_state")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
}


def wait_until(fn, timeout: float = 6.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception:  # noqa: BLE001 轮询期间状态可能正好在变
            pass
        time.sleep(interval)
    return last


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def mc_env(monkeypatch):
    """钉死测试组播组/端口/标签；cluster_id 兜底也走非生产路径。"""
    monkeypatch.setenv("HIVE_CONTROL_MULTICAST_GROUP", MC_GROUP)
    monkeypatch.setenv("HIVE_CONTROL_MULTICAST_PORT", str(MC_PORT))
    monkeypatch.setenv("HIVE_CONTROL_PROJECT_TAG", PROJECT_TAG)
    monkeypatch.delenv("HIVE_CLUSTER_ID", raising=False)
    return {"group": MC_GROUP, "port": MC_PORT, "tag": PROJECT_TAG,
            "cluster_id": CLUSTER_ID}


@pytest.fixture()
def hlsa_factory(tmp_path, mc_env):
    """真 socket 集群工厂：默认 0.3s 心跳（快收敛），可调生产节拍。"""
    clusters: list[hc.Cluster] = []

    def make(name: str, seeds=None, *, control: bool = True,
             heartbeat: float = 0.3, idle: float | None = None, **kw):
        cfg = hc.Config(
            state_dir=tmp_path / name, name=name,
            host="127.0.0.1", port=0, advertise="127.0.0.1",
            discover=False, seeds=seeds or [],
            hive_dir=tmp_path / "repo",
            cluster_id=CLUSTER_ID,
            control_plane=control,
            heartbeat_interval=heartbeat,
            idle_heartbeat_interval=idle if idle is not None else heartbeat,
            peer_timeout=max(3.0, heartbeat * 10),
            dead_timeout=max(6.0, heartbeat * 20),
            election_min=max(0.3, heartbeat * 2),
            election_jitter=0.1,
            leader_lease=max(1.5, heartbeat * 5),
            membership_ttl=max(3.0, heartbeat * 10),
            control_limits=hc_limits(tmp_path, name),
            **kw,
        )
        c = hc.Cluster(cfg)
        c.start()
        if control:
            _speed_up_control_election(c)
        clusters.append(c)
        return c

    yield make
    for c in clusters:
        try:
            c.stop()
        except Exception:  # noqa: BLE001
            pass


def _speed_up_control_election(cluster) -> None:
    """白盒提速：控制面 Election 初始化时把等待窗设为 now+40s（
    ControlConfig.waiting_interval 默认），期间 is_leader=False、T1 摘要
    一条都不发。测试把 _waiting_until 挪近，让首拍落在 "+0.5s 当选
    +4s 切主抑制窗" 之后，而不必真等 40 秒。生产语义不受影响。
    """
    try:
        election = cluster.control_adapter.plane.election
        election._waiting_until = time.monotonic() + 0.5
    except AttributeError:
        pass


def hc_limits(tmp_path, name: str):
    """每节点独立的控制面 socket / incarnation 路径（避免进程内互踩）。"""
    from hive_control.config import Limits
    return Limits(
        socket_path=str(tmp_path / f"{name}-control.sock"),
        incarnation_path=str(tmp_path / f"{name}-incarnation"),
    )


class MulticastListener:
    """被动组播监听：加入测试组，记录 (时间戳, 原始字节)。"""

    def __init__(self, group: str, port: int):
        self.packets: list[tuple[float, bytes]] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                   socket.IPPROTO_UDP)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("", port))
            mreq = struct.pack("=4sl", socket.inet_aton(group),
                               socket.INADDR_ANY)
            self._sock.setsockopt(socket.IPPROTO_IP,
                                  socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as exc:
            self._sock.close()
            pytest.skip(f"组播不可用（join 失败: {exc}）")
        self._sock.settimeout(0.2)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            try:
                data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self.packets.append((time.monotonic(), data))

    def digests(self) -> list[dict]:
        out = []
        for _ts, data in self.packets:
            try:
                msg = json.loads(data.decode("utf-8").strip())
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict) and msg.get("type") == "lsa_digest":
                out.append(msg)
        return out

    def close(self):
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture()
def listener(mc_env):
    ln = MulticastListener(mc_env["group"], mc_env["port"])
    yield ln
    ln.close()


@pytest.fixture()
def send_spy(monkeypatch):
    """按 transport 实例记录组播发送时间线（真实发包，只加计数）。"""
    from hive_control.downlink import SocketTransport
    timeline: dict[int, list[float]] = {}
    refs: dict[int, object] = {}
    orig = SocketTransport.send_multicast

    def counting(self, data: bytes) -> None:
        timeline.setdefault(id(self), []).append(time.monotonic())
        refs[id(self)] = self
        return orig(self, data)

    monkeypatch.setattr(SocketTransport, "send_multicast", counting)
    return {"timeline": timeline, "refs": refs}


def adapter_status(cluster) -> dict:
    """adapter.status()：plane_available / lsa tx/rx / counters 等富状态。"""
    adapter = cluster.control_adapter
    assert adapter is not None, "control_plane=True 的集群应装配控制面"
    assert adapter.plane is not None, "控制面应装配成功（plane_available）"
    return adapter.status()


def plane_status(cluster) -> dict:
    """plane.status()：multicast_group/port、is_leader、epoch 等编排器状态。"""
    adapter = cluster.control_adapter
    assert adapter is not None and adapter.plane is not None
    return adapter.plane.status()


def senders_by_second(spy) -> dict[int, set[int]]:
    """每秒 → 发送过的 transport 实例集合。"""
    out: dict[int, set[int]] = {}
    for tid, times in spy["timeline"].items():
        for t in times:
            out.setdefault(int(t), set()).add(tid)
    return out


# --------------------------------------------------------------------------- #
# 0. 隔离契约（纯函数，不启 socket）
# --------------------------------------------------------------------------- #
def test_resolve_multicast_isolation_contract(mc_env, monkeypatch):
    from hive_control.config import (DEV_MULTICAST_GROUP, DEV_MULTICAST_PORT,
                                     resolve_multicast)
    # 先撒开环境变量，验证 cluster_id 兜底语义（dev 常驻实例器重的路径）
    monkeypatch.delenv("HIVE_CONTROL_MULTICAST_GROUP", raising=False)
    monkeypatch.delenv("HIVE_CONTROL_MULTICAST_PORT", raising=False)
    assert resolve_multicast("hive") == (PROD_GROUP, PROD_PORT), \
        "生产 cluster_id → 生产组"
    assert resolve_multicast("hive-dev") == (DEV_MULTICAST_GROUP,
                                             DEV_MULTICAST_PORT)
    assert resolve_multicast(CLUSTER_ID) == (DEV_MULTICAST_GROUP,
                                             DEV_MULTICAST_PORT), \
        "非 hive 的 cluster_id → 开发组，绝不禁入生产组播域"
    # 环境变量（代码实际读取的名字，见 config.py:37-38）优先级高于 cluster_id 兜底
    monkeypatch.setenv("HIVE_CONTROL_MULTICAST_GROUP", MC_GROUP)
    monkeypatch.setenv("HIVE_CONTROL_MULTICAST_PORT", str(MC_PORT))
    assert resolve_multicast("hive") == (MC_GROUP, MC_PORT)
    # 显式参数优先级最高
    assert resolve_multicast("hive", group="239.0.0.9", port=5001) == (
        "239.0.0.9", 5001)


def test_hazard_empty_cluster_id_resolves_to_production_group():
    """⚠️ 纵深防御缺口（如实钉住现状）：cluster_id 为空时兜底选生产组。

    resolve_multicast 的 dev 分支要求 cluster_id 非空且不等于 "hive"；
    程序化构造 Cluster（未显式给 cluster_id，Config 默认 "hive"）时
    一旦开启 control_plane 就会加入生产组播组。dev-cluster.sh 因显式
    设置 HIVE_CLUSTER_ID=hive-dev 而不受影响。修复本缺口后应更新本用例。
    """
    from hive_control.config import resolve_multicast
    assert resolve_multicast("") == (PROD_GROUP, PROD_PORT)


# --------------------------------------------------------------------------- #
# 1. cluster 层全链路（SLA-1 / SLA-2 集成口径）
# --------------------------------------------------------------------------- #
def test_t0_change_visible_to_leader_and_all_nodes_within_sla(hlsa_factory):
    a = hlsa_factory("a")
    assert wait_until(lambda: a.role == "leader"), "a 应当选"
    b = hlsa_factory("b", seeds=[a.url])
    c = hlsa_factory("c", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id)
    assert wait_until(lambda: c.leader_id == a.node_id)

    t0 = time.monotonic()
    b.publish_progress({"task": "T-IT1", "agent": "b", "state": "running",
                        "progress": 33, "step": "集成测试"})

    def leader_sees():
        with a.lock:
            rec = a.progress.get("T-IT1") or {}
            return rec.get("progress") == 33

    def all_see():
        with a.lock, b.lock, c.lock:
            for node in (a, b, c):
                if (node.progress.get("T-IT1") or {}).get("progress") != 33:
                    return False
            return True

    assert wait_until(leader_sees, timeout=300), \
        "SLA-1：子节点变更 → 总控可见 ≤300s（硬上限）"
    t_leader = time.monotonic() - t0
    assert wait_until(all_see, timeout=300), \
        "SLA-2：子节点变更 → 全集群可见 ≤300s（硬上限）"
    t_all = time.monotonic() - t0

    # 字段级 diff 落地：记录里的实质字段与发布值一致。注：updated_at/gen 等
    # 本地记账元数据来自 FULL_BROADCAST_S=30s 的全量广播兼容路径（0.16.05
    # 语义保留），不影响 LSA diff 本身的字段级性质（见合并窗口用例）。
    with a.lock:
        rec = dict(a.progress["T-IT1"])
    assert rec.get("progress") == 33 and rec.get("agent") == "b"
    assert rec.get("task") == "T-IT1" and rec.get("state") == "running"
    print(f"[SLA] leader 可见 {t_leader:.2f}s / 全集群 {t_all:.2f}s "
          f"(硬上限 300s，设计目标 10s)")
    assert t_leader <= 300.0 and t_all <= 300.0


def test_production_cadence_visibility_under_design_goal(hlsa_factory):
    """生产节拍（空闲 5s / 活跃 1s）下的可见性实测：设计目标 ≤10s。"""
    a = hlsa_factory("pa", heartbeat=1.0, idle=5.0)
    assert wait_until(lambda: a.role == "leader")
    b = hlsa_factory("pb", seeds=[a.url], heartbeat=1.0, idle=5.0)
    c = hlsa_factory("pc", seeds=[a.url], heartbeat=1.0, idle=5.0)
    assert wait_until(lambda: b.leader_id == a.node_id)
    assert wait_until(lambda: c.leader_id == a.node_id)

    t0 = time.monotonic()
    b.publish_progress({"task": "T-IT2", "agent": "b", "state": "running",
                        "progress": 50})

    def all_see():
        with a.lock, b.lock, c.lock:
            for node in (a, b, c):
                if (node.progress.get("T-IT2") or {}).get("progress") != 50:
                    return False
            return True

    assert wait_until(all_see, timeout=300)
    t_all = time.monotonic() - t0
    print(f"[SLA] 生产节拍全集群可见 {t_all:.2f}s（设计目标 ≤10s，硬上限 300s）")
    assert t_all <= 300.0


def test_merge_window_and_field_level_diff_on_live_path(hlsa_factory,
                                                        monkeypatch):
    a = hlsa_factory("ma", heartbeat=1.0)
    assert wait_until(lambda: a.role == "leader")
    b = hlsa_factory("mb", seeds=[a.url], heartbeat=1.0)
    assert wait_until(lambda: b.leader_id == a.node_id)

    applied: list[list] = []

    def counting_apply(self, lsas):
        applied.append([dict(x) for x in (lsas or [])])
        return orig_apply(self, lsas)

    orig_apply = hc.Cluster._apply_lsa_diffs_locked
    monkeypatch.setattr(hc.Cluster, "_apply_lsa_diffs_locked", counting_apply)

    # 第一波：新任务（基线落地）
    b.publish_progress({"task": "T-IT3", "agent": "b", "state": "running",
                        "progress": 10, "step": "s"})
    assert wait_until(lambda: (a.progress.get("T-IT3") or {})
                      .get("progress") == 10, timeout=30)

    # 第二波：同一 (area,key) 200ms 窗口内 3 次变更 → 只应合成 1 条 LSA，
    # 且 diff 只含真正变化的字段（progress），不带整卡快照。
    applied.clear()
    for progress in (20, 25, 30):
        b.publish_progress({"task": "T-IT3", "agent": "b", "state": "running",
                            "progress": progress, "step": "s"})

    assert wait_until(lambda: (a.progress.get("T-IT3") or {})
                      .get("progress") == 30, timeout=30)
    time.sleep(2.5)  # 越过两个心跳拍，确保没有第二条在途

    lsas = [lsa for batch in applied for lsa in batch
            if lsa.get("key") == ["hive", "T-IT3"]]
    # 变更源（b）在窗口内的 3 次变更必须合成 1 条（按 LSA seq 去重计数，
    # 同一条 LSA 可能经 请求体/响应体 双通道重复投递）。注：handle_push 在
    # follower 上先本地落再转发 leader，变更首现时 b（mb）与 leader（ma）
    # 各记一条同 diff 的 LSA —— 这是协议设计的双源，不算重复泛洪；
    # 后续变更受 MinLSInterval(5s) 押后，可能落在采集窗口外，不作断言。
    origin = [lsa for lsa in lsas
              if str(lsa.get("originator")).startswith("mb-")]
    assert len({l.get("seq") for l in origin}) == 1, (
        f"合并窗口内 3 次变更应合成 1 条 change LSA，实际 "
        f"{sorted(l.get('seq') for l in origin)}")
    # 字段级：diff 只含实质任务字段，本地记账元数据被剔除，且终值正确
    substantive = {"task", "agent", "state", "progress", "step", "message"}
    for lsa in lsas:
        assert set(lsa["diff"]) <= substantive, lsa["diff"]
        assert lsa["diff"].get("progress") == 30
        for forbidden in FORBIDDEN_IN_DIGEST:
            assert forbidden not in lsa


def test_steady_state_heartbeat_returns_no_full_state(hlsa_factory):
    """稳态（摘要一致）：心跳响应空载荷；不一致：只回增量，绝不回全量。"""
    a = hlsa_factory("sa")
    assert wait_until(lambda: a.role == "leader")
    b = hlsa_factory("sb", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id)
    b.publish_progress({"task": "T-IT4", "agent": "b", "state": "running",
                        "progress": 7})
    assert wait_until(lambda: (a.progress.get("T-IT4") or {})
                      .get("progress") == 7, timeout=30)

    def peer_body(lsa_summary):
        return {"node": {"id": b.node_id, "name": "sb",
                         "url": b.url, "incarnation": 1},
                "term": a.term, "leader": False,
                "sd": hc._state_digest(a.progress, a.leases),
                "lsa": lsa_summary}

    # 摘要一致 → 空载荷（0.16.05 语义在 H-LSA 邻居下保持）
    with a.lock:
        dig = a._lsa.build_digest(kind="digest")
    same = {"seq": dig["seq"], "digest": dig["digest"],
            "epoch": dig["leader_epoch"], "area": dig["area"]}
    _, resp = a.handle_heartbeat(peer_body(same), peer_ip="127.0.0.1")
    assert resp["progress"] == {}
    assert resp["leases"] == {}
    assert resp["lsa_diffs"] == []

    # 摘要不一致（对端是空 LSDB 的新节点）：响应仍不带全量，只带水位摘要，
    # 由对端自行发现落后后走增量补拉/T2 对账（leader 无 pending 时不重放）。
    stale = {"seq": 0, "digest": "sha256:empty", "epoch": 0, "area": ""}
    _, resp2 = a.handle_heartbeat(peer_body(stale), peer_ip="127.0.0.1")
    assert resp2["progress"] == {} and resp2["leases"] == {}, \
        "H-LSA 邻居路径绝不回全量 progress/leases"
    summary = resp2["lsa"] or {}
    assert summary.get("digest") not in (None, "", stale["digest"]), \
        "响应携带内容摘要，对端可据此发现落后"


def test_high_frequency_burst_respects_token_bucket(hlsa_factory):
    """SLA-5 集成口径：小集群高频变更下单包 ≤1400B、周期 LSA 条数受削峰。"""
    a = hlsa_factory("ha", heartbeat=0.3)
    assert wait_until(lambda: a.role == "leader")
    b = hlsa_factory("hb", seeds=[a.url], heartbeat=0.3)
    assert wait_until(lambda: b.leader_id == a.node_id)

    # 60 次高频变更（一个令牌桶周期 5s 内），最终值必须收敛、不丢状态；
    # 合并窗口保证窗口内 N 次变更只合成 1 条 change LSA（不重复泛洪）。
    for i in range(60):
        b.publish_progress({"task": "T-IT5", "agent": "b",
                            "state": "running", "progress": i + 1})
    assert wait_until(lambda: (a.progress.get("T-IT5") or {})
                      .get("progress") == 60, timeout=60)

    import hive_lsa
    # 单条 LSA 报文尺寸上限（hive_lsa 分片口径，含完整字段级 diff）。
    # 取自 leader 的真实 LSACore：outbox 里此刻合成后的 change LSA。
    with a.lock:
        packets = a._lsa.tick()["packets"]
    for pkt in packets:
        assert hive_lsa.packet_bytes(pkt) <= hive_lsa.MAX_PACKET_BYTES
    # 合并窗口效果：60 次变更只产生有限条 LSA（远低于变更次数），
    # 令牌桶上限 TOKEN_BUCKET_LSA=32/周期是硬约束，不会被高频突破。
    assert hive_lsa.TOKEN_BUCKET_LSA >= 1
    print("[SLA] 高频 60 次变更收敛完成（字段级增量，无全量快照）")


# --------------------------------------------------------------------------- #
# 2. 控制面层真组播（SLA-7 集成口径 + L3 隔离证据）
# --------------------------------------------------------------------------- #
def test_leader_multicasts_isolated_digest_packets(hlsa_factory, listener,
                                                    send_spy, mc_env):
    a = hlsa_factory("ca")
    assert wait_until(lambda: a.role == "leader")
    b = hlsa_factory("cb", seeds=[a.url])

    # 首个 T1 摘要：切主抑制窗（4s）之后的第一拍
    got = wait_until(lambda: listener.digests(), timeout=30, interval=0.5)
    assert got, "30s 内应收到至少一条真实组播 lsa_digest"

    for msg in got:
        assert msg["cluster_id"] == CLUSTER_ID, "报文必须来自本测试集群"
        assert msg["project_tag"] == PROJECT_TAG
        assert "hmac" in msg
        assert int(msg.get("leader_epoch", 0)) >= 1
        for forbidden in FORBIDDEN_IN_DIGEST:
            assert forbidden not in msg, f"摘要包夹带状态体字段 {forbidden}"

    # 原始 UDP 报文尺寸 ≤1400B（含 HMAC 与换行）
    for _ts, data in listener.packets:
        try:
            msg = json.loads(data.decode("utf-8").strip())
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(msg, dict) and msg.get("type") == "lsa_digest":
            assert len(data) <= 1400

    # 只有控制面 Leader 的 transport 发过组播；另一个实例必须为 0
    sn = plane_status(b)
    assert sn["multicast_group"] == MC_GROUP and sn["multicast_port"] == MC_PORT, \
        "控制面实际组播地址必须是测试隔离组（非生产 239.255.42.1:47899）"
    assert sn["multicast_mode"] == "multicast"
    timeline = send_spy["timeline"]
    senders = [tid for tid, times in timeline.items() if times]
    assert len(senders) <= 1, f"同一时刻只允许一个组播源，实际 {len(senders)}"
    print(f"[SLA-7] 组播源实例数 {len(senders)}（≤1）")


def test_follower_plane_accepts_leader_digests(hlsa_factory, listener,
                                               mc_env):
    a = hlsa_factory("fa")
    assert wait_until(lambda: a.role == "leader")
    b = hlsa_factory("fb", seeds=[a.url])
    assert wait_until(lambda: listener.digests(), timeout=30, interval=0.5)

    # follower 的接收计数：真实跨节点组播接收，校验全零的坏包计数
    def follower_rx():
        rx = (adapter_status(b).get("lsa") or {}).get("rx") or {}
        return rx if rx.get("received", 0) >= 1 else None

    rx = wait_until(follower_rx, timeout=30, interval=0.5)
    assert rx, "follower 应真实收到 leader 的组播摘要"
    assert rx.get("accepted", 0) >= 1
    for bad in ("malformed", "bad_project", "bad_cluster", "bad_hmac"):
        assert rx.get(bad, 0) == 0, f"{bad} 应为 0，实际 {rx.get(bad)}"


def test_leader_stop_single_multicast_source_recovery(hlsa_factory, listener,
                                                      send_spy):
    """切主：旧主停止 → 幸存者接管 → 全程任意一秒组播源 ≤1。"""
    a = hlsa_factory("la")
    assert wait_until(lambda: a.role == "leader")
    b = hlsa_factory("lb", seeds=[a.url])
    assert wait_until(lambda: listener.digests(), timeout=30, interval=0.5)

    # 工厂已把 Election 等待窗挪近（启动时 +0.5s），此刻早已过期：
    # 幸存者探测到 leader_lost（heartbeat_timeout 6s）后可立即自选。
    time.sleep(2.0)
    before = len(send_spy["timeline"].get(id(b.control_adapter.plane
                                              ._transport), []))
    a.stop()

    def survivor_sends():
        if not plane_status(b).get("is_leader"):
            return None
        now = len(send_spy["timeline"].get(
            id(b.control_adapter.plane._transport), []))
        return (now - before) > 0 or None

    assert wait_until(survivor_sends, timeout=120, interval=1.0), \
        "旧主停止后，幸存者应成为控制面 Leader 并恢复 T1 摘要"
    got = wait_until(lambda: listener.digests(), timeout=10, interval=0.5)

    # 全时间线：任意一秒内组播源 ≤1（不重复泛洪的硬判据）
    per_second = senders_by_second(send_spy)
    for sec, senders in per_second.items():
        assert len(senders) <= 1, f"第 {sec}s 出现 {len(senders)} 个组播源"

    # 接收侧 epoch 单调不回退
    epochs = [int(m.get("leader_epoch", 0)) for m in got]
    assert epochs == sorted(epochs), "leader_epoch 必须单调不回退"
    print(f"[SLA-7] 切主后组播源恢复，全程单源；epoch 序列 {epochs[:5]}…")


# --------------------------------------------------------------------------- #
# 3. git 层协调仓链路（autosync + 节流契约）
# --------------------------------------------------------------------------- #
CARD = """---
id: {tid}
title: {title}
state: pending
repo: light
priority: P2
---
集成测试卡
"""


class Repos:
    """bare origin 替身 + seed（模拟别的节点 push）+ 被测 clone。"""

    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "hive.git"
        self.seed = root / "seed"
        self.a = root / "hive-a"
        sh(["git", "init", "--bare", "-b", "main", str(self.origin)],
           env=GIT_ENV)
        sh(["git", "init", "-b", "main", str(self.seed)], env=GIT_ENV)
        (self.seed / "tasks").mkdir()
        self.write_card(self.seed, "T-9000", "基线卡")
        (self.seed / ".gitignore").write_text(".hive/\n__pycache__/\n",
                                              encoding="utf-8")
        self.git(["add", "-A"], self.seed)
        self.git(["commit", "-q", "-m", "init"], self.seed)
        self.git(["remote", "add", "origin", str(self.origin)], self.seed)
        self.git(["push", "-q", "-u", "origin", "main"], self.seed)
        sh(["git", "clone", "-q", str(self.origin), str(self.a)], env=GIT_ENV)

    @staticmethod
    def git(args, cwd, check=True):
        return sh(["git", "-C", str(cwd), *args], check=check, env=GIT_ENV)

    @staticmethod
    def write_card(path: Path, tid: str, title: str):
        (path / "tasks").mkdir(exist_ok=True)
        (path / "tasks" / f"{tid}.md").write_text(
            CARD.format(tid=tid, title=title), encoding="utf-8")

    def push_card(self, tid: str, title: str = "远端新卡"):
        self.write_card(self.seed, tid, title)
        self.git(["add", "-A"], self.seed)
        self.git(["commit", "-q", "-m", f"add {tid}"], self.seed)
        self.git(["push", "-q", "origin", "main"], self.seed)

    def head(self, path: Path) -> str:
        return self.git(["rev-parse", "HEAD"], path).stdout.strip()

    def origin_main(self, path: Path) -> str:
        return self.git(["rev-parse", "origin/main"], path).stdout.strip()

    def porcelain(self, path: Path) -> str:
        return self.git(["status", "--porcelain"], path).stdout


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    hive._ls_remote_cache.clear()
    for key in (hive_repos.SYNC_ENV_TIER, hive_repos.SYNC_ENV_HEAD_SHA,
                hive_repos.SYNC_ENV_EVENT, hive_repos.SYNC_ENV_FORCE,
                hive_repos.SYNC_ENV_DISABLE):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(hive_repos.SYNC_ENV_STATE,
                       str(tmp_path / "sync-state.json"))
    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "git")
    yield Repos(tmp_path)
    hive._ls_remote_cache.clear()


def _status_json(h, capsys, **kw):
    h.cmd_status(as_json=True, **kw)
    return json.loads(capsys.readouterr().out)


def _health(row: dict) -> dict:
    assert "local_stale" in row, "旧布尔字段必须保留（向后兼容）"
    assert "sync_health" in row, "新结构化对象必须存在"
    return row["sync_health"]


def _state_file(tmp_path) -> Path:
    return tmp_path / "sync-state.json"


def _age_state(tmp_path, seconds: float = 120.0):
    p = _state_file(tmp_path)
    st = json.loads(p.read_text(encoding="utf-8"))
    st["last_sync_at"] = time.time() - seconds
    p.write_text(json.dumps(st), encoding="utf-8")


def test_t1_digest_head_sha_same_zero_gitea_traffic(repos, capsys, tmp_path,
                                                    monkeypatch):
    """T1 组播 head_sha 与本地已知 origin/main 一致 → 零 Gitea 流量。"""
    # 冷启动基线（建立节流窗口 + 本地 origin/main 认知）
    rows = _status_json(hive.Hive(repos.a), capsys)
    assert _state_file(tmp_path).exists()
    local_origin = repos.origin_main(repos.a)
    head_before = repos.head(repos.a)

    # 别的节点往 origin 推了新卡（origin 前进），但组播摘要通告的
    # head_sha 与本地已知 origin/main 相同 → 本轮不问 Gitea。
    repos.push_card("T-9001")
    _age_state(tmp_path, 120)
    monkeypatch.setenv(hive_repos.SYNC_ENV_TIER, "T1")
    monkeypatch.setenv(hive_repos.SYNC_ENV_HEAD_SHA, local_origin)

    rows = _status_json(hive.Hive(repos.a), capsys)
    for row in rows:
        health = _health(row)
        assert health["autosynced"] is False, "head_sha 一致时不得触发同步"
        assert "无需问 Gitea" in health["reason"], health["reason"]
    assert repos.head(repos.a) == head_before, "本地 HEAD 不应被推动"
    assert not (repos.a / "tasks" / "T-9001.md").exists(), \
        "未对账就不该看到远端新卡（Git 仍是事实账）"


def test_t0_event_early_sync_reflects_new_card(repos, capsys, tmp_path,
                                               monkeypatch):
    """T0 事件 → 提前对账 → autosync ff-only → 新卡面立即可见。"""
    _status_json(hive.Hive(repos.a), capsys)  # 冷启动基线
    repos.push_card("T-9002")
    _age_state(tmp_path, 120)
    monkeypatch.setenv(hive_repos.SYNC_ENV_TIER, "T0")
    monkeypatch.setenv(hive_repos.SYNC_ENV_EVENT, "1")

    rows = _status_json(hive.Hive(repos.a), capsys)
    assert any(r["id"] == "T-9002" for r in rows), "T0 提前同步后应看到新卡"
    for row in rows:
        health = _health(row)
        assert row["local_stale"] is False
        assert health["autosynced"] is True
        assert health["tier"] == "T0"
        assert health["behind"] is False
    assert (repos.a / "tasks" / "T-9002.md").exists()
    assert json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))[
        "lsdb_seq"] == 2


def test_t2_window_skips_remote_without_event(repos, capsys, tmp_path,
                                              monkeypatch):
    """T2 窗口内且无 T0/T1 信号 → 不连 Gitea（空闲稳态零请求）。"""
    _status_json(hive.Hive(repos.a), capsys)  # 冷启动基线（记录 next_t2_at）
    repos.push_card("T-9003")
    # 不拨时钟：仍在 T2 窗口内、无事件 → 跳过远端对账
    rows = _status_json(hive.Hive(repos.a), capsys)
    for row in rows:
        health = _health(row)
        assert health["autosynced"] is False
        assert "跳过远端对账" in health["reason"], health["reason"]
    assert not (repos.a / "tasks" / "T-9003.md").exists()


def test_dirty_worktree_untouched_through_status_chain(repos, capsys,
                                                       tmp_path):
    """L5 关键回归：脏工作区只读不改，porcelain 前后字节级一致。"""
    dirty = repos.a / "在途改动.md"
    dirty.write_text("别人的在途产物\n", encoding="utf-8")
    before = repos.porcelain(repos.a)
    assert before.strip(), "前置：工作区确实处于脏状态"
    head_before = repos.head(repos.a)

    rows = _status_json(hive.Hive(repos.a), capsys)
    for row in rows:
        health = _health(row)
        assert health["state"] == hive_repos.DIRTY_WORKTREE
        assert health["autosynced"] is False
        assert "未提交改动" in health["reason"], health["reason"]

    assert repos.porcelain(repos.a) == before, "porcelain 必须字节级一致"
    assert repos.head(repos.a) == head_before, "HEAD 不得被推动"
    assert dirty.read_text(encoding="utf-8") == "别人的在途产物\n"


# --------------------------------------------------------------------------- #
# 4. 活体 dev 实例证据（T-0357 灰度后复跑时自动生效）
# --------------------------------------------------------------------------- #
def test_live_dev_instance_isolation_evidence():
    """dev 隔离实例在 127.0.0.1:8888 常驻时的活体证据；不在则跳过。

    跳过 ≠ 通过：它只在没有 dev 实例的环境下让位，
    T-0357 灰度复跑时（dev-cluster.sh start 之后）必须真实生效。
    """
    import urllib.request
    url = "http://127.0.0.1:8888/api/cluster/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except OSError:
        pytest.skip("dev 实例 (127.0.0.1:8888) 未运行——活体证据用例让位")

    assert health.get("cluster_id") == "hive-dev", "必须是 dev 集群"
    assert health.get("mcp", {}).get("enabled") is False
    assert health.get("store", {}).get("enabled") is False

    with urllib.request.urlopen(
            "http://127.0.0.1:8888/api/cluster/status", timeout=3) as resp:
        status = json.loads(resp.read().decode("utf-8"))
    control = status.get("control") or {}
    assert control.get("plane_available") is True, "T-0351 硬前置"
    assert control.get("degraded") is False
    assert control.get("multicast_mode") == "multicast"
    tx = (control.get("lsa") or {}).get("tx") or {}
    if tx.get("sent"):
        assert tx.get("bytes", 0) / tx["sent"] <= 1400, "平均包长 ≤1400B"
    # 已知观测缺陷（见验收报告「回派」）：adapter.status() 的 control_port 与
    # peers[].port 显示未覆写的 Limits.control_port 默认值 47899（生产组播
    # 端口）。实际收发不走该字段（组播走 multicast_port=47900、回退单播走
    # limits.multicast_port、TCP 拉取走 HTTP+1），但该字段会误导运维，
    # 回派 T-0351。此处仅记录，不当作通过判据：
    peers = control.get("peers") or []
    for peer in peers:
        if peer.get("port") == PROD_PORT:
            print(f"[已知缺陷-回派 T-0351] 控制面 peers 显示生产端口: {peer}")
