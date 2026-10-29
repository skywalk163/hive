"""Worker 基础设施恢复能力（T-0204）：配置热更新 + worktree 缺失 + 单卡异常兜底。

三个回归守卫，都对应 2026-10-29 给 light 仓出卡时当场踩到的两个缺陷：

1. **配置热更新**：Worker.__init__ 只在启动时读一次 agents/<name>.json，之后总控
   往配置里加一个新仓库，正在跑的 worker 完全看不到，每一张该仓的卡都瞬间失败
   （"没有配置仓库 ... 的本地路径"），而且 dispatch / worker 双方都不觉得异常。
   修复：每次 pending() / claim() 前重读配置。
2. **worktree 缺失**：一张 assigned + 有结果 + worktree 目录被回收的卡，worker 想
   resume 时拿不存在的目录去 subprocess.run，FileNotFoundError 直接打死整台机器的
   worker。修复：resume 前校验/重建 worktree，重建失败转 blocked，绝不整进程退出。
3. **异常兜底**：单卡处理抛任意异常，只判这张卡失败，主循环继续处理后续卡。
"""
from __future__ import annotations

import json
import shutil
import sys

from conftest import _tune_bare, sh
from hive import Worker


def _agent_cfg(world, name: str) -> dict:
    p = world.hive1 / "agents" / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _write_agent_cfg(world, name: str, cfg: dict) -> None:
    p = world.hive1 / "agents" / f"{name}.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _recycle_worktree(world, code_repo, wt) -> None:
    """模拟 worktree 被回收：删目录 + 让 git 忘掉注册，但卡仍是 assigned+有结果。"""
    shutil.rmtree(wt, ignore_errors=True)
    sh(["git", "-C", str(code_repo), "worktree", "prune"], check=False)


