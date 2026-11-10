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
UPLINK_PORT_OFFSET = 1


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