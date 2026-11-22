"""T-0435：`hive task list` 不看 res 结果，把「已提交待合入」显示成 assigned。

2026-11-21 主控连合 T-0429/0430/0431，三次都被同一个现象骗到：卡面一直
`assigned`，看着像「agent 推了分支没提交」，其实三张卡都正常 submit 过。
根因是两条读取口径不一致 —— `status` 读 `res/<tid>`，`task list` 只读卡面。

本文件钉住的契约（每条都能反跑）：
1. 卡面 assigned + 结果 done → 显示 `submitted`（不是 assigned）
2. 删掉 res 分支 → 回到 assigned（不许凭空捏出一个显示态）
3. 卡面 done / shelved / blocked 不被展示态盖掉（人做过的决定优先）
4. 结果 failed 也要显形（卡面没有 failed，只能靠推导）
5. `task list` 与 `status --json` 对同一张卡的 state 必须一致（验收红线）
6. 只有租约、还没有结果 → 仍是 running（看板推导不变）
7. `task list --state submitted` 能只列出待合入的卡
"""
from __future__ import annotations

import json

import pytest

import hive


def _put_result(world, tid: str, status: str = "done", agent: str = "fb") -> None:
    """往协调仓推一条 res/<tid> 结果（等价于 agent 跑完 hive_submit）。"""
    h = hive.Hive(str(world.hive1))
    h.fetch()
    result = {
        "task": tid, "agent": agent, "mode": "manual", "status": status,
        "branch": f"task/{tid}", "base": "main", "head": "a" * 40,
        "exit_code": 0 if status == "done" else 1, "timed_out": False,
        "started_at": "2026-11-21T10:00:00+08:00",
        "finished_at": "2026-11-21T10:20:00+08:00",
        "duration_s": 1200.0,
        "verify": [{"cmd": "true", "code": 0, "tail": []}],
        "summary": "做完了", "error": "" if status == "done" else "verify 未通过",
    }
    hive.commit_file_to_ref(
        h.dir, f"results/{tid}.json", json.dumps(result, ensure_ascii=False),
        f"res/{tid}", "origin/main", f"result {tid}: {status}", force=True)
    world.sync()


def _set_card_state(world, tid: str, state: str) -> None:
    """直接改卡面 state 并推上去（模拟「派单时的状态」留在了卡面上）。"""
    p = world.hive1 / "tasks" / f"{tid}.md"
    text = p.read_text(encoding="utf-8")
    lines = [f"state: {state}" if ln.startswith("state:") else ln
             for ln in text.splitlines()]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    hive.Hive(str(world.hive1)).commit_and_push(
        f"plan: {tid} state={state}", [f"tasks/{tid}.md"])
    _reset_hive_cache(world)


def _reset_hive_cache(world) -> None:
    """进程内 ls-remote TTL 只认 SHA：本测试连推好几次 main，必须清掉。"""
    hive._ls_remote_cache.clear()


def _delete_result(world, tid: str) -> None:
    h = hive.Hive(str(world.hive1))
    hive.git_net(["push", "origin", f":refs/heads/res/{tid}"], h.dir)
    world.sync()
    _reset_hive_cache(world)


def _task_row(world, tid: str) -> dict:
    out = json.loads(world.hive("task", "list", "--json").stdout)
    return next(r for r in out if r["id"] == tid)


def _status_row(world, tid: str) -> dict:
    return next(r for r in world.hive_json("status") if r["id"] == tid)


# --------------------------------------------------------------------------- #
# 判据 1：卡面 assigned + 结果 done → submitted
# --------------------------------------------------------------------------- #

def test_task_list_shows_submitted_when_result_done(world):
    """最原始的现场：卡面 assigned，但 res 上有 done —— 不能再显示 assigned。"""
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001", title="已经交了的卡")
    world.hive("assign", "T-0001", "--agent", "fb", check=False)
    _put_result(world, "T-0001", "done")

    # 前提：卡面确实是 assigned（submit 不改卡面）
    assert world.card("T-0001")["state"] == "assigned"

    row = _task_row(world, "T-0001")
    assert row["state"] == "submitted", row
    assert row["card_state"] == "assigned", "卡面原值要能查到，便于对照"
    assert row["result"] == "done"
    assert row["owner"] == "fb"


