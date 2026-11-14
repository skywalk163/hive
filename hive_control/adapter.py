"""控制面 ↔ 集群服务适配器（实现约定 §3.10 / T-0311）。

职责边界：把控制面挂到 ``hive cluster serve`` 的生命周期上，并向集群的
HTTP 面暴露状态与人工拉取端点。本模块 **不 import hive_cluster**：``cluster``
是 duck-typed 的，单测可以用假对象，避免为了测一个适配器去起整个集群。

D14：是否装配由调用方（``Cluster`` / ``hive cluster serve``）决定，本模块
自己不打开任何端口、不启动任何线程。这样程序化构造的 50+ 个既有 Cluster
测试实例不会因为抢 47899 组播端口而互相干扰。

依赖降级：编排器 ``hive_control.plane``（T-0310）尚未落地时，适配器仍可
工作，但只维护本地的拉取请求状态，并在 ``status()`` 里如实报告
``plane_available=False`` / ``degraded=True``。不伪造不存在的接口或数据。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from urllib.parse import urlparse

from .config import ControlConfig, Limits
from .downlink import LsaDigestReceiver, LsaDigestSender

PULL_MODES = ("incremental", "full")

logger = logging.getLogger(__name__)


class ClusterControlAdapter:
    """控制面适配层。一个 ``Cluster`` 实例对应一个适配器。"""

    def __init__(self, cluster, *, limits=None, plane_factory=None,
                 config=None, transport=None, clock=None, start_threads=True,
                 lsa_provider=None, lsa_auto_tick=True):
        self.cluster = cluster
        self.limits = limits or Limits()
        self._plane_factory = plane_factory
        self._config = config
        self._transport = transport
        self._clock = clock or time.monotonic
        self._start_threads = bool(start_threads)
        self._lsa_provider = lsa_provider
        self._lsa_auto_tick = bool(lsa_auto_tick)
        self._effective_limits = self.limits
        self._plane = None
        self._plane_available = False
        self._started = False
        self._lock = threading.RLock()
        self._pulls: dict[str, dict] = {}
        self._lsa_sender: LsaDigestSender | None = None
        self._lsa_receiver: LsaDigestReceiver | None = None
        self._lsa_stop: threading.Event | None = None
        self._lsa_thread: threading.Thread | None = None
        self._error = ""
        self._counters = {
            "pull_requests": 0,
            "pull_completed": 0,
            "pull_failed": 0,
        }

    # -- 供编排器注入的依赖 ------------------------------------------------ #
    @property
    def peer_provider(self):
        """从 ``cluster.nodes`` 取每个节点的 url host + 控制端口。

        集群节点的 ``url`` 形如 ``http://192.168.0.88:8788``；控制面用的是
        另一个端口（默认 47899），所以只取 host，端口换成控制端口。
        """
        nodes = getattr(self.cluster, "nodes", None) or {}
        peers = []
        seen = set()
        for node in (nodes.values() if isinstance(nodes, dict) else list(nodes)):
            if not isinstance(node, dict):
                continue
            raw = str(node.get("url") or "")
            if not raw:
                continue
            parsed = urlparse(raw if "://" in raw else "http://" + raw)
            host = parsed.hostname or ""
            if not host or host in seen:
                continue
            seen.add(host)
            peers.append({"host": host, "port": int(self.limits.control_port),
                          "node_id": str(node.get("id") or node.get("node_id") or "")})
        return peers

    @property
    def local_agents(self):
        """本机登记的 agent，交给编排器做定向通知。"""
        getter = getattr(self.cluster, "_local_agents", None)
        if not callable(getter):
            return []
        try:
            rows = getter()
        except Exception:  # noqa: BLE001  假对象/仓内配置坏了都不能拖垮适配器
            return []
        return [r for r in (rows or []) if isinstance(r, dict)]

    def _cluster_config(self):
        return getattr(self.cluster, "config", None)

    def control_config(self) -> ControlConfig:
        """从 ``cluster serve`` 的既有配置推导控制面配置（不新增 CLI 参数）。"""
        if self._config is not None:
            return self._config
        cfg = self._cluster_config()
        cluster_id = str(getattr(cfg, "cluster_id", "") or "")
        project_tag = (os.environ.get("HIVE_CONTROL_PROJECT_TAG")
                       or cluster_id)
        self._config = ControlConfig(cluster_id=cluster_id,
                                     project_tag=project_tag)
        return self._config

    def plane_identity(self) -> tuple[str, str, int]:
        """``(node, address, listen_port)``：来自 --name / --advertise / --port。"""
        cfg = self._cluster_config()
        node = str(getattr(cfg, "name", "")
                   or getattr(self.cluster, "node_id", "") or "node")
        address = str(getattr(self.cluster, "advertise_host", "")
                      or getattr(cfg, "advertise", "") or "0.0.0.0")
        try:
            listen_port = int(getattr(cfg, "port", 0) or 0)
        except (TypeError, ValueError):
            listen_port = 0
        return node, address, listen_port

    def _digest_provider(self):
        """H-LSA 摘要来源：优先注入；否则尝试 T-0350 的 ``hive_lsa``。"""
        if self._lsa_provider is not None:
            return self._lsa_provider
        try:
            import hive_lsa  # type: ignore[import-not-found]
        except ImportError:
            return None

        def build() -> dict | None:
            tick = getattr(hive_lsa, "tick", None)
            if callable(tick):
                tick()
            builder = getattr(hive_lsa, "build_digest", None)
            if not callable(builder):
                return None
            value = builder()
            return value if isinstance(value, dict) else None

        return build

    def _wire_lsa(self, plane, cfg: ControlConfig) -> None:
        """把 T1 摘要发送/接收接到编排器已有的收发组件上。"""
        self._effective_limits = cfg.limits_with_multicast(self.limits)
        receiver = LsaDigestReceiver(
            project_tag=cfg.project_tag, cluster_id=cfg.cluster_id,
            hmac_key=cfg.hmac_key)
        self._lsa_receiver = receiver
        downlink_rx = getattr(plane, "downlink_receiver", None)
        setter = getattr(downlink_rx, "set_lsa_handler", None)
        if callable(setter):
            setter(receiver.handle)
        self._lsa_sender = LsaDigestSender(
            getattr(plane, "downlink_sender", None),
            limits=self._effective_limits, config=cfg, clock=self._clock,
            digest_provider=self._digest_provider())

    def _init_plane(self):
        """装配编排器；装配失败返回 None（降级，不抛异常）。"""
        if self._plane_factory is not None:
            try:
                return self._plane_factory(self)
            except Exception:  # noqa: BLE001  装配失败按降级处理
                logger.exception("control: plane_factory 失败，控制面降级")
                return None
        try:
            from .plane import ControlPlane  # type: ignore  # T-0310 提供
        except ImportError:
            return None
        try:
            cfg = self.control_config()
            cluster_cfg = self._cluster_config()
            # 没有 --name/--port 的 duck-typed 对象无法构成可寻址的编排器，
            # 如实降级（既有测试替身正是这种形态），不伪造身份。
            if (cluster_cfg is None or not getattr(cluster_cfg, "name", None)
                    or not hasattr(cluster_cfg, "port")):
                logger.info("control: cluster 缺少 name/port，控制面降级")
                return None
            node, address, listen_port = self.plane_identity()
            self._effective_limits = cfg.limits_with_multicast(self.limits)
            plane = ControlPlane(
                node=node,
                address=address,
                listen_port=listen_port,
                config=cfg,
                limits=self._effective_limits,
                peer_provider=self.peer_provider,
                local_agents=self.local_agents,
                transport=self._transport,
                clock=self._clock,
                start_threads=self._start_threads,
            )
            self._wire_lsa(plane, cfg)
            return plane
        except Exception:  # noqa: BLE001  控制面是可选件，失败只降级
            logger.exception("control: ControlPlane 装配失败，控制面降级")
            return None

    # -- 生命周期 ---------------------------------------------------------- #
    @property
    def plane(self):
        """已装配的编排器（未装配/降级时为 None）。"""
        return self._plane

    @property
    def plane_available(self) -> bool:
        return self._plane_available

    def start(self) -> None:
        """幂等启动。不打开端口，只装配组件；真正的端口由编排器持有。"""
        with self._lock:
            if self._started:
                return
            self._error = ""
            plane = self._init_plane()
            if plane is not None:
                starter = getattr(plane, "start", None)
                if callable(starter):
                    try:
                        starter()
                    except Exception as exc:  # noqa: BLE001
                        # 约定：控制面任何失败都不能拖垮集群启动。
                        logger.exception("control: 编排器启动失败，控制面降级")
                        self._error = f"{type(exc).__name__}: {exc}"
                        plane = None
            self._plane = plane
            self._plane_available = plane is not None
            self._started = True
        if plane is not None:
            self._start_lsa_loop()

    def stop(self) -> None:
        """幂等停止。先停 LSA 线程，再停编排器。"""
        self._stop_lsa_loop()
        with self._lock:
            if not self._started:
                return
            if self._plane is not None:
                stopper = getattr(self._plane, "stop", None)
                if callable(stopper):
                    try:
                        stopper()
                    except Exception:  # noqa: BLE001  停止路径尽力而为
                        logger.exception("control: 编排器停止失败")
            self._started = False

    # -- H-LSA T1 摘要 ------------------------------------------------------ #
    def tick_lsa(self) -> int:
        """推进一拍 T1 摘要；返回发出的包数（测试可直接调用）。"""
        if self._plane is None or self._lsa_sender is None:
            return 0
        snapshot = self._plane_status()
        return self._lsa_sender.tick(
            is_leader=bool(snapshot.get("is_leader")),
            epoch=int(snapshot.get("control_epoch") or 0))

    def _start_lsa_loop(self) -> None:
        if not self._lsa_auto_tick or self._lsa_sender is None:
            return
        self._lsa_stop = threading.Event()
        self._lsa_thread = threading.Thread(
            target=self._lsa_loop, name="control-lsa", daemon=True)
        self._lsa_thread.start()

    def _stop_lsa_loop(self) -> None:
        stop, thread = self._lsa_stop, self._lsa_thread
        self._lsa_stop = None
        self._lsa_thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _lsa_loop(self) -> None:
        stop = self._lsa_stop
        while stop is not None and not stop.wait(1.0):
            try:
                self.tick_lsa()
            except Exception:  # noqa: BLE001  后台线程必须活着
                logger.exception("control-lsa 轮次失败")

    # -- 状态 -------------------------------------------------------------- #
    def _plane_status(self) -> dict:
        if self._plane is None:
            return {}
        getter = getattr(self._plane, "status", None)
        if not callable(getter):
            return {}
        try:
            value = getter()
        except Exception:  # noqa: BLE001
            return {}
        return value if isinstance(value, dict) else {}

    def status(self) -> dict:
        """``GET /api/cluster/status`` 的 ``control`` 段。"""
        with self._lock:
            plane = self._plane_status()
            queues = plane.get("queues") if isinstance(plane.get("queues"), dict) else {}
            counters = dict(self._counters)
            if isinstance(plane.get("counters"), dict):
                counters.update(plane["counters"])
            lsa_tx = self._lsa_sender.status() if self._lsa_sender else {}
            lsa_rx = self._lsa_receiver.status() if self._lsa_receiver else {}
            # 旧 epoch / 跨实例报文丢弃计数也要能在 /api/cluster/status 里看到。
            if lsa_rx:
                counters.update({f"lsa_{key}": int(value) for key, value
                                 in lsa_rx.items()
                                 if isinstance(value, int)})
            return {
                "enabled": True,
                "running": self._started,
                "plane_available": self._plane_available,
                "degraded": not self._plane_available,
                "multicast_mode": plane.get("multicast_mode")
                or ("multicast" if self._plane_available else "off"),
                "control_leader": plane.get("control_leader"),
                "control_epoch": int(plane.get("control_epoch") or 0),
                "control_port": int(self.limits.control_port),
                "peers": self.peer_provider,
                "local_agents": self.local_agents,
                "queues": {
                    "pull": int(queues.get("pull", self._pending_count())),
                    "uplink": int(queues.get("uplink", 0)),
                    "downlink": int(queues.get("downlink", 0)),
                    "notify": int(queues.get("notify", 0)),
                },
                "lsa": {"tx": lsa_tx, "rx": lsa_rx},
                "counters": counters,
                "error": self._error or None,
            }

    def _pending_count(self) -> int:
        return sum(1 for row in self._pulls.values()
                   if row.get("state") in ("pending", "running"))

    # -- 人工拉取 ---------------------------------------------------------- #
    def request_pull(self, body: dict) -> tuple[int, dict]:
        """``POST /api/cluster/pull`` -> 202 + request_id。"""
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "请求体必须是对象"}
        mode = str(body.get("mode") or "incremental").strip().lower()
        if mode not in PULL_MODES:
            return 400, {"ok": False,
                         "error": f"mode 只能是 {'/'.join(PULL_MODES)}：{mode}"}
        project = str(body.get("project") or "").strip()
        request_id = uuid.uuid4().hex
        with self._lock:
            row = {
                "request_id": request_id,
                "project": project,
                "mode": mode,
                "state": "pending",
                "result": None,
            }
            self._pulls[request_id] = row
            self._counters["pull_requests"] += 1
            delegated = False
            if self._plane is not None:
                handler = getattr(self._plane, "request_pull", None)
                if callable(handler):
                    try:
                        # 编排器既有签名是 (project, mode) -> request_id；
                        # 注入的假编排器仍可能接受旧的 body 字典。
                        try:
                            answer = handler(project, mode)
                        except TypeError:
                            answer = handler(body)
                        if isinstance(answer, str) and answer:
                            row["plane_request_id"] = answer
                            row["state"] = "pending"
                            delegated = True
                        elif isinstance(answer, dict):
                            row.update({k: v for k, v in answer.items()
                                        if k not in ("request_id", "ok")})
                            delegated = True
                    except Exception as exc:  # noqa: BLE001  不把编排器异常透给 HTTP
                        row["state"] = "failed"
                        row["error"] = f"{type(exc).__name__}: {exc}"
                        self._counters["pull_failed"] += 1
            payload = {
                "ok": True,
                "request_id": request_id,
                "state": row["state"],
                "mode": mode,
                "project": project,
                "delegated": delegated,
                "degraded": not self._plane_available,
            }
            if row.get("error"):
                payload["ok"] = False
                payload["error"] = row["error"]
                return 500, payload
        return 202, payload

    def pull_status(self, request_id: str) -> tuple[int, dict]:
        """``GET /api/cluster/pull/status?id=...`` -> 结果 / 进行中 / 404。"""
        rid = str(request_id or "").strip()
        if not rid:
            return 400, {"ok": False, "error": "缺少 id"}
        with self._lock:
            row = self._pulls.get(rid)
            if row is None:
                return 404, {"ok": False, "error": f"未知 request_id {rid}"}
            payload = {"ok": True, **{k: v for k, v in row.items()
                                      if k not in ("result", "plane_request_id")}}
            if row.get("result") is not None:
                payload["result"] = row["result"]
            code = 200 if row.get("state") in ("done", "failed", "completed") else 202
            return code, payload
