"""端口规划守卫：默认值只允许在 hive_ports.py 定义一次。

2026-10-28 的「手动 8787、服务 8791」错位就是默认值散落造成的；集群上线后
又多了 HTTP 8788 / UDP 8789。这个测试把「唯一来源 + 段位 + 环境变量覆盖」钉死。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import hive
import hive_cluster as hc
import hive_ports as hp
from conftest import HIVE, ROOT, sh

DASH = ROOT / "dashboard"
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))


def test_specs_are_unique_and_in_plan():
    ports = [s.port for s in hp.SPECS]
    assert len(ports) == len(set(ports)), f"端口重复：{ports}"
    assert set(ports) == {8787, 8788, 8789, 8790, 8799}
    protocols = {s.key: s.protocol for s in hp.SPECS}
    assert protocols["dashboard"] == "tcp"
    assert protocols["cluster"] == "tcp"
    assert protocols["discovery"] == "udp", "发现端口必须是 UDP，别和 TCP 混"
    assert protocols["mcp"] == "tcp"
    assert protocols["smoke"] == "tcp"


def test_reserved_range_is_empty_and_documented():
    first, last = hp.RESERVED_RANGE
    assert first == 8791 and last == 8798
    allocated = {s.port for s in hp.SPECS}
    assert not (allocated & set(range(first, last + 1))), "预留段不应有已分配端口"
    rows = hp.port_plan()
    reserved = [r for r in rows if r.get("reserved")]
    assert reserved and str(first) in str(reserved[0]["port"])


def test_defaults_come_from_single_source():
    assert hive.DASH_DEFAULT_PORT == hp.DASHBOARD_PORT
    assert hive.CLUSTER_DEFAULT_PORT == hp.CLUSTER_HTTP_PORT
    assert hive.CLUSTER_DISCOVERY_DEFAULT_PORT == hp.CLUSTER_DISCOVERY_PORT
    assert hc.DEFAULT_PORT == hp.CLUSTER_HTTP_PORT
    assert hc.DEFAULT_DISCOVERY_PORT == hp.CLUSTER_DISCOVERY_PORT
    assert hc.DEFAULT_MCP_PORT == hp.MCP_HTTP_PORT
    import serve
    assert serve.DEFAULT_DASHBOARD_PORT == hp.DASHBOARD_PORT
    assert serve.CLUSTER_DEFAULT_PORT == hp.CLUSTER_HTTP_PORT


def test_env_override_and_legacy_port(monkeypatch):
    monkeypatch.setenv("HIVE_DASHBOARD_PORT", "8899")
    assert hp.effective_port("dashboard") == 8899
    monkeypatch.delenv("HIVE_DASHBOARD_PORT")
    monkeypatch.setenv("PORT", "8898")
    assert hp.effective_port("dashboard") == 8898, "看板保留 PORT 历史覆盖"
    monkeypatch.setenv("HIVE_CLUSTER_PORT", "8897")
    assert hp.effective_port("cluster") == 8897
    monkeypatch.setenv("HIVE_CLUSTER_DISCOVERY_PORT", "8896")
    assert hp.effective_port("discovery") == 8896


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HIVE_CLUSTER_PORT", "not-a-port")
    assert hp.effective_port("cluster") == hp.CLUSTER_HTTP_PORT
    monkeypatch.setenv("HIVE_CLUSTER_PORT", "70000")
    assert hp.effective_port("cluster") == hp.CLUSTER_HTTP_PORT


def test_check_ports_returns_one_row_per_service():
    rows = hp.check_ports()
    assert [r["key"] for r in rows] == [s.key for s in hp.SPECS]
    for row in rows:
        assert row["protocol"] in ("tcp", "udp")
        assert isinstance(row["free"], bool)


def test_cli_ports_json():
    env = {**dict(__import__("os").environ), "HIVE_TESTING": "1"}
    p = sh([sys.executable, HIVE, "--dir", ROOT, "ports", "--json"],
           env=env, check=False)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    plan = {row["key"]: row for row in data["plan"]}
    assert plan["dashboard"]["port"] == hp.DASHBOARD_PORT
    assert plan["cluster"]["port"] == hp.CLUSTER_HTTP_PORT
    assert plan["discovery"]["protocol"] == "udp"
    assert plan["mcp"]["port"] == hp.MCP_HTTP_PORT
    assert data["external"], "外部依赖端口表不应为空"


def test_cli_ports_check_json():
    env = {**dict(__import__("os").environ), "HIVE_TESTING": "1"}
    p = sh([sys.executable, HIVE, "--dir", ROOT, "ports", "--json", "--check"],
           env=env, check=False)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert len(data["check"]) == len(hp.SPECS)


def test_init_copies_port_module(world):
    assert (world.hive1 / "hive_ports.py").exists()
