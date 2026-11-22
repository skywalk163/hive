"""cluster.pid 身份化与判活的回归测试（T-0440）。

2026-11-23 事故：hived 死后 `cluster.pid` 留下 1556，号码被 msedge.exe 复用。
旧判活只看「这个号码在不在」：
  * `cluster start` 认为「进程在跑但暂未响应」，节点永远起不来；
  * `cluster stop` / `restart` 会对 1556 直接 os.kill —— 杀掉用户的浏览器。

本文件锁住四条验收：
  1. pid 指向无关的存活进程 → 判活必须 False，`_spawn_background` 必须当陈旧
     清理并继续拉起（旧行为是 return 0）；
  2. 旧格式纯数字 pid 文件 → 不可信，不得据「号码存在」判定在跑；
  3. pid + 命令行 + 启动时间都匹配 → 判定在跑，健康不通时给带证据的提示；
  4. `_stop_background` 面对无关 PID → 不调用 os.kill，只清 pid 文件。

不拉起常驻 daemon、不出网、不写 state_dir 之外的文件（全部 tmp_path）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

import pytest

import hive_cluster as hc


def _cfg(tmp_path, name: str, port: int = 0):
    cfg = hc.Config(
        state_dir=tmp_path / name, name=name, host="127.0.0.1", port=port,
        advertise="127.0.0.1", discover=False, hive_dir=tmp_path / "repo",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class _DeadProc:
    """代表「拉起来了但立刻退出」的子进程。"""
    pid = 4343

    def poll(self):
        return 1


def _spawn_sleeper():
    """一个与本节点 hived 无关的存活进程（命令行里没有 hive.py/cluster）。"""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            **hc.no_window_kwargs())


def _spawn_stub(monkeypatch):
    """只拦截「拉起 daemon」的那一次 Popen；探测用的 tasklist/wmic 仍走真命令。

    直接全局替换 subprocess.Popen 会把 `subprocess.run` 里的 tasklist 也换成
    假对象（run 内部就调 Popen），判活探针会一起失效。
    """
    calls = []
    real_popen = subprocess.Popen

    def fake(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and "serve" in [str(x) for x in argv]:
            calls.append(list(argv))
            return _DeadProc()
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(hc.subprocess, "Popen", fake)
    return calls


def test_pid_file_carries_identity(tmp_path):
    """写盘的是身份记录，不是裸号码（那是本事故的根因）。"""
    cfg = _cfg(tmp_path, "identity")
    hc._write_pid_record(cfg, os.getpid(), node_id="desk-1",
                         argv=["hive.py", "cluster", "serve"])
    record = hc._read_pid_record(cfg)
    assert record["pid"] == os.getpid()
    assert record.get("schema") == hc.PID_FILE_SCHEMA
    assert isinstance(record.get("started_at"), float)
    assert record.get("node_id") == "desk-1"
    assert record.get("argv") == ["hive.py", "cluster", "serve"]
    assert "version" in record and not record.get("legacy")


def test_unrelated_live_pid_is_stale_and_start_continues(monkeypatch, tmp_path, capsys):
    """验收 1：无关的存活进程被写进 pid 文件 → 判死，start 清理后继续拉起。"""
    child = _spawn_sleeper()
    try:
        cfg = _cfg(tmp_path, "reused")
        hc._write_pid_record(cfg, child.pid, node_id="someone-else")
        record = hc._read_pid_record(cfg)

        ok, reason, _info = hc._pid_identity(child.pid, record)
        assert ok is False, "号码活着不等于本节点 hived"
        assert "hived" in reason

        monkeypatch.setattr(hc, "_local_health", lambda *a, **k: None)
        monkeypatch.setattr(hc, "_startup_timeout", lambda: 0.05)
        calls = _spawn_stub(monkeypatch)

        rc = hc._spawn_background(cfg, argparse.Namespace())
        out = capsys.readouterr()
        text = out.out + out.err
        assert "检测到陈旧 pid" in text
        assert str(child.pid) in text
        assert "已清理并继续启动" in text
        assert calls, "必须真的去拉起，而不是像旧代码那样 return 0"
        assert rc == 1, "stub 子进程立刻退出，启动失败（rc=1）才说明它确实走了拉起路径"
        assert not cfg.pid_path.exists(), "陈旧 pid 文件必须被清掉"
    finally:
        child.kill()
        child.wait()


def test_legacy_numeric_pid_is_untrusted(monkeypatch, tmp_path, capsys):
    """验收 2：旧格式只有号码 → 不可信，绝不据此判定「在跑」。"""
    cfg = _cfg(tmp_path, "legacy")
    cfg.pid_path.write_text(str(os.getpid()), encoding="utf-8")
    record = hc._read_pid_record(cfg)
    assert record.get("legacy") is True, "纯数字必须被识别成旧格式"

    ok, reason, _info = hc._pid_identity(os.getpid(), record)
    assert ok is False, "号码存在不能当作在跑的证据"
    assert "旧格式" in reason

    monkeypatch.setattr(hc, "_local_health", lambda *a, **k: None)
    monkeypatch.setattr(hc, "_startup_timeout", lambda: 0.05)
    calls = _spawn_stub(monkeypatch)
    rc = hc._spawn_background(cfg, argparse.Namespace())
    out = capsys.readouterr()
    assert "旧格式" in (out.out + out.err)
    assert calls and rc == 1
    assert not cfg.pid_path.exists()


def test_matching_identity_keeps_unresponsive_cluster(monkeypatch, tmp_path, capsys):
    """验收 3：身份三层全过 → 判定在跑；健康不通时提示带探活证据 + restart。"""
    cfg = _cfg(tmp_path, "running", port=18788)
    hc._write_pid_record(cfg, os.getpid(), node_id="desk-1",
                         argv=["hive.py", "cluster", "serve"])
    record = hc._read_pid_record(cfg)
    fake_info = {
        "name": "python.exe",
        "command_line": f"{sys.executable} G:/hive/hive.py cluster serve --port 18788",
        "started_at": record["started_at"],
        "pid": os.getpid(),
    }
    monkeypatch.setattr(hc, "_proc_info", lambda pid: dict(fake_info))

    ok, reason, _info = hc._pid_identity(os.getpid(), record)
    assert ok is True, reason
    assert "启动时间" in reason

    monkeypatch.setattr(hc, "_local_health", lambda *a, **k: None)
    calls = _spawn_stub(monkeypatch)
    rc = hc._spawn_background(cfg, argparse.Namespace())
    assert rc == 0
    assert calls == [], "身份校验通过在跑，不许再拉第二个抢端口"

    err = capsys.readouterr().err
    assert "暂未响应" in err
    assert "18788" in err, "提示必须带探活证据（端口）"
    assert "hive cluster restart" in err
    assert cfg.pid_path.exists(), "真在跑时不清理 pid"


def test_start_time_mismatch_is_stale(monkeypatch, tmp_path):
    """验收 3 的反面：命令行像了，但启动时间对不上（号码被复用）→ 陈旧。"""
    cfg = _cfg(tmp_path, "timejump")
    hc._write_pid_record(cfg, os.getpid(), node_id="desk-1")
    record = hc._read_pid_record(cfg)
    fake_info = {
        "name": "python.exe",
        "command_line": f"{sys.executable} G:/hive/hive.py cluster serve",
        "started_at": record["started_at"] - 30.0,
        "pid": os.getpid(),
    }
    monkeypatch.setattr(hc, "_proc_info", lambda pid: dict(fake_info))
    ok, reason, _info = hc._pid_identity(os.getpid(), record)
    assert ok is False
    assert "启动时间" in reason
    assert "容差" in reason


def test_stop_never_kills_unrelated_pid(monkeypatch, tmp_path, capsys):
    """验收 4：strip 面对无关 PID 不 os.kill，只清 pid 文件（本卡最高优先安全项）。"""
    child = _spawn_sleeper()
    try:
        cfg = _cfg(tmp_path, "stopguard", port=18789)
        hc._write_pid_record(cfg, child.pid, node_id="someone-else")
        monkeypatch.setattr(hc, "_local_health", lambda *a, **k: None)
        # 判活换成桩：POSIX 的 _pid_alive 用 os.kill(pid, 0) 探活，不桩掉的话
        # 下面的 os.kill 记录里会混进一个信号 0 的探测，断言就不再是「没发信号」。
        monkeypatch.setattr(hc, "_pid_alive", lambda pid: int(pid) == child.pid)

        kills = []
        with monkeypatch.context() as m:
            m.setattr(hc.os, "kill", lambda pid, sig: kills.append((pid, sig)))
            rc = hc._stop_background(cfg)

        assert kills == [], f"不许对未经身份校验的 PID 发信号：{kills}"
        assert rc == 0
        assert not cfg.pid_path.exists()
        assert (cfg.state_dir / hc.STOPPED_MARKER_NAME).exists()
        err = capsys.readouterr().err
        assert "未向该进程发信号" in err
    finally:
        child.kill()
        child.wait()
