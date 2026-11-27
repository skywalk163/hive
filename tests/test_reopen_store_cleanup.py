"""T-0535：`hive reopen` 必须同时清 store 侧，否则重开后的卡领不动。

T-0497 现场：controller 已经 `reopen` 过，Git 卡面是 `state: ready`，但
`hive_claim` 仍报「T-0497 已关闭」——挡人的不是 Git 卡面，而是 leader store 里
任务还是 `done` 且留着归档结果，claim 的 store 闸门
（`Store.claim` 的 E_CLOSED / E_HAS_RESULT）硬顶。当时只能人工
`store_retry("T-0497")` 才领到卡。

这里钉住四件事：

1. reopen 之后 store 侧是 ready、结果 / 租约 / 归档待办清空，`claim` 立刻能领；
2. 反证：把 store 清理跳过（旧代码行为）→ reopen 后 claim 一定被闸门挡回；
3. 失败不留半状态：store 不可用时 reopen 明确报错，Git 卡面保持终态；
4. git-only 模式 reopen 行为不变（不碰 store，向后兼容）。

store 走的是真 `hive_cluster.Cluster._store_op_for` 的 leader 动作映射 + 真
`Store.commit_op`，只是把 pytest 里被 `_cluster_http` 主动屏蔽的 HTTP 通道
换成进程内直连，避免另写一份会漂移的假协议。
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

import hive as H
import hive_cluster as hc
from hive import Hive, Worker, commit_file_to_ref, parse_card, write_card
from hive_store import Store


# --------------------------------------------------------------------------- #
# 夹具小工具
# --------------------------------------------------------------------------- #
def _leader_bridge(store):
    """把 `hive.store_call` 接到临时 Store：真 leader 映射 + 真 commit。"""
    node = SimpleNamespace(store=store, term=1)

    def call(action, payload, timeout=3.0):
        try:
            out = hc.Cluster._store_op_for(node, action, dict(payload or {}))
        except hc.ClusterError as exc:
            code = str(getattr(exc, "code", "") or "")
            raise H.StoreCallError(
                str(exc), kind=H._classify_store_error(code, str(exc), 409),
                code=code) from exc
        seq = int(out.get("seq") or 0)
        if seq:
            store.commit_op(seq)
        return {"ok": True, **out}

    return call


def _use_store(monkeypatch, db_path) -> None:
    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "store")
    monkeypatch.setenv("HIVE_STORE_PATH", str(db_path))


def _git_close(world, tid: str) -> None:
    """模拟「另一侧已经在 Git 上 close 成 done」（T-0497 的卡面状态）。"""
    p = world.hive1 / "tasks" / f"{tid}.md"
    data, body = parse_card(p)
    data["state"] = "done"
    write_card(p, data, body)
    for cmd in (["add", "-A"], ["commit", "-m", f"close {tid}"],
                ["push", "origin", "main"]):
        subprocess.run(["git", "-C", str(world.hive1), *cmd], check=True,
                       env=world.git_env, capture_output=True)


def _store_done(store, tid: str, agent="agent-x") -> None:
    """store 侧走到 done：claim → result → close（结果行会留在库里等归档）。"""
    store.claim(tid, agent, term=1, lease_minutes=10)
    lease = store.claim_info(tid)["lease_id"]
    store.result(tid, agent, lease, term=1,
                 payload={"status": "done", "summary": "做完了"})
    store.close(tid, done=True, note="land")


def _store_done_fixture(world, tmp_path, tid: str, name="leader.db", title="重开要能领"):
    """建卡 → 灌进 temp store → store 记 done → Git 卡面也 done。"""
    world.add_task(tid, title=title)
    store = Store(tmp_path / name, "n")
    store.ingest_cards(Hive(str(world.hive1)).cards())
    _store_done(store, tid)
    _git_close(world, tid)
    return store


def _seed_res_branch(world, tid: str) -> None:
    """Git 侧留一份 res/<tid> 分支 + 工作区归档结果，reopen 必须一起撤。"""
    commit_file_to_ref(world.hive1, f"results/{tid}.json",
                       json.dumps({"task_id": tid, "status": "done"},
                                  ensure_ascii=False),
                       f"res/{tid}", "origin/main", f"result {tid}")
    (world.hive1 / "results").mkdir(parents=True, exist_ok=True)
    (world.hive1 / "results" / f"{tid}.json").write_text(
        json.dumps({"task_id": tid, "status": "done"}, ensure_ascii=False),
        encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. 主回路：reopen 清 store 之后立刻能领（T-0497 的反例）
# --------------------------------------------------------------------------- #
def test_reopen_clears_store_so_claim_works(world, tmp_path, monkeypatch):
    tid = "T-9001"
    world.add_agent("agent-y", mode="manual")
    store = _store_done_fixture(world, tmp_path, tid)
    _seed_res_branch(world, tid)

    assert store.task(tid)["state"] == "done"
    assert store.result_info(tid) is not None
    assert store.summary()["archive_pending"] >= 1

    _use_store(monkeypatch, tmp_path / "leader.db")
    monkeypatch.setattr(H, "store_call", _leader_bridge(store))

    Hive(str(world.hive1)).cmd_reopen(tid, "误关，继续做")

    # Git 侧照旧
    assert world.card(tid)["state"] == "ready"
    assert world.result_refs(tid) == []
    assert not (world.hive1 / "results" / f"{tid}.json").exists()

    # store 侧：ready、result/claim 清空、没有待归档结果
    assert store.task(tid)["state"] == "ready", store.task(tid)
    assert store.claim_info(tid) is None
    assert store.result_info(tid) is None
    assert store.summary()["archive_pending"] == 0

    # T-0497 就是卡在这一步：reopen 后立刻 claim
    w = Worker(str(world.hive1), "agent-y", hive_state=str(tmp_path / "ws-y"))
    assert w.claim(tid) is True, getattr(w, "last_claim_error", None)
    assert store.claim_info(tid)["owner"] == "agent-y"


# --------------------------------------------------------------------------- #
# 2. 反证：跳过 store 清理 = 旧行为，claim 被 store 闸门挡回
# --------------------------------------------------------------------------- #
def test_claim_is_blocked_when_store_cleanup_skipped(world, tmp_path, monkeypatch):
    tid = "T-9004"
    world.add_agent("agent-z", mode="manual")
    store = _store_done_fixture(world, tmp_path, tid, name="old.db", title="旧行为")

    _use_store(monkeypatch, tmp_path / "old.db")
    monkeypatch.setattr(H, "store_call", _leader_bridge(store))
    # 还原 T-0497 的旧代码：reopen 只改 Git，不清 store
    monkeypatch.setattr(H, "store_reopen_reset", lambda tid, card=None: {})

    Hive(str(world.hive1)).cmd_reopen(tid, "还原旧行为")

    assert world.card(tid)["state"] == "ready"       # Git 看着已经能领
    assert store.task(tid)["state"] == "done"        # store 还是 done

    w = Worker(str(world.hive1), "agent-z", hive_state=str(tmp_path / "ws-z"))
    assert w.claim(tid) is False, "旧行为下 claim 必须被 store 闸门挡住"
    err = w.last_claim_error
    assert isinstance(err, H.StoreCallError) and "已关闭" in str(err)


# --------------------------------------------------------------------------- #
# 3. 失败语义：store 不可用 → 明确报错，Git 卡面一点不动（不留半状态）
# --------------------------------------------------------------------------- #
def test_reopen_reports_store_failure_without_touching_git(world, tmp_path, monkeypatch):
    tid = "T-9003"
    store = _store_done_fixture(world, tmp_path, tid, name="down.db", title="store 挂了")
    _seed_res_branch(world, tid)
    before = (world.hive1 / "tasks" / f"{tid}.md").read_text(encoding="utf-8")

    _use_store(monkeypatch, tmp_path / "down.db")

    def _boom(action, payload, timeout=3.0):
        raise H.StoreCallError("store 未启用", kind="unavailable")

    monkeypatch.setattr(H, "store_call", _boom)

    with pytest.raises(H.HiveError) as ei:
        Hive(str(world.hive1)).cmd_reopen(tid, "重开")

    msg = str(ei.value)
    assert "store 侧清理失败" in msg and "store 未启用" in msg, msg
    assert "Git 卡面未改动" in msg, msg
    # Git 侧必须原样：卡面仍终态、res 分支还在、卡面没写重开留痕
    assert world.card(tid)["state"] == "done"
    assert world.result_refs(tid) != []
    assert (world.hive1 / "tasks" / f"{tid}.md").read_text(encoding="utf-8") == before
    assert store.task(tid)["state"] == "done"


# --------------------------------------------------------------------------- #
# 4. 向后兼容：git-only 模式 reopen 不碰 store
# --------------------------------------------------------------------------- #
def test_git_only_reopen_leaves_store_untouched(world, tmp_path, monkeypatch):
    tid = "T-9005"
    store = _store_done_fixture(world, tmp_path, tid, name="legacy.db", title="git-only")

    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "git")
    monkeypatch.setenv("HIVE_STORE_PATH", str(tmp_path / "legacy.db"))

    def _must_not_call(action, payload, timeout=3.0):
        raise AssertionError(f"git-only 模式不该调 store（{action}）")

    monkeypatch.setattr(H, "store_call", _must_not_call)

    Hive(str(world.hive1)).cmd_reopen(tid, "误关")
    assert world.card(tid)["state"] == "ready"
    assert store.task(tid)["state"] == "done", "git-only 模式不得擅自改 store"


# --------------------------------------------------------------------------- #
# 5. leader store 缺行（起始后新建的卡）：reopen 补登记 + 置回 ready
# --------------------------------------------------------------------------- #
def test_reopen_ingests_card_missing_from_store(world, tmp_path, monkeypatch):
    tid = "T-9002"
    world.add_task(tid, title="leader 没摄入的卡")
    store = Store(tmp_path / "empty.db", "n")   # 故意不 ingest：模拟 leader 起始后新建
    _git_close(world, tid)
    assert store.task(tid) is None

    _use_store(monkeypatch, tmp_path / "empty.db")
    monkeypatch.setattr(H, "store_call", _leader_bridge(store))

    Hive(str(world.hive1)).cmd_reopen(tid, "接着干")

    row = store.task(tid)
    assert row["state"] == "ready", row
    assert row["title"] == "leader 没摄入的卡", "ingest 要把卡面定义补进 store"


# --------------------------------------------------------------------------- #
# 6. 旧版 reopen 留下的半状态（Git ready / store done）要有明确说明，不静默
# --------------------------------------------------------------------------- #
def test_non_terminal_card_with_store_done_is_explained(world, tmp_path, monkeypatch):
    tid = "T-9006"
    world.add_task(tid, title="两侧不一致")
    store = Store(tmp_path / "mismatch.db", "n")
    store.ingest_cards(Hive(str(world.hive1)).cards())
    _store_done(store, tid)             # store 已 done 且留着结果，Git 卡面仍是 ready

    _use_store(monkeypatch, tmp_path / "mismatch.db")

    with pytest.raises(H.HiveError) as ei:
        Hive(str(world.hive1)).cmd_reopen(tid, "重开")

    msg = str(ei.value)
    assert "只有已完成" in msg, msg                 # 仍按终态卡判定拒绝
    assert "两侧不一致" in msg and "store" in msg, msg   # 但把真正的原因说清楚

