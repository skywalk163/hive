"""跨机留言（聊天）测试：落盘、频道、跨节点同步、看板降级。

留言走集群而不是 git：worker 的 worktree / MCP 的独占 clone 每轮都会
`reset --hard origin/main`，写进仓里的东西会被冲掉；聊天要的是秒级。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

import hive_cluster as hc

DASH = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASH))


def wait_until(fn, timeout: float = 8.0, interval: float = 0.1):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    return last


@pytest.fixture()
def make_node(tmp_path):
    nodes: list[hc.Cluster] = []

    def make(name: str, seeds=None, state_dir=None):
        cfg = hc.Config(
            state_dir=state_dir or (tmp_path / name),
            name=name,
            host="127.0.0.1",
            port=0,
            advertise="127.0.0.1",
            discover=False,
            seeds=seeds or [],
            hive_dir=tmp_path / "repo",
            heartbeat_interval=0.08,
            peer_timeout=0.6,
            dead_timeout=1.5,
            election_min=0.3,
            election_jitter=0.1,
            leader_lease=0.8,
            lease_grace=2.0,
        )
        c = hc.Cluster(cfg)
        c.start()
        nodes.append(c)
        return c

    yield make
    for c in nodes:
        try:
            c.stop()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 基本读写
# --------------------------------------------------------------------------- #

def test_post_and_list_roundtrip(make_node):
    a = make_node("a")
    assert wait_until(lambda: a.role == "leader")

    code, resp = a.handle_chat_post({"text": "T-0231 我理解成改判据",
                                     "from": "fb88-dsh",
                                     "scope": "task:T-0231",
                                     "kind": "question"})
    assert code == 200 and resp["ok"]
    assert resp["message"]["scope"] == "task:T-0231"
    assert resp["message"]["kind"] == "question"

    msgs = a.chat_list()
    assert len(msgs) == 1
    assert msgs[0]["text"] == "T-0231 我理解成改判据"
    assert msgs[0]["from"] == "fb88-dsh"
    assert msgs[0]["node_name"] == "a"


def test_scope_filter_and_default_hall(make_node):
    a = make_node("a")
    assert wait_until(lambda: a.role == "leader")
    a.handle_chat_post({"text": "大厅", "scope": "hall"})
    a.handle_chat_post({"text": "某卡", "scope": "task:T-0200"})
    a.handle_chat_post({"text": "私信", "scope": "agent:raccoon"})
    # 非法频道不报错，落到大厅（写错冒号不该让人发不出去）
    code, resp = a.handle_chat_post({"text": "写错频道了", "scope": "task-0200"})
    assert code == 200 and resp["message"]["scope"] == "hall"

    only_task = a.chat_list(scope="task:T-0200")
    assert [m["text"] for m in only_task] == ["某卡"]

    two = a.chat_list(scope="task:T-0200,agent:raccoon")
    assert {m["text"] for m in two} == {"某卡", "私信"}

    assert len(a.chat_list(scope="hall")) == 2


def test_empty_and_oversized_text_rejected(make_node):
    a = make_node("a")
    assert wait_until(lambda: a.role == "leader")
    code, resp = a.handle_chat_post({"text": "   "})
    assert code == 400 and not resp["ok"]
    code, resp = a.handle_chat_post({"text": "x" * (hc.CHAT_MAX_TEXT + 1)})
    assert code == 400
    assert a.chat_list() == []


def test_since_returns_only_newer(make_node):
    a = make_node("a")
    assert wait_until(lambda: a.role == "leader")
    a.handle_chat_post({"text": "第一句"})
    first = a.chat_list()[0]
    a.handle_chat_post({"text": "第二句"})
    newer = a.chat_list(since=first["epoch"])
    assert [m["text"] for m in newer] == ["第二句"]


def test_chat_survives_restart(tmp_path, make_node):
    a = make_node("a")
    assert wait_until(lambda: a.role == "leader")
    a.handle_chat_post({"text": "重启后我还在吗", "from": "human:sky"})
    state_dir = a.config.state_dir
    a.stop()

    b = make_node("a", state_dir=state_dir)
    assert wait_until(lambda: b.role == "leader")
    texts = [m["text"] for m in b.chat_list()]
    assert "重启后我还在吗" in texts
    # 落盘的是 JSONL，一行一条，便于人肉 grep
    lines = (Path(state_dir) / "chat.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["text"] == "重启后我还在吗"


# --------------------------------------------------------------------------- #
# 跨节点
# --------------------------------------------------------------------------- #

def test_follower_forwards_to_leader(make_node):
    a = make_node("a")
    assert wait_until(lambda: a.role == "leader")
    b = make_node("b", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id)

    code, resp = b.handle_chat_post({"text": "从 B 发的", "from": "raccoon"})
    assert code == 200 and resp["ok"]
    # 主节点落了一份（全网同一份日志），B 本地也留了一份，看板立刻就看得见
    assert wait_until(lambda: any(m["text"] == "从 B 发的" for m in a.chat_list()))
    # 主节点只是代写：来源仍记成 B（否则看板上全变成「@主节点」）
    got = [m for m in a.chat_list() if m["text"] == "从 B 发的"][0]
    assert got["node_name"] == "b"


def test_replicate_keeps_original_id(make_node):
    """别的节点把自己写的、主节点还没有的留言原样送过来：id/时间戳不变，按 id 去重。"""
    a = make_node("a")
    assert wait_until(lambda: a.role == "leader")
    payload = {"id": "b-100-abc", "ts": "2026-11-02T00:00:00+00:00",
               "epoch": 1.0, "text": "分区期间写的", "from": "raccoon",
               "node": "b-node", "node_name": "b", "scope": "hall",
               "replicate": True}
    assert a.handle_chat_post(payload)[0] == 200
    assert a.handle_chat_post(payload)[1].get("replicated") is False  # 重复投递不再落
    msgs = a.chat_list()
    assert len(msgs) == 1 and msgs[0]["id"] == "b-100-abc"
    assert msgs[0]["node_name"] == "b"


def test_leader_messages_reach_follower(make_node):
    a = make_node("a")
    assert wait_until(lambda: a.role == "leader")
    b = make_node("b", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id)

    a.handle_chat_post({"text": "主节点里的一句", "scope": "task:T-0231"})
    assert wait_until(lambda: [m["text"] for m in b.chat_list()] == ["主节点里的一句"],
                      timeout=10.0), "follower 应主动向主节点增量拉取"


# --------------------------------------------------------------------------- #
# 看板侧降级（集群没起时写本地文件）
# --------------------------------------------------------------------------- #

def test_dashboard_local_fallback(tmp_path, monkeypatch):
    import serve  # dashboard/serve.py

    local = tmp_path / "chat.jsonl"
    monkeypatch.setenv("HIVE_CHAT_LOCAL", str(local))
    serve.chat_local_append({"id": "x-1", "ts": "2026-11-02T00:00:00+00:00",
                             "epoch": 1000.0, "from": "human", "scope": "hall",
                             "kind": "note", "text": "离线留言"})
    serve.chat_local_append({"id": "x-2", "ts": "2026-11-02T00:01:00+00:00",
                             "epoch": 1060.0, "from": "human", "scope": "task:T-0231",
                             "kind": "note", "text": "某卡的"})
    assert len(serve.chat_local_read()) == 2
    assert [m["id"] for m in serve.chat_local_read(since=1010.0)] == ["x-2"]
    assert [m["id"] for m in serve.chat_local_read(scope="task:T-0231")] == ["x-2"]
    assert serve.norm_chat_scope("task-0231") == "hall"
    assert serve.norm_chat_scope("agent:fb88-dsh") == "agent:fb88-dsh"
