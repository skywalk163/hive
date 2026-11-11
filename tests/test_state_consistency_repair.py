"""状态一致性修复的回归测试（2026-11-11）。

钉住这几天真实踩过的四类问题，防止修完又被改回去：

1. 交付机把 `origin` 配成本机路径裸仓 → 结果写了 done、网络远端却没分支
   （T-0320 / T-0324 / T-0326）。`network_code_remotes` + 结果闸门必须拦住。
2. `close` 不幂等、且只读 `origin/res/<tid>` → 第二次 close 把已完成的卡
   改判成 `closed`（T-0324 的 424a99b cancel）。
3. `land` 不拦 `closed` → 已合入的卡被重复 land，越跑越坏。
4. 交付后主控为解冲突又合了一次 main，结果里的 head 变成分支头的祖先
   （T-0326：bae769e4 → bf778988），land 自洽校验不该误判成「另一棵树」。
"""
from __future__ import annotations

import subprocess

import hive


def _git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if check:
        assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout.strip()


def _commit(repo, msg):
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", msg)


def _state(world, tid: str) -> str:
    for line in (world.hive1 / "tasks" / f"{tid}.md").read_text(
            encoding="utf-8").splitlines():
        if line.startswith("state:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{tid} 卡面没有 state 字段")


# --------------------------------------------------------------------------- #
# 1. 交付侧：识别「本机路径远端」与「网络远端」
# --------------------------------------------------------------------------- #

def test_network_code_remotes_only_network_urls(tmp_path):
    repo = tmp_path / "code"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin", str(tmp_path / "local.git"))
    _git(repo, "remote", "add", "gitea",
         "http://192.168.1.5:3000/skywalk/light.git")
    _git(repo, "remote", "add", "winpath", "G:/github/light")
    _git(repo, "remote", "add", "fileshare", "file:///share/light.git")

    assert hive.network_code_remotes(repo) == ["gitea"]


def test_guard_result_head_rejects_local_path_only_origin(tmp_path, monkeypatch):
    """origin 自洽、网络远端没有分支时，结果不能判 done。"""
    repo = tmp_path / "code"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin", str(tmp_path / "local.git"))
    head = "a" * 40

    monkeypatch.setattr(hive, "network_code_remotes", lambda r: ["gitea"])
    monkeypatch.setattr(
        hive, "remote_branch_head",
        lambda r, ref, remote="origin": head if remote == "origin" else None)

    ok, err = hive.guard_result_head(repo, "T-9001", head, False, ["x.py"])
    assert not ok
    assert "网络远端" in err and "补推" in err


def test_guard_result_head_passes_when_network_remote_has_head(tmp_path,
                                                              monkeypatch):
    repo = tmp_path / "code"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    head = "b" * 40
    monkeypatch.setattr(hive, "network_code_remotes", lambda r: ["gitea"])
    monkeypatch.setattr(hive, "remote_branch_head",
                        lambda r, ref, remote="origin": head)
    ok, err = hive.guard_result_head(repo, "T-9002", head, False, ["x.py"])
    assert ok, err


# --------------------------------------------------------------------------- #
# 2/3. close 幂等、land 拦终态
# --------------------------------------------------------------------------- #

def test_close_twice_is_idempotent_and_never_cancels_done(world):
    """424a99b 现场：第二次 close 读不到已删除的 res 分支，把 done 改判 closed。"""
    world.add_task("T-9003", title="重复 close 不该作废")
    world.hive("close", "T-9003", "--as-done", "--note", "已完成")
    assert _state(world, "T-9003") == "done"

    # 这次没有 res 分支（第一次 close 已删／本就没有结果），旧逻辑必然判作废
    world.hive("close", "T-9003", "--note", "重复执行（不该产生任何改动）")
    assert _state(world, "T-9003") == "done", \
        "终态卡重复 close 必须幂等，绝不能翻回 closed"
    body = (world.hive1 / "tasks" / "T-9003.md").read_text(encoding="utf-8")
    assert "关闭作废" not in body
    assert "重复执行" not in body, "幂等跳过时不应再往卡面追加关闭记录"


def test_land_refuses_cancelled_card(world):
    world.add_task("T-9004", title="已作废的卡不该再 land")
    world.hive("close", "T-9004", "--note", "中途终止")
    assert _state(world, "T-9004") == "closed"

    plan = world.hive_json("land", "T-9004")
    assert plan["status"] == "blocked"
    assert plan["merged"] is False and plan["closed"] is False
    assert any("closed" in p and "reopen" in p for p in plan["problems"])


# --------------------------------------------------------------------------- #
# 4. land 自洽校验：允许「交付 head 是当前分支头的祖先」
# --------------------------------------------------------------------------- #

def _build_branch_history(tmp_path):
    repo = tmp_path / "code"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _commit(repo, "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)

    (repo / "x.py").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _commit(repo, "delivery")
    delivered = _git(repo, "rev-parse", "HEAD")

    (repo / "y.py").write_text("y\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _commit(repo, "merge main to resolve conflict")
    merged = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/task/T-9005", merged)
    return repo, delivered, merged


def test_land_guard_allows_delivery_head_as_ancestor(tmp_path):
    """T-0326：解冲突后分支头变了，但结果仍对应同一棵树 → 放行。"""
    repo, delivered, _merged = _build_branch_history(tmp_path)
    result = {"head": delivered, "changed_files": ["x.py"]}
    ok, err = hive.guard_land_self_consistency(
        repo, "T-9005", "origin/main", result, False)
    assert ok, err


def test_land_guard_still_rejects_foreign_head(tmp_path):
    repo, _delivered, _merged = _build_branch_history(tmp_path)
    result = {"head": "deadbeef" * 5, "changed_files": ["x.py"]}
    ok, err = hive.guard_land_self_consistency(
        repo, "T-9005", "origin/main", result, False)
    assert not ok
    assert "不一致" in err


# --------------------------------------------------------------------------- #
# 5. status 暴露真实可合入信号
# --------------------------------------------------------------------------- #

def test_status_exposes_land_probe_and_stale_fields(world):
    world.add_task("T-9006", title="看板字段")
    rows = world.hive_json("status")
    row = next(r for r in rows if r["id"] == "T-9006")
    assert "land_ready" in row
    assert "land_block" in row
    assert "local_stale" in row


def test_status_marks_result_without_code_branch_as_unlandable(world):
    """结果写了 done、代码分支没推 → 看板必须说「不能合入」，别再叫待合入。"""
    import json
    world.add_agent("a1")
    world.add_task("T-9007", title="只有结果没有代码分支", paths=["x.py"])
    world.sync()
    result = {
        "task": "T-9007", "status": "done", "head": "a" * 40,
        "branch": "task/T-9007", "changed_files": ["x.py"],
        "verify": [{"cmd": "python -m pytest -q", "code": 0}],
    }
    hive.commit_file_to_ref(
        world.hive1, "results/T-9007.json", json.dumps(result),
        "res/T-9007", "origin/main", "result T-9007", force=True)
    world.sync()

    row = next(r for r in world.hive_json("status") if r["id"] == "T-9007")
    assert row["result_status"] == "done"
    assert row["land_ready"] is False
    assert "task/T-9007" in row["land_block"]
