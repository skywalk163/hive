"""T-0492：hive_submit 异步化。

现场：`cmd_submit` 把 commit/push/verify/写结果串成一条同步链，verify 单条上限
1800s；MCP stdio 又只有一个线程。T-0489 的 verify 跑了 30 分钟没完，客户端
先超时（`LocalHub returned an invalid raw MCP result`），之后 `hive_get_task`
也被同一段 verify 堵住，分支已推、结果没落，卡停在 running。

这里钉住新契约：
  1. 同步段只做 commit+push，返回 submitting，不跑 verify；
  2. 后台 job 独立成进程（pid 探活、O_EXCL 锁），verify 完把结果落 res/store；
  3. verify 失败/超时都必须产 failed 结果，不悬挂；
  4. pid 死了且没有结果 → phase=lost，不伪造结果；
  5. 租约被接管 → lease_lost，不写结果。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

import hive
from conftest import sh

VERIFY_OK = 'python -c "print(1)"'
VERIFY_FAIL = 'python -c "raise SystemExit(1)"'
VERIFY_SLOW = 'python -c "import time;time.sleep(3)"'


@pytest.fixture()
def ready(world, monkeypatch, tmp_path):
    """一张已 take、已改好文件、等待 submit 的卡（git claim 后端）。"""
    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "git")
    monkeypatch.setenv("HIVE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("HIVE_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.delenv("HIVE_VERIFY_TIMEOUT", raising=False)

    world.add_agent("w1", mode="manual")
    world.add_task("T-1001", title="异步提交", repo="light",
                   paths=("src/main.py",), verify=(VERIFY_OK,))
    world.hive("take", "T-1001", "--agent", "w1")

    wt = tmp_path / "state" / "wt" / "T-1001"
    assert wt.exists(), f"take 应该建好 worktree: {wt}"
    (wt / "src" / "main.py").write_text("print('hi')\nprint('T-1001')\n",
                                       encoding="utf-8")
    (wt / ".hive" / "summary.md").write_text("T-1001 后台提交\n", encoding="utf-8")

    h = hive.Hive(str(world.hive1))
    return {"world": world, "h": h, "wt": wt, "tid": "T-1001",
            "job_root": tmp_path / "state" / "jobs"}


def _make_job(data, verify=None):
    """只准备 job 文件，不起真实子进程；便于进程内直接跑 run_submit_job。"""
    h, tid = data["h"], data["tid"]
    ctx = h.prepare_submit(tid, note="后台提交")
    cmds = list(verify if verify is not None else ctx["verify"])
    job = {
        "task": tid, "agent": ctx["agent"], "dir": str(h.dir),
        "repo_path": ctx["repo_path"], "wt": ctx["wt"], "head": ctx["head"],
        "base": ctx["base"], "changed": ctx["changed"],
        "out_of_paths": ctx["out_of_paths"], "verify": cmds,
        "keep": True, "pr": False, "note": "",
        "lease_id": ctx["lease_id"], "fence": ctx["fence"],
        "claimed_at": (ctx["claim"] or {}).get("claimed_at") or "",
        "phase": "verifying", "pid": 0, "started_at": hive.iso(),
        "updated": hive.iso(), "log": "", "branch": f"task/{tid}",
        "verify_total": len(cmds), "verify_index": 0,
    }
    path = hive.write_submit_job(job)
    hive.acquire_submit_job_lock(tid, hive.submit_job_root(ctx["agent"]))
    return path, job


def test_submit_async_returns_without_running_verify(ready, monkeypatch):
    """同步段只 push；verify 不能在这一步跑。"""
    data = ready
    calls: list[list[str]] = []

    class _FakeProc:
        pid = os.getpid()   # 探活时算「活着」，避免被标 lost

    def fake_popen(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(hive.subprocess, "Popen", fake_popen)

    t0 = time.monotonic()
    info = data["h"].submit_async(data["tid"], note="异步提交")
    elapsed = time.monotonic() - t0

    assert elapsed < 15.0, (
        f"同步段只该做 git 操作，不能把 verify 串在里面（实测 {elapsed:.2f}s）")
    assert info["status"] == "submitting"
    assert info["branch"] == f"task/{data['tid']}"
    assert info["head"] == hive.rev_parse(data["wt"], "HEAD")
    job_path = Path(info["job"])
    assert job_path.exists(), "job 状态文件必须写下来"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["phase"] == "verifying"
    assert job["verify_total"] == 1
    assert job["pid"] == os.getpid()
    assert len(calls) == 1, "只该起一个后台进程"
    assert data["tid"] in data["world"].code_refs("refs/heads/task/*")
    assert f"refs/heads/res/{data['tid']}" not in data["world"].refs("refs/heads/res/*")


def test_submit_async_is_idempotent_while_job_alive(ready, monkeypatch):
    data = ready
    calls: list[list[str]] = []

    class _FakeProc:
        pid = os.getpid()

    monkeypatch.setattr(hive.subprocess, "Popen",
                        lambda cmd, **kw: (calls.append(cmd), _FakeProc())[1])

    first = data["h"].submit_async(data["tid"])
    second = data["h"].submit_async(data["tid"])

    assert first["existing"] is False
    assert second["existing"] is True
    assert second["head"] == first["head"]
    assert len(calls) == 1, "已有活 job 时不许再起进程"


def test_run_submit_job_writes_done_result(ready):
    data = ready
    path, _job = _make_job(data)

    rc = data["h"].run_submit_job(path)

    assert rc == 0
    res = data["h"].result_info(data["tid"]) or {}
    assert res.get("status") == "done", res
    assert res["verify"][0]["code"] == 0
    assert f"refs/heads/res/{data['tid']}" in data["world"].refs("refs/heads/res/*")
    assert data["wt"].exists(), "keep=True 时 worktree 必须保留"
    job = hive.read_submit_job("w1", data["tid"]) or {}
    assert job.get("phase") == "done"
    assert not (data["job_root"] / f"{data['tid']}.lock").exists(), (
        "job 结束必须清锁，否则父进程还活着会把下一次 submit 挡死")


def test_run_submit_job_failed_verify_writes_failed_result(ready):
    data = ready
    path, _job = _make_job(data, verify=[VERIFY_FAIL])

    rc = data["h"].run_submit_job(path)

    assert rc == 0, "verify 红也要把结果写下来（返回码按结果落库计）"
    res = data["h"].result_info(data["tid"]) or {}
    assert res.get("status") == "failed", res
    assert res.get("error"), "失败必须带原因"
    job = hive.read_submit_job("w1", data["tid"]) or {}
    assert job.get("phase") == "failed"


def test_run_submit_job_timeout_marks_timed_out(ready, monkeypatch):
    data = ready
    monkeypatch.setenv("HIVE_VERIFY_TIMEOUT", "1")
    path, _job = _make_job(data, verify=[VERIFY_SLOW])

    rc = data["h"].run_submit_job(path)

    assert rc == 0
    res = data["h"].result_info(data["tid"]) or {}
    assert res.get("status") == "failed"
    assert res.get("timed_out") is True, res


def test_job_status_marks_lost_when_pid_dead(ready, monkeypatch):
    data = ready

    class _DeadProc:
        pid = 999999

    monkeypatch.setattr(hive.subprocess, "Popen", lambda cmd, **kw: _DeadProc())
    info = data["h"].submit_async(data["tid"])

    status = hive.submit_job_status("w1", data["tid"])
    assert status and status.get("phase") == "lost", status
    assert data["h"].result_info(data["tid"]) is None, "丢失不许伪造结果"
    assert hive.pid_alive(999999) is False
    assert info["job"]


def test_run_submit_job_detects_lease_takeover(ready):
    """租约被接管后，后台 job 不许再写结果（结果会被 store 拒收）。"""
    data = ready
    path, job = _make_job(data)

    job["lease_id"] = "deadbeef"
    hive.write_submit_job(job)
    rc = data["h"].run_submit_job(path)

    assert rc == 1
    assert data["h"].result_info(data["tid"]) is None
    status = hive.read_submit_job("w1", data["tid"]) or {}
    assert status.get("phase") == "lease_lost", status


def test_verify_timeout_env_is_used(monkeypatch):
    monkeypatch.setenv("HIVE_VERIFY_TIMEOUT", "5")
    assert hive.verify_timeout_seconds() == 5
    monkeypatch.delenv("HIVE_VERIFY_TIMEOUT", raising=False)
    assert hive.verify_timeout_seconds() == 1800


def test_pid_alive_self(monkeypatch):
    """探活不能把当前进程判死（Windows 上尤其不能用 os.kill 探活）。"""
    assert hive.pid_alive(os.getpid()) is True
    assert hive.pid_alive(0) is False
