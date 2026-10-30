"""verify 命令的 POSIX → Windows 垫片（hive_compat.py，T-0208）。

背景：任务卡里的 verify 常写 `true` / `test -f x`。FreeBSD 上是内建命令，
Windows 的 cmd.exe 没有，于是同一张卡在验收机上**必然**红灯。
垫片只映射高频、语义明确的写法，未知写法原样交给 shell（绝不猜成功）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hive  # noqa: E402
import hive_compat  # noqa: E402


# --------------------------------------------------------------------------- #
# 纯求值层（与平台无关：直接调 evaluate）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cmd,want", [
    ("true", 0),
    ("false", 1),
    (":", 0),
    ("! true", 1),
    ("! false", 0),
    ("test -f hive.py", 0),
    ("test -f no_such_file_xyz", 1),
    ("test -d dashboard", 0),
    ("! test -f no_such_file_xyz", 0),
    ("test 2 -eq 2", 0),
    ("test 2 -ne 2", 1),
    ("test 3 -gt 1", 0),
])
def test_evaluate_known_forms(cmd, want, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert hive_compat.evaluate(cmd) == want, cmd


def test_evaluate_unquotes_single_quotes(monkeypatch):
    """cmd.exe 不剥单引号：`run 'true'` 到 Python 是 "'true'"。"""
    monkeypatch.chdir(ROOT)
    assert hive_compat.evaluate("'true'") == 0
    assert hive_compat.evaluate("'test -f hive.py'") == 0


def test_unknown_form_is_not_guessed():
    """不认识的写法返回 2（未处理），绝不冒充成功。"""
    assert hive_compat.evaluate("pytest -q tests") == 2
    assert hive_compat.evaluate("") == 2


# --------------------------------------------------------------------------- #
# 平台开关
# --------------------------------------------------------------------------- #

def test_should_wrap_only_on_windows(monkeypatch):
    monkeypatch.setattr(hive_compat.os, "name", "posix")
    assert hive_compat.should_wrap("true") is False
    monkeypatch.setattr(hive_compat.os, "name", "nt")
    assert hive_compat.should_wrap("true") is True
    assert hive_compat.should_wrap("test -f x") is True
    assert hive_compat.should_wrap("pytest -q") is False
    # 已经包过的别再套娃
    assert hive_compat.should_wrap(
        f'{sys.executable} "{hive_compat._self_path()}" run true') is False


def test_rewrite_is_idempotent_and_quoted():
    text = "test -f docs/ok.md"
    out = hive_compat.rewrite(text)
    # 脚本路径是带引号的绝对路径，MARK 整串（含空格）对不上——只认文件名。
    assert "hive_compat.py" in out
    assert " run " in out
    assert text in out
    # 幂等：already-wrapped 的输入不再包一层
    assert hive_compat.should_wrap(out) is False


def test_hive_verify_command_respects_kill_switch(monkeypatch):
    monkeypatch.setattr(hive_compat.os, "name", "nt")
    assert hive.verify_command("true") != "true"
    monkeypatch.setenv("HIVE_COMPAT", "0")
    assert hive.verify_command("true") == "true"


def test_hive_verify_command_untouched_on_posix(monkeypatch):
    monkeypatch.setattr(hive_compat.os, "name", "posix")
    monkeypatch.delenv("HIVE_COMPAT", raising=False)
    for cmd in ("true", "test -f x", "pytest -q tests"):
        assert hive.verify_command(cmd) == cmd


# --------------------------------------------------------------------------- #
# 端到端：worker 在 Windows 上跑 `true` 必须 done
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(os.name != "nt", reason="Windows 专用回归")
def test_worker_true_verify_is_done(tmp_path):
    """跨平台卡的 `verify: [true]` 在 Windows 上必须绿（不是平台性失败）。"""
    p = subprocess.run(
        [sys.executable, str(ROOT / "hive_compat.py"), "run", "true"],
        capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr

    from conftest import World  # noqa: WPS433
    world = World(tmp_path)
    world.add_agent("win", "verify", caps=["windows"])
    world.add_task("T-0001", needs=["windows"], verify=["true"])
    world.hive("assign", "T-0001", "--agent", "win")
    r = world.run_worker("win")
    assert r.returncode == 0, r.stdout + r.stderr
    res = world.hive_json("result", "T-0001")
    assert res["status"] == "done", res
    assert res["verify"][0]["code"] == 0, res
