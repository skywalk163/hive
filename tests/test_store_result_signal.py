"""submit 假成功（T-0296）：结果写 store 失败必须被看见，不能静默丢掉。

现场（T-0250 / T-0252）：集群选举风暴期间 leader 换届，`POST /api/cluster/store/result`
返回 409；`store_result()` 把 `HiveError` 吞成 `None`，`cmd_submit()` 又不看返回值，
于是 agent 侧照常打印 `T-xxxx done by ...` 并返回 0。store 里没有结果行，卡在 Git 上
看着是 done，看板看不到、`close/land` 不执行，租约一过期同一张卡又被派发一遍。

这里钉住修复后的契约：
  1. `store_result()` 失败抛 `ResultWriteError`（保留 kind/code/http），不再返回 None；
  2. 失败按 2s/4s/8s 重试（默认 3 次，总尝试 4 次），每次失败都有日志；
  3. 重试用尽 → `cmd_submit` 返回非 0，**不打印成功回执**（stdout 保持静默），
     并把「git 已写、store 未写」写进 summary.md；
  4. 失败时保留 worktree 与 `res/<tid>` 分支，且**不释放租约**（否则未落库就被重派）；
  5. 重试中途成功 → 正常返回 0 并打印原有格式的成功回执。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import hive
from conftest import sh

VERIFY = f'{sys.executable} -c "print(1)"'


# --------------------------------------------------------------------------- #
# store_result：不再吞异常
# --------------------------------------------------------------------------- #

def _fail(monkeypatch, exc: Exception | None = None):
    """让 `store_call("result", ...)` 永远失败；返回真实调用次数计数器。

    只拦 result 动作：`store_release` 也走 store_call，若一并计数，
    「尝试了几次」就不是结果写入的次数了。
    """
    err = exc or hive.StoreCallError(
        "T-1001 已被 deepseek-win 领取", kind="taken", code="E_TAKEN", http=409)
    calls = {"n": 0}

    def fake(action, payload, timeout=3.0):
        if action == "result":
            calls["n"] += 1
            raise err
        return {"ok": True}

    monkeypatch.setattr(hive, "store_call", fake)
    return calls


def test_store_result_raises_instead_of_returning_none(monkeypatch):
    """旧实现 `except HiveError: return None` —— 调用方无法区分成功/失败。"""
    sleeps: list[float] = []
    monkeypatch.setattr(hive, "_result_write_sleep", sleeps.append)
    calls = _fail(monkeypatch)

    with pytest.raises(hive.ResultWriteError) as e:
        hive.store_result("T-1001", "w1", "L1", {"status": "done"})

    assert calls["n"] == 4, "默认 3 次重试 = 总共 4 次尝试"
    assert sleeps == [2.0, 4.0, 8.0], f"退避应是 2s/4s/8s，实际 {sleeps}"
    assert isinstance(e.value, hive.HiveError), "调用方仍可按 HiveError 接"
    # 原始错误码必须保留，否则排障又只能猜
    assert e.value.kind == "taken" and e.value.code == "E_TAKEN"
    assert e.value.http == 409


def test_store_result_retry_count_is_configurable(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(hive, "_result_write_sleep", sleeps.append)
    monkeypatch.setenv("HIVE_RESULT_WRITE_RETRIES", "2")
    monkeypatch.setenv("HIVE_RESULT_WRITE_BASE", "0.5")
    calls = _fail(monkeypatch)

    with pytest.raises(hive.ResultWriteError):
        hive.store_result("T-1001", "w1", "L1", {"status": "done"})

    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]


def test_store_result_succeeds_after_transient_failure(monkeypatch):
    """换届期间的 409 是瞬时的：重试第二次成功就该静默返回。"""
    sleeps: list[float] = []
    monkeypatch.setattr(hive, "_result_write_sleep", sleeps.append)
    calls = {"n": 0}

    def fake(action, payload, timeout=3.0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise hive.StoreCallError("没有主节点", kind="unavailable", http=409)
        return {"ok": True, "seq": 7}

    monkeypatch.setattr(hive, "store_call", fake)

    out = hive.store_result("T-1001", "w1", "L1", {"status": "done"})
    assert out["ok"] is True and out["seq"] == 7, "成功时应返回 store 原始响应"
    assert calls["n"] == 2
    assert sleeps == [2.0]


def test_store_result_retries_non_storecall_hiveerror(monkeypatch):
    """超时/连接失败被包成普通 HiveError 时也不能跳过重试。"""
    calls = {"n": 0}
    sleeps: list[float] = []
    monkeypatch.setattr(hive, "_result_write_sleep", sleeps.append)

    def fake(action, payload, timeout=3.0):
        calls["n"] += 1
        raise hive.HiveError("命令超时（3 秒内未返回，已终止）: store/result")

    monkeypatch.setattr(hive, "store_call", fake)

    with pytest.raises(hive.ResultWriteError) as e:
        hive.store_result("T-1001", "w1", "L1", {"status": "done"})
    assert calls["n"] == 4 and len(sleeps) == 3
    assert e.value.kind == "other"


# --------------------------------------------------------------------------- #
# cmd_submit：返回码 + 不假成功
# --------------------------------------------------------------------------- #

@pytest.fixture()
def submitted(world, monkeypatch, tmp_path):
    """准备好一张已 take、已改好文件、待 submit 的卡（store 后端）。"""
    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "store")
    monkeypatch.setenv("HIVE_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("HIVE_STATE", str(tmp_path / "state"))
    # 退避不要真等：重试次数仍按默认 3 次（4 次尝试）来断言
    monkeypatch.setattr(hive, "_result_write_sleep", lambda s: None)
    monkeypatch.setattr(hive, "store_call", lambda *a, **kw: {"ok": True, "seq": 1})

    world.add_agent("w1", mode="manual")
    world.add_task("T-1001", title="结果要落 store", paths=("src/main.py",),
                   verify=(VERIFY,))
    # take 走在 store 后端下会打 store：让它走本机 git claim，submit 再走 store
    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "git")
    world.hive("take", "T-1001", "--agent", "w1")
    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "store")

    wt = Path(tmp_path / "state" / "wt" / "T-1001")
    assert wt.exists(), f"take 应该建好 worktree: {wt}"
    (wt / "src" / "main.py").write_text("print('hi')\nprint('T-1001')\n",
                                       encoding="utf-8")
    (wt / ".hive" / "summary.md").write_text("T-1001 小结\n", encoding="utf-8")

    h = hive.Hive(str(world.hive1))
    return {"world": world, "h": h, "wt": wt, "tid": "T-1001"}


def test_cmd_submit_returns_nonzero_and_prints_no_success(
        submitted, monkeypatch, capsys):
    data, tid = submitted, submitted["tid"]
    calls = _fail(monkeypatch)

    rc = data["h"].cmd_submit(tid)

    out = capsys.readouterr().out
    assert rc != 0, "结果没写进 store 就不许返回 0"
    assert f"{tid} done by" not in out, f"失败时绝不能打印成功回执: {out!r}"
    assert "done by" not in out
    assert calls["n"] == 4, "默认 3 次重试 = 4 次尝试"


def test_cmd_submit_keeps_worktree_and_result_branch_on_failure(
        submitted, monkeypatch):
    data, tid = submitted, submitted["tid"]
    _fail(monkeypatch)

    assert data["h"].cmd_submit(tid) != 0

    # git 侧的交付物一个都不能丢
    assert data["wt"].exists(), "失败时 worktree 必须保留，别把 agent 的活删掉"
    assert (data["wt"] / "src" / "main.py").exists()
    refs = data["world"].refs("refs/heads/res/*")
    assert any(r.endswith(f"/{tid}") for r in refs), f"res/{tid} 必须保留: {refs}"
    tree = sh(["git", "--git-dir", str(data["world"].code_origin),
               "ls-tree", "-r", "--name-only", f"task/{tid}"]).stdout
    assert "src/main.py" in tree, "代码分支早已推上去了，失败也不该影响它"


def test_cmd_submit_records_store_failure_in_summary(submitted, monkeypatch):
    """人要能在一页纸上看出「git 已写、store 未写」和下一步。"""
    data, tid = submitted, submitted["tid"]
    _fail(monkeypatch)

    assert data["h"].cmd_submit(tid) != 0
    text = (data["wt"] / ".hive" / "summary.md").read_text(encoding="utf-8")
    assert "T-1001 小结" in text, "agent 自己写的小结不能被覆盖"
    assert "store" in text and "未写入" in text
    assert f"res/{tid}" in text and "hive submit" in text


def test_cmd_submit_marks_result_json_when_store_write_fails(
        submitted, monkeypatch):
    """看板 / close / land 读的是 res/<tid>：失败标记必须出现在那里，不能只在日志里。"""
    import json

    data, tid = submitted, submitted["tid"]
    _fail(monkeypatch)

    assert data["h"].cmd_submit(tid) != 0
    raw = sh(["git", "--git-dir", str(data["world"].hive_origin), "show",
              f"res/{tid}:results/{tid}.json"]).stdout
    res = json.loads(raw)
    assert res["status"] == "done", "Git 侧事实是 done，不能篡改"
    assert "store" in res["store_write_error"], res


def test_cmd_submit_does_not_release_lease_on_failure(submitted, monkeypatch):
    """未落库还被释放 = 卡被重新派发、结果彻底没人认领（T-0250 现场）。"""
    data, tid = submitted, submitted["tid"]
    _fail(monkeypatch)
    released: list = []
    monkeypatch.setattr(hive, "store_release",
                        lambda *a, **kw: released.append((a, kw)))

    assert data["h"].cmd_submit(tid) != 0
    assert released == [], "store 没写进去就绝不释放租约"


def test_cmd_submit_releases_lease_only_after_successful_write(submitted):
    """成功路径：写完 store 才释放租约，且 stdout 格式保持原样。"""
    data, tid = submitted, submitted["tid"]
    order: list[str] = []
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(hive, "store_result",
                            lambda *a, **kw: order.append("store_result") or {"ok": True})
        monkeypatch.setattr(hive, "store_release",
                            lambda *a, **kw: order.append("store_release"))
        rc = data["h"].cmd_submit(tid)
    finally:
        monkeypatch.undo()

    assert rc == 0
    assert order == ["store_result", "store_release"], order


def test_cmd_submit_success_path_format_unchanged(submitted, capsys):
    """成功回执必须逐字保持旧格式（看板/脚本都在解析它）。"""
    data, tid = submitted, submitted["tid"]
    assert data["h"].cmd_submit(tid) == 0

    out = capsys.readouterr().out
    assert f"{tid} done by w1" in out
    assert f"branch: task/{tid}" in out and "verify: 0" in out


def test_cmd_submit_recovers_when_retry_succeeds(submitted, monkeypatch, capsys):
    """换届只是短暂 409：重试成功就照常返回 0，不该把好卡判失败。"""
    data, tid = submitted, submitted["tid"]
    calls = {"n": 0}

    def flaky(action, payload, timeout=3.0):
        if action != "result":
            return {"ok": True}
        calls["n"] += 1
        if calls["n"] == 1:
            raise hive.StoreCallError("没有主节点", kind="unavailable", http=409)
        return {"ok": True, "seq": 1}

    monkeypatch.setattr(hive, "store_call", flaky)

    assert data["h"].cmd_submit(tid) == 0
    assert calls["n"] == 2
    assert f"{tid} done by" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 端到端：CLI 退出码
# --------------------------------------------------------------------------- #

def test_cli_submit_exits_nonzero_when_store_unreachable(world, monkeypatch,
                                                         tmp_path):
    """`hive submit` 的进程退出码必须是 1 —— 调用方靠它判断交付是否真的落地。"""
    monkeypatch.setenv("HIVE_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("HIVE_STATE", str(tmp_path / "state"))
    world.add_agent("w1", mode="manual")
    world.add_task("T-1002", title="CLI 退出码", paths=("src/main.py",),
                   verify=(VERIFY,))
    world.hive("take", "T-1002", "--agent", "w1")
    wt = Path(tmp_path / "state" / "wt" / "T-1002")
    (wt / "src" / "main.py").write_text("print('x')\n", encoding="utf-8")
    (wt / ".hive" / "summary.md").write_text("小结\n", encoding="utf-8")

    # store 后端 + 指向一个没有 leader 的端口：store_call 一定失败
    env = {
        "HIVE_CLAIM_BACKEND": "store",
        "HIVE_STORE_PATH": str(tmp_path / "no-such-store.db"),
        "HIVE_CLUSTER_PORT": "1",
        "HIVE_RESULT_WRITE_RETRIES": "1",
        "HIVE_RESULT_WRITE_BASE": "0.01",
        "HIVE_LEASE_DIR": str(tmp_path / "leases"),
        "HIVE_STATE": str(tmp_path / "state"),
    }
    p = world.hive("submit", "T-1002", check=False, env=env)

    assert p.returncode == 1, f"结果没落 store，退出码必须非 0: {p.returncode}"
    assert "done by" not in p.stdout, p.stdout
    assert "store" in p.stderr
    assert wt.exists(), "失败时 worktree 必须保留"
