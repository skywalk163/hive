"""T-0283 验收：104 节点的 hive 集群服务是否真正就绪。

这张卡是纯运维任务，改动落在 104 本机的 ~/.hive/cluster/ 与 Windows 计划任务，
协调仓里没有代码改动。所以验收采用「从主控探测 104 + 本机集群视图」的方式，
由 land 阶段在主控上真跑，避免 agent 只靠一句「已启动」就算完成。

显式指定路径运行（文件名不以 test_ 开头，不会被日常全量 pytest 收集）：
    python3 -m pytest scripts/verify_104_readiness.py -q -o addopts=
"""

import json
import time
import urllib.request

NODE_104 = "http://192.168.0.104:8788"
LOCAL = "http://127.0.0.1:8788"
GHOST = "laiweilideMacBook"


def _get(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def test_104_health_responds_fast() -> None:
    """104 的 health 必须秒回 200。

    2026-11-07 的故障形态是「端口开着、404 秒回，但 /api/cluster/* 10 秒超时
    0 字节」—— 只检查端口或 HTTP 200 不够，必须卡响应时间。
    """
    started = time.time()
    data = _get(NODE_104 + "/api/cluster/health", timeout=3.0)
    elapsed = time.time() - started
    assert data.get("ok") is True, data
    assert elapsed < 1.0, f"health 用了 {elapsed:.2f}s，仍卡在锁或双进程上"


def test_104_state_has_no_ghost_identity() -> None:
    """104 的集群状态里不能复活旧 Mac 身份，节点表也不能有同 URL 双身份。"""
    state = _get(NODE_104 + "/api/cluster/state", timeout=3.0).get("state") or {}
    assert GHOST not in json.dumps(state, ensure_ascii=False), "幽灵 leader 身份复活"
    nodes = state.get("nodes") or []
    urls = [str(n.get("url") or "").rstrip("/") for n in nodes]
    assert len(urls) == 2 and len(set(urls)) == 2, f"节点表异常：{urls}"


def test_cluster_quorum_restored() -> None:
    """本机应恢复双节点 quorum，且 leader 是真实节点而不是幽灵。"""
    data = _get(LOCAL + "/api/cluster/health", timeout=3.0)
    assert data.get("peers") == 2, data
    assert data.get("quorum") == 2, data
    leader = str((data.get("leader") or {}).get("id") or "")
    assert leader, f"仍然无主：{data}"
    assert GHOST not in leader, f"leader 是幽灵身份：{leader}"
