"""store 后端租约时长：卡面写了就按卡面，别 10 分钟兜底（T-0423）。

现场（T-0410）
--------------
卡面 `lease_minutes: 300`、`timeout_minutes: 180`（正确），但 17:44
`lightharness-fb98` 领取时 store 里的 `lease_until` 只到 17:54 —— **恰好 10 分钟**。
T-0387（88536cb）已经把「读卡面 lease_minutes」改好了，可还是落到兜底值。

根因（本卡取证）
----------------
`Worker.claim` 的 store 分支取的是

    store_lease_minutes_of(self.h.card(tid))

而 `Hive.card()` 读的是**工作区里的文件**，不是 git 里的卡。`sync_main` 为了
不抹掉人的未提交改动，落后且脏时直接不 reset；本地领先时也不动；fetch 失败
还会被吞。于是「卡面明明写了 300，工作区那份还是旧的一版（没有
lease_minutes）」完全成立 —— 取到空 → 退回 `STORE_LEASE_MINUTES = 10`。

取证脚本输出（修复前，三种局面，隔离 store）：
    A 本地卡面有 300            -> 300.0 分钟
    B 本地陈旧（origin 已是 300）-> 10.0 分钟   ← 复现
    C 卡面确实没写              -> 10.0 分钟

修复口径（不动 T-0387 的主逻辑）
--------------------------------
卡面有值仍然以卡面为准；只是「本地卡面没写」时**先回读 origin/main 的卡面**，
两处都没写才退回 10 分钟兜底。反证：把 `store_lease_minutes_for` 换回
`store_lease_minutes_of(local_card)`，`test_claim_reads_origin_card_when_worktree_stale`
立刻变红。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hive
from hive_store import Store

CARD = """---
id: {tid}
title: 租约取证卡
repo: light
base: main
state: ready
owner: null
agent: null
priority: P1
verify:
  - "true"
{lease}timeout_minutes: 180
---
正文
"""

LEASE_LINE = "lease_minutes: 300\n"


def _card(tid: str, lease: bool = True) -> str:
    return CARD.format(tid=tid, lease=LEASE_LINE if lease else "")


def _git(*args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0, f"git {' '.join(args)} 失败: {p.stderr}"
    return p


def _repo(root: Path, tid: str, local: str, origin: str | None = None) -> Path:
    """一个协调仓；`origin` 给了就让 ``origin/main`` 上的卡面比工作区新。

    这里不建裸仓再 clone（Windows 上一次 clone+push 要好几秒）：直接用
    ``update-ref refs/remotes/origin/main`` 造出远端跟踪引用，`git show
    origin/main:tasks/<tid>.md` 读到的就是它 —— 与真实「origin 已推进、工作区
    还停在旧提交」等价，但快一个数量级。
    """
    work = root / "work"
    work.mkdir(parents=True)
    _git("init", "-q", "-b", "main", str(work), cwd=root)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "tasks").mkdir()
    (work / "agents").mkdir()
    (work / "agents" / "a.json").write_text(
        json.dumps({"name": "a", "repos": {}, "mode": "manual"}), encoding="utf-8")
    (work / "tasks" / f"{tid}.md").write_text(local, encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "old", cwd=work)
    if origin is not None and origin != local:
        (work / "tasks" / f"{tid}.md").write_text(origin, encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "new", cwd=work)
        # origin/main 指向「已推进」的那一版，工作区退回旧提交
        _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=work)
        _git("reset", "--hard", "-q", "HEAD~1", cwd=work)
    else:
        _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=work)
    return work


# --------------------------------------------------------------------------- #
# 取值函数：卡面 → origin/main → 10 分钟兜底
# --------------------------------------------------------------------------- #

def test_lease_minutes_prefers_local_card(tmp_path):
    """卡面写了就以卡面为准（T-0387 的主逻辑，一条都不许退）。"""
    repo = _repo(tmp_path, "T-0901", _card("T-0901"))
    assert hive.store_lease_minutes_for(repo, "T-0901") == 300


def test_lease_minutes_reads_origin_when_worktree_is_stale(tmp_path):
    """本机卡面陈旧（缺 lease_minutes）时必须回读 origin/main，不能掉进 10 分钟。"""
    repo = _repo(tmp_path, "T-0901", _card("T-0901", lease=False),
                 origin=_card("T-0901"))
    assert hive.store_lease_minutes_for(repo, "T-0901") == 300


def test_lease_minutes_keeps_ten_minute_fallback(tmp_path):
    """卡面确实没写（本地和 origin 都没写）才用 STORE_LEASE_MINUTES 兜底。"""
    repo = _repo(tmp_path, "T-0901", _card("T-0901", lease=False))
    assert hive.STORE_LEASE_MINUTES == 10, "兜底常量本身不能改"
    assert hive.store_lease_minutes_for(repo, "T-0901") == 10


def test_lease_minutes_tolerates_missing_card(tmp_path):
    """卡文件根本不存在也不该炸：交给调用方按「缺卡」处理，取值走兜底。"""
    repo = _repo(tmp_path, "T-0901", _card("T-0901", lease=False))
    assert hive.local_card_file(repo, "T-9999") is None
    assert hive.store_lease_minutes_for(repo, "T-9999") == 10


def test_card_from_origin_parses_card_text(tmp_path):
    """origin 上的卡面得能按文本解析（parse_card 原来只吃 Path）。"""
    repo = _repo(tmp_path, "T-0901", _card("T-0901", lease=False),
                 origin=_card("T-0901"))
    card = hive.card_from_origin(repo, "T-0901")
    assert card is not None
    assert card["lease_minutes"] == 300 and card["title"] == "租约取证卡"
    assert hive.card_from_origin(repo, "T-9999") is None


# --------------------------------------------------------------------------- #
# Worker.claim：真实（隔离）store 上验租约时长
# --------------------------------------------------------------------------- #

def _store_call(store: Store, clamp_minutes: int | None = None):
    """把 store_call 的 HTTP 出口接到一个临时 db 上，不碰线上集群。"""

    def _http(method, path, payload=None, timeout=None, token=None):
        action = str(path).rsplit("/", 1)[-1]
        payload = payload or {}
        try:
            if action == "claim":
                minutes = int(payload.get("lease_minutes") or 0)
                if clamp_minutes:          # 模拟老 leader 把时长截短
                    minutes = min(minutes, clamp_minutes)
                out = store.claim(payload["task"], payload["agent"], term=1,
                                  lease_minutes=minutes)
            elif action == "lease":
                out = store.renew(payload["task"], payload["agent"],
                                  payload["lease_id"], term=1,
                                  minutes=int(payload.get("minutes") or 0))
            else:
                return 200, {"ok": True}
        except Exception as exc:  # noqa: BLE001  与真实 leader 一样回 ok=False
            return 409, {"ok": False, "error": str(exc),
                         "code": getattr(exc, "code", "")}
        return 200, dict(out, ok=True)

    return _http


def _claim(repo: Path, monkeypatch, store: Store, tid: str,
           clamp_minutes: int | None = None) -> dict:
    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "store")
    monkeypatch.setattr(hive, "claim_backend", lambda: "store")
    monkeypatch.setattr(hive, "_cluster_http", _store_call(store, clamp_minutes))

    w = object.__new__(hive.Worker)
    w.name = "a"
    w.h = hive.Hive(str(repo))
    w.dir = w.h.dir
    w.last_claim_error = None
    monkeypatch.setattr(hive.Worker, "_reload_cfg", lambda self: {})

    assert w.claim(tid) is True, "隔离 store 上领取应当成功"
    return store.claim_info(tid) or {}


def _left_minutes(claim: dict) -> float:
    return (float(claim["lease_until"]) - time.time()) / 60.0


def _isolated_store(tmp_path: Path, tid: str) -> Store:
    store = Store(tmp_path / "hive.db", "node-a")
    store.ingest_card(tid, {"id": tid, "title": "租约取证卡", "state": "ready",
                            "repo": "light"}, commit=True)
    return store


def test_claim_uses_card_lease_minutes(tmp_path, monkeypatch):
    """卡面 300 → store 租约就是 300 分钟（不是 10）。"""
    repo = _repo(tmp_path, "T-0901", _card("T-0901"))
    claim = _claim(repo, monkeypatch, _isolated_store(tmp_path, "T-0901"), "T-0901")
    assert _left_minutes(claim) == pytest.approx(300, abs=2)


def test_claim_reads_origin_card_when_worktree_stale(tmp_path, monkeypatch):
    """新卡 push 后首领立即领取：本机卡面没跟上，也必须按 origin 的 300 算租约。

    反证用例：改回 `store_lease_minutes_of(self.h.card(tid))` 这条必须红。
    """
    repo = _repo(tmp_path, "T-0901", _card("T-0901", lease=False),
                 origin=_card("T-0901"))
    claim = _claim(repo, monkeypatch, _isolated_store(tmp_path, "T-0901"), "T-0901")
    assert _left_minutes(claim) == pytest.approx(300, abs=2), (
        "本机卡面落后于 origin/main，租约掉回 10 分钟了（T-0410 复发）")


def test_claim_keeps_ten_minutes_when_card_is_silent(tmp_path, monkeypatch):
    """卡面确实没写 lease_minutes：兜底 10 分钟的语义不变（红线）。"""
    repo = _repo(tmp_path, "T-0901", _card("T-0901", lease=False))
    claim = _claim(repo, monkeypatch, _isolated_store(tmp_path, "T-0901"), "T-0901")
    assert _left_minutes(claim) == pytest.approx(10, abs=1)


def test_claim_corrects_lease_clamped_by_leader(tmp_path, monkeypatch):
    """leader 只给了 10 分钟、卡面要 300：当场按卡面补差额，并留痕。"""
    repo = _repo(tmp_path, "T-0901", _card("T-0901"))
    store = _isolated_store(tmp_path, "T-0901")
    claim = _claim(repo, monkeypatch, store, "T-0901", clamp_minutes=10)
    assert _left_minutes(claim) == pytest.approx(300, abs=2), (
        "lease_until 与卡面不一致时必须回写纠正，而不是等十分钟后判超时")


def test_claim_does_not_double_extend_lease(tmp_path, monkeypatch):
    """一致就别动：纠正逻辑不能把 300 变成 600（续租是累加的）。"""
    repo = _repo(tmp_path, "T-0901", _card("T-0901"))
    claim = _claim(repo, monkeypatch, _isolated_store(tmp_path, "T-0901"), "T-0901")
    assert _left_minutes(claim) == pytest.approx(300, abs=2)


# --------------------------------------------------------------------------- #
# 补登记：缺卡自愈也要把 lease_minutes 一起送进 store
# --------------------------------------------------------------------------- #

def test_ingest_missing_card_prefers_origin_card(tmp_path, monkeypatch):
    """补登记送的是 origin 的完整卡面（带 lease_minutes），不是陈旧的那份。"""
    repo = _repo(tmp_path, "T-0901", _card("T-0901", lease=False),
                 origin=_card("T-0901"))
    sent = {}

    def fake_ingest(tid, card):
        sent.update(card)
        return {"ok": True, "ingested": True}

    monkeypatch.setattr(hive, "store_ingest_card", fake_ingest)
    monkeypatch.setattr(hive, "claim_backend", lambda: "store")

    w = object.__new__(hive.Worker)
    w.name = "a"
    w.h = hive.Hive(str(repo))
    w.dir = w.h.dir
    assert w._ingest_missing_card("T-0901") is True
    assert sent.get("lease_minutes") == 300, "补登记漏了 lease_minutes，等于把缺字段补进 store"
    assert "_body" not in sent and "_path" not in sent


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
