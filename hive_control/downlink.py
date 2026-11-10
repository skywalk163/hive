"""控制面下行：组播/单播回退发送、TTL/LOOP、1400B 分包、接收与 TTL 校验。

对应母文档 §5.2 / §5.3 / §8 与实现约定 §2.5 / §3.6。

- 组播地址硬编码 ``239.255.42.1:47899``；发送 ``IP_MULTICAST_TTL=1``、
  ``IP_MULTICAST_LOOP=0``。
- 周期包：待下行队列里的条目行；按 1400 字节上限分包，包间 1ms。
- 事件包：只装触发它的那一条（``claimed``/``releasing``/``urgent=true``）。
- 心跳是单独一个包，不与其他条目合并。
- 接收端 ``SO_REUSEADDR`` + ``IP_ADD_MEMBERSHIP``；校验 TTL≠1 丢弃并记
  ``ttl_violation``。回退模式下同一 socket 也接收单播 UDP。
- 回退（D9）：``IP_ADD_MEMBERSHIP`` 失败或 30s 无下行且上行存活时，
  ``multicast_mode="fallback"``，发送改逐节点单播；恢复不自动回切。

``Transport`` 是测试缝：测试里用 FakeTransport 记录发送，不碰真实组播。
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from typing import Any, Callable

from .config import MULTICAST_GROUP, MULTICAST_PORT, Limits
from .model import StateEntry
from .wire import dumps

__all__ = [
    "Transport",
    "SocketTransport",
    "pack",
    "DownlinkSender",
    "DownlinkReceiver",
]


# --------------------------------------------------------------------------- #
# 打包
# --------------------------------------------------------------------------- #

def pack(entries: list[StateEntry], max_bytes: int = 1400
         ) -> tuple[list[bytes], int]:
    """把条目行按 ``max_bytes`` 切包。

    返回 ``(packets, oversize_count)``。单条序列化后超限的条目独占一包，
    绝不把 JSON 行切断（D8），并计入 ``oversize_count``。
    """
    packets: list[bytes] = []
    oversize = 0
    current = bytearray()
    for entry in entries:
        line = entry.to_line().encode("utf-8") + b"\n"
        if len(line) > max_bytes:
            # 先把攒了一半的包 flush 掉
            if current:
                packets.append(bytes(current))
                current = bytearray()
            packets.append(line)
            oversize += 1
            continue
        if current and len(current) + len(line) > max_bytes:
            packets.append(bytes(current))
            current = bytearray()
        current += line
    if current:
        packets.append(bytes(current))
    return packets, oversize


# --------------------------------------------------------------------------- #
# Transport（测试缝 + 默认 UDP 实现）
# --------------------------------------------------------------------------- #

class Transport:
    """传输层抽象（duck-typed）。测试里用 FakeTransport 代替。"""

    def send_multicast(self, data: bytes) -> None: ...

    def send_unicast(self, addr: tuple[str, int], data: bytes) -> None: ...

    def start_receiver(self, callback: Callable[[bytes, tuple[str, int], int], None]) -> None: ...

    def stop(self) -> None: ...


class SocketTransport(Transport):
    """默认 UDP 组播/单播实现。

    - 发送 socket：``IP_MULTICAST_TTL=1``、``IP_MULTICAST_LOOP=0``。
    - 接收 socket：``SO_REUSEADDR`` + ``IP_ADD_MEMBERSHIP``；
      用 ``IP_RECVTTL``（Linux）/ ``IP_RECVHOPLIMIT`` 取 TTL 校验。
      Windows 不支持 IP_RECVTTL，TTL 校验退回「恒通过」并记
      ``ttl_check_unavailable``（测试不依赖真实 socket）。
    """

    def __init__(self, group: str = MULTICAST_GROUP, port: int = MULTICAST_PORT):
        self.group = group
        self.port = port
        self._send_sock: socket.socket | None = None
        self._recv_sock: socket.socket | None = None
        self._recv_thread: threading.Thread | None = None
        self._running = False
        self._membership_ok = True

    # -- 发送 ---------------------------------------------------------- #
    def _send_socket(self) -> socket.socket:
        if self._send_sock is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
            self._send_sock = s
        return self._send_sock

    def send_multicast(self, data: bytes) -> None:
        self._send_socket().sendto(data, (self.group, self.port))

    def send_unicast(self, addr: tuple[str, int], data: bytes) -> None:
        self._send_socket().sendto(data, addr)

    # -- 接收 ---------------------------------------------------------- #
    def start_receiver(self,
                       callback: Callable[[bytes, tuple[str, int], int], None]
                       ) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except (OSError, AttributeError):
            pass
        s.bind(("", self.port))
        # 加入组播组；失败标记 membership_ok=False（触发回退 D9）
        try:
            mreq = struct.pack("=4sl", socket.inet_aton(self.group),
                               socket.INADDR_ANY)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            self._membership_ok = False
        self._recv_sock = s
        self._running = True
        self._recv_thread = threading.Thread(
            target=self._recv_loop, args=(s, callback), daemon=True)
        self._recv_thread.start()

    def _recv_loop(self, s: socket.socket,
                   callback: Callable[[bytes, tuple[str, int], int], None]) -> None:
        s.settimeout(1.0)
        while self._running:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            ttl = 1  # 无法取 cmsg 时默认 TTL=1（恒通过）
            callback(data, addr, ttl)

    def stop(self) -> None:
        self._running = False
        for s in (self._send_sock, self._recv_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._send_sock = None
        self._recv_sock = None


# --------------------------------------------------------------------------- #
# 发送器
# --------------------------------------------------------------------------- #

class DownlinkSender:
    """Leader 侧下行发送器。

    - ``send_periodic``：drain 队列 → pack → 组播（或回退逐节点单播）。
    - ``send_event``：单条事件包（只装触发它的那一条）。
    - ``send_heartbeat``：心跳单独一包，不与条目合并。
    - ``interval(active)``：按 D7 动态算周期。
    """

    def __init__(self, queue, *, limits: Limits,
                 transport: Transport | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._queue = queue
        self._limits = limits
        self._transport = transport or SocketTransport(
            group=limits.multicast_group, port=limits.multicast_port)
        self._clock = clock
        self._peers: list[tuple[str, int]] = []
        self._fallback = False
        self._multicast_mode = "multicast"  # "multicast" | "fallback"
        # 统计
        self._packets_sent = 0
        self._bytes_sent = 0
        self._periodic_cycles = 0
        self._events_sent = 0
        self._heartbeats_sent = 0
        self._oversize = 0
        self._fallback_sends = 0
        self._last_downlink_at: float | None = None

    # -- 配置 ---------------------------------------------------------- #
    def set_peers(self, peers: list[tuple[str, int]]) -> None:
        self._peers = list(peers)

    def set_fallback(self, enabled: bool) -> None:
        """手动切换回退模式（D9 也可由 transport 自动触发）。"""
        self._fallback = bool(enabled)
        if self._fallback:
            self._multicast_mode = "fallback"

    @property
    def multicast_mode(self) -> str:
        return self._multicast_mode

    # -- 周期 ---------------------------------------------------------- #
    def interval(self, active_entries: int) -> float:
        """D7：>2000→0.5s，>500→1.0s，否则 2.0s。"""
        return _dynamic_interval(active_entries)

    def send_periodic(self) -> int:
        """drain 队列、打包、发送。返回发送的包数。"""
        entries = self._queue.drain()
        if not entries:
            return 0
        packets, oversize = pack(entries, self._limits.max_packet_bytes)
        self._oversize += oversize
        n = self._send_packets(packets)
        self._periodic_cycles += 1
        return n

    def send_event(self, entry: StateEntry) -> int:
        """事件包：只装触发它的那一条。"""
        packets, oversize = pack([entry], self._limits.max_packet_bytes)
        self._oversize += oversize
        n = self._send_packets(packets)
        self._events_sent += 1
        return n

    def send_heartbeat(self, payload: dict) -> None:
        """心跳单独一包，不与条目合并。"""
        data = dumps(payload).encode("utf-8") + b"\n"
        packets = [data]
        self._send_packets(packets)
        self._heartbeats_sent += 1

    # -- 内部 ---------------------------------------------------------- #
    def _send_packets(self, packets: list[bytes]) -> int:
        n = 0
        for pkt in packets:
            if self._multicast_mode == "fallback":
                for peer in self._peers:
                    self._transport.send_unicast(peer, pkt)
                    self._fallback_sends += 1
                    n += 1
            else:
                self._transport.send_multicast(pkt)
                n += 1
            self._packets_sent += 1
            self._bytes_sent += len(pkt)
            # packet_gap 只在真实线程里 sleep；单测直接调方法不 sleep
        self._last_downlink_at = self._clock()
        return n

    def status(self) -> dict:
        return {
            "multicast_mode": self._multicast_mode,
            "packets_sent": self._packets_sent,
            "bytes_sent": self._bytes_sent,
            "periodic_cycles": self._periodic_cycles,
            "events_sent": self._events_sent,
            "heartbeats_sent": self._heartbeats_sent,
            "oversize_packets": self._oversize,
            "fallback_sends": self._fallback_sends,
            "peers": len(self._peers),
        }


def _dynamic_interval(active: int) -> float:
    """D7：>2000→0.5s，>500→1.0s，否则 2.0s。"""
    if active > 2000:
        return 0.5
    if active > 500:
        return 1.0
    return 2.0


# --------------------------------------------------------------------------- #
# 接收器
# --------------------------------------------------------------------------- #

class DownlinkReceiver:
    """非 Leader 节点侧：收下行 UDP、按 type 分发。

    - ``entry`` → ``on_entry(StateEntry)``
    - ``heartbeat`` → ``on_heartbeat(dict)``
    - ``expiring`` → ``on_expiring(dict)``
    - TTL≠1 丢弃并记 ``ttl_violation``（I4）。
    """

    def __init__(self, *,
                 on_entry: Callable[[StateEntry], None],
                 on_heartbeat: Callable[[dict], None],
                 on_expiring: Callable[[dict], None],
                 limits: Limits,
                 transport: Transport | None = None):
        self._on_entry = on_entry
        self._on_heartbeat = on_heartbeat
        self._on_expiring = on_expiring
        self._limits = limits
        self._transport = transport or SocketTransport(
            group=limits.multicast_group, port=limits.multicast_port)
        # 统计
        self._packets_received = 0
        self._entries_installed = 0
        self._heartbeats = 0
        self._expirings = 0
        self._ttl_violations = 0
        self._parse_errors = 0
        self._last_from: tuple[str, int] | None = None

    def start(self) -> None:
        self._transport.start_receiver(self._on_packet)

    def stop(self) -> None:
        self._transport.stop()

    def _on_packet(self, data: bytes, addr: tuple[str, int], ttl: int) -> None:
        self._packets_received += 1
        self._last_from = addr
        # I4：TTL≠1 丢弃
        if ttl != 1:
            self._ttl_violations += 1
            return
        # 按行解析
        for raw in data.split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._parse_errors += 1
                continue
            if not isinstance(msg, dict):
                self._parse_errors += 1
                continue
            # 分发：有显式 "type" 按 type 分发；裸条目（带 project/tid）按 entry
            msg_type = msg.get("type")
            if msg_type == "heartbeat":
                self._on_heartbeat(msg)
                self._heartbeats += 1
            elif msg_type == "expiring":
                self._on_expiring(msg)
                self._expirings += 1
            elif msg_type == "entry" or msg_type is None:
                # 包裹条目 {"type":"entry","entry":{...}} 或裸条目
                body = msg.get("entry") if msg_type == "entry" else msg
                if not isinstance(body, dict):
                    self._parse_errors += 1
                    continue
                try:
                    entry = StateEntry.from_dict(body)
                except ValueError:
                    self._parse_errors += 1
                    continue
                self._on_entry(entry)
                self._entries_installed += 1
            else:
                # 未知 type 静默丢弃（容错）
                pass

    def status(self) -> dict:
        return {
            "packets_received": self._packets_received,
            "entries_installed": self._entries_installed,
            "heartbeats": self._heartbeats,
            "expirings": self._expirings,
            "ttl_violations": self._ttl_violations,
            "parse_errors": self._parse_errors,
            "last_from": self._last_from,
        }
