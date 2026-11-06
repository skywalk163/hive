"""store claim 失败分类 + leader 缺卡自愈。

现场（T-0257）：leader 进程只在启动时摄入一次卡，启动后新增的 T-0254~T-0258
在 store 里缺行。claim 报的是「没有任务 T-0257」，但 Worker.claim 把原因丢了、
t_claim / cmd_take 又把一切失败兜底成「已被 别人 领取，抢不到」——卡明明是
ready、远端也没有 claim 分支，人却去找一个根本不存在的持有者。

这里钉住两件事：
  1) leader 侧能把 follower 手上的 Git 卡面补摄入（走 op，复制到所有副本）；
  2) 失败按「没有这张卡 / 已被 X 领取 / store 不可用」分开说人话。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import hive
import mcp_server
from hive_store import Store, StoreError


# --------------------------------------------------------------------------- #
# store 层：结构化错误码
# --------------------------------------------------------------------------- #

def test_store_claim_errors_have_machine_readable_code(tmp_path):
    s = Store(tmp_path / "a.db", "node-a")
    with pytest.raises(StoreError) as e:
        s.claim("T-9999", "agent-a", term=1, lease_minutes=10)
    assert e.value.code == "E_NO_TASK", "缺卡必须能跟「被别人领走」区分开"
    assert "没有任务" in str(e.value)

    s.ingest_cards({"T-0001": {"id": "T-0001", "state": "ready"}})
    s.claim("T-0001", "agent-a", term=1, lease_minutes=10)
    with pytest.raises(StoreError) as e:
        s.claim("T-0001", "agent-b", term=1, lease_minutes=10)
    assert e.value.code == "E_TAKEN"


def test_ingest_card_inserts_missing_card_with_full_definition(tmp_path):
    s = Store(tmp_path / "b.db", "node-a")
    card = {"id": "T-0254", "title": "新卡", "state": "ready",
            "repo": "lightharness", "priority": "P1", "verify": ["true"],
            "_body": "正文", "_path": "/tmp/T-0254.md"}
    out = s.ingest_card("T-0254", card, commit=True)

    assert out["ingested"] is True
    row = s.task("T-0254")
    assert row["state"] == "ready"
    assert row["title"] == "新卡" and row["repo"] == "lightharness"
    assert row["verify"] == ["true"]
    assert "_body" not in row and "_path" not in row, "内部字段不该进 store"
    assert s.last_seq() == 1, "必须走 op 日志，才能复制到所有副本"


def test_ingest_card_never_resets_running_state(tmp_path):
    """补摄入只补卡面定义，不能把正在跑的卡翻回 ready。"""
    s = Store(tmp_path / "c.db", "node-a")
    s.ingest_card("T-0001", {"id": "T-0001", "state": "ready", "title": "旧标题"})
    s.claim("T-0001", "agent-a", term=1, lease_minutes=10)
    assert s.task("T-0001")["state"] == "running"

    s.ingest_card("T-0001", {"id": "T-0001", "state": "ready", "title": "新标题"})
    row = s.task("T-0001")
    assert row["state"] == "running"
    assert row["owner"] == "agent-a"
    assert row["title"] == "新标题", "卡面定义要能补上"


# --------------------------------------------------------------------------- #
# hive.py：失败分类
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ecode,msg,http,expected", [
    ("E_NO_TASK", "没有任务 T-0257", 409, "no_task"),
    ("E_TAKEN", "T-0257 已被 deepseek-win 领取", 409, "taken"),
    ("", "没有任务 T-0257", 409, "no_task"),          # 旧 leader 无 code，靠文案兜底
    ("", "T-0257 已被 deepseek-win 领取", 409, "taken"),
    ("", "没有主节点", 409, "unavailable"),
    ("", "转发主节点失败", 502, "unavailable"),
    ("E_NO_QUORUM", "未达到多数 prepare", 503, "unavailable"),
    ("E_UNKNOWN_ACTION", "未知 store 动作 ingest", 409, "unsupported"),
    ("", "未知 store 动作 ingest", 409, "unsupported"),  # 旧 leader 无 code
    ("", "", None, "unavailable"),
    ("", "store claim 失败（集群不可用/无主，HTTP None）", None, "unavailable"),
    ("E_CLOSED", "T-0001 已关闭", 409, "other"),
])
def test_classify_store_error(ecode, msg, http, expected):
    assert hive._classify_store_error(ecode, msg, http) == expected


def test_store_call_raises_classified_error(monkeypatch):
    monkeypatch.setattr(hive, "_cluster_http", lambda *a, **kw:
                        (409, {"ok": False, "error": "没有任务 T-0257",
                               "code": "E_NO_TASK"}))
    with pytest.raises(hive.StoreCallError) as e:
        hive.store_call("claim", {"task": "T-0257"})
    assert e.value.kind == "no_task" and e.value.code == "E_NO_TASK"
    text = e.value.describe("T-0257")
    assert "leader store 里没有这张卡" in text
    assert "已被别人领取" not in text, "缺卡绝不能再说成被抢"


def test_store_call_marks_unknown_action_as_unsupported(monkeypatch):
    monkeypatch.setattr(hive, "_cluster_http", lambda *a, **kw:
                        (409, {"ok": False, "error": "未知 store 动作 ingest",
                               "code": "E_UNKNOWN_ACTION"}))
    with pytest.raises(hive.StoreCallError) as e:
        hive.store_call("ingest", {"task": "T-0257"})
    assert e.value.kind == "unsupported"


def test_describe_unavailable_does_not_blame_other_agent():
    e = hive.StoreCallError("没有主节点", kind="unavailable", http=409)
    text = e.describe("T-0257")
    assert "不可用" in text and "不是被别人抢走" in text


# --------------------------------------------------------------------------- #
# Worker.claim：缺卡自愈
# --------------------------------------------------------------------------- #

def _stub_worker(monkeypatch):
    """不启真实 git 仓/agent 配置，只测 claim 的 store 分支。"""
    w = object.__new__(hive.Worker)
    w.name = "deepseek-win"
    w.h = SimpleNamespace(
        claim_info=lambda tid: None,
        card=lambda tid: {"id": tid, "state": "ready", "title": "新卡",
                          "repo": "light", "_body": "正文"},
    )
    w.last_claim_error = None
    monkeypatch.setattr(hive.Worker, "_reload_cfg", lambda self: {})
    monkeypatch.setattr(hive, "claim_backend", lambda: "store")
    return w


def test_claim_self_heals_when_leader_store_misses_card(monkeypatch):
    calls = {"claim": 0, "ingest": 0, "assign": 0}

    def fake_claim(tid, agent, minutes):
        calls["claim"] += 1
        if calls["claim"] == 1:
            raise hive.StoreCallError("没有任务 T-0257", kind="no_task",
                                      code="E_NO_TASK")
        return {"ok": True, "claim": {"lease_id": "L1"}}

    def fake_ingest(tid, card):
        calls["ingest"] += 1
        assert tid == "T-0257" and card["id"] == "T-0257"
        assert "_body" not in card, "送入 leader 的卡面要去掉内部字段"
        return {"ok": True, "ingested": True}

    monkeypatch.setattr(hive, "store_claim", fake_claim)
    monkeypatch.setattr(hive, "store_ingest_card", fake_ingest)
    monkeypatch.setattr(hive, "store_assign",
                        lambda tid, agent: calls.__setitem__("assign", 1))

    w = _stub_worker(monkeypatch)
    assert w.claim("T-0257") is True
    assert calls == {"claim": 2, "ingest": 1, "assign": 0}
    assert w.last_claim_error is None


def test_claim_falls_back_to_assign_on_old_leader(monkeypatch):
    """leader 还没升级（没有 ingest 动作）时，follower 也能自愈。"""
    calls = {"claim": 0, "assign": []}

    def fake_claim(tid, agent, minutes):
        calls["claim"] += 1
        if calls["claim"] == 1:
            raise hive.StoreCallError("没有任务 T-0257", kind="no_task")
        return {"ok": True, "claim": {"lease_id": "L1"}}

    def fake_ingest(tid, card):
        raise hive.StoreCallError("未知 store 动作 ingest", kind="unsupported",
                                  code="E_UNKNOWN_ACTION")

    monkeypatch.setattr(hive, "store_claim", fake_claim)
    monkeypatch.setattr(hive, "store_ingest_card", fake_ingest)
    monkeypatch.setattr(hive, "store_assign",
                        lambda tid, agent: calls["assign"].append((tid, agent)))

    w = _stub_worker(monkeypatch)
    assert w.claim("T-0257") is True
    assert calls["claim"] == 2
    assert calls["assign"] == [("T-0257", "deepseek-win")]


def test_claim_does_not_fall_back_on_non_version_errors(monkeypatch):
    """补摄入失败若是「集群不可用」，不该误判成旧版节点去做 assign 兜底。"""
    calls = {"claim": 0, "assign": 0}

    def fake_claim(tid, agent, minutes):
        calls["claim"] += 1
        raise hive.StoreCallError("没有任务 T-0257", kind="no_task")

    def fake_ingest(tid, card):
        raise hive.StoreCallError("没有主节点", kind="unavailable", http=409)

    monkeypatch.setattr(hive, "store_claim", fake_claim)
    monkeypatch.setattr(hive, "store_ingest_card", fake_ingest)
    monkeypatch.setattr(hive, "store_assign",
                        lambda tid, agent: calls.__setitem__("assign", 1))

    w = _stub_worker(monkeypatch)
    assert w.claim("T-0257") is False
    assert calls == {"claim": 1, "assign": 0}


def test_claim_unavailable_does_not_retry_or_blame_others(monkeypatch):
    calls = {"claim": 0, "ingest": 0}

    def fake_claim(tid, agent, minutes):
        calls["claim"] += 1
        raise hive.StoreCallError("没有主节点", kind="unavailable", http=409)

    monkeypatch.setattr(hive, "store_claim", fake_claim)
    monkeypatch.setattr(hive, "store_ingest_card",
                        lambda *a: calls.__setitem__("ingest", 1))

    w = _stub_worker(monkeypatch)
    assert w.claim("T-0257") is False
    assert calls == {"claim": 1, "ingest": 0}, "集群不可用不该触发补摄入重试"
    assert w.last_claim_error.kind == "unavailable"


def test_claim_confirmed_taken_keeps_holder(monkeypatch):
    w = _stub_worker(monkeypatch)
    w.h.claim_info = lambda tid: {"agent": "other-agent", "state": "active"}
    monkeypatch.setattr(hive, "store_claim",
                        lambda *a: pytest.fail("预检到有主不该再打 store"))
    assert w.claim("T-0257") is False
    assert w.last_claim_error.kind == "taken"


# --------------------------------------------------------------------------- #
# 集群层：新动作与错误码透传
# --------------------------------------------------------------------------- #

def _stub_cluster(tmp_path):
    import threading

    import hive_cluster
    c = object.__new__(hive_cluster.Cluster)
    c.store = Store(tmp_path / "cluster.db", "node-a")
    c.term = 7
    c.role = "leader"
    c.leader_url = ""
    c.lock = threading.RLock()
    c._replicate_op = lambda op: True
    c._commit_op_replicated = lambda seq: True
    return c


def test_cluster_ingest_action_registers_card(tmp_path):
    c = _stub_cluster(tmp_path)
    out = c._store_op_for("ingest", {
        "task": "T-0254",
        "card": {"id": "T-0254", "state": "ready", "title": "新卡"},
    })
    assert out["ok"] and out["ingested"] is True
    assert out["seq"] >= 1
    # _store_op_for 一律 commit=False（只 stage），由 handle_store 复制后再 commit
    assert c.store.task("T-0254") is None


def test_handle_store_ingest_replicates_to_followers(tmp_path):
    c = _stub_cluster(tmp_path)
    code, resp = c.handle_store("ingest", {
        "task": "T-0254",
        "card": {"id": "T-0254", "state": "ready", "title": "新卡"},
    })
    assert code == 200 and resp["ok"] is True
    assert c.store.task("T-0254")["state"] == "ready"
    assert c.store.commit_seq() >= 1, "op 要被 commit，follower 才能追平"


def test_handle_store_exposes_error_code(tmp_path):
    c = _stub_cluster(tmp_path)
    code, resp = c.handle_store("claim", {"task": "T-9999", "agent": "a"})
    assert code == 409 and resp["ok"] is False
    assert resp["code"] == "E_NO_TASK", "上层要能靠 code 而不是中文文案分类"
    assert "没有任务" in resp["error"]


# --------------------------------------------------------------------------- #
# 持续不变式：新卡摄入必须走 op，副本才追得上
# --------------------------------------------------------------------------- #

def _hive_dir_with_cards(tmp_path, ids):
    (tmp_path / ".git").mkdir(exist_ok=True)
    tasks = tmp_path / "tasks"
    tasks.mkdir(exist_ok=True)
    for tid in ids:
        (tasks / f"{tid}.md").write_text(
            f"---\nid: {tid}\ntitle: {tid} 标题\nstate: ready\n"
            f"repo: light\n---\n正文\n", encoding="utf-8")
    return tmp_path


def test_ingest_store_cards_emits_op_for_missing_rows(tmp_path):
    """缺卡必须产生 op 并推进 commit_seq，否则 follower 永远追不上。"""
    import hive_cluster

    _hive_dir_with_cards(tmp_path, ["T-0001", "T-0254"])
    c = object.__new__(hive_cluster.Cluster)
    c.store = Store(tmp_path / "reconcile.db", "node-a")
    c.config = SimpleNamespace(hive_dir=tmp_path)
    c.store.ingest_cards({"T-0001": {"id": "T-0001", "state": "ready",
                                     "title": "旧定义"}})
    c.store.claim("T-0001", "agent-a", term=1, lease_minutes=10)
    before = c.store.commit_seq()

    n = c._ingest_store_cards()
    assert n == 1, "只补 T-0254 这一张缺卡"
    assert c.store.task("T-0254")["state"] == "ready"
    assert c.store.task("T-0254")["title"] == "T-0254 标题"
    assert c.store.task("T-0001")["state"] == "running", \
        "已有行的运行时状态不能被启动摄入翻回 ready"
    assert c.store.commit_seq() > before, \
        "旧实现直接写库不产生 op → 水位相同内容不同，这是本次要根治的洞"
    assert c._ingest_store_cards() == 0, "重复摄入要幂等，不制造重复 op"


def test_reconcile_is_leader_only_and_throttled(tmp_path, monkeypatch):
    import hive_cluster

    c = object.__new__(hive_cluster.Cluster)
    c.store = Store(tmp_path / "throttle.db", "node-a")
    c.role = "leader"
    c._last_store_reconcile = 0.0
    calls = []
    monkeypatch.setattr(c, "_ingest_store_cards",
                        lambda: calls.append(1) or 0)

    assert c._maybe_reconcile_store_cards() == 0
    assert len(calls) == 1
    assert c._maybe_reconcile_store_cards() == 0
    assert len(calls) == 1, "同一周期内不该反复扫 Git"

    c.role = "follower"
    c._last_store_reconcile = 0.0
    assert c._maybe_reconcile_store_cards() == 0
    assert len(calls) == 1, "follower 不做摄入，写入只由 leader 发起"


# --------------------------------------------------------------------------- #
# doctor：让副本不一致自己喊出来
# --------------------------------------------------------------------------- #

def _fake_store(n, seq, ids=None):
    rows = ({t: {"id": t} for t in ids} if ids is not None
            else {f"T-{i:04d}": {"id": f"T-{i:04d}"} for i in range(n)})
    return SimpleNamespace(tasks=lambda: rows, commit_seq=lambda: seq)


def _doctor(monkeypatch, store, cards, cluster=None, node_summary=None):
    monkeypatch.setattr(hive, "local_store", lambda: store)
    if cluster is None:
        monkeypatch.setattr(hive, "_cluster_http", lambda *a, **kw: (None, None))
    else:
        monkeypatch.setattr(hive, "_cluster_http", lambda *a, **kw: (200, cluster))
        monkeypatch.setattr(hive, "_node_store_summary",
                            lambda url, **kw: node_summary)
    errors, warns = [], []
    h = object.__new__(hive.Hive)
    h._doctor_store_consistency(cards, errors, warns)
    return errors, warns


def test_doctor_lists_cards_missing_from_local_store(monkeypatch):
    errors, warns = _doctor(monkeypatch, _fake_store(1, 43, ["T-0001"]),
                            {"T-0001": {}, "T-0254": {}, "T-0255": {}})
    assert not errors
    assert any("缺 2 张卡" in w and "T-0254" in w for w in warns)
    assert any("store/ingest" in w for w in warns)


def test_doctor_flags_same_watermark_different_content(monkeypatch):
    """这正是现场 43/43 但 138/134 的情形：水位相同，追平永不触发。"""
    cluster = {"state": {"nodes": [
        {"name": "leader", "url": "http://192.168.0.104:8788", "status": "alive"},
        {"name": "offline", "url": "http://10.0.0.9:8788", "status": "dead"},
    ]}}
    errors, warns = _doctor(monkeypatch, _fake_store(134, 43), {},
                            cluster=cluster,
                            node_summary={"tasks": 138, "commit_seq": 43})
    assert any("水位相同" in e and "不会自愈" in e for e in errors), errors
    assert not any("尚未追平" in w for w in warns)


def test_doctor_warns_when_replica_only_lags(monkeypatch):
    cluster = {"state": {"nodes": [
        {"name": "leader", "url": "http://192.168.0.104:8788", "status": "alive"},
    ]}}
    errors, warns = _doctor(monkeypatch, _fake_store(134, 41), {},
                            cluster=cluster,
                            node_summary={"tasks": 138, "commit_seq": 43})
    assert not errors
    assert any("尚未追平" in w for w in warns), warns


def test_doctor_skips_peer_check_when_cluster_is_down(monkeypatch):
    errors, warns = _doctor(monkeypatch, _fake_store(134, 43), {})
    assert not errors and not warns, "集群不可达只做本机检查，不该报错"


# --------------------------------------------------------------------------- #
# 上层文案：CLI 与 MCP 都不再把缺卡说成「被别人抢走」
# --------------------------------------------------------------------------- #

def test_cmd_take_reports_missing_card_instead_of_taken(monkeypatch, tmp_path):
    h = object.__new__(hive.Hive)
    h.dir = tmp_path
    monkeypatch.setattr(hive.Hive, "fetch", lambda self: None)
    monkeypatch.setattr(hive.Hive, "card",
                        lambda self, tid: {"id": tid, "state": "ready"})
    monkeypatch.setattr(hive.Hive, "result_info", lambda self, tid: None)
    monkeypatch.setattr(hive.Hive, "claim_info", lambda self, tid: None)
    monkeypatch.setattr(hive, "claim_backend", lambda: "store")

    def fake_worker_init(self, hive_dir, agent_name, *a, **kw):
        self.h = SimpleNamespace()
        self.dir = tmp_path
        self.name = agent_name
        self.cfg = {}
        self.state = tmp_path / "state"
        self.last_claim_error = hive.StoreCallError(
            "没有任务 T-0257", kind="no_task", code="E_NO_TASK")
    monkeypatch.setattr(hive.Worker, "__init__", fake_worker_init)
    monkeypatch.setattr(hive.Worker, "_reload_cfg", lambda self: {})
    monkeypatch.setattr(hive.Worker, "claim", lambda self, tid: False)

    with pytest.raises(hive.HiveError) as e:
        h.cmd_take("T-0257", "deepseek-win")
    assert "leader store 里没有这张卡" in str(e.value)
    assert "已被" not in str(e.value)


def test_mcp_claim_reports_missing_card_instead_of_taken(monkeypatch, tmp_path):
    srv = object.__new__(mcp_server.HiveMcp)
    srv.agent = "deepseek-win"
    srv.hive = lambda: SimpleNamespace(
        card=lambda tid: {"id": tid, "state": "ready"},
        result_info=lambda tid: None,
        claim_info=lambda tid: None,
    )
    fake_w = SimpleNamespace(
        claim=lambda tid: False,
        last_claim_error=hive.StoreCallError(
            "没有任务 T-0257", kind="no_task", code="E_NO_TASK"),
    )
    srv.worker = lambda: fake_w
    monkeypatch.setattr(mcp_server, "claim_backend", lambda: "store")

    env = srv.t_claim({"task_id": "T-0257"})
    assert env["status"] == "error"
    assert "leader store 里没有这张卡" in env["message"]
    assert "已被 别人 领取" not in env["message"]
    assert "hint" in env["details"]
