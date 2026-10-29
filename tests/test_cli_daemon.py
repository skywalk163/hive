"""P1：hive daemon 别名默认开 MCP，--no-mcp 可显式关闭。"""
from __future__ import annotations

import sys
from argparse import Namespace

import hive
import hive_cluster


def _run(monkeypatch, argv):
    seen = {}

    def fake_run_cli(args):
        seen["mcp"] = getattr(args, "mcp", None)
        return 0

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(hive_cluster, "run_cli", fake_run_cli)
    args = Namespace(dir=".", action="status", mcp=False, no_mcp=False)
    assert hive.cmd_cluster(args) == 0
    return seen, args


def test_daemon_alias_defaults_mcp_on(monkeypatch):
    seen, args = _run(monkeypatch, ["hive.py", "daemon", "status"])
    assert seen["mcp"] is True
    assert args.mcp is True


def test_daemon_no_mcp_stays_off(monkeypatch):
    seen, args = _run(monkeypatch, ["hive.py", "daemon", "status", "--no-mcp"])
    assert seen["mcp"] is False
    assert args.mcp is False
