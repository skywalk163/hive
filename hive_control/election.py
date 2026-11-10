"""控制面 Leader 选举：带宽探测、Waiting / 非抢占、心跳失效与确定性排序。

对应 ``docs/控制面v0.5-实现约定.md`` §3.2 与母文档 §3；裁定见 §4 的
D2（Hello 单播）/ D10（带宽不可用）/ D11（Hello 与邻居时钟）/ D12（无需投票）。

本模块**只生成 Hello / LeaderHeartbeat 的 payload 并维护纯内存状态机**：

- 不创建、不连接、不发送任何网络报文；UDP 收发由 T-0307 / T-0310 装配；
- 不实现组播、单播、投票、quorum；排序确定性 + ``leader_declaration`` 即可选出唯一 Leader；
- 所有时间来自注入的 ``clock``（默认 ``time.monotonic``），没有任何 ``sleep``；
- 只依赖标准库与共享骨架（``config.ControlConfig`` / ``config.Limits``），不改骨架。
"""

from __future__ import annotations

import logging
import math
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from typing import Any, Callable

from .config import ControlConfig, Limits

__all__ = ["SelfInfo", "BandwidthProbe", "Election"]

logger = logging.getLogger(__name__)

# SIOCETHTOOL / ETHTOOL_GSET（Linux 本地 ioctl 常量，见 BandwidthProbe）。
_SIOCETHTOOL = 0x8946
_ETHTOOL_GSET = 0x00000001
_SPEED_UNKNOWN = 0xFFFF


