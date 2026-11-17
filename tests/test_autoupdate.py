#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hive_autoupdate：自愈式自动更新的回归测试。

覆盖现场那四个「自动更新失效」的根因：
1. 陈旧 index.lock 被识别并**归档**，不删、不误伤活进程持有的锁；
2. 撞锁的 git 写操作自愈后重试一次，真正失败原样返回；
3. 脏工作区「不重叠」时能快进且本地改动一个字节都不动，重叠时停下并报 blocked；
4. 看板运行时代码比进程新时安排独立会话重启；cron 安装幂等。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import hive_autoupdate as au
import hive_repos

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
}


def sh(cmd, cwd, check=True):
    import subprocess
    env = {**os.environ, **GIT_ENV}
    p = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True,
                       encoding="utf-8", errors="replace", env=env)
    if check and p.returncode != 0:
        raise AssertionError("命令失败(%s): %s\n%s\n%s"
                             % (p.returncode, cmd, p.stdout, p.stderr))
    return p


def git(*args, cwd):
    return sh(["git", *args], cwd)


def head_of(repo) -> str:
    return git("rev-parse", "HEAD", cwd=repo).stdout.strip()


class Repo:
    """bare origin + seed（模拟别的节点）+ 被测 clone。"""

    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin.git"
        self.seed = root / "seed"
        self.a = root / "hive-a"
        git("init", "--bare", "-b", "main", str(self.origin), cwd=root)
        git("init", "-b", "main", str(self.seed), cwd=root)
        (self.seed / "tasks").mkdir()
        (self.seed / "README.md").write_text("base\n", encoding="utf-8")
        (self.seed / "tasks" / "T-0001.md").write_text("card one\n",
                                                       encoding="utf-8")
        git("add", "-A", cwd=self.seed)
        git("commit", "-q", "-m", "init", cwd=self.seed)
        git("remote", "add", "origin", str(self.origin), cwd=self.seed)
        git("push", "-q", "-u", "origin", "main", cwd=self.seed)
        sh(["git", "clone", "-q", str(self.origin), str(self.a)], root)

    def push(self, rel: str, text: str) -> str:
        f = self.seed / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        git("add", "-A", cwd=self.seed)
        git("commit", "-q", "-m", "remote change %s" % rel, cwd=self.seed)
        git("push", "-q", "origin", "main", cwd=self.seed)
        return head_of(self.seed)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv(au.ENV_STATE, str(tmp_path / "autoupdate-state.json"))
    monkeypatch.setenv(au.ENV_LOCK, str(tmp_path / "autoupdate.lock"))
    return Repo(tmp_path)


# --------------------------------------------------------------------------- #
# 陈旧锁
# --------------------------------------------------------------------------- #
def test_stale_lock_is_archived_not_deleted(repo):
    lock = repo.a / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 900
    os.utime(lock, (old, old))

    res = hive_repos.repair_stale_locks(repo.a)

    assert res["error"] == ""
    assert len(res["repaired"]) == 1
    assert not lock.exists(), "陈旧锁必须让位给真正的 git 操作"
    archived = Path(res["repaired"][0]["archived_as"])
    assert archived.exists(), "必须留证据，不能直接 unlink"
    assert archived.name.startswith("index.lock.stale-")


def test_fresh_lock_is_left_alone(repo):
    lock = repo.a / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")           # 刚创建 = 可能有人正在写

    res = hive_repos.repair_stale_locks(repo.a)

    assert res["repaired"] == []
    assert lock.exists(), "新鲜锁一律不碰"


def test_lock_held_by_live_process_is_busy(repo):
    """有进程正打开这个文件 → 判 busy，绝不当陈旧锁归档。"""
    lock = repo.a / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 900
    os.utime(lock, (old, old))
    with open(lock, "a") as fh:                     # 本进程持有它
        fh.write("")
        fh.flush()
        scan = hive_repos.inspect_locks(repo.a)
        verdict = scan["locks"][0]["verdict"]
        if not scan["locks"][0]["holders_known"]:
            pytest.skip("本机既无 fstat 也无 /proc，无法查持有者")
        assert verdict == "busy"
        res = hive_repos.repair_stale_locks(repo.a)
        assert res["repaired"] == [], "活进程持有的锁不能动"
        assert lock.exists()


