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

import threading
import uuid
from urllib.parse import urlparse

from .config import Limits

PULL_MODES = ("incremental", "full")


class ClusterControlAdapter:
    """控制面适配层。一个 ``Cluster`` 实例对应一个适配器。"""

    def __init__(self, cluster, *, limits=None, plane_factory=None):
        self.cluster = cluster
        self.limits = limits or Limits()
        self._plane_factory = plane_factory
        self._plane = None
        self._plane_available = False
        self._started = False
        self._lock = threading.RLock()
        self._pulls: dict[str, dict] = {}
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

    def _init_plane(self):
        """装配编排器；T-0310 未落地时返回 None（降级，不抛异常）。"""
        if self._plane_factory is not None:
            try:
                return self._plane_factory(self)
            except Exception:  # noqa: BLE001  装配失败按降级处理
                return None
        try:
            from .plane import ControlPlane  # type: ignore  # T-0310 提供
        except ImportError:
            return None
        try:
            return ControlPlane(
                peer_provider=self.peer_provider,
                local_agents=self.local_agents,
                limits=self.limits,
            )
        except Exception:  # noqa: BLE001
            return None

    # -- 生命周期 ---------------------------------------------------------- #
    def start(self) -> None:
        """幂等启动。不打开端口，只装配组件；真正的端口由编排器持有。"""
        with self._lock:
            if self._started:
                return
            self._plane = self._init_plane()
            self._plane_available = self._plane is not None
            if self._plane is not None:
                starter = getattr(self._plane, "start", None)
                if callable(starter):
                    starter()
            self._started = True

    def stop(self) -> None:
        """幂等停止。先停编排器，再把自己标记为未运行。"""
        with self._lock:
            if not self._started:
                return
            if self._plane is not None:
                stopper = getattr(self._plane, "stop", None)
                if callable(stopper):
                    try:
                        stopper()
                    except Exception:  # noqa: BLE001  停止路径尽力而为
                        pass
            self._started = False

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
                "counters": counters,
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
                        answer = handler(body)
                        if isinstance(answer, dict):
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
                                      if k != "result"}}
            if row.get("result") is not None:
                payload["result"] = row["result"]
            code = 200 if row.get("state") in ("done", "failed", "completed") else 202
            return code, payload