def _finite_positive(value: Any) -> float | None:
    """把任意输入收敛成「正的有限浮点数」；不合法一律返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _as_int(value: Any) -> int | None:
    """把任意输入收敛成 int；bool / 非法值返回 None。"""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 带宽探测
# --------------------------------------------------------------------------- #

def _default_interface() -> str | None:
    """从 /proc/net/route 找默认路由网卡；不可读返回 None（不碰网络）。"""
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as fh:
            for line in fh.read().splitlines()[1:]:
                columns = line.split()
                if len(columns) >= 2 and columns[1] == "00000000":
                    return columns[0]
    except OSError:
        pass
    return None


def _sysfs_speed(interface: str) -> float | None:
    """Linux sysfs 暴露的 ETHTOOL_GSET speed（Mbps）；不可读返回 None。

    sysfs 与 SIOCETHTOOL 读的是同一个内核字段，但没有 ioctl 依赖，
    因此优先用它；容器 / 虚拟网卡常返回 -1，``_finite_positive`` 会挡掉。
    """
    try:
        with open(f"/sys/class/net/{interface}/speed", "r", encoding="utf-8") as fh:
            return _finite_positive(fh.read().strip())
    except OSError:
        return None


def _ethtool_speed_linux(interface: str) -> float | None:
    """SIOCETHTOOL/ETHTOOL_GSET 兜底，仅 Linux 且仅在 sysfs 读不到时调用。

    这是**纯本地 ioctl**：``socket`` 只用来取一个 fd 传给内核，全程不
    ``connect`` / ``send`` / ``sendto`` 任何报文，也不发组播。任何失败都
    返回 None。非 Linux 平台（Windows / FreeBSD）永不进入本函数。
    """
    try:
        import array
        import fcntl  # noqa: F401  (仅 Linux 存在)
        import socket as _socket
    except ImportError:  # pragma: no cover - 非 Linux
        return None
    try:
        # struct ethtool_cmd：自然对齐后 48 字节；speed@12、speed_hi@32。
        command = array.array("B", b"\x00" * 48)
        struct.pack_into("I", command, 0, _ETHTOOL_GSET)
        name = interface.encode("utf-8")[:15]
        # struct ifreq：16 字节名字 + 指向 ethtool_cmd 的指针。
        request = bytearray(struct.pack("16sP", name, command.buffer_info()[0]))
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as sock:
            fcntl.ioctl(sock.fileno(), _SIOCETHTOOL, request)
        low = struct.unpack_from("H", command, 12)[0]
        high = struct.unpack_from("H", command, 32)[0]
        if low == _SPEED_UNKNOWN:
            return None
        return _finite_positive(low | (high << 16))
    except (OSError, ValueError, struct.error, OverflowError):
        return None


class BandwidthProbe:
    """本机网卡速率探测与 router_id 生成。

    ``speed_mbps`` 是测试缝：单测注入速率，避免依赖真实网卡 / 平台。
    生产环境不传，由 ``interface_mbps()`` 自行探测；探测不到即 ``None``，
    ``factor()`` 回落 1.0（D10）。
    """

    def __init__(self, interface: str | None = None, *,
                 speed_mbps: float | None = None):
        self.interface = interface
        self._injected = _finite_positive(speed_mbps)
        self._unavailable_logged = False

    def interface_mbps(self) -> float | None:
        """Linux SIOCETHTOOL/ETHTOOL_GSET（优先 sysfs）；失败一律 None。"""
        if self._injected is not None:
            return self._injected
        if not sys.platform.startswith("linux"):
            return self._note_unavailable()
        iface = self.interface or _default_interface()
        if not iface:
            return self._note_unavailable()
        value = _sysfs_speed(iface)
        if value is None:
            value = _ethtool_speed_linux(iface)
        if value is None:
            return self._note_unavailable()
        return float(value)

    @staticmethod
    def router_id(address: str | None, hostname: str) -> int:
        """由地址 + 主机名生成稳定、确定性、全网一致的 32 位 router_id。"""
        material = f"{address or ''}|{hostname or ''}".encode("utf-8")
        return zlib.crc32(material) & 0xFFFFFFFF

    def factor(self, reference_mbps: float | None) -> float:
        """自身速率 / 参考速率；任一侧无效 → 1.0（D10）。"""
        own = self.interface_mbps()
        reference = _finite_positive(reference_mbps)
        if own is None or reference is None:
            self._log_unavailable()
            return 1.0
        value = own / reference
        if not math.isfinite(value):
            self._log_unavailable()
            return 1.0
        return max(0.0, value)

    # -- 内部 ------------------------------------------------------------ #
    def _note_unavailable(self) -> None:
        self._log_unavailable()
        return None

    def _log_unavailable(self) -> None:
        """``bandwidth_unavailable`` 每个实例只记一次日志，避免刷屏。"""
        if not self._unavailable_logged:
            self._unavailable_logged = True
            logger.warning(
                "bandwidth_unavailable: 本机网卡速率不可探测，"
                "bandwidth_factor 回落 1.0")


@dataclass(frozen=True)
class SelfInfo:
    """本节点参与选举的静态身份（§3.2）。"""

    router_id: int
    node: str
    address: str
    port: int
    priority: int
    interface_mbps: float | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "router_id", int(self.router_id))
        object.__setattr__(self, "node", str(self.node))
        object.__setattr__(self, "address", str(self.address))
        object.__setattr__(self, "port", int(self.port))
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "interface_mbps",
                           _finite_positive(self.interface_mbps))


# --------------------------------------------------------------------------- #
# 选举状态机
# --------------------------------------------------------------------------- #

class Election:
    """确定性、非抢占、带宽感知的控制面 Leader 选举状态机。

    阶段：``waiting`` → ``electing`` → ``leading`` / ``following``。

    - 启动先 Waiting ``config.waiting_interval``；期间收到
      ``leader_declaration=true``（或 Leader 心跳）立即承认，不竞选。
    - Waiting 结束仍无 Leader：存活节点中排名第一者发 ``declare``
      （Hello 带 ``leader_declaration=true``）；其余节点等它的声明 / 心跳。
    - **非抢占**：现任 Leader 心跳有效时，排名更高也不发起竞选。
    - 心跳超过 ``limits.heartbeat_timeout`` 未刷新 → ``leader_lost``；
      新 Leader epoch = 已知最大 epoch + 1。
    - 排名键：``(priority, bandwidth_factor, router_id)``，都大者胜（字典序）。
      ``priority == 0`` 不参选，但仍可被选 / 被承认。
    """

    def __init__(self, self_info: SelfInfo, *, limits: Limits,
                 config: ControlConfig | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.self_info = self_info
        self.limits = limits
        # 契约 §3.2 的签名不含 config；这里作为**可选**关键字补上，
        # 让 waiting_interval / reference_bandwidth（§9 用户配置）可注入，
        # 不传时行为与契约默认值完全一致。
        self.config = config or ControlConfig()
        self.clock = clock

        self._peers: dict[int, dict[str, Any]] = {}
        self._leader_id: int | None = None
        self._leader_epoch = 0
        self._max_epoch = 0
        self._leader_last_seen = 0.0
        self._is_leader = False
        self._declared = False
        self._declared_epoch = 0
        self._phase = "waiting"
        self._waiting_until = self._now() + float(self.config.waiting_interval)

        self._last_hello_at: float | None = None
        self._last_heartbeat_at: float | None = None
        self._downlink_sequence = 0
        self._bandwidth_unavailable_logged = False

    # -- 时间 / 时钟 ------------------------------------------------------ #
    def _now(self) -> float:
        return float(self.clock())

    # -- 邻居 ------------------------------------------------------------ #
    def _expire_peers(self, now: float) -> None:
        timeout = float(self.limits.peer_timeout)
        for rid, peer in list(self._peers.items()):
            if now - peer["last_seen"] >= timeout:
                del self._peers[rid]

    def _alive_peers(self, now: float) -> list[dict[str, Any]]:
        return list(self._peers.values())

    # -- 带宽 / 排名 ----------------------------------------------------- #
    def _self_mbps(self) -> float | None:
        return _finite_positive(self.self_info.interface_mbps)

    def _effective_reference(self) -> float | None:
        """参考带宽 = 存活节点（含自己）interface_mbps 最大值。

        ``config.reference_bandwidth`` 是 §9 的人工覆盖，设置时优先；
        全未知 / 单节点时回落到自身值或 None。
        """
        override = _finite_positive(self.config.reference_bandwidth)
        if override is not None:
            return override
        now = self._now()
        self._expire_peers(now)
        speeds = [self._self_mbps()]
        speeds.extend(_finite_positive(p.get("interface_mbps"))
                      for p in self._alive_peers(now))
        known = [v for v in speeds if v is not None]
        return max(known) if known else None

    @property
    def reference_mbps(self) -> float | None:
        return self._effective_reference()

    def _note_bandwidth_unavailable(self) -> None:
        """``bandwidth_unavailable`` 每个 Election 实例只记一次日志。"""
        if not self._bandwidth_unavailable_logged:
            self._bandwidth_unavailable_logged = True
            logger.warning(
                "bandwidth_unavailable: 参考/自身带宽不可知，"
                "bandwidth_factor 回落 1.0")

    def _own_factor(self) -> float:
        own = self._self_mbps()
        reference = self.reference_mbps
        if own is None or reference is None:
            self._note_bandwidth_unavailable()
            return 1.0
        factor = own / reference
        if not math.isfinite(factor):
            self._note_bandwidth_unavailable()
            return 1.0
        return max(0.0, factor)

    def _peer_rank(self, peer: dict[str, Any]) -> tuple[int, float, int]:
        """用**本节点统一算出的参考带宽**重算邻居系数，保证排名确定性。"""
        reference = self.reference_mbps
        mbps = _finite_positive(peer.get("interface_mbps"))
        advertised = _finite_positive(peer.get("bandwidth_factor"))
        if mbps is not None and reference is not None:
            factor = mbps / reference
        elif advertised is not None:
            factor = advertised
        else:
            self._note_bandwidth_unavailable()
            factor = 1.0
        if not math.isfinite(factor):
            factor = 1.0
        return (int(peer.get("priority", 0)), max(0.0, factor),
                int(peer["router_id"]))

    def _self_rank(self) -> tuple[int, float, int]:
        return (self.self_info.priority, self._own_factor(),
                self.self_info.router_id)

    def _top_ranked(self, now: float) -> bool:
        self._expire_peers(now)
        own = self._self_rank()
        return all(own > self._peer_rank(peer)
                   for peer in self._alive_peers(now))

    # -- Leader 状态 ----------------------------------------------------- #
    def _leader_valid(self, now: float) -> bool:
        """现任（非自己）Leader 的心跳是否仍在 heartbeat_timeout 内。"""
        if self._leader_id is None or self._is_leader:
            return False
        return (now - self._leader_last_seen) < float(
            self.limits.heartbeat_timeout)

    def _set_leader(self, rid: int, epoch: int, now: float) -> None:
        self._leader_id = rid
        self._leader_epoch = int(epoch)
        self._max_epoch = max(self._max_epoch, self._leader_epoch)
        self._leader_last_seen = now
        self._is_leader = False
        self._declared = False
        self._phase = "following"

    def _step_down(self) -> None:
        self._is_leader = False
        self._leader_id = None
        self._declared = False
        self._phase = "electing"

    def _outranked_by(self, rid: int, now: float) -> bool:
        """同 epoch 冲突时的确定性裁决：对方排名更高吗？"""
        peer = self._peers.get(rid)
        if peer is None:
            return False
        self._expire_peers(now)
        return self._peer_rank(peer) > self._self_rank()

    def _adopt(self, rid: int, epoch: int, kind: str, now: float) -> str:
        """处理一次「某节点自称 Leader」的声明 / 心跳。"""
        if self._is_leader:
            # 已有更高 epoch 的 Leader 出现：单调 epoch 安全优先，主动下野。
            if epoch > self._leader_epoch:
                self._step_down()
                self._set_leader(rid, epoch, now)
                return "leader_lost"
            # 分区愈合：同 epoch 双主时按确定性排名让位，避免永久双主。
            if epoch == self._leader_epoch and self._outranked_by(rid, now):
                self._step_down()
                self._set_leader(rid, epoch, now)
                return "leader_lost"
            return "ignored"

        if self._declared:
            # 自己已发 declare，又有同 epoch 的更高排名者声明：撤回本轮。
            if epoch >= self._declared_epoch and self._outranked_by(rid, now):
                self._declared = False
                self._set_leader(rid, epoch, now)
                return "leader_lost"
            if epoch > self._declared_epoch:
                self._declared = False
                self._set_leader(rid, epoch, now)
                return "leader_lost"

        current = self._leader_id
        if current is not None and self._leader_valid(now):
            if current == rid:
                self._leader_last_seen = now
                self._leader_epoch = max(self._leader_epoch, epoch)
                self._max_epoch = max(self._max_epoch, self._leader_epoch)
                return kind  # "hello" / "heartbeat"
            if epoch > self._leader_epoch:
                # 更高 epoch 的 Leader 取代旧 Leader（分区愈合）。
                self._leader_id = None
                self._declared = False
                self._set_leader(rid, epoch, now)
                return "leader_lost"
            # 非抢占：现任 Leader 心跳有效，别的声明一律不改主。
            return "hello" if kind == "hello" else "ignored"

        if current is not None and current != rid:
            # 旧 Leader 已失效，换人。
            self._leader_id = None
            self._set_leader(rid, epoch, now)
            return "leader_lost"

        self._set_leader(rid, epoch, now)
        return "adopted"

    # -- 收包（只更新内存状态，不收发任何报文）-------------------------- #
    def observe(self, message: dict,
                source: tuple[str, int] | None = None) -> str:
        """处理一条入站控制面报文。

        返回 ``"hello"`` / ``"heartbeat"`` / ``"ignored"`` /
        ``"adopted"`` / ``"leader_lost"``：

        - 普通 Hello / Heartbeat 只刷新邻居与心跳，不改主；
        - 首次承认某 Leader（含 Waiting 期间收到 ``leader_declaration``）
          返回 ``"adopted"``；
        - 更高 epoch 的 Leader 取代现任（或自己下野）返回 ``"leader_lost"``；
        - 畸形报文、自己的回环包、无关类型返回 ``"ignored"``。
        """
        if not isinstance(message, dict):
            return "ignored"
        kind = str(message.get("type") or message.get("kind") or "")
        if kind == "hello":
            return self._on_hello(message, source)
        if kind == "heartbeat":
            return self._on_heartbeat(message, source)
        return "ignored"

    def _on_hello(self, message: dict,
                  source: tuple[str, int] | None) -> str:
        rid = _as_int(message.get("router_id"))
        if rid is None:
            return "ignored"
        now = self._now()
        epoch = _as_int(message.get("leader_epoch")) or 0
        self._max_epoch = max(self._max_epoch, epoch)
        if rid == self.self_info.router_id:
            return "ignored"  # 自己的 Hello 回环，不建邻居、不自封
        address = message.get("address") or (source[0] if source else "")
        port = _as_int(message.get("port"))
        if port is None:
            port = source[1] if source else 0
        peer = {
            "router_id": rid,
            "node": str(message.get("node") or ""),
            "address": str(address),
            "port": int(port),
            "priority": _as_int(message.get("priority")) or 0,
            "interface_mbps": _finite_positive(message.get("interface_mbps")),
            "bandwidth_factor": _finite_positive(
                message.get("bandwidth_factor")),
            "leader_epoch": epoch,
            "leader_declaration": bool(message.get("leader_declaration")),
            "last_seen": now,
            "source": tuple(source) if source else None,
        }
        self._peers[rid] = peer
        if peer["leader_declaration"]:
            return self._adopt(rid, epoch, "hello", now)
        return "hello"

    def _on_heartbeat(self, message: dict,
                      source: tuple[str, int] | None) -> str:
        rid = _as_int(message.get("router_id"))
        if rid is None:
            return "ignored"
        now = self._now()
        epoch = _as_int(message.get("leader_epoch")) or 0
        self._max_epoch = max(self._max_epoch, epoch)
        if rid == self.self_info.router_id:
            return "ignored"
        peer = self._peers.get(rid)
        if peer is not None:
            # 心跳也刷新邻居存活时间（D11 的 6s 过期窗）。
            peer["last_seen"] = now
            peer["leader_epoch"] = max(peer.get("leader_epoch", 0), epoch)
        return self._adopt(rid, epoch, "heartbeat", now)

    # -- 定时驱动 -------------------------------------------------------- #
    def tick(self) -> str:
        """推进一轮状态机；返回 ``none/wait/declare/become_leader/leader_lost``。

        不 sleep、不发包。发送 Hello / Heartbeat 的节奏由调用方（plane）
        按 ``limits`` 定时器触发；本方法只决定选举事件。
        """
        now = self._now()
        self._expire_peers(now)

        if self._is_leader:
            return "none"

        if self._leader_id is not None:
            if not self._leader_valid(now):
                # 心跳失效：放弃旧 Leader，下一轮再竞选（epoch 单调 +1）。
                self._leader_id = None
                self._declared = False
                self._phase = "electing"
                return "leader_lost"
            return "none"  # 非抢占：现任心跳有效，排名再高也不发起

        if self._phase == "waiting":
            if now < self._waiting_until:
                return "wait"
            self._phase = "electing"

        if self.self_info.priority == 0:
            # 不参选；仍可被别人选 / 被承认（见 observe）。
            return "none"

        if self._declared:
            # 声明已发出、期间没有更高 epoch / 有效 Leader 介入 → 正式称主。
            self._is_leader = True
            self._declared = False
            self._leader_id = self.self_info.router_id
            self._leader_epoch = self._declared_epoch
            self._max_epoch = max(self._max_epoch, self._leader_epoch)
            self._leader_last_seen = now
            self._phase = "leading"
            return "become_leader"

        if self._top_ranked(now):
            # 存活节点中排名第一者发 declare；其余节点等它的声明 / 心跳。
            self._declared = True
            self._declared_epoch = max(self._max_epoch,
                                       self._leader_epoch) + 1
            return "declare"
        return "none"

    # -- 出站 payload（只生成，不发送）---------------------------------- #
    def hello(self) -> dict:
        """生成 Hello payload（§2.2）；由调用方 UDP 单播给 ``peers()``（D2）。"""
        now = self._now()
        self._last_hello_at = now
        return {
            "type": "hello",
            "router_id": self.self_info.router_id,
            "node": self.self_info.node,
            "address": self.self_info.address,
            "port": self.self_info.port,
            "priority": self.self_info.priority,
            "bandwidth_factor": self._own_factor(),
            "interface_mbps": self._self_mbps(),
            "leader_declaration": self._is_leader or self._declared,
            "leader_epoch": max(self._leader_epoch, self._max_epoch,
                                self._declared_epoch),
            "sent_at": int(now * 1000),
        }

    def heartbeat(self) -> dict:
        """生成 LeaderHeartbeat payload（§2.2）；调用方仅在 ``is_leader`` 时发。"""
        self._downlink_sequence += 1
        self._last_heartbeat_at = self._now()
        return {
            "type": "heartbeat",
            "router_id": self.self_info.router_id,
            "leader_epoch": (self._leader_epoch if self._is_leader
                             else self._max_epoch),
            "downlink_sequence": self._downlink_sequence,
        }

    # -- 查询 ------------------------------------------------------------ #
    @property
    def leader_id(self) -> int | None:
        return self._leader_id

    @property
    def leader_epoch(self) -> int:
        return self._leader_epoch

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    def peers(self) -> list[dict]:
        """存活邻居（按 router_id 排序）；供 Hello 单播 / 回退单播使用。"""
        now = self._now()
        self._expire_peers(now)
        out: list[dict] = []
        for peer in sorted(self._peers.values(),
                           key=lambda p: p["router_id"]):
            row = dict(peer)
            row["source"] = list(row["source"]) if row["source"] else None
            out.append(row)
        return out

    def status(self) -> dict:
        """可观测状态快照（供 ``/api/cluster/status`` / 单测断言）。"""
        now = self._now()
        self._expire_peers(now)
        peers = self.peers()
        hello_due = (self._last_hello_at is None or
                     now - self._last_hello_at >= self.limits.hello_interval)
        heartbeat_due = (self._last_heartbeat_at is None or
                         now - self._last_heartbeat_at >=
                         self.limits.heartbeat_interval)
        return {
            "phase": self._phase,
            "state": self._phase,
            "router_id": self.self_info.router_id,
            "node": self.self_info.node,
            "address": self.self_info.address,
            "port": self.self_info.port,
            "priority": self.self_info.priority,
            "interface_mbps": self._self_mbps(),
            "bandwidth_factor": self._own_factor(),
            "reference_mbps": self.reference_mbps,
            "bandwidth_unavailable": self._bandwidth_unavailable_logged,
            "leader_id": self._leader_id,
            "leader_epoch": self._leader_epoch,
            "max_epoch": self._max_epoch,
            "leader_age": (now - self._leader_last_seen
                           if self._leader_id is not None else None),
            "is_leader": self._is_leader,
            "declared": self._declared,
            "waiting": self._phase == "waiting",
            "waiting_until": self._waiting_until,
            "peer_count": len(peers),
            "peers": [p["router_id"] for p in peers],
            "hello_due": hello_due,
            "heartbeat_due": heartbeat_due,
            "waiting_interval": self.config.waiting_interval,
            "hello_interval": self.limits.hello_interval,
            "heartbeat_interval": self.limits.heartbeat_interval,
            "heartbeat_timeout": self.limits.heartbeat_timeout,
            "peer_timeout": self.limits.peer_timeout,
        }