def _make_mem_repo(world) -> object:
    """建一个独立的 lightmem 代码仓（bare origin + 工作 clone），返回 clone 路径。"""
    seed = world.root / "mem-seed"
    sh(["git", "init", "-q", "-b", "main", seed])
    (seed / "README.md").write_text("# mem\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=seed, env=world.git_env)
    sh(["git", "commit", "-q", "-m", "init"], cwd=seed, env=world.git_env)
    origin = world.root / "mem.git"
    sh(["git", "init", "--bare", "-q", "-b", "main", origin])
    _tune_bare(origin)
    sh(["git", "remote", "add", "origin", origin], cwd=seed, env=world.git_env)
    sh(["git", "push", "-q", "-u", "origin", "main"], cwd=seed, env=world.git_env)
    clone = world.root / "mem-w1"
    sh(["git", "clone", "-q", origin, clone])
    return clone


# --------------------------------------------------------------------------- #
# 1. 配置热更新：新增仓库无需重启即可见（T-0204）
# --------------------------------------------------------------------------- #

def test_config_hot_reload_picks_up_new_repo(world, tmp_path):
    """worker 起来后给 agents/<name>.json 加一个新仓库，不重启，pending() 就能看到。

    修复前：self.cfg 是 __init__ 时读的一次性快照，新增仓库对正在跑的 worker
    完全不可见，该仓的每一张卡都静默秒败。
    """
    world.add_agent("w1", "exec", command="x",
                    repos={"light": world.code_clone("w1")})
    w = Worker(str(world.hive1), "w1", hive_state=str(tmp_path / "state-w1"))
    assert set((w.cfg.get("repos") or {})) == {"light"}

    # 模拟总控给该 agent 加了一个新仓库（改本机 agents/w1.json，已推到 main）
    cfg = _agent_cfg(world, "w1")
    cfg["repos"]["lightmem"] = str(tmp_path / "lightmem")
    _write_agent_cfg(world, "w1", cfg)

    # pending() 触发重读：不重启 worker 也必须看到新仓库
    w.pending()
    assert "lightmem" in (w.cfg.get("repos") or {}), (
        "新增仓库在 pending() 后仍不可见 —— 配置热更新没生效")
    assert (w.cfg["repos"]["lightmem"]) == str(tmp_path / "lightmem")
    # 旧仓库不受影响
    assert w.cfg["repos"]["light"] == str(world.code_clone("w1"))


def test_config_hot_reload_new_repo_task_does_not_silently_fail(
        world, tmp_path, fake_cmd, monkeypatch):
    """端到端：往配置里加新仓库后，不重启，该仓的一张新卡能被正常处理（不再秒败）。

    这是对「静默秒败」最直接的回归：修复前 worker 拿旧配置找不到新仓库，
    每张卡 0 秒失败且双方都不觉得异常。
    """
    monkeypatch.setenv("FAKE_SLEEP", "0")
    mem_clone = _make_mem_repo(world)

    world.add_agent("w1", "exec", command=fake_cmd,
                    repos={"light": world.code_clone("w1")})
    w = Worker(str(world.hive1), "w1", hive_state=str(tmp_path / "state-w1"))

    # 启动后总控加了新仓库
    cfg = _agent_cfg(world, "w1")
    cfg["repos"]["lightmem"] = str(mem_clone)
    _write_agent_cfg(world, "w1", cfg)

    # pending() 触发重读（等价于领卡前刷新配置）
    w.pending()
    assert "lightmem" in (w.cfg.get("repos") or {})

    # 往新仓库里派一张卡
    tid = "T-9201"
    world.add_task(tid, repo="lightmem", paths=["README.md", "feature.txt"],
                   verify=[f"{sys.executable} -c pass"])
    world.hive("assign", tid, "--agent", "w1")

    # 跑这张卡：修复前会 0 秒失败（找不到 lightmem 路径），修复后应能完成
    result = w.run_task(tid)
    assert result["status"] == "done", (
        f"新仓库的卡没有被正确处理：{result.get('error')} —— 配置热更新没生效？")


# --------------------------------------------------------------------------- #
# 2. worktree 缺失：resume 不得崩进程（T-0204）
# --------------------------------------------------------------------------- #

def test_worktree_missing_is_rebuilt_not_crash(
        world, tmp_path, fake_cmd, monkeypatch):
    """assigned + 有结果 + worktree 被回收的卡，resume 时应重建而不是 FileNotFoundError。

    修复前：拿不存在的目录去 subprocess.run，未捕获的 FileNotFoundError 让整台
    机器上的 worker 进程退出，一张状态不一致的卡打死整条流水线。
    """
    monkeypatch.setenv("FAKE_SLEEP", "0")
    world.add_agent("w1", "exec", command=fake_cmd)
    w = Worker(str(world.hive1), "w1", keep_worktree=True,
               hive_state=str(tmp_path / "state-w1"))

    tid = "T-9301"
    world.add_task(tid, repo="light", paths=["feature.txt"],
                   verify=[f"{sys.executable} -c pass"])
    world.hive("assign", tid, "--agent", "w1")

    # 第一次跑：产生结果 + worktree（keep=True 保留下来）
    first = w.run_task(tid)
    assert first["status"] == "done", first
    wt = (w.state / "wt") / tid
    assert wt.exists(), "keep=True 时第一次跑完 worktree 应还在"

    # 模拟 worktree 被回收（gc / 手动清理），但卡仍是 assigned + 有结果
    _recycle_worktree(world, world.code_clone("w1"), wt)
    assert not wt.exists(), "worktree 目录应已不在（模拟回收）"

    # resume：绝不能崩，必须重建 worktree（或转 blocked），而不是拿旧 cwd 去跑
    second = w.run_task(tid)
    assert second["status"] in ("done", "failed", "blocked"), second
    assert wt.exists(), "resume 后 worktree 应被重建（或该卡转 blocked）"
    # 重建成功的话不该报 worktree 相关错误
    assert not second.get("error") or "worktree" not in second.get("error", ""), (
        "不该报 worktree 相关错误 —— 重建应该成功")


def test_worktree_missing_does_not_stop_subsequent_cards(
        world, tmp_path, fake_cmd, monkeypatch):
    """一张 worktree 缺失的卡不会挡住后续的卡（continue 而不是整进程退出）。"""
    monkeypatch.setenv("FAKE_SLEEP", "0")
    world.add_agent("w1", "exec", command=fake_cmd)
    w = Worker(str(world.hive1), "w1", keep_worktree=True,
               hive_state=str(tmp_path / "state-w1"))

    bad, good = "T-9401", "T-9402"
    for tid in (bad, good):
        world.add_task(tid, repo="light", paths=["feature.txt"],
                       verify=[f"{sys.executable} -c pass"])
        world.hive("assign", tid, "--agent", "w1")

    # 先跑 bad 产生结果，然后回收它的 worktree
    w.run_task(bad)
    _recycle_worktree(world, world.code_clone("w1"), (w.state / "wt") / bad)

    # 同一轮处理 [bad, good]：bad 的 worktree 缺失不能挡住 good
    out = w.run_tasks([bad, good])
    assert set(out) == {bad, good}
    assert out[bad]["status"] in ("done", "failed", "blocked"), out[bad]
    assert out[good]["status"] == "done", (
        f"后续卡 {good} 未被处理 —— 一张坏卡挡住了流水线: {out[good]}")
    assert (w.state / "wt" / good).exists(), "后续卡的 worktree 应已建好"


# --------------------------------------------------------------------------- #
# 3. 异常兜底：单卡抛任意异常，主循环继续（T-0204）
# --------------------------------------------------------------------------- #

def test_run_tasks_isolates_arbitrary_exception(world, tmp_path, monkeypatch):
    """单卡处理抛任意异常，只判这张卡失败，后续卡照常完成。"""
    world.add_agent("w1", "exec", command="x")
    w = Worker(str(world.hive1), "w1", hive_state=str(tmp_path / "state-w1"))

    def boom(tid):
        if tid == "T-BAD":
            raise RuntimeError("boom")
        return {"task": tid, "status": "done"}

    monkeypatch.setattr(w, "run_task", boom)
    monkeypatch.setattr(
        w, "_fail",
        lambda tid, card, wt, err: {"task": tid, "status": "failed", "error": err})

    out = w.run_tasks(["T-BAD", "T-GOOD"])
    assert set(out) == {"T-BAD", "T-GOOD"}
    assert out["T-BAD"]["status"] == "failed"
    assert "boom" in out["T-BAD"]["error"]
    assert out["T-GOOD"]["status"] == "done", "后续卡没被处理"


def test_loop_survives_single_card_exception(world, tmp_path, monkeypatch):
    """主循环对单卡异常做兜底：一次 loop(once=True) 不因单卡崩溃而退出。"""
    world.add_agent("w1", "exec", command="x")
    w = Worker(str(world.hive1), "w1", hive_state=str(tmp_path / "state-w1"))

    tid = "T-9501"
    world.add_task(tid, repo="light", verify=[f"{sys.executable} -c pass"])
    world.hive("assign", tid, "--agent", "w1")

    # 单卡处理抛任意异常
    def boom(tid):
        raise RuntimeError("boom")

    monkeypatch.setattr(w, "run_task", boom)
    monkeypatch.setattr(
        w, "_fail",
        lambda tid, card, wt, err: {"task": tid, "status": "failed", "error": err})

    # loop(once=True) 不应抛出（否则测试失败 = 主循环没兜底）
    w.loop(once=True)
