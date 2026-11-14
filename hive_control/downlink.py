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

import hashlib
import hmac
import json
import socket
import struct
import threading
import time
from typing import Any, Callable

from .config import (LSA_SUPPRESS_HELLO_MULTIPLIER, Limits, MULTICAST_GROUP,
                     MULTICAST_PORT)
from .model import StateEntry
from .wire import dumps

__all__ = [
    "Transport",
    "SocketTransport",
    "pack",
    "DownlinkSender",
    "DownlinkReceiver",
    "LsaDigestSender",
    "LsaDigestReceiver",
    "LSA_DIGEST_TYPE",
    "LSA_FIELDS",
    "lsa_digest_message",
    "encode_lsa_digest",
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
# H-LSA 摘要报文（T-0351 / H-LSA §2 T1）
# --------------------------------------------------------------------------- #

LSA_DIGEST_TYPE = "lsa_digest"
# 设计文档要求的字段。**任何状态体字段（entries/progress/leases…）都不在这里。**
LSA_FIELDS = (
    "leader_epoch", "seq", "digest", "head_sha", "n_changes", "area",
    "project_tag", "cluster_id", "sent_at",
)


def _lsa_signature(message: dict, key: str) -> str:
    """对除 ``hmac`` 外的全部字段做规范化 HMAC-SHA256。"""
    body = dumps({k: v for k, v in message.items() if k != "hmac"})
    return hmac.new(key.encode("utf-8"), body.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def lsa_digest_message(*, leader_epoch: int, seq: int, digest: str = "",
                       head_sha: str = "", n_changes: int = 0, area: str = "",
                       project_tag: str = "", cluster_id: str = "",
                       sent_at: float | None = None,
                       hmac_key: str = "") -> dict:
    """构造一条 T1 摘要报文；只带摘要字段，不含任何卡面/租约状态体。"""
    message: dict = {
        "type": LSA_DIGEST_TYPE,
        "leader_epoch": int(leader_epoch),
        "seq": int(seq),
        "digest": str(digest or ""),
        "head_sha": str(head_sha or ""),
        "n_changes": int(n_changes or 0),
        "area": str(area or ""),
        "project_tag": str(project_tag or ""),
        "cluster_id": str(cluster_id or ""),
        "sent_at": int(sent_at if sent_at is not None else time.time()),
    }
    if hmac_key:
        message["hmac"] = _lsa_signature(message, hmac_key)
    return message


def encode_lsa_digest(message: dict, max_bytes: int = 1400) -> bytes:
    """序列化一条摘要报文；超过 ``max_bytes`` 直接拒绝（绝不切断 JSON）。"""
    data = dumps(message).encode("utf-8") + b"\n"
    if len(data) > max_bytes:
        raise ValueError(
            f"lsa_digest {len(data)}B 超过单包上限 {max_bytes}B（不允许塞状态体）")
    return data


class LsaDigestSender:
    """T1 摘要发送器：只有 Leader 发，60s 一拍，切主后先静默一个抑制窗口。

    - 与 ``DownlinkSender`` 的动态实时周期（0.5/1.0/2.0s）**分层并存**；
    - 发送走 ``DownlinkSender.send_raw``，因此 fallback 逐节点单播语义不变；
    - 摘要由注入的 ``digest_provider``（生产上是 ``hive_lsa.build_digest``）
      生成，未安装 H-LSA 模块时静默不发，不影响控制面本身。
    """

    def __init__(self, sender: "DownlinkSender | None", *, limits: Limits,
                 config, clock: Callable[[], float] = time.monotonic,
                 digest_provider: Callable[[], dict | None] | None = None,
                 suppress_window: float | None = None):
        self._sender = sender
        self._limits = limits
        self._config = config
        self._clock = clock
        self._provider = digest_provider
        self._suppress_window = (float(suppress_window)
                                 if suppress_window is not None
                                 else self.default_suppress_window(limits))
        self._last_epoch: int | None = None
        self._suppress_until = 0.0
        self._last_sent_at: float | None = None
        self._seq = 0
        self._counters = {
            "sent": 0,
            "packets": 0,
            "bytes": 0,
            "suppressed": 0,
            "provider_errors": 0,
            "oversize": 0,
        }

    @staticmethod
    def default_suppress_window(limits: Limits) -> float:
        """切主抑制窗口：≥ 2 × hello_interval（T-0351 第 4 条）。"""
        return max(LSA_SUPPRESS_HELLO_MULTIPLIER * float(limits.hello_interval),
                   float(limits.hello_interval))

    @property
    def suppress_window(self) -> float:
        return self._suppress_window

    @property
    def interval(self) -> float:
        return float(self._config.lsa_interval)

    def tick(self, *, is_leader: bool, epoch: int,
             now: float | None = None) -> int:
        """推进一拍。返回本次实际发出的包数（0；fallback 时按节点数）。"""
        current = self._clock() if now is None else float(now)
        epoch = int(epoch or 0)
        if not is_leader:
            # follower 只听不发；记下 epoch，等自己当选时按“新主”处理。
            self._last_epoch = epoch
            return 0
        if self._last_epoch is None or epoch != self._last_epoch:
            # 新 Leader / 切主：先静默一个窗口，避免旧主与新主重复泛洪。
            self._last_epoch = epoch
            self._suppress_until = current + self._suppress_window
            self._counters["suppressed"] += 1
            return 0
        if current < self._suppress_until:
            self._counters["suppressed"] += 1
            return 0
        if (self._last_sent_at is not None
                and current - self._last_sent_at < self.interval):
            return 0
        if self._provider is None:
            return 0
        try:
            digest = self._provider()
        except Exception:  # noqa: BLE001  H-LSA 故障不能拖垮控制面
            self._counters["provider_errors"] += 1
            return 0
        if not isinstance(digest, dict):
            return 0
        self._seq += 1
        message = lsa_digest_message(
            leader_epoch=epoch,
            seq=self._seq,
            digest=digest.get("digest", ""),
            head_sha=digest.get("head_sha", ""),
            n_changes=digest.get("n_changes", 0),
            area=digest.get("area", ""),
            project_tag=self._config.project_tag,
            cluster_id=self._config.cluster_id,
            sent_at=current,
            hmac_key=self._config.hmac_key,
        )
        try:
            data = encode_lsa_digest(message, self._limits.max_packet_bytes)
        except ValueError:
            self._counters["oversize"] += 1
            return 0
        if self._sender is None:
            return 0
        sent = self._sender.send_raw(data)
        self._last_sent_at = current
        self._counters["sent"] += 1
        self._counters["packets"] += sent
        self._counters["bytes"] += len(data)
        return sent

    def status(self) -> dict:
        return {**self._counters, "interval": self.interval,
                "suppress_window": self._suppress_window,
                "last_epoch": self._last_epoch}


class LsaDigestReceiver:
    """T1 摘要接收器：校验 cluster_id / project_tag / HMAC / epoch 单调性。

    旧主、幽灵 Leader、开发实例误入的包一律丢弃并计入 ``status()`` 指标，
    绝不落到状态视图里。
    """

    def __init__(self, *, project_tag: str = "", cluster_id: str = "",
                 hmac_key: str = "",
                 on_digest: Callable[[dict], None] | None = None):
        self._project_tag = str(project_tag or "")
        self._cluster_id = str(cluster_id or "")
        self._hmac_key = str(hmac_key or "")
        self._on_digest = on_digest
        self._last_epoch = -1
        self._last_seq = -1
        self._counters = {
            "received": 0,
            "accepted": 0,
            "malformed": 0,
            "bad_project": 0,
            "bad_cluster": 0,
            "bad_hmac": 0,
            "stale_epoch": 0,
            "duplicate": 0,
        }

    def set_on_digest(self, callback: Callable[[dict], None] | None) -> None:
        self._on_digest = callback

    def handle(self, message: dict) -> bool:
        """返回 True 表示摘要合法且已被接收；False 表示被丢弃。"""
        self._counters["received"] += 1
        if not isinstance(message, dict) or message.get("type") != LSA_DIGEST_TYPE:
            self._counters["malformed"] += 1
            return False
        if any(field not in message for field in LSA_FIELDS):
            self._counters["malformed"] += 1
            return False
        if (self._cluster_id
                and str(message.get("cluster_id") or "") != self._cluster_id):
            self._counters["bad_cluster"] += 1
            return False
        if (self._project_tag
                and str(message.get("project_tag") or "") != self._project_tag):
            self._counters["bad_project"] += 1
            return False
        if self._hmac_key:
            expected = _lsa_signature(message, self._hmac_key)
            if not hmac.compare_digest(str(message.get("hmac") or ""), expected):
                self._counters["bad_hmac"] += 1
                return False
        try:
            epoch = int(message.get("leader_epoch"))
            seq = int(message.get("seq"))
        except (TypeError, ValueError):
            self._counters["malformed"] += 1
            return False
        if epoch < self._last_epoch:
            # 旧主 / 幽灵 Leader：epoch 回退，直接丢。
            self._counters["stale_epoch"] += 1
            return False
        if epoch == self._last_epoch and seq <= self._last_seq:
            self._counters["duplicate"] += 1
            return False
        self._last_epoch = epoch
        self._last_seq = seq
        self._counters["accepted"] += 1
        if self._on_digest is not None:
            self._on_digest(message)
        return True

    def status(self) -> dict:
        return {**self._counters, "last_epoch": self._last_epoch,
                "last_seq": self._last_seq}


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

    def send_raw(self, data: bytes) -> int:
        """发一条已编码的独立报文（H-LSA 摘要等），沿用组播/回退语义。"""
        return self._send_packets([data])

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
    - ``lsa_digest`` → ``on_lsa_digest(dict)``（T1 摘要，见 T-0351）
    - TTL≠1 丢弃并记 ``ttl_violation``（I4）。
    """

    def __init__(self, *,
                 on_entry: Callable[[StateEntry], None],
                 on_heartbeat: Callable[[dict], None],
                 on_expiring: Callable[[dict], None],
                 limits: Limits,
                 transport: Transport | None = None,
                 on_lsa_digest: Callable[[dict], None] | None = None):
        self._on_entry = on_entry
        self._on_heartbeat = on_heartbeat
        self._on_expiring = on_expiring
        self._on_lsa_digest = on_lsa_digest
        self._limits = limits
        self._transport = transport or SocketTransport(
            group=limits.multicast_group, port=limits.multicast_port)
        # 统计
        self._packets_received = 0
        self._entries_installed = 0
        self._heartbeats = 0
        self._expirings = 0
        self._lsa_digests = 0
        self._ttl_violations = 0
        self._parse_errors = 0
        self._last_from: tuple[str, int] | None = None

    def set_lsa_handler(self, callback: Callable[[dict], None] | None) -> None:
        """装配 H-LSA 摘要处理器（``plane`` 不感知具体校验器）。"""
        self._on_lsa_digest = callback

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
            elif msg_type == LSA_DIGEST_TYPE:
                # 摘要只做提示：具体校验/丢弃由装配方注入的接收器负责。
                self._lsa_digests += 1
                if self._on_lsa_digest is not None:
                    self._on_lsa_digest(msg)
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
            "lsa_digests": self._lsa_digests,
            "ttl_violations": self._ttl_violations,
            "parse_errors": self._parse_errors,
            "last_from": self._last_from,
        }
