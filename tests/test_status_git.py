"""P0：进度默认不再每 60s force push Git，只在里程碑+集群不可用时兜底。

T-0216：status 命令的 fetch 缓存（TTL + SHA 前置比对）——看板每 30s 跑一次
hive status --json，远端没变化时不该白跑一次 git fetch --prune。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import hive
from conftest import sh


def _status_ref(world, tid: str) -> str:
    out = sh(["git", "--git-dir", world.hive_origin, "for-each-ref",
              "--format=%(refname)", f"refs/heads/status/{tid}"],
             check=False).stdout
    return out.strip()


def _worker(world, tmp_path, agent="fb"):
    world.add_agent(agent, mode="manual")
    return hive.Worker(str(world.hive1), agent,
                       hive_state=str(tmp_path / f"state-{agent}"))


def test_status_git_mode_values(monkeypatch):
    for raw, want in (
            (None, "auto"), ("", "auto"), ("auto", "auto"),
            ("1", "always"), ("true", "always"), ("always", "always"),
            ("0", "never"), ("false", "never"), ("never", "never"),
            ("garbage", "auto")):
        if raw is None:
            monkeypatch.delenv(hive.STATUS_GIT_ENV, raising=False)
        else:
            monkeypatch.setenv(hive.STATUS_GIT_ENV, raw)
        assert hive.status_git_mode() == want


def test_status_never_does_not_write_git(world, tmp_path, monkeypatch):
    monkeypatch.setenv(hive.STATUS_GIT_ENV, "never")
    monkeypatch.setattr(hive, "cluster_available", lambda *a, **k: False)
    w = _worker(world, tmp_path)

    w.set_status("T-0001", state="running", progress=10, step="coding")
    assert _status_ref(world, "T-0001") == ""


def test_status_auto_milestone_writes_when_cluster_down(world, tmp_path, monkeypatch):
    monkeypatch.setenv(hive.STATUS_GIT_ENV, "auto")
    monkeypatch.setattr(hive, "cluster_available", lambda *a, **k: False)
    w = _worker(world, tmp_path)

    w.set_status("T-0001", state="running", progress=10)
    assert _status_ref(world, "T-0001") != ""


def test_status_auto_milestone_skips_git_when_cluster_up(world, tmp_path, monkeypatch):
    monkeypatch.setenv(hive.STATUS_GIT_ENV, "auto")
    monkeypatch.setattr(hive, "cluster_available", lambda *a, **k: True)
    w = _worker(world, tmp_path)

    w.set_status("T-0001", state="running", progress=10)
    assert _status_ref(world, "T-0001") == ""


def test_status_auto_periodic_never_writes(world, tmp_path, monkeypatch):
    monkeypatch.setenv(hive.STATUS_GIT_ENV, "auto")
    monkeypatch.setattr(hive, "cluster_available", lambda *a, **k: False)
    w = _worker(world, tmp_path)

    w.push_status("T-0001", state="running", progress=20)
    assert _status_ref(world, "T-0001") == ""


def test_status_always_keeps_old_behaviour(world, tmp_path, monkeypatch):
    monkeypatch.setenv(hive.STATUS_GIT_ENV, "1")
    monkeypatch.setattr(hive, "cluster_available", lambda *a, **k: False)
    w = _worker(world, tmp_path)

    w.set_status("T-0001", state="running", progress=30)
    assert _status_ref(world, "T-0001") != ""


@pytest.mark.parametrize("raw", ["0", "false", "off"])
def test_status_disabled_values(raw, monkeypatch):
    monkeypatch.setenv(hive.STATUS_GIT_ENV, raw)
    assert hive.status_git_mode() == "never"


# --------------------------------------------------------------------------- #
# T-0216：fetch 前置比对（remote_head + fetch_if_changed）
# --------------------------------------------------------------------------- #

def _clear_ls_remote_cache():
    """每个测试前清空进程内 ls-remote 缓存。"""
    hive._ls_remote_cache.clear()


def test_remote_head_returns_sha(world):
    """remote_head 从 ls-remote 拿到远端 main 的 SHA。"""
    _clear_ls_remote_cache()
    sha = hive.remote_head(world.hive1)
    assert len(sha) == 40, f"应该是 40 字符 SHA，got {sha!r}"
    # 和本地 origin/main 一致（刚 clone 完）
    local = hive.rev_parse(world.hive1, "refs/remotes/origin/main")
    assert sha == local


def test_remote_head_empty_on_bad_path(tmp_path):
    """remote_head 在非 git 仓 / 无 origin 时返回空串。"""
    _clear_ls_remote_cache()
    assert hive.remote_head(tmp_path) == ""


def test_fetch_if_changed_false_when_up_to_date(world, monkeypatch):
    """远端 SHA 和本地 origin/main 一致 → 返回 False（不需要 fetch）。"""
    _clear_ls_remote_cache()
    monkeypatch.delenv(hive.FETCH_ALWAYS_ENV, raising=False)

    # 间谍：统计 git_net 调用
    calls: list[list[str]] = []
    real = hive.git_net
    def spy(args, *a, **k):
        calls.append(list(args))
        return real(args, *a, **k)
    monkeypatch.setattr(hive, "git_net", spy)

    result = hive.fetch_if_changed(world.hive1, ttl=10.0)

    assert result is False, "远端没变不该需要 fetch"
    # 不应该有 git fetch 调用
    assert not any("fetch" in c for c in calls), \
        f"远端没变不该跑 fetch，calls={calls}"


def test_fetch_if_changed_true_when_remote_changes(world, monkeypatch):
    """远端有新提交 → 返回 True（需要 fetch）。"""
    _clear_ls_remote_cache()
    monkeypatch.delenv(hive.FETCH_ALWAYS_ENV, raising=False)

    # 先让 fetch_if_changed 跑一次（建立基线：远端没变 → False）
    assert hive.fetch_if_changed(world.hive1, ttl=10.0) is False

    # 在 hive2 上推一个新提交到 origin/main
    world.hive("task", "new", "--id", "T-0999", "--title", "远端新任务",
               "--repo", "light", "--body", "测试", dir=world.hive2)
    sh(["git", "-C", str(world.hive2), "push", "-q", "origin", "main"],
       env=world.git_env)

    # 清 TTL 缓存，强制重新 ls-remote
    _clear_ls_remote_cache()

    result = hive.fetch_if_changed(world.hive1, ttl=10.0)
    assert result is True, "远端变了应该需要 fetch"


def test_fetch_if_changed_true_on_ls_remote_failure(world, monkeypatch):
    """ls-remote 失败 → 回落无条件 fetch（不能因为网络抖动让跨机卡消失）。"""
    _clear_ls_remote_cache()
    monkeypatch.delenv(hive.FETCH_ALWAYS_ENV, raising=False)

    # 让 remote_head 返回空串（模拟 ls-remote 失败）
    monkeypatch.setattr(hive, "remote_head", lambda p: "")

    result = hive.fetch_if_changed(world.hive1, ttl=10.0)
    assert result is True, "ls-remote 失败应该回落无条件 fetch"


def test_fetch_if_changed_ttl_caches_result(world, monkeypatch):
    """进程内 TTL：10s 内第二次调用不重跑 ls-remote。"""
    _clear_ls_remote_cache()
    monkeypatch.delenv(hive.FETCH_ALWAYS_ENV, raising=False)

    ls_remote_calls = []
    real_remote_head = hive.remote_head
    def spy_remote_head(path):
        ls_remote_calls.append(str(path))
        return real_remote_head(path)
    monkeypatch.setattr(hive, "remote_head", spy_remote_head)

    # 第一次：跑 ls-remote
    hive.fetch_if_changed(world.hive1, ttl=10.0)
    n1 = len(ls_remote_calls)

    # 第二次：TTL 内，不该再跑 ls-remote
    hive.fetch_if_changed(world.hive1, ttl=10.0)
    n2 = len(ls_remote_calls)

    assert n1 == 1, f"第一次应该跑一次 ls-remote，calls={ls_remote_calls}"
    assert n2 == 1, f"TTL 内不该再跑 ls-remote，calls={ls_remote_calls}"


def test_fetch_if_changed_always_env_forces_fetch(world, monkeypatch):
    """HIVE_FETCH_ALWAYS=1：回退阀，每次都返回 True。"""
    _clear_ls_remote_cache()
    monkeypatch.setenv(hive.FETCH_ALWAYS_ENV, "1")

    result = hive.fetch_if_changed(world.hive1, ttl=10.0)
    assert result is True, "HIVE_FETCH_ALWAYS=1 应该强制 fetch"


def test_cmd_status_no_fetch_when_remote_unchanged(world, monkeypatch):
    """cmd_status：远端没变时不跑 git fetch。"""
    _clear_ls_remote_cache()
    monkeypatch.delenv(hive.FETCH_ALWAYS_ENV, raising=False)
    world.add_agent("fb", mode="manual")

    h = hive.Hive(world.hive1)

    # 间谍 git_net：统计 fetch 调用
    fetch_calls: list = []
    real = hive.git_net
    def spy(args, *a, **k):
        if "fetch" in args:
            fetch_calls.append(list(args))
        return real(args, *a, **k)
    monkeypatch.setattr(hive, "git_net", spy)

    # 第一次 status：远端没变，不该 fetch
    h.cmd_status(as_json=True)

    # 远端确实没变（没人 push），所以 fetch 调用次数应该是 0
    assert fetch_calls == [], \
        f"远端没变时 cmd_status 不该跑 fetch，fetch_calls={fetch_calls}"
