"""控制面配置。

用户可配置项**只有**母文档 §9 的 9 项（``ControlConfig``）。
其余窗口、容量、间隔是内部常量（``Limits``），测试可构造覆盖，
但不通过 CLI / config.json / 环境变量暴露。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

MULTICAST_GROUP = "239.255.42.1"
MULTICAST_PORT = 47899
# 开发/隔离实例的默认组播组：与生产错开，避免 dev 实例误入生产组播域（T-0351 L3）。
DEV_MULTICAST_GROUP = "239.255.42.2"
DEV_MULTICAST_PORT = 47900
PRODUCTION_CLUSTER_ID = "hive"
# T1 摘要节奏（H-LSA §2）：默认 60s，与 dynamic_multicast_interval() 的高频
# 实时提示（0.5/1.0/2.0s）分层并存。
LSA_INTERVAL = 60.0
# 切主后抑制窗口 = 该系数 × limits.hello_interval（≥2，见 T-0351 第 4 条）。
LSA_SUPPRESS_HELLO_MULTIPLIER = 2.0
UPLINK_PORT_OFFSET = 1


def resolve_multicast(cluster_id: str = "", group: str = "",
                      port: int = 0) -> tuple[str, int]:
    """解析本实例应使用的组播地址。

    优先级：显式参数 > 环境变量 > 按 cluster_id 选择默认值。
    ``cluster_id`` 非空且不是生产 ``"hive"`` 时默认走开发组
    ``239.255.42.2:47900``，保证 dev 实例不会向生产组播组发包。
    """
    env_group = (os.environ.get("HIVE_CONTROL_MULTICAST_GROUP") or "").strip()
    env_port = (os.environ.get("HIVE_CONTROL_MULTICAST_PORT") or "").strip()
    dev = bool(cluster_id) and cluster_id != PRODUCTION_CLUSTER_ID
    final_group = str(group or env_group
                      or (DEV_MULTICAST_GROUP if dev else MULTICAST_GROUP))
    raw_port: object = port or env_port or (
        DEV_MULTICAST_PORT if dev else MULTICAST_PORT)
    try:
        final_port = int(raw_port)
    except (TypeError, ValueError):
        final_port = DEV_MULTICAST_PORT if dev else MULTICAST_PORT
    return final_group, final_port


def default_socket_path() -> str:
    """Worker ↔ serve 的 Unix socket（母文档 §2 固定路径；D10 平台适配）。"""
    if os.name == "nt":
        return str(Path(tempfile.gettempdir()) / "hive-control.sock")
    return "/tmp/hive-control.sock"


def default_incarnation_path() -> str:
    """incarnation 持久化文件（母文档 §4.1；D10 平台适配）。"""
    if os.name == "nt":
        return str(Path(tempfile.gettempdir()) / "hive-incarnation")
    return "/tmp/hive-incarnation"


DEFAULT_SOCKET_PATH = default_socket_path()
DEFAULT_INCARNATION_PATH = default_incarnation_path()


def dynamic_multicast_interval(active_entries: int) -> float:
    """母文档 §5.2 的动态周期（秒）。"""
    if active_entries <= 500:
        return 2.0
    if active_entries <= 2000:
        return 1.0
    return 0.5


@dataclass
class ControlConfig:
    """母文档 §9 的 9 个用户配置项，全部有默认值。"""

    leader_priority: int = 1
    reference_bandwidth: float | None = None
    waiting_interval: float = 40.0
    leader_multicast_interval: float | None = None  # None = 按 active 动态算
    lease_ttl: float = 90.0
    grace_period: float = 30.0
    rxmt_interval: float = 2.0
    pull_page_size: int = 100
    project_tag: str = ""
    # -- T-0351：网络隔离与 H-LSA 摘要（默认值即可跑通，dev 实例会自动错开） -- #
    cluster_id: str = ""
    multicast_group: str = ""       # "" = 按 cluster_id 自动选择
    multicast_port: int = 0         # 0  = 按 cluster_id 自动选择
    lsa_interval: float = LSA_INTERVAL
    hmac_key: str = ""              # "" = 按 cluster_id 派生（无 cluster_id 时关闭）

    def __post_init__(self) -> None:
        self.leader_priority = int(self.leader_priority)
        if not 0 <= self.leader_priority <= 255:
            raise ValueError("leader_priority 必须在 0..255")
        if self.reference_bandwidth is not None:
            self.reference_bandwidth = float(self.reference_bandwidth)
        for name in ("waiting_interval", "lease_ttl", "grace_period", "rxmt_interval"):
            value = float(getattr(self, name))
            if value <= 0:
                raise ValueError(f"{name} 必须为正数")
            setattr(self, name, value)
        if self.leader_multicast_interval is not None:
            self.leader_multicast_interval = float(self.leader_multicast_interval)
            if self.leader_multicast_interval <= 0:
                raise ValueError("leader_multicast_interval 必须为正数")
        self.pull_page_size = int(self.pull_page_size)
        if self.pull_page_size <= 0:
            raise ValueError("pull_page_size 必须为正数")
        self.project_tag = str(self.project_tag or "")
        self.cluster_id = str(self.cluster_id or "")
        self.multicast_group = str(self.multicast_group or "")
        self.multicast_port = int(self.multicast_port or 0)
        if not 0 <= self.multicast_port <= 65535:
            raise ValueError("multicast_port 必须在 0..65535")
        self.lsa_interval = float(self.lsa_interval)
        if self.lsa_interval <= 0:
            raise ValueError("lsa_interval 必须为正数")
        # HMAC 不是安全边界（密钥随集群公开），只用于挡住跨实例/幽灵节点的
        # 误入报文；有 cluster_id 时默认派生一个，保证 dev 与生产互不串包。
        self.hmac_key = str(self.hmac_key or (
            f"hive-control:{self.cluster_id}" if self.cluster_id else ""))

    def limits_with_multicast(self, base: "Limits | None" = None) -> "Limits":
        """把可配置的组播组/端口落进 ``Limits``，其余常量保持不变。"""
        group, port = resolve_multicast(
            self.cluster_id, self.multicast_group, self.multicast_port)
        return (base or Limits()).overridden(
            multicast_group=group, multicast_port=port)


@dataclass(frozen=True)
class Limits:
    """内部常量。测试可覆盖；不是用户配置项。"""

    # 上行
    aggregation_window: float = 0.200
    uplink_queue_capacity: int = 4096
    uplink_backoff_max: float = 10.0

    # 下行
    max_packet_bytes: int = 1400
    packet_gap: float = 0.001
    control_port: int = MULTICAST_PORT
    multicast_group: str = MULTICAST_GROUP
    multicast_port: int = MULTICAST_PORT
    fallback_after: float = 30.0

    # 选举 / 邻居
    hello_interval: float = 2.0
    heartbeat_interval: float = 2.0
    heartbeat_timeout: float = 6.0
    peer_timeout: float = 6.0

    # 状态 / DNA / 缺口
    dna_quiet_intervals: int = 3
    gap_intervals: int = 3

    # 拉取
    pull_page_size: int = 100
    pull_incremental_per_sec: int = 10
    pull_full_per_minute: int = 1
    pull_queue_capacity: int = 64
    tcp_idle_timeout: float = 30.0

    # Worker socket
    socket_path: str = field(default_factory=default_socket_path)
    incarnation_path: str = field(default_factory=default_incarnation_path)
    worker_queue_capacity: int = 4096
    notify_queue: int = 1024

    # 端口
    uplink_port_offset: int = UPLINK_PORT_OFFSET

    def overridden(self, **changes) -> "Limits":
        from dataclasses import replace

        return replace(self, **changes)