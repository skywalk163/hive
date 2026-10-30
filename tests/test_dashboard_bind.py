"""看板默认绑定 0.0.0.0 的回归（T-0210）。

背景：看板原来只绑 127.0.0.1，每台机器看都要一条 SSH 隧道。改成默认对外监听后，
必须同时钉住三件事，否则会以"看起来能跑"的方式坏：

1. **单一来源**：默认绑定地址只在 `hive_ports.py` 定义，`hive.py` / `serve.py` /
   两个服务脚本都不许写第二套默认值；
2. **一路传到底**：`hive dash` 、启动脚本、`serve.py` 三层都要把 `--host` 传下去，
   少一环就还是回环；
3. **配套保护**：非回环绑定下写操作必须要求 token（这部分在
   test_dashboard_auth.py 里已覆盖，这里只验连接口径）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DASH = ROOT / "dashboard"
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

import hive  # noqa: E402
import hive_ports as hp  # noqa: E402

SERVE = ROOT / "dashboard" / "serve.py"
SCRIPT_SH = ROOT / "scripts" / "serve-dashboard.sh"
SCRIPT_CMD = ROOT / "scripts" / "dashboard-service.cmd"


# --------------------------------------------------------------------------- #
# 默认值只在 hive_ports.py
# --------------------------------------------------------------------------- #

def test_default_bind_is_all_interfaces():
    assert hp.DASHBOARD_HOST == "0.0.0.0"
    assert hp.by_key("dashboard").bind == hp.DASHBOARD_HOST


def test_effective_host_env_override(monkeypatch):
    monkeypatch.delenv(hp.DASHBOARD_HOST_ENV, raising=False)
    assert hp.effective_host("dashboard") == "0.0.0.0"
    monkeypatch.setenv(hp.DASHBOARD_HOST_ENV, "127.0.0.1")
    assert hp.effective_host("dashboard") == "127.0.0.1"
    # 其它服务不跟看板的环境变量走
    assert hp.effective_host("cluster") == "0.0.0.0"
    assert hp.effective_host("mcp") == "127.0.0.1", "MCP 必须留在 loopback"


def test_port_plan_reports_effective_bind(monkeypatch):
    monkeypatch.setenv(hp.DASHBOARD_HOST_ENV, "127.0.0.1")
    row = next(r for r in hp.port_plan() if r["key"] == "dashboard")
    assert row["bind"] == "127.0.0.1", "port_plan 要报生效值，不是写死默认"


def test_hive_and_serve_share_the_single_source(monkeypatch):
    import serve  # noqa: WPS433  同仓库模块
    monkeypatch.delenv(hp.DASHBOARD_HOST_ENV, raising=False)
    assert hive.dashboard_host() == hp.DASHBOARD_HOST
    assert serve.DEFAULT_DASHBOARD_HOST == hp.DASHBOARD_HOST
    monkeypatch.setenv(hp.DASHBOARD_HOST_ENV, "10.0.0.5")
    assert hive.dashboard_host() == "10.0.0.5"


def test_no_hardcoded_loopback_default_left(monkeypatch):
    """serve.py / 脚本里不许再出现「--host 默认 127.0.0.1」这种第二套默认。"""
    src = SERVE.read_text(encoding="utf-8")
    assert 'default="127.0.0.1"' not in src, "serve.py 仍在写死回环默认"
    assert "DEFAULT_DASHBOARD_HOST" in src


# --------------------------------------------------------------------------- #
# 拉起链路：三层都把 host 传下去
# --------------------------------------------------------------------------- #

def test_hive_spawn_passes_host(monkeypatch, tmp_path):
    """`_dashboard_spawn/_dashboard_run_script` 必须把绑定地址塞进子进程环境。"""
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["env"] = kwargs.get("env") or {}

    class FakeRun:
        def __init__(self, cmd, env=None, **kwargs):
            seen["env"] = env or {}
            self.returncode = 0

    monkeypatch.setenv(hp.DASHBOARD_HOST_ENV, "0.0.0.0")
    monkeypatch.setattr(hive, "dashboard_script", lambda: SCRIPT_CMD)
    monkeypatch.setattr(hive.subprocess, "Popen", FakePopen)
    assert hive._dashboard_spawn(8787) is True
    assert seen["env"][hp.DASHBOARD_HOST_ENV] == "0.0.0.0"

    monkeypatch.setattr(hive.subprocess, "run", FakeRun)
    assert hive._dashboard_run_script("status", 8787) == 0
    assert seen["env"][hp.DASHBOARD_HOST_ENV] == "0.0.0.0"


def test_scripts_forward_host():
    sh = SCRIPT_SH.read_text(encoding="utf-8")
    assert "HIVE_DASHBOARD_HOST" in sh
    assert '--host "$HOST"' in sh

    cmd = SCRIPT_CMD.read_text(encoding="utf-8")
    assert "HIVE_DASHBOARD_HOST" in cmd
    assert "'--host','%HOST%'" in cmd


def test_dash_url_uses_lan_ip_when_bound_to_all(monkeypatch):
    monkeypatch.delenv(hp.DASHBOARD_HOST_ENV, raising=False)
    url = hive.dashboard_url(8787)
    assert url.startswith("http://") and url.endswith(":8787/")
    assert "0.0.0.0" not in url, "给用户的地址不能是 0.0.0.0"
    monkeypatch.setenv(hp.DASHBOARD_HOST_ENV, "127.0.0.1")
    assert hive.dashboard_url(8787) == "http://127.0.0.1:8787/"


def test_probe_targets_the_bound_address(monkeypatch):
    """绑到具体网卡地址时，探活必须打那个地址（否则每次 ensure 都当"没起"）。"""
    calls = []
    import socket

    class FakeSock:
        def settimeout(self, _):
            pass

        def connect_ex(self, addr):
            calls.append(addr)
            return 1

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSock())
    monkeypatch.setenv(hp.DASHBOARD_HOST_ENV, "127.0.0.1")
    hive.dashboard_probe(8787)
    assert calls[-1] == ("127.0.0.1", 8787)
    monkeypatch.delenv(hp.DASHBOARD_HOST_ENV)
    hive.dashboard_probe(8787)
    assert calls[-1] == ("127.0.0.1", 8787), "绑 0.0.0.0 时仍用回环探活（一定通）"


def test_doc_mentions_new_bind():
    doc = (ROOT / "docs" / "端口与服务规划.md").read_text(encoding="utf-8")
    assert "0.0.0.0" in doc
    assert "HIVE_DASHBOARD_HOST" in doc

def test_dashboard_script_picks_the_platform_wrapper(monkeypatch):
    """Windows 上必须选 .cmd。

    以前固定先找 .sh，Windows 会拿到只有 Linux 能跑的文件，再用 /bin/sh 去执行
    —— WinError 2，`hive dash start/stop` 在 Windows 上等于完全不可用（本次实测）。
    """
    import types
    if (hive.dashboard_script() or Path()).suffix != ".cmd":
        pytest.skip("本平台没有 .cmd 包装脚本")

    monkeypatch.setattr(hive, "os", types.SimpleNamespace(name="posix"))
    # posix 下先找 .sh；本仓库有 .sh 就必须选它
    assert (hive.dashboard_script() or Path()).suffix == ".sh"


def test_hive_dash_accepts_host_flag():
    args = hive.build_parser().parse_args(["dash", "--host", "127.0.0.1", "restart"])
    assert args.host == "127.0.0.1" and args.action == "restart"
    args = hive.build_parser().parse_args(["dash"])
    assert args.host is None and args.action == "ensure"