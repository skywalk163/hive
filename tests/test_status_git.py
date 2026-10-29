"""P0：进度默认不再每 60s force push Git，只在里程碑+集群不可用时兜底。"""
from __future__ import annotations

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