def test_task_list_text_output_contains_submitted(world):
    """文本列表同样要显示 submitted，且不能显示 assigned。"""
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001")
    _put_result(world, "T-0001", "done")

    out = world.hive("task", "list").stdout
    line = next(l for l in out.splitlines() if l.startswith("T-0001"))
    assert "submitted" in line, line
    assert "assigned" not in line, line


# --------------------------------------------------------------------------- #
# 判据 2：反跑 —— 删掉 res 分支必须回到 assigned
# --------------------------------------------------------------------------- #

def test_reversed_after_result_ref_deleted(world):
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001")
    world.hive("assign", "T-0001", "--agent", "fb", check=False)
    _put_result(world, "T-0001", "done")
    assert _task_row(world, "T-0001")["state"] == "submitted"

    _delete_result(world, "T-0001")
    row = _task_row(world, "T-0001")
    assert row["state"] == "assigned", "结果没了就该回到卡面态，不许凭空显示 submitted"
    assert row["result"] == ""


# --------------------------------------------------------------------------- #
# 判据 3：卡面终态 / 人的决定优先
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("card_state", ["done", "closed", "shelved", "blocked",
                                        "conflict"])
def test_card_state_wins_over_delivery(world, card_state):
    """卡面已经 done/closed/搁置/挂起/冲突时，展示态不许把它改写。"""
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001")
    _set_card_state(world, "T-0001", card_state)
    _put_result(world, "T-0001", "done")

    assert _task_row(world, "T-0001")["state"] == card_state
    assert _status_row(world, "T-0001")["state"] == card_state


def test_failed_card_still_shows_failed(world):
    """done 卡面 + failed 结果：终态优先，不许被翻成 submitted。"""
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001")
    _set_card_state(world, "T-0001", "done")
    _put_result(world, "T-0001", "failed")
    assert _task_row(world, "T-0001")["state"] == "done"


# --------------------------------------------------------------------------- #
# 判据 4：结果 failed 也要显形
# --------------------------------------------------------------------------- #

def test_failed_result_shows_instead_of_assigned(world):
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001")
    world.hive("assign", "T-0001", "--agent", "fb", check=False)
    _put_result(world, "T-0001", "failed")

    row = _task_row(world, "T-0001")
    assert row["state"] == "failed", row
    assert row["card_state"] == "assigned"
    assert row["result"] == "failed"


# --------------------------------------------------------------------------- #
# 判据 5：验收红线 —— task list 与 status 的 state 列必须一致
# --------------------------------------------------------------------------- #

def test_task_list_and_status_agree(world):
    """同一张卡，两条命令的显示态不许打架（本次验收红线）。"""
    world.add_agent("fb", mode="manual")
    for i, (tid, status) in enumerate((("T-0001", "done"), ("T-0002", "failed"))):
        world.add_task(tid, title=f"卡 {i}")
        world.hive("assign", tid, "--agent", "fb", check=False)
        _put_result(world, tid, status)
    world.add_task("T-0003", title="没交的卡")

    listed = {r["id"]: r["state"] for r in json.loads(
        world.hive("task", "list", "--json").stdout)}
    status = {r["id"]: r["state"] for r in world.hive_json("status")}
    for tid in ("T-0001", "T-0002", "T-0003"):
        assert listed[tid] == status[tid], (tid, listed[tid], status[tid])
    assert listed["T-0001"] == "submitted"
    assert listed["T-0002"] == "failed"
    assert listed["T-0003"] == "ready"


# --------------------------------------------------------------------------- #
# 判据 6：租约态不被展示态吞掉
# --------------------------------------------------------------------------- #

def test_live_lease_without_result_still_running(world):
    """只有租约、还没有结果 → running（这是看板推导，不能被展示态改写）。"""
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001")
    h = hive.Hive(str(world.hive1))
    h.fetch()
    hive.commit_file_to_ref(
        h.dir, "claims/T-0001.json",
        json.dumps({"agent": "fb", "lease_id": "L1", "fence": 1,
                    "lease_until": "2099-01-01T00:00:00+00:00"}),
        "claim/T-0001", "origin/main", "fake claim")
    world.sync()

    assert _task_row(world, "T-0001")["state"] == "running"
    assert _status_row(world, "T-0001")["state"] == "running"


# --------------------------------------------------------------------------- #
# 判据 7：--state 过滤（主控「到底交没交」的日常用法）
# --------------------------------------------------------------------------- #

