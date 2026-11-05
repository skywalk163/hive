"""任务计划：存储规则、HTTP 接口、权限、留言指令、持久化。

分三层测：
* plan_store 纯函数/领域逻辑段（排序、状态机、软删、进度、自动完成、指令解析）——
  不起服务器，跑得快，改规则时先看这里红不红；
* /api/plans 接口段 —— 复用 test_dashboard_auth 的 server/token 夹具，
  验证「创建者或 controller 才能写」「删除需确认或管理员」「全部写操作留审计」；
* /api/chat 的计划指令段 —— 验证留言能建/改/关/删，解析失败给正确格式，
  权限不足被拒且不留脏数据。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
sys.path.insert(0, str(DASH))

import plan_store as ps  # noqa: E402
import serve  # noqa: E402
from test_dashboard_auth import _post, _serve, audit, status_file, tokens  # noqa: E402,F401


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_cluster(monkeypatch):
    """别让测试碰到本机真集群。

    /api/chat 的写入会先问 `http://127.0.0.1:<cluster_port>`；开发机上集群
    常常真在跑，测试留言就会灌进所有人的大厅频道（第一批用例就踩过这个坑）。
    指到一个没人监听的端口，serve 自然降级到本地 chat.jsonl。
    """
    monkeypatch.setenv("HIVE_CLUSTER_PORT", "9")


@pytest.fixture()
def plans_file(tmp_path, monkeypatch):
    """每个用例一份独立计划文件，且让 serve 侧的默认路径也指过去。"""
    path = tmp_path / "plans.json"
    monkeypatch.setenv("HIVE_PLANS_FILE", str(path))
    return path


@pytest.fixture()
def status_full(tmp_path):
    """带若干任务的快照：T-0001 done、T-0002 待领取、T-0003 待合并。"""
    rows = [
        {"id": "T-0001", "state": "done", "result_status": "done"},
        {"id": "T-0002", "state": "ready", "result_status": ""},
        {"id": "T-0003", "state": "assigned", "result_status": "done"},
    ]
    p = tmp_path / "status.json"
    p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return p


CTRL = {"agent": "board-ctrl", "role": "controller"}
WORKER = {"agent": "alice", "role": "worker"}
OTHER = {"agent": "bob", "role": "worker"}


def _mk(prefix="PAY", title="支付改造", tasks=None, who=WORKER, path=None, **kw):
    return ps.create_plan(prefix, title, kw.pop("description", ""),
                          task_ids=tasks or [], created_by=who["agent"], path=path, **kw)


# --------------------------------------------------------------------------- #
# 存储与字段
# --------------------------------------------------------------------------- #
def test_create_assigns_sequential_id_and_fields(plans_file):
    a = _mk(path=plans_file)
    b = _mk("INFRA", "基础设施", path=plans_file)
    assert a["id"] == "P-0001" and b["id"] == "P-0002"
    assert a["status"] == "active" and a["status_label"] == "进行中"
    for key in ("id", "prefix", "title", "description", "status", "pinned",
                "task_ids", "created_by", "created_at", "updated_at", "completed_at"):
        assert key in a, "计划对象缺字段 %s" % key
    assert a["created_by"] == "alice"


def test_create_validates_prefix_title_and_tasks(plans_file):
    for args in (("", "标题"), ("PAY", "  ")):
        with pytest.raises(ps.PlanError):
            ps.create_plan(args[0], args[1], path=plans_file)
    with pytest.raises(ps.PlanError):
        ps.create_plan("PAY", "标题", task_ids=["not-a-task"], path=plans_file)


def test_task_ids_deduped_and_uppercased(plans_file):
    plan = _mk(tasks=["t-0001", "T-0001", "T-0002"], path=plans_file)
    assert plan["task_ids"] == ["T-0001", "T-0002"]


def test_persistence_roundtrip(plans_file):
    """刷新后数据仍在：重新读文件即可看到同一条计划（含新建后的修改）。"""
    plan = _mk(tasks=["T-0001"], path=plans_file)
    ps.update_plan(plan["id"], WORKER, {"title": "支付改造（二期）", "description": "加退款"},
                   path=plans_file)
    again = ps.get_plan(plan["id"], {}, path=plans_file)
    assert again and again["title"] == "支付改造（二期）"
    assert again["description"] == "加退款"
    raw = json.loads(plans_file.read_text(encoding="utf-8"))
    assert raw["plans"][0]["id"] == plan["id"]


def test_corrupt_file_degrades_to_empty(tmp_path):
    p = tmp_path / "plans.json"
    p.write_text("{ not json", encoding="utf-8")
    assert ps.load_all(p)["plans"] == []
    # 坏文件不该阻止新建（新建会整份重写）
    plan = ps.create_plan("PAY", "标题", path=p)
    assert plan["id"] == "P-0001"


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
def test_complete_moves_to_completed_with_timestamp(plans_file):
    plan = _mk(path=plans_file)
    done = ps.complete_plan(plan["id"], WORKER, path=plans_file)
    assert done["status"] == "completed" and done["completed_at"]
    assert done["pinned"] is False       # 离开进行中自动撤销置顶


def test_complete_is_idempotent(plans_file):
    plan = _mk(path=plans_file)
    first = ps.complete_plan(plan["id"], WORKER, path=plans_file)
    second = ps.complete_plan(plan["id"], WORKER, path=plans_file)
    assert first["completed_at"] == second["completed_at"]


def test_close_keeps_plan_visible_with_closed_flag(plans_file):
    plan = _mk(path=plans_file)
    closed = ps.close_plan(plan["id"], WORKER, path=plans_file)
    assert closed["status"] == "closed" and closed["closed_at"]
    assert closed["completed_at"] == ""
    listed = ps.list_plans({}, path=plans_file)
    assert [p["id"] for p in listed] == [plan["id"]]


def test_delete_is_soft_and_hidden_by_default(plans_file):
    plan = _mk(path=plans_file)
    ps.delete_plan(plan["id"], CTRL, confirm=True, path=plans_file)
    assert ps.list_plans({}, path=plans_file) == []
    # 记录仍在文件里（软删除，可追溯）
    raw = json.loads(plans_file.read_text(encoding="utf-8"))
    assert raw["plans"][0]["status"] == "deleted"
    assert raw["plans"][0]["deleted_at"]
    # 显式要求时才出现
    assert ps.list_plans({}, include_deleted=True, path=plans_file)[0]["status"] == "deleted"


def test_delete_requires_confirm_or_admin(plans_file):
    plan = _mk(path=plans_file)
    with pytest.raises(ps.PlanError) as exc:
        ps.delete_plan(plan["id"], WORKER, path=plans_file)
    assert exc.value.code == 409 and "确认" in exc.value.message
    # controller 免确认
    ps.delete_plan(plan["id"], CTRL, path=plans_file)
    assert ps.list_plans({}, path=plans_file) == []


def test_deleted_plan_is_terminal(plans_file):
    plan = _mk(path=plans_file)
    ps.delete_plan(plan["id"], CTRL, path=plans_file)
    for call in (lambda: ps.update_plan(plan["id"], CTRL, {"title": "x"}, path=plans_file),
                 lambda: ps.complete_plan(plan["id"], CTRL, path=plans_file)):
        with pytest.raises(ps.PlanError) as exc:
            call()
        assert exc.value.code == 409


def test_unknown_plan_id(plans_file):
    with pytest.raises(ps.PlanError) as exc:
        ps.complete_plan("P-9999", CTRL, path=plans_file)
    assert exc.value.code == 404
    with pytest.raises(ps.PlanError):
        ps.complete_plan("../../etc/passwd", CTRL, path=plans_file)


# --------------------------------------------------------------------------- #
# 权限
# --------------------------------------------------------------------------- #
def test_creator_can_write_others_cannot(plans_file):
    plan = _mk(who=WORKER, path=plans_file)
    ps.update_plan(plan["id"], WORKER, {"title": "改一下"}, path=plans_file)
    with pytest.raises(ps.PlanError) as exc:
        ps.update_plan(plan["id"], OTHER, {"title": "别人改"}, path=plans_file)
    assert exc.value.code == 403
    assert "alice" in exc.value.message


def test_controller_can_write_any_plan(plans_file):
    plan = _mk(who=WORKER, path=plans_file)
    out = ps.update_plan(plan["id"], CTRL, {"title": "总控改"}, path=plans_file)
    assert out["title"] == "总控改"


def test_plain_members_can_read(plans_file):
    _mk(who=WORKER, path=plans_file)
    assert len(ps.list_plans({}, path=plans_file)) == 1     # 不需要身份即可读


# --------------------------------------------------------------------------- #
# 排序
# --------------------------------------------------------------------------- #
def test_active_sorting_pinned_first_then_updated_desc(plans_file):
    a = _mk("A", "最早更新", path=plans_file)
    b = _mk("B", "最新更新", path=plans_file)
    c = _mk("C", "置顶的", path=plans_file)
    ps.update_plan(b["id"], CTRL, {"title": "最新更新 2"}, path=plans_file)
    ps.set_pinned(c["id"], CTRL, True, path=plans_file)
    ids = [p["id"] for p in ps.list_plans({}, path=plans_file)]
    assert ids[0] == c["id"], "置顶计划必须排最前：%s" % ids
    assert ids[1] == b["id"] and ids[2] == a["id"], "其余按更新时间倒序：%s" % ids


def test_completed_sorted_by_completed_at_desc(plans_file):
    a = _mk("A", "先完成", path=plans_file)
    b = _mk("B", "后完成", path=plans_file)
    ps.complete_plan(a["id"], CTRL, path=plans_file)
    ps.complete_plan(b["id"], CTRL, path=plans_file)
    # 手工把完成时间拉开，避免同秒并列
    data = ps.load_all(plans_file)
    data["plans"][0]["completed_at"] = "2026-01-01T10:00:00+00:00"
    data["plans"][1]["completed_at"] = "2026-02-01T10:00:00+00:00"
    ps.save_all(data, plans_file)
    ids = [p["id"] for p in ps.list_plans({}, path=plans_file)]
    assert ids[0] == b["id"] and ids[1] == a["id"]


def test_active_group_comes_before_completed(plans_file):
    a = _mk("A", "进行中", path=plans_file)
    b = _mk("B", "已完成", path=plans_file)
    ps.complete_plan(b["id"], CTRL, path=plans_file)
    listed = ps.list_plans({}, path=plans_file)
    assert [p["status"] for p in listed] == ["active", "completed"]


def test_pin_ignored_for_non_active(plans_file):
    plan = _mk(path=plans_file)
    ps.complete_plan(plan["id"], CTRL, path=plans_file)
    out = ps.set_pinned(plan["id"], CTRL, True, path=plans_file)
    assert out["pinned"] is False, "置顶只对进行中生效"


# --------------------------------------------------------------------------- #
# 进度与自动完成
# --------------------------------------------------------------------------- #
def test_progress_counts_done_and_pending():
    plan = {"task_ids": ["T-0001", "T-0002", "T-0003"], "status": "active"}
    tasks = {"T-0001": {"state": "done"},
             "T-0002": {"state": "ready"},
             "T-0003": {"state": "assigned", "result_status": "done"}}
    g = ps.progress_of(plan, tasks)
    assert g["total"] == 3 and g["done"] == 2
    assert g["rate"] == pytest.approx(66.7, abs=0.1)


def test_progress_reports_missing_tasks():
    plan = {"task_ids": ["T-0001", "T-0404"], "status": "active"}
    g = ps.progress_of(plan, {"T-0001": {"state": "done"}})
    assert g["total"] == 2 and g["done"] == 1
    assert g["missing"] == ["T-0404"] and g["known"] == 1


def test_progress_empty_plan_is_zero():
    g = ps.progress_of({"task_ids": []}, {})
    assert g == {"total": 0, "done": 0, "rate": 0.0, "missing": [], "known": 0}


def test_auto_complete_when_all_tasks_done(plans_file):
    plan = _mk(tasks=["T-0001", "T-0003"], path=plans_file)
    tasks = {"T-0001": {"state": "done"},
             "T-0003": {"state": "assigned", "result_status": "done"}}
    ps.sync_progress(tasks, path=plans_file)
    assert ps.get_plan(plan["id"], tasks, path=plans_file)["status"] == "completed"


def test_no_auto_complete_while_task_pending(plans_file):
    plan = _mk(tasks=["T-0001", "T-0002"], path=plans_file)
    tasks = {"T-0001": {"state": "done"}, "T-0002": {"state": "ready"}}
    ps.sync_progress(tasks, path=plans_file)
    assert ps.get_plan(plan["id"], tasks, path=plans_file)["status"] == "active"


def test_auto_complete_can_be_disabled(plans_file):
    plan = _mk(tasks=["T-0001"], auto_complete=False, path=plans_file)
    tasks = {"T-0001": {"state": "done"}}
    ps.sync_progress(tasks, path=plans_file)
    assert ps.get_plan(plan["id"], tasks, path=plans_file)["status"] == "active"


def test_auto_complete_requires_tasks(plans_file):
    plan = _mk(tasks=[], path=plans_file)
    ps.sync_progress({}, path=plans_file)
    assert ps.get_plan(plan["id"], {}, path=plans_file)["status"] == "active"


def test_sync_progress_returns_map(plans_file):
    a = _mk("A", "有任务", tasks=["T-0001"], path=plans_file)
    b = _mk("B", "无任务", path=plans_file)
    out = ps.sync_progress({"T-0001": {"state": "done"}}, path=plans_file)
    assert out[a["id"]]["done"] == 1 and out[b["id"]]["total"] == 0


# --------------------------------------------------------------------------- #
# 留言指令解析
# --------------------------------------------------------------------------- #
def test_parse_create():
    cmd = ps.parse_command("/plan create PAY 支付链路改造")
    assert cmd == {"action": "create", "prefix": "PAY", "title": "支付链路改造",
                   "description": ""}


def test_parse_create_keeps_spaces_in_title():
    cmd = ps.parse_command("/plan create PAY 支付 二期 联调")
    assert cmd["title"] == "支付 二期 联调"


def test_parse_create_with_fields_and_tasks():
    """agent 式写法：一条命令就带上关联任务与置顶（标题里允许空格）。"""
    cmd = ps.parse_command(
        "@command:plan create 前缀=PAY 标题=支付 二期 关联=T-0001,T-0002 置顶=on")
    assert cmd["action"] == "create" and cmd["prefix"] == "PAY"
    assert cmd["title"] == "支付 二期"
    assert cmd["task_ids"] == ["T-0001", "T-0002"] and cmd["pinned"] is True


def test_run_command_create_with_tasks_then_autocompletes(plans_file):
    out = ps.run_command("@command:plan create 前缀=PAY 标题=支付 关联=T-0001",
                         WORKER, path=plans_file)
    assert out["ok"] and out["plan"]["task_ids"] == ["T-0001"]
    ps.sync_progress({"T-0001": {"state": "done"}}, path=plans_file)
    assert ps.get_plan(out["plan"]["id"], {}, path=plans_file)["status"] == "completed"


@pytest.mark.parametrize("text", [
    "/plan update P-0001 标题=新标题 置顶=on",
    "@command:plan update P-0001 标题=新标题 置顶=on",
])
def test_parse_update_both_syntaxes(text):
    cmd = ps.parse_command(text)
    assert cmd["action"] == "update" and cmd["plan_id"] == "P-0001"
    assert cmd["fields"] == {"title": "新标题", "pinned": True}


def test_parse_update_tasks_and_auto_complete():
    cmd = ps.parse_command("@command:plan update P-0001 关联=T-0001,T-0002 自动完成=off")
    assert cmd["fields"]["task_ids"] == ["T-0001", "T-0002"]
    assert cmd["fields"]["auto_complete"] is False


def test_parse_simple_actions():
    assert ps.parse_command("@command:plan complete P-0002")["action"] == "complete"
    assert ps.parse_command("@command:plan close P-0002")["action"] == "close"
    assert ps.parse_command("@command:plan delete P-0002")["action"] == "delete"
    assert ps.parse_command("@command:plan pin P-0002")["pinned"] is True
    assert ps.parse_command("@command:plan unpin P-0002")["pinned"] is False


def test_non_plan_text_is_not_a_command():
    assert ps.parse_command("这张卡我理解成改判据") is None
    assert ps.looks_like_plan_command("这卡要 /plan 一下") is False
    assert ps.looks_like_plan_command("/plan create A b") is True


@pytest.mark.parametrize("bad", [
    "/plan",
    "/plan create",
    "/plan create PAY",
    "/plan complete",
    "/plan update P-0001",
    "/plan update P-0001 未知字段=x",
    "/plan update P-0001 置顶=也许",
    "/plan frobnicate P-0001",
])
def test_parse_errors_carry_usage(bad):
    with pytest.raises(ps.PlanError) as exc:
        ps.parse_command(bad)
    assert "/plan create" in exc.value.message, "报错必须给出正确格式：%s" % exc.value.message


# --------------------------------------------------------------------------- #
# 指令执行（领域层）
# --------------------------------------------------------------------------- #
def test_run_command_create_and_update(plans_file):
    out = ps.run_command("/plan create PAY 支付改造", WORKER, path=plans_file)
    assert out["ok"] and out["plan"]["id"] == "P-0001"
    pid = out["plan"]["id"]
    out = ps.run_command("@command:plan update %s 标题=支付二期 关联=T-0001" % pid,
                         WORKER, path=plans_file)
    assert out["ok"] and out["plan"]["title"] == "支付二期"
    assert out["plan"]["task_ids"] == ["T-0001"]


def test_run_command_complete_and_close(plans_file):
    pid = _mk(path=plans_file)["id"]
    assert ps.run_command("@command:plan complete %s" % pid, WORKER, path=plans_file)["ok"]
    pid2 = _mk("B", "第二个", path=plans_file)["id"]
    assert ps.run_command("@command:plan close %s" % pid2, WORKER, path=plans_file)["ok"]
    got = {p["id"]: p["status"] for p in ps.list_plans({}, path=plans_file)}
    assert got == {pid: "completed", pid2: "closed"}


def test_run_command_update_denied_for_others(plans_file):
    pid = _mk(who=WORKER, path=plans_file)["id"]
    out = ps.run_command("@command:plan update %s 标题=抢改" % pid, OTHER, path=plans_file)
    assert out["ok"] is False and out["code"] == 403
    assert ps.get_plan(pid, {}, path=plans_file)["title"] == "支付改造"


def test_run_command_delete_requires_controller(plans_file):
    pid = _mk(who=WORKER, path=plans_file)["id"]
    out = ps.run_command("@command:plan delete %s" % pid, WORKER, path=plans_file)
    assert out["ok"] is False and out["code"] == 403
    assert "controller" in out["error"]
    assert len(ps.list_plans({}, path=plans_file)) == 1, "被拒绝时不能删掉"
    out = ps.run_command("@command:plan delete %s" % pid, CTRL, path=plans_file)
    assert out["ok"] and ps.list_plans({}, path=plans_file) == []


def test_run_command_bad_format_returns_usage(plans_file):
    out = ps.run_command("/plan create PAY", WORKER, path=plans_file)
    assert out["ok"] is False and "/plan create" in out["usage"]


# --------------------------------------------------------------------------- #
# HTTP：/api/plans
# --------------------------------------------------------------------------- #
def _serve_with_plans(status_path, write_auth="on"):
    return _serve(status_path, write_auth)


def test_api_get_lists_with_progress(status_full, plans_file):
    _mk(tasks=["T-0001", "T-0002"], path=plans_file)
    base, shutdown = _serve_with_plans(status_full)
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/api/plans", timeout=10) as r:
            body = json.loads(r.read().decode("utf-8"))
    finally:
        shutdown()
    assert body["ok"] and len(body["plans"]) == 1
    plan = body["plans"][0]
    assert plan["progress"]["total"] == 2 and plan["progress"]["done"] == 1
    assert plan["progress"]["rate"] == 50.0
    assert body["counts"]["active"] == 1


def test_api_get_autocompletes_on_read(status_full, plans_file):
    plan = _mk(tasks=["T-0001", "T-0003"], path=plans_file)
    base, shutdown = _serve_with_plans(status_full)
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/api/plans", timeout=10) as r:
            body = json.loads(r.read().decode("utf-8"))
    finally:
        shutdown()
    assert body["plans"][0]["status"] == "completed"
    assert body["counts"] == {"active": 0, "completed": 1, "closed": 0}
    assert ps.get_plan(plan["id"], {}, path=plans_file)["status"] == "completed"


def test_api_get_autocomplete_writes_audit_row(status_full, plans_file, audit):
    _mk(tasks=["T-0001", "T-0003"], path=plans_file)
    base, shutdown = _serve_with_plans(status_full)
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/api/plans", timeout=10) as r:
            r.read()
    finally:
        shutdown()
    rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
    auto = [r for r in rows if r.get("action") == "plan.auto-complete"]
    assert auto and auto[0]["ok"] and auto[0]["agent"] == "rule"


def test_api_create_by_controller_audited(status_full, plans_file, audit, tokens):
    ctrl, _worker = tokens
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/plans",
                              {"action": "create", "prefix": "PAY", "title": "支付改造",
                               "task_ids": ["T-0001"]},
                              {"X-Hive-Token": ctrl})
    finally:
        shutdown()
    assert code == 200 and body["ok"], (code, body)
    assert body["plan"]["created_by"] == "board-ctrl"
    rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
    ok = [r for r in rows if r.get("action") == "plan.create" and r.get("ok")]
    assert ok and ok[-1]["agent"] == "board-ctrl"


def test_api_requires_token_when_auth_on(status_full, plans_file):
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/plans",
                              {"action": "create", "prefix": "P", "title": "T"})
    finally:
        shutdown()
    assert code == 401, (code, body)


def test_api_worker_can_edit_own_plan(status_full, plans_file, tokens):
    """worker 通过接口建的 → created_by 就是它的 agent 名 → 能改；controller 也能改。"""
    ctrl, worker = tokens
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/plans",
                              {"action": "create", "prefix": "PAY", "title": "工人自己的"},
                              {"X-Hive-Token": worker})
        assert code == 200 and body["plan"]["created_by"] == "board-worker", (code, body)
        pid = body["plan"]["id"]
        code, body, _ = _post(base, "/api/plans",
                              {"action": "update", "plan_id": pid,
                               "fields": {"title": "工人改"}},
                              {"X-Hive-Token": worker})
        assert code == 200 and body["plan"]["title"] == "工人改", (code, body)
        code, body, _ = _post(base, "/api/plans",
                              {"action": "update", "plan_id": pid,
                               "fields": {"title": "总控改"}},
                              {"X-Hive-Token": ctrl})
        assert code == 200 and body["plan"]["title"] == "总控改", (code, body)
    finally:
        shutdown()


def test_api_update_denied_for_non_owner(status_full, plans_file, tokens):
    _ctrl, worker = tokens
    plan = ps.create_plan("PAY", "别人的", created_by="someone-else", path=plans_file)
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/plans",
                              {"action": "complete", "plan_id": plan["id"]},
                              {"X-Hive-Token": worker})
    finally:
        shutdown()
    assert code == 403 and "创建者" in body["error"], (code, body)


def test_api_delete_confirm_required(status_full, plans_file, audit, tokens):
    ctrl, worker = tokens
    plan = ps.create_plan("PAY", "待删", created_by="board-worker", path=plans_file)
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/plans",
                              {"action": "delete", "plan_id": plan["id"]},
                              {"X-Hive-Token": worker})
        assert code == 409 and "确认" in body["error"], (code, body)
        code, body, _ = _post(base, "/api/plans",
                              {"action": "delete", "plan_id": plan["id"], "confirm": True},
                              {"X-Hive-Token": worker})
        assert code == 200 and body["ok"], (code, body)
        assert ps.list_plans({}, path=plans_file) == []
        rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
        assert any(r.get("action") == "plan.delete" and not r.get("ok") for r in rows)
    finally:
        shutdown()


def test_api_pin_and_close(status_full, plans_file, tokens):
    ctrl, _worker = tokens
    plan = ps.create_plan("PAY", "置顶测试", created_by="board-ctrl", path=plans_file)
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/plans",
                              {"action": "pin", "plan_id": plan["id"]},
                              {"X-Hive-Token": ctrl})
        assert code == 200 and body["plan"]["pinned"] is True
        code, body, _ = _post(base, "/api/plans",
                              {"action": "close", "plan_id": plan["id"]},
                              {"X-Hive-Token": ctrl})
        assert code == 200 and body["plan"]["status"] == "closed"
        assert body["plan"]["pinned"] is False
    finally:
        shutdown()


def test_api_unknown_action_and_plan(status_full, plans_file, tokens):
    ctrl, _worker = tokens
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/plans",
                              {"action": "explode", "plan_id": "P-0001"},
                              {"X-Hive-Token": ctrl})
        assert code == 400 and "不支持" in body["error"]
        code, body, _ = _post(base, "/api/plans",
                              {"action": "complete", "plan_id": "P-9999"},
                              {"X-Hive-Token": ctrl})
        assert code == 404
    finally:
        shutdown()


def test_api_plans_readable_without_token(status_full, plans_file):
    _mk(path=plans_file)
    base, shutdown = _serve_with_plans(status_full)
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/api/plans", timeout=10) as r:
            assert r.status == 200
            assert json.loads(r.read().decode("utf-8"))["plans"]
    finally:
        shutdown()


def test_api_get_survives_broken_status_file(tmp_path, plans_file):
    """任务快照坏了，计划仍要能读（进度按 0 / missing 处理，不 500）。"""
    bad = tmp_path / "broken.json"
    bad.write_text("{ nope", encoding="utf-8")
    _mk(tasks=["T-0001"], path=plans_file)
    base, shutdown = _serve_with_plans(bad)
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/api/plans", timeout=10) as r:
            body = json.loads(r.read().decode("utf-8"))
    finally:
        shutdown()
    assert body["ok"] and body["plans"][0]["progress"]["missing"] == ["T-0001"]


# --------------------------------------------------------------------------- #
# HTTP：/api/chat 的计划指令
# --------------------------------------------------------------------------- #
def test_chat_command_creates_plan(status_full, plans_file, tokens, audit):
    ctrl, _worker = tokens
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/chat",
                              {"text": "/plan create PAY 支付链路改造", "scope": "hall"},
                              {"X-Hive-Token": ctrl})
    finally:
        shutdown()
    assert code == 200 and body["ok"] and body.get("plan_command"), (code, body)
    assert body["plan"]["prefix"] == "PAY"
    assert len(ps.list_plans({}, path=plans_file)) == 1
    assert body["reply"].startswith("📋")
    rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
    assert any(r.get("action") == "chat.plan-cmd" and r.get("ok") for r in rows)


def test_chat_command_update_complete_close(status_full, plans_file, tokens):
    ctrl, _worker = tokens
    a = _mk("PAY", "一", path=plans_file)["id"]
    b = _mk("INF", "二", path=plans_file)["id"]
    c = _mk("DOC", "三", path=plans_file)["id"]
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/chat",
                              {"text": "@command:plan update %s 标题=改过的 关联=T-0001" % a,
                               "scope": "hall"}, {"X-Hive-Token": ctrl})
        assert code == 200 and body["ok"], (code, body)
        assert ps.get_plan(a, {}, path=plans_file)["title"] == "改过的"
        assert ps.get_plan(a, {}, path=plans_file)["task_ids"] == ["T-0001"]

        code, body, _ = _post(base, "/api/chat",
                              {"text": "@command:plan complete %s" % a, "scope": "hall"},
                              {"X-Hive-Token": ctrl})
        assert code == 200 and ps.get_plan(a, {}, path=plans_file)["status"] == "completed"

        code, body, _ = _post(base, "/api/chat",
                              {"text": "@command:plan close %s" % b, "scope": "hall"},
                              {"X-Hive-Token": ctrl})
        assert code == 200 and ps.get_plan(b, {}, path=plans_file)["status"] == "closed"
        assert ps.get_plan(c, {}, path=plans_file)["status"] == "active", "别的计划不受影响"
    finally:
        shutdown()


def test_chat_command_delete_needs_controller(status_full, plans_file, tokens):
    _ctrl, worker = tokens
    plan = _mk(who={"agent": "board-worker"}, path=plans_file)
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/chat",
                              {"text": "@command:plan delete %s" % plan["id"], "scope": "hall"},
                              {"X-Hive-Token": worker})
    finally:
        shutdown()
    assert code == 403, (code, body)
    assert "controller" in body["error"]
    assert len(ps.list_plans({}, path=plans_file)) == 1


def test_chat_command_bad_format_returns_usage(status_full, plans_file, tokens):
    ctrl, _worker = tokens
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/chat",
                              {"text": "/plan create PAY", "scope": "hall"},
                              {"X-Hive-Token": ctrl})
    finally:
        shutdown()
    assert code == 400
    assert "/plan create" in body["usage"], body


def test_chat_command_unknown_plan_is_404(status_full, plans_file, tokens):
    ctrl, _worker = tokens
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/chat",
                              {"text": "@command:plan complete P-0404", "scope": "hall"},
                              {"X-Hive-Token": ctrl})
    finally:
        shutdown()
    assert code == 404 and "P-0404" in body["error"]


# --------------------------------------------------------------------------- #
# 集群留言里的指令（agent 走 hive chat post / MCP，不经过看板输入框）
# --------------------------------------------------------------------------- #
def test_inbound_commands_from_cluster_are_applied(plans_file):
    pid = _mk(who={"agent": "alice"}, path=plans_file)["id"]
    msgs = [{"id": "m1", "from": "alice", "scope": "hall",
             "text": "@command:plan complete %s" % pid},
            {"id": "m2", "from": "alice", "scope": "hall", "text": "顺手说一句，不是指令"}]
    out = ps.apply_inbound(msgs, {}, path=plans_file)
    assert len(out) == 1 and out[0]["ok"], out
    assert ps.get_plan(pid, {}, path=plans_file)["status"] == "completed"


def test_inbound_commands_are_idempotent(plans_file):
    """同一个消息 id 只执行一次：GET /api/plans 会被反复轮询。"""
    pid = _mk(who={"agent": "alice"}, path=plans_file)["id"]
    msgs = [{"id": "m1", "from": "alice", "scope": "hall",
             "text": "@command:plan close %s" % pid}]
    assert len(ps.apply_inbound(msgs, {}, path=plans_file)) == 1
    assert ps.apply_inbound(msgs, {}, path=plans_file) == []
    # 关掉之后再发一条新的 close（不同 id）仍然会执行（幂等，不报错）
    msgs2 = [{"id": "m2", "from": "alice", "scope": "hall",
              "text": "@command:plan close %s" % pid}]
    assert ps.apply_inbound(msgs2, {}, path=plans_file)[0]["ok"] is True


def test_inbound_delete_requires_controller_role(plans_file, monkeypatch):
    """留言里没有二次确认，所以删除必须靠角色：查不到 token 记录就是 worker。"""
    pid = _mk(who={"agent": "alice"}, path=plans_file)["id"]
    monkeypatch.setattr(ps, "_role_of", lambda name: "worker")
    out = ps.apply_inbound([{"id": "m1", "from": "alice", "scope": "hall",
                             "text": "@command:plan delete %s" % pid}], {}, path=plans_file)
    assert out[0]["ok"] is False and out[0]["code"] == 403
    assert ps.list_plans({}, path=plans_file), "被拒绝时计划还在"
    monkeypatch.setattr(ps, "_role_of", lambda name: "controller")
    out = ps.apply_inbound([{"id": "m2", "from": "root", "scope": "hall",
                             "text": "@command:plan delete %s" % pid}], {}, path=plans_file)
    assert out[0]["ok"] and ps.list_plans({}, path=plans_file) == []


def test_inbound_bad_format_is_reported_not_swallowed(plans_file):
    out = ps.apply_inbound([{"id": "m1", "from": "alice", "scope": "hall",
                             "text": "/plan create PAY"}], {}, path=plans_file)
    assert out[0]["ok"] is False and "/plan create" in (out[0].get("usage") or "")


def test_api_get_consumes_cluster_commands(status_full, plans_file, tokens, monkeypatch):
    """别的机器 agent 用 hive chat post 发的指令，看板读 /api/plans 时补执行。"""
    ctrl, _worker = tokens
    plan = ps.create_plan("PAY", "待完成", created_by="board-ctrl", path=plans_file)
    posted = []

    def fake_chat(method, query=None, payload=None, timeout=2.0):
        if method == "GET":
            # from 是 controller 的 agent 名：角色由本机 token 表现查（_role_of）
            return True, {"ok": True, "messages": [
                {"id": "c-1", "from": "board-ctrl", "scope": "hall",
                 "text": "@command:plan complete %s" % plan["id"]}]}
        posted.append(payload)
        return True, {"ok": True, "message": {"id": "reply-1"}}

    monkeypatch.setattr(serve, "_cluster_chat_http", fake_chat)
    # 模拟「看板已经在跑」：游标就位后新留言才会被消费（首轮只定位不执行历史）
    monkeypatch.setattr(serve, "_INBOUND_READY", True)
    monkeypatch.setattr(serve, "_INBOUND_CURSOR", 0.0)
    base, shutdown = _serve_with_plans(status_full)
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/api/plans", timeout=10) as r:
            body = json.loads(r.read().decode("utf-8"))
        # 再读一次：账本保证不重复执行
        with urllib.request.urlopen(base + "/api/plans", timeout=10) as r:
            again = json.loads(r.read().decode("utf-8"))
    finally:
        shutdown()
    assert body["plans"][0]["status"] == "completed"
    assert body.get("commands") and body["commands"][0]["id"] == "c-1"
    assert "commands" not in again, "同一条留言不该执行第二次"
    assert posted and posted[0]["text"].startswith("📋"), "结果要回贴到原频道"


def test_api_first_read_does_not_replay_history(status_full, plans_file, tokens, monkeypatch):
    """首次运行只定位游标：历史留言里的 /plan 文本不能被重放执行。"""
    ctrl, _worker = tokens
    plan = ps.create_plan("PAY", "历史计划", created_by="board-ctrl", path=plans_file)
    posted = []

    def fake_chat(method, query=None, payload=None, timeout=2.0):
        if method == "GET":
            return True, {"ok": True, "last_epoch": 1000.0, "messages": [
                {"id": "old-1", "epoch": 900.0, "from": "board-ctrl", "scope": "hall",
                 "text": "@command:plan complete %s" % plan["id"]}]}
        posted.append(payload)
        return True, {"ok": True, "message": {"id": "r"}}

    monkeypatch.setattr(serve, "_cluster_chat_http", fake_chat)
    monkeypatch.setattr(serve, "_INBOUND_READY", False)
    monkeypatch.setattr(serve, "_INBOUND_CURSOR", 0.0)
    base, shutdown = _serve_with_plans(status_full)
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/api/plans", timeout=10) as r:
            body = json.loads(r.read().decode("utf-8"))
    finally:
        shutdown()
    assert ps.get_plan(plan["id"], {}, path=plans_file)["status"] == "active"
    assert not body.get("commands")


def test_normal_chat_message_still_works(status_full, plans_file, tokens, monkeypatch):
    """普通留言不能被计划指令拦掉（回归：/api/chat 的分支顺序）。"""
    ctrl, _worker = tokens
    monkeypatch.setattr(serve, "_cluster_chat_http", lambda *a, **k: (False, None))
    monkeypatch.setattr(serve, "chat_local_path", lambda: plans_file.parent / "chat.jsonl")
    base, shutdown = _serve_with_plans(status_full)
    try:
        code, body, _ = _post(base, "/api/chat",
                              {"text": "T-0231 我理解成改判据", "scope": "hall"},
                              {"X-Hive-Token": ctrl})
    finally:
        shutdown()
    assert code == 200 and body["ok"], (code, body)
    assert "plan_command" not in body
    assert ps.list_plans({}, path=plans_file) == [], "普通留言不能顺手建计划"