def test_git_write_repairs_lock_and_retries(repo):
    repo.push("README.md", "remote update\n")
    git("fetch", "origin", cwd=repo.a)          # 先让 origin/main 指向新提交
    lock = repo.a / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 900
    os.utime(lock, (old, old))

    rc, _out, err = hive_repos.git_write(
        repo.a, ["merge", "--ff-only", "origin/main"])

    assert rc == 0, "自愈后应当合并成功：%s" % err
    assert head_of(repo.a) == head_of(repo.seed)
    assert not lock.exists()


def test_git_write_does_not_retry_real_conflicts(repo):
    """非锁失败（真冲突）原样返回，不拿重试掩盖错误。"""
    lock = repo.a / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 900
    os.utime(lock, (old, old))

    # 不存在的 merge 目标：失败原因不是锁
    rc, _out, err = hive_repos.git_write(
        repo.a, ["merge", "--ff-only", "no-such-ref"])
    assert rc != 0
    assert not hive_repos.is_lock_error(err)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def test_autoupdate_fast_forwards_and_keeps_local_changes(repo):
    target = repo.a / "README.md"
    target.write_text("base\nlocal edit\n", encoding="utf-8")
    remote = repo.push("tasks/T-0002.md", "new card on remote\n")

    res = au.run(repo.a, use_lock=True, now=time.time())

    assert res["exit_code"] == au.EXIT_OK
    assert res["synced"] is True
    assert res["changed"] is True
    assert res["state"] == hive_repos.DIRTY_FF
    assert res["head_after"] == remote
    assert res["blocked"] == []
    assert target.read_text(encoding="utf-8") == "base\nlocal edit\n", \
        "不重叠的本地改动必须原样保留"
    assert (repo.a / "tasks" / "T-0002.md").exists(), "远端新文件应当进来"

    st = json.loads(Path(os.environ[au.ENV_STATE]).read_text(encoding="utf-8"))
    assert st["synced"] is True and st["head_after"] == remote


def test_autoupdate_blocks_on_overlap(repo):
    target = repo.a / "tasks" / "T-0001.md"
    target.write_text("local edit\n", encoding="utf-8")
    before = head_of(repo.a)
    repo.push("tasks/T-0001.md", "remote edit\n")

    res = au.run(repo.a, use_lock=True, now=time.time())

    assert res["exit_code"] == au.EXIT_BLOCKED
    assert res["synced"] is False
    assert res["blocked"][0]["kind"] == "dirty-overlap"
    assert "tasks/T-0001.md" in res["blocked"][0]["files"]
    assert head_of(repo.a) == before, "重叠时绝不 merge"
    assert target.read_text(encoding="utf-8") == "local edit\n"


def test_autoupdate_up_to_date(repo):
    res = au.run(repo.a, use_lock=True, now=time.time())
    assert res["exit_code"] == au.EXIT_OK
    assert res["synced"] is False
    assert res["changed"] is False


def test_autoupdate_skips_when_another_run_holds_lock(repo):
    ok, info = au.acquire_lock(str(Path(os.environ[au.ENV_LOCK])))
    assert ok, info
    try:
        res = au.run(repo.a, use_lock=True, now=time.time())
        assert res["action"] == "skip-concurrent"
        assert res["exit_code"] == au.EXIT_OK
        assert res["synced"] is False
    finally:
        au.release_lock(str(Path(os.environ[au.ENV_LOCK])))


def test_autoupdate_stale_run_lock_is_taken_over(repo):
    p = Path(os.environ[au.ENV_LOCK])
    p.write_text(json.dumps({"pid": 999999, "at": time.time() - 7200}),
                 encoding="utf-8")
    res = au.run(repo.a, use_lock=True, now=time.time())
    assert res["action"] != "skip-concurrent", "死进程的锁必须能抢过来"
    assert res["exit_code"] == au.EXIT_OK


def test_dry_run_writes_nothing(repo):
    before = head_of(repo.a)
    repo.push("tasks/T-0003.md", "remote\n")
    res = au.run(repo.a, dry_run=True, now=time.time())
    assert res["action"] == "would-ff"
    assert head_of(repo.a) == before
    assert not Path(os.environ[au.ENV_STATE]).exists()