def test_task_list_state_filter(world):
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001", title="交了的")
    world.add_task("T-0002", title="没交的")
    _put_result(world, "T-0001", "done")

    out = json.loads(world.hive("task", "list", "--state", "submitted", "--json").stdout)
    assert [r["id"] for r in out] == ["T-0001"], out

    out = json.loads(world.hive("task", "list", "--state", "ready", "--json").stdout)
    assert [r["id"] for r in out] == ["T-0002"], out


# --------------------------------------------------------------------------- #
# 纯函数内核：delivery_state 的边界
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("card_state,res_status,want", [
    ("assigned", "done", "submitted"),
    ("ready", "done", "submitted"),
    ("running", "done", "submitted"),
    ("assigned", "failed", "failed"),
    ("assigned", "", "assigned"),
    ("ready", "", "ready"),
    ("done", "done", "done"),
    ("done", "failed", "done"),
    ("shelved", "done", "shelved"),
    ("blocked", "done", "blocked"),
])
def test_delivery_state_table(card_state, res_status, want):
    res = {"status": res_status} if res_status else {}
    assert hive.Hive.delivery_state(card_state, res) == want


def test_batch_prime_reads_all_results_in_one_go(world):
    """cards() 预热后，结果读取一次批量完成（不许退回每卡一个 git show）。"""
    world.add_task("T-0001")
    _put_result(world, "T-0001", "done")
    h = hive.Hive(str(world.hive1))
    h.fetch()
    h._ref_cache = None
    cards = h.cards()
    assert "origin/res/T-0001:results/T-0001.json" in h._ref_cache
    assert cards["T-0001"]["state"] == "ready", "cards() 本身不改卡面（也没人领）"
    assert h.result_info("T-0001")["status"] == "done"


# --------------------------------------------------------------------------- #
# 判据 8（T-0438）：交接态/终态 + 未过期租约，不许被翻成 running
# --------------------------------------------------------------------------- #

def _put_claim(world, tid: str, agent: str = "fb") -> None:
    """往协调仓推一条未过期租约（等价于 agent 刚 take）。"""
    h = hive.Hive(str(world.hive1))
    h.fetch()
    hive.commit_file_to_ref(
        h.dir, f"claims/{tid}.json",
        json.dumps({"agent": agent, "lease_id": "L1", "fence": 1,
                    "lease_until": "2099-01-01T00:00:00+00:00"}),
        f"claim/{tid}", "origin/main", f"fake claim {tid}")
    world.sync()


@pytest.mark.parametrize("card_state", ["closed", "conflict", "needs_review"])
def test_handoff_card_state_survives_live_claim(world, card_state):
    """T-0438：`_set_state`/关卡的残留租约不许把人的决定推回 running。"""
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001")
    _set_card_state(world, "T-0001", card_state)
    _put_claim(world, "T-0001")

    assert _task_row(world, "T-0001")["state"] == card_state
    assert _status_row(world, "T-0001")["state"] == card_state


def test_shelved_with_live_claim_not_running_in_mcp(world):
    """T-0438：MCP 的 `_rows` 与 CLI 同源，搁置 + 租约也是 shelved，不是 running。"""
    import mcp_server

    world.add_agent("fb", mode="manual")
    world.add_task("T-0001", title="搁置的")
    _set_card_state(world, "T-0001", "shelved")
    _put_claim(world, "T-0001")

    mcp = mcp_server.HiveMcp(world.hive1, "fb")
    rows = mcp._rows(hive.Hive(str(world.hive1)))
    assert rows[0]["state"] == "shelved", rows


def test_mcp_rows_use_display_state(world):
    """T-0438：MCP 也要显示 submitted/failed，不能还停在卡面 assigned。"""
    import mcp_server

    world.add_agent("fb", mode="manual")
    world.add_task("T-0001", title="交了的")
    world.add_task("T-0002", title="失败的")
    _put_result(world, "T-0001", "done")
    _put_result(world, "T-0002", "failed")

    mcp = mcp_server.HiveMcp(world.hive1, "fb")
    states = {r["id"]: r["state"] for r in mcp._rows(hive.Hive(str(world.hive1)))}
    assert states["T-0001"] == "submitted", states
    assert states["T-0002"] == "failed", states
