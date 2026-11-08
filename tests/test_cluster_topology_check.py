"""T-0288：集群拓扑体检工具的离线单元测试（标准库 unittest）。

只用内置夹具，不联网、不读真机 `~/.hive/cluster`、不依赖 pytest。
可被下列命令收集（卡面第 1 条 verify）：

    python -m unittest discover -s tests -p test_cluster_topology_check.py -v

覆盖：正常节点表无 todo、幽灵 leader、dead 节点、leader 自记录陈旧、
node id / URL 重复、节点名与机器名不一致、roster 未登记 leader、
`--self-test` 夹具与真机解耦。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_104_readiness.py"

NOW = 1_800_000_000.0


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_104_readiness", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def node(nid: str, url: str, *, last_seen: float = NOW,
         name: str | None = None, hostname: str | None = None) -> dict:
    return {
        "id": nid,
        "name": name or nid,
        "url": url,
        "host": "10.0.0.1",
        "hostname": hostname or (name or nid),
        "last_seen": iso(last_seen),
        "status": "alive",
    }


def healthy_nodes() -> list[dict]:
    return [
        node("n1", "http://10.0.0.1:8788"),
        node("n2", "http://10.0.0.2:8788"),
        node("n3", "http://10.0.0.3:8788"),
        node("n4", "http://10.0.0.4:8788"),
    ]


def health(leader_id: str = "n1", url: str = "http://10.0.0.1:8788",
           peers: int = 3, quorum: int = 2, term: int = 42) -> dict:
    return {
        "ok": True,
        "term": term,
        "role": "follower",
        "peers": peers,
        "quorum": quorum,
        "leader": {"id": leader_id, "name": leader_id, "url": url},
    }


def report(nodes=None, **kwargs) -> dict:
    state = {"term": 42, "nodes": healthy_nodes() if nodes is None else nodes}
    return mod.build_report(kwargs.pop("health", health()), state, now=NOW, **kwargs)


def todo_kinds(rep: dict) -> list[str]:
    return [t["kind"] for t in rep["todos"]]


class ReportShapeTest(unittest.TestCase):
    def test_required_fields_present(self):
        rep = report()
        for key in ("term", "role", "peers", "quorum", "leader_id", "nodes",
                    "dead_nodes", "duplicate_ids", "duplicate_urls",
                    "ghost_leader", "todos"):
            self.assertIn(key, rep)
        self.assertEqual(rep["term"], 42)
        self.assertEqual(rep["role"], "follower")
        self.assertEqual((rep["peers"], rep["quorum"]), (3, 2))

    def test_state_wrapper_and_dict_nodes(self):
        wrapped = {"state": {"term": 9, "leader_id": "n1",
                             "nodes": {n["id"]: n for n in healthy_nodes()}}}
        rep = mod.build_report({}, wrapped, now=NOW)
        self.assertEqual(rep["term"], 9)
        self.assertEqual(len(rep["nodes"]), 4)
        self.assertEqual(rep["leader_id"], "n1")


class HealthyClusterTest(unittest.TestCase):
    def test_four_healthy_nodes_have_no_todo(self):
        rep = report()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["todos"], [])
        self.assertEqual(rep["dead_nodes"], [])
        self.assertFalse(rep["ghost_leader"]["is_ghost"])


class GhostLeaderTest(unittest.TestCase):
    def test_leader_absent_from_node_table(self):
        rep = report(health=health("ghost-x", "http://10.0.0.9:8788"))
        self.assertTrue(rep["ghost_leader"]["is_ghost"])
        self.assertIn("ghost_leader", todo_kinds(rep))

    def test_leader_url_mismatch(self):
        rep = report(health=health("n1", "http://10.0.0.99:8788"))
        self.assertTrue(rep["ghost_leader"]["is_ghost"])
        self.assertFalse(rep["ghost_leader"]["url_consistent"])

    def test_leader_not_in_roster(self):
        rep = report(roster={"n2", "n3", "n4"})
        self.assertTrue(rep["ghost_leader"]["is_ghost"])
        self.assertIs(rep["ghost_leader"]["registered"], False)
        self.assertIn("ghost_leader", todo_kinds(rep))


class DeadNodeTest(unittest.TestCase):
    def test_stale_node_detected(self):
        nodes = healthy_nodes()
        nodes[1]["last_seen"] = iso(NOW - 7200)
        rep = report(nodes)
        self.assertEqual([d["id"] for d in rep["dead_nodes"]], ["n2"])
        self.assertIn("dead_node", todo_kinds(rep))

    def test_leader_stale_is_reported_separately(self):
        nodes = healthy_nodes()
        nodes[0]["last_seen"] = iso(NOW - 7200)
        rep = report(nodes)
        self.assertEqual(rep["dead_nodes"], [], "leader 不应混进 dead_nodes")
        self.assertIsNotNone(rep["leader_record_stale"])
        self.assertIn("leader_record_stale", todo_kinds(rep))

    def test_threshold_is_adjustable(self):
        nodes = healthy_nodes()
        nodes[1]["last_seen"] = iso(NOW - 120)
        self.assertEqual(report(nodes, dead_after_s=600)["dead_nodes"], [])
        self.assertEqual(len(report(nodes, dead_after_s=60)["dead_nodes"]), 1)


class DuplicateTest(unittest.TestCase):
    def test_duplicate_node_id(self):
        nodes = healthy_nodes() + [node("n1", "http://10.0.0.9:8788")]
        rep = report(nodes)
        self.assertEqual(rep["duplicate_ids"], {"n1": 2})
        self.assertIn("duplicate_node_id", todo_kinds(rep))

    def test_duplicate_node_url(self):
        nodes = healthy_nodes()
        nodes[1]["url"] = nodes[0]["url"]
        rep = report(nodes)
        self.assertIn(nodes[0]["url"], rep["duplicate_urls"])
        self.assertIn("duplicate_node_url", todo_kinds(rep))


class IdentityTest(unittest.TestCase):
    def test_node_name_differs_from_hostname(self):
        nodes = healthy_nodes()
        nodes[2]["name"] = "DESKTOP-OLD"
        nodes[2]["hostname"] = "win10"
        rep = report(nodes)
        self.assertEqual([m["id"] for m in rep["identity_mismatches"]], ["n3"])
        self.assertIn("identity_mismatch", todo_kinds(rep))

    def test_hostname_with_domain_and_case_is_not_mismatch(self):
        nodes = healthy_nodes()
        nodes[2]["name"] = "MacBook-Air"
        nodes[2]["hostname"] = "macbook-air.local"
        rep = report(nodes)
        self.assertEqual(rep["identity_mismatches"], [])


class OfflineSelfTestTest(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(mod.run_self_test(verbose=False), 0)

    def test_self_test_never_touches_network_or_real_state(self):
        """把采集函数全部换成「一碰就炸」，自检仍应通过 —— 证明夹具与真机解耦。"""
        originals = (mod.fetch_json, mod.load_state)

        def boom(*_args, **_kwargs):
            raise AssertionError("self-test 不应联网或读真实 state.json")

        mod.fetch_json = boom
        mod.load_state = boom
        try:
            self.assertEqual(mod.run_self_test(verbose=False), 0)
        finally:
            mod.fetch_json, mod.load_state = originals

    def test_cli_self_test_exit_zero(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--self-test"],
            cwd=str(ROOT), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-1000:])
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["self_test"])
        self.assertEqual(payload["failed"], 0)


if __name__ == "__main__":
    unittest.main()
