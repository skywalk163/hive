"""T-0145：网络类 git 操作必须有超时，远端不可达时要明确报错而不是挂起。

两条防线：
* 行为：`run(..., timeout=N)` 到点必须抛 `HiveError`，而不是把 TimeoutExpired
  漏出去、更不是无限等。
* 结构：`hive.py` 里所有 fetch/push/clone/ls-remote 调用点都必须走 `git_net()`
  （本机网络正常时，行为测试根本测不出调用点漏传 timeout）。
"""
from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

import pytest

import hive

ROOT = Path(__file__).resolve().parents[1]
HIVE_PY = ROOT / "hive.py"
NET_SUBCOMMANDS = {"fetch", "push", "clone", "ls-remote"}


class _Proc:
    """subprocess.CompletedProcess 的最小替身。"""

    returncode = 0
    stdout = ""
    stderr = ""


def _const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _network_subcommand(call: ast.Call):
    """git()/git_ok()/run() 调用如果是网络子命令，返回子命令名，否则 None。"""
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    if name not in ("git", "git_ok", "run") or not call.args:
        return None
    first = call.args[0]
    if not isinstance(first, ast.List) or not first.elts:
        return None
    head = _const_str(first.elts[0])
    if head is None:
        return None
    if name == "run":
        # run(["git", "fetch", ...])
        if head != "git" or len(first.elts) < 2:
            return None
        return _const_str(first.elts[1])
    return head


# --------------------------------------------------------------------------- #
# 行为：run() 超时变成 HiveError
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("check", [True, False])
def test_run_timeout_raises_hive_error_instead_of_hanging(check):
    started = time.monotonic()
    with pytest.raises(hive.HiveError) as excinfo:
        hive.run([sys.executable, "-c", "import time; time.sleep(30)"],
                 timeout=0.2, check=check)
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"超时没有生效，跑了 {elapsed:.1f}s"
    msg = str(excinfo.value)
    assert "超时" in msg
    assert "连通" in msg or "远端" in msg
    assert getattr(excinfo.value, "timed_out", False)


def test_run_timeout_none_still_means_unlimited_and_keeps_utf8(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return _Proc()

    monkeypatch.setattr(hive.subprocess, "run", fake_run)
    hive.run(["git", "rev-parse", "HEAD"])
    assert seen["timeout"] is None       # signature 语义不变：None = 不限制
    assert seen["encoding"] == "utf-8"   # T-0143 的编码红线
    assert seen["errors"] == "replace"


# --------------------------------------------------------------------------- #
# 结构：网络 git 调用点全部走 git_net，且 git_net 真把 timeout 传下去
# --------------------------------------------------------------------------- #

def test_network_git_calls_all_go_through_git_net():
    tree = ast.parse(HIVE_PY.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            sub = _network_subcommand(node)
            if sub in NET_SUBCOMMANDS:
                bad.append((node.lineno, sub))
    assert not bad, ("这些网络 git 调用没走 git_net()，会没有超时："
                     + repr(bad))


def test_git_net_only_wraps_network_subcommands():
    tree = ast.parse(HIVE_PY.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "git_net"):
            continue
        first = node.args[0] if node.args else None
        sub = _const_str(first.elts[0]) if (
            isinstance(first, ast.List) and first.elts) else None
        if sub not in NET_SUBCOMMANDS:
            bad.append((node.lineno, sub))
    assert not bad, ("git_net() 只能用于网络子命令，本地 git 操作不该有超时："
                     + repr(bad))


def test_git_net_passes_default_timeout_down(monkeypatch):
    seen = {}

    def fake_run(cmd, cwd=None, check=True, input=None, timeout=None, **kwargs):
        seen.update(cmd=cmd, cwd=cwd, timeout=timeout)
        return _Proc()

    monkeypatch.setattr(hive, "run", fake_run)
    monkeypatch.delenv(hive.NET_TIMEOUT_ENV, raising=False)
    hive.git_net(["fetch", "--prune", "origin"], "/tmp/repo")
    assert seen["cmd"][:2] == ["git", "fetch"]
    assert seen["timeout"] == hive.NET_TIMEOUT_S


@pytest.mark.parametrize("raw,expected", [
    ("7", 7.0),
    ("0", None),          # <= 0 = 不限制，保留逃生口
    ("-3", None),
    ("not-a-number", hive.NET_TIMEOUT_S),
])
def test_net_timeout_env_override(monkeypatch, raw, expected):
    monkeypatch.setenv(hive.NET_TIMEOUT_ENV, raw)
    assert hive.net_timeout() == expected


def test_fetch_timeout_names_remote_and_connectivity(monkeypatch, tmp_path):
    def fake_run(cmd, cwd=None, check=True, input=None, timeout=None, **kwargs):
        assert timeout is not None, "fetch 必须带超时"
        err = hive.HiveError("命令超时（测试模拟）")
        err.timed_out = True
        raise err

    monkeypatch.setattr(hive, "run", fake_run)
    h = object.__new__(hive.Hive)   # 绕过 __init__ 的 .git 目录检查
    h.dir = tmp_path
    with pytest.raises(hive.HiveError) as excinfo:
        h.fetch()
    msg = str(excinfo.value)
    assert "origin" in msg            # 说清是哪个远端
    assert "连通" in msg              # 给出下一步建议
    assert getattr(excinfo.value, "timed_out", False)