# --------------------------------------------------------------------------- #
# 看板重启判定
# --------------------------------------------------------------------------- #
def test_dashboard_freshness_detects_stale_code(repo, tmp_path):
    dash = repo.a / "dashboard"
    dash.mkdir()
    (dash / "serve.py").write_text("# new code\n", encoding="utf-8")
    pid_file = tmp_path / "dashboard.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    old = time.time() - 3600
    os.utime(pid_file, (old, old))

    fresh = au.dashboard_freshness(repo.a, pid_file=pid_file)
    assert fresh["running"] is True
    assert fresh["needed"] is True
    assert "dashboard/serve.py" in fresh["stale_files"]

    os.utime(pid_file, (time.time(), time.time()))
    fresh = au.dashboard_freshness(repo.a, pid_file=pid_file)
    assert fresh["needed"] is False


def test_dashboard_freshness_not_running(repo, tmp_path):
    pid_file = tmp_path / "dashboard.pid"
    pid_file.write_text("999999", encoding="utf-8")
    fresh = au.dashboard_freshness(repo.a, pid_file=pid_file)
    assert fresh["running"] is False
    assert fresh["needed"] is False, "没在跑就没有「代码比进程新」这回事"


def test_schedule_restart_uses_detached_helper(repo, monkeypatch, tmp_path):
    script = repo.a / "scripts" / "dashboard-service.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    started = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            started["argv"] = argv
            started["kwargs"] = kwargs
            self.pid = 4242

    monkeypatch.setattr(au.subprocess, "Popen", FakePopen)
    res = au.schedule_dashboard_restart(repo.a, action="restart",
                                        log_path=tmp_path / "restart.log")

    assert res["scheduled"] is True
    assert res["helper_pid"] == 4242
    assert started["argv"][1] == "-c"
    assert "time.sleep" in started["argv"][2]
    assert started["argv"][-1] == str(repo.a)          # cwd 传给了 helper
    if os.name == "posix":
        assert started["kwargs"].get("start_new_session") is True


# --------------------------------------------------------------------------- #
# cron
# --------------------------------------------------------------------------- #
def test_cron_install_is_idempotent(repo, monkeypatch):
    state = {"text": ""}

    monkeypatch.setattr(au, "_crontab_read", lambda: (0, state["text"]))
    monkeypatch.setattr(au, "_crontab_write",
                        lambda text: (state.__setitem__("text", text) or True,
                                      ""))

    r1 = au.install_cron(repo.a, every_min=5)
    assert r1["ok"] and r1["changed"]
    assert au.CRON_MARKER in state["text"]
    assert "*/5 * * * *" in state["text"]
    assert "autoupdate --restart" in state["text"]

    r2 = au.install_cron(repo.a, every_min=5)
    assert r2["changed"] is False, "重复安装必须幂等"

    r3 = au.install_cron(repo.a, every_min=10)
    assert r3["changed"] is True
    assert state["text"].count(au.CRON_MARKER) == 1, "换间隔不能留下旧行"

    r4 = au.uninstall_cron()
    assert r4["changed"] is True
    assert au.CRON_MARKER not in state["text"]


def test_cron_install_keeps_other_lines(repo, monkeypatch):
    state = {"text": "0 3 * * * /usr/bin/backup.sh\n"}
    monkeypatch.setattr(au, "_crontab_read", lambda: (0, state["text"]))
    monkeypatch.setattr(au, "_crontab_write",
                        lambda text: (state.__setitem__("text", text) or True,
                                      ""))
    au.install_cron(repo.a)
    assert "/usr/bin/backup.sh" in state["text"]
    assert state["text"].count(au.CRON_MARKER) == 1


def test_cron_rejects_bad_interval(repo):
    res = au.install_cron(repo.a, every_min=0)
    assert res["ok"] is False
    assert "1..59" in res["reason"]


def test_summary_text_mentions_actions():
    res = {"exit_code": 0, "synced": False, "state": "up-to-date",
           "head_before": "a" * 40, "remote_sha": "a" * 40,
           "blocked": [], "restart": {}}
    assert "已是最新" in au.summary_text(res)
    res.update({"exit_code": 2, "state": "dirty",
                "blocked": [{"kind": "dirty-overlap", "files": ["a.py"],
                             "reason": "重叠"}]})
    assert "需要人工/agent 介入" in au.summary_text(res)