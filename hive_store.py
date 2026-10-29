#!/usr/bin/env python3
"""P2a：hive 控制面的本地全量副本（SQLite WAL）。

设计边界（先读）：
- 这是 **leader 单写** 的状态机；每个节点一份完整副本。
- leader 把一次变更算成 op，本地落盘后复制给 follower；follower 必须 fsync 后回 ACK。
- Git 仍是任务定义和最终归档的真相；store 承担运行时的 claim/lease/result/assign。
- P2a 先做 claim/lease/release/result/assign/close 的最小内核；dispatch 和归档
  仍走既有 Git/coordinator，后续 P2b 再接。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _now() -> float:
    return time.time()


def _iso(epoch: float | None = None) -> str:
    dt = datetime.fromtimestamp(epoch if epoch is not None else _now(),
                                tz=timezone.utc).astimezone()
    return dt.isoformat(timespec="seconds")


def _epoch(value) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


class StoreError(Exception):
    pass


class Store:
    def __init__(self, path: str | os.PathLike, node_id: str = ""):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.node_id = node_id
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False,
                                   isolation_level=None, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    # -- schema ------------------------------------------------------------ #
    def _migrate(self) -> None:
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'ready',
            owner TEXT NOT NULL DEFAULT '',
            retries INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            def_hash TEXT NOT NULL DEFAULT '',
            updated TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS claims (
            task_id TEXT PRIMARY KEY,
            lease_id TEXT NOT NULL,
            lease_seq INTEGER NOT NULL DEFAULT 0,
            owner TEXT NOT NULL,
            term INTEGER NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0,
            claimed_at TEXT NOT NULL DEFAULT '',
            renewed_at TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS results (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL DEFAULT '',
            term INTEGER NOT NULL DEFAULT 0,
            lease_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            accepted_at TEXT NOT NULL DEFAULT '',
            archived_at TEXT NOT NULL DEFAULT '',
            archive_sha TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS ops (
            seq INTEGER PRIMARY KEY,
            term INTEGER NOT NULL DEFAULT 0,
            op_id TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL,
            task_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            ts TEXT NOT NULL DEFAULT ''
        );
        """)
        self._db.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),))
        self._db.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('last_seq','0')")

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # -- meta -------------------------------------------------------------- #
    def _meta(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def last_seq(self) -> int:
        with self._lock:
            return int(self._meta("last_seq", "0") or 0)

    def summary(self) -> dict:
        with self._lock:
            tasks = self._db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
            claims = self._db.execute(
                "SELECT COUNT(*) AS n FROM claims WHERE state='active'").fetchone()["n"]
            results = self._db.execute("SELECT COUNT(*) AS n FROM results").fetchone()["n"]
            return {"tasks": int(tasks), "claims": int(claims), "results": int(results),
                    "last_seq": self.last_seq(), "node": self.node_id,
                    "path": str(self.path)}

    # -- 读 ---------------------------------------------------------------- #
    def task(self, tid: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (str(tid),)).fetchone()
            return self._task_row(row) if row else None

    def claim_info(self, tid: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM claims WHERE task_id=?",
                                   (str(tid),)).fetchone()
            return self._claim_row(row) if row else None

    def result_info(self, tid: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM results WHERE task_id=?",
                                   (str(tid),)).fetchone()
            return self._result_row(row) if row else None

    def tasks(self) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM tasks ORDER BY id").fetchall()
            return {str(r["id"]): self._task_row(r) for r in rows}

    def ops_since(self, seq: int = 0, limit: int = 1000) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM ops WHERE seq>? ORDER BY seq LIMIT ?",
                (int(seq), int(limit))).fetchall()
            out = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except ValueError:
                    payload = {}
                out.append({"seq": int(row["seq"]), "term": int(row["term"]),
                            "op_id": row["op_id"], "type": row["type"],
                            "task_id": row["task_id"], "ts": row["ts"],
                            "payload": payload})
            return out

    # -- 任务定义（从 Git 摄入） -------------------------------------------- #
    def ingest_cards(self, cards: dict[str, dict]) -> int:
        """把 Git 任务卡写进 store；已有运行时状态不覆盖。"""
        n = 0
        with self._lock:
            for tid, card in cards.items():
                payload = {k: v for k, v in card.items()
                           if not str(k).startswith("_")}
                def_hash = str(card.get("updated") or card.get("created")
                               or card.get("title") or "")
                row = self._db.execute("SELECT id FROM tasks WHERE id=?",
                                       (tid,)).fetchone()
                if row is None:
                    self._db.execute(
                        "INSERT INTO tasks(id,state,owner,retries,payload_json,"
                        "def_hash,updated) VALUES(?,?,?,?,?,?,?)",
                        (tid, str(card.get("state") or "ready"),
                         str(card.get("owner") or ""),
                         int(card.get("retries") or 0),
                         json.dumps(payload, ensure_ascii=False), def_hash, _iso()))
                else:
                    self._db.execute(
                        "UPDATE tasks SET payload_json=?, def_hash=?, updated=? "
                        "WHERE id=?", (json.dumps(payload, ensure_ascii=False),
                                       def_hash, _iso(), tid))
                n += 1
        return n

    # -- 写：leader 计算快照，op 里带结果行 -------------------------------- #
    def _next_seq_locked(self) -> int:
        return int(self._meta("last_seq", "0") or 0) + 1

    def _append_op_locked(self, op: dict) -> int:
        seq = int(op.get("seq") or self._next_seq_locked())
        try:
            self._db.execute(
                "INSERT INTO ops(seq,term,op_id,type,task_id,payload_json,ts) "
                "VALUES(?,?,?,?,?,?,?)",
                (seq, int(op.get("term") or 0), str(op["op_id"]),
                 str(op["type"]), str(op.get("task_id") or ""),
                 json.dumps(op.get("payload") or {}, ensure_ascii=False),
                 str(op.get("ts") or _iso())))
        except sqlite3.IntegrityError:
            row = self._db.execute("SELECT seq FROM ops WHERE op_id=?",
                                   (str(op["op_id"]),)).fetchone()
            return int(row["seq"]) if row else seq
        self._set_meta("last_seq", str(seq))
        return seq

    def apply_op(self, op: dict) -> int:
        """幂等应用一个 op（leader 本地或 follower 复制）。"""
        with self._lock:
            existing = self._db.execute(
                "SELECT seq FROM ops WHERE op_id=?", (str(op.get("op_id") or ""),)
            ).fetchone()
            if existing:
                return int(existing["seq"])
            seq = self._append_op_locked(op)
            payload = op.get("payload") or {}
            if isinstance(payload.get("task"), dict):
                self._upsert_task(payload["task"])
            if "claim" in payload:
                if payload.get("claim") is None:
                    self._db.execute("DELETE FROM claims WHERE task_id=?",
                                     (str(op.get("task_id") or ""),))
                else:
                    self._upsert_claim(payload.get("claim"))
            if "result" in payload:
                self._upsert_result(payload.get("result"))
            return seq

    def _upsert_task(self, task: dict | None) -> None:
        if not isinstance(task, dict) or not task.get("id"):
            return
        self._db.execute(
            "INSERT INTO tasks(id,state,owner,retries,payload_json,def_hash,updated) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "state=excluded.state, owner=excluded.owner, retries=excluded.retries, "
            "payload_json=excluded.payload_json, updated=excluded.updated",
            (str(task["id"]), str(task.get("state") or "ready"),
             str(task.get("owner") or ""), int(task.get("retries") or 0),
             json.dumps({k: v for k, v in task.items()
                        if k not in ("id", "state", "owner", "retries",
                                     "updated", "def_hash")},
                       ensure_ascii=False),
             str(task.get("def_hash") or ""), str(task.get("updated") or _iso())))

    def _upsert_claim(self, claim: dict | None) -> None:
        if claim is None:
            return
        if not isinstance(claim, dict) or not claim.get("task_id"):
            return
        self._db.execute(
            "INSERT INTO claims(task_id,lease_id,lease_seq,owner,term,lease_until,"
            "claimed_at,renewed_at,state) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET lease_id=excluded.lease_id, "
            "lease_seq=excluded.lease_seq, owner=excluded.owner, term=excluded.term, "
            "lease_until=excluded.lease_until, claimed_at=excluded.claimed_at, "
            "renewed_at=excluded.renewed_at, state=excluded.state",
            (str(claim["task_id"]), str(claim.get("lease_id") or ""),
             int(claim.get("lease_seq") or 0), str(claim.get("owner") or ""),
             int(claim.get("term") or 0), float(claim.get("lease_until") or 0),
             str(claim.get("claimed_at") or ""), str(claim.get("renewed_at") or ""),
             str(claim.get("state") or "active")))

    def _upsert_result(self, result: dict | None) -> None:
        if result is None:
            return
        if not isinstance(result, dict) or not result.get("task_id"):
            return
        self._db.execute(
            "INSERT INTO results(task_id,status,agent,term,lease_id,payload_json,"
            "accepted_at,archived_at,archive_sha) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, "
            "agent=excluded.agent, term=excluded.term, lease_id=excluded.lease_id, "
            "payload_json=excluded.payload_json, accepted_at=excluded.accepted_at, "
            "archived_at=excluded.archived_at, archive_sha=excluded.archive_sha",
            (str(result["task_id"]), str(result.get("status") or ""),
             str(result.get("agent") or ""), int(result.get("term") or 0),
             str(result.get("lease_id") or ""),
             json.dumps(result.get("payload") or {}, ensure_ascii=False),
             str(result.get("accepted_at") or ""),
             str(result.get("archived_at") or ""),
             str(result.get("archive_sha") or "")))

    # -- 业务动作（leader） ------------------------------------------------- #
    def claim(self, tid: str, agent: str, term: int, lease_minutes: int,
              host: str = "", op_id: str = "") -> dict:
        lease_seconds = max(60, int(lease_minutes) * 60)
        with self._lock:
            if op_id:
                row = self._db.execute("SELECT seq FROM ops WHERE op_id=?",
                                       (str(op_id),)).fetchone()
                if row:
                    return {"ok": True, "idempotent": True,
                            "claim": self.claim_info(tid), "task": self.task(tid),
                            "seq": int(row["seq"])}
            task = self.task(tid)
            if task is None:
                raise StoreError(f"没有任务 {tid}")
            if self.result_info(tid):
                raise StoreError(f"{tid} 已经有结果")
            cur = self.claim_info(tid)
            now = _now()
            if cur and cur.get("state") == "active" and cur.get("lease_until", 0) > now:
                if cur.get("owner") == agent:
                    return {"ok": True, "idempotent": True, "claim": cur,
                            "task": task, "seq": self.last_seq()}
                raise StoreError(f"{tid} 已被 {cur.get('owner')} 领取")
            seq = int(cur.get("lease_seq") or 0) + 1 if cur else 1
            claim = {
                "task_id": tid, "lease_id": uuid.uuid4().hex, "lease_seq": seq,
                "owner": agent, "term": int(term),
                "lease_until": now + lease_seconds,
                "claimed_at": _iso(), "renewed_at": _iso(), "state": "active",
            }
            task = dict(task)
            task.update({"state": "claimed", "owner": agent, "updated": _iso()})
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "claim",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"task": task, "claim": claim}}
            op["seq"] = self.apply_op(op)
            return {"ok": True, "idempotent": False, "claim": claim,
                    "task": task, "seq": op["seq"]}

    def renew(self, tid: str, agent: str, lease_id: str, term: int,
              minutes: int, op_id: str = "") -> dict:
        with self._lock:
            cur = self.claim_info(tid)
            if not cur or cur.get("state") != "active":
                raise StoreError(f"{tid} 没有有效租约")
            if cur.get("owner") != agent or cur.get("lease_id") != lease_id:
                raise StoreError(f"{tid} 的租约不属于 {agent}/{lease_id}")
            if float(cur.get("lease_until") or 0) <= _now():
                raise StoreError(f"{tid} 的租约已过期，不能再续")
            claim = dict(cur)
            claim["lease_until"] = max(_now(), float(cur.get("lease_until") or 0)) \
                + max(60, int(minutes) * 60)
            claim["renewed_at"] = _iso()
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "lease",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"claim": claim}}
            op["seq"] = self.apply_op(op)
            return {"ok": True, "claim": claim, "seq": op["seq"]}

    def release(self, tid: str, agent: str, lease_id: str, term: int,
                requeue: bool = True, op_id: str = "") -> dict:
        with self._lock:
            cur = self.claim_info(tid)
            if cur and (cur.get("owner") != agent
                        or (lease_id and cur.get("lease_id") != lease_id)):
                raise StoreError(f"{tid} 的租约不属于 {agent}")
            task = self.task(tid) or {"id": tid}
            task = dict(task)
            has_result = self.result_info(tid) is not None
            task.update({"state": ("submitted" if has_result
                                   else ("ready" if requeue else "blocked")),
                         "owner": "", "updated": _iso()})
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "release",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"task": task, "claim": None}}
            op["seq"] = self.apply_op(op)
            return {"ok": True, "task": task, "seq": op["seq"]}

    def result(self, tid: str, agent: str, lease_id: str, term: int,
               payload: dict, status: str = "done", op_id: str = "") -> dict:
        with self._lock:
            if op_id:
                row = self._db.execute("SELECT seq FROM ops WHERE op_id=?",
                                       (str(op_id),)).fetchone()
                if row:
                    return {"ok": True, "idempotent": True,
                            "result": self.result_info(tid), "task": self.task(tid),
                            "seq": int(row["seq"])}
            cur = self.claim_info(tid)
            if cur and (cur.get("owner") != agent
                        or (lease_id and cur.get("lease_id") != lease_id)):
                raise StoreError(f"{tid} 的租约不属于 {agent}")
            if cur and float(cur.get("lease_until") or 0) <= _now():
                raise StoreError(f"{tid} 的租约已过期，结果被拒（lease lost）")
            task = dict(self.task(tid) or {"id": tid})
            task.update({"state": "submitted", "owner": agent, "updated": _iso()})
            result = {
                "task_id": tid, "status": str(status or "done"),
                "agent": agent, "term": int(term), "lease_id": lease_id,
                "payload": payload or {}, "accepted_at": _iso(),
                "archived_at": "", "archive_sha": "",
            }
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "result",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"task": task, "result": result}}
            op["seq"] = self.apply_op(op)
            return {"ok": True, "task": task, "result": result, "seq": op["seq"]}

    def assign(self, tid: str, agent: str, term: int, op_id: str = "") -> dict:
        with self._lock:
            task = dict(self.task(tid) or {"id": tid})
            task.update({"state": "assigned", "owner": agent, "updated": _iso()})
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "assign",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"task": task}}
            op["seq"] = self.apply_op(op)
            return {"ok": True, "task": task, "seq": op["seq"]}

    def close(self, tid: str, done: bool = True, note: str = "",
              term: int = 0, op_id: str = "") -> dict:
        with self._lock:
            task = dict(self.task(tid) or {"id": tid})
            task.update({"state": "done" if done else "closed", "owner": "",
                         "updated": _iso()})
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "close",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"task": task, "claim": None,
                              "note": str(note or "")}}
            op["seq"] = self.apply_op(op)
            return {"ok": True, "task": task, "seq": op["seq"]}

    # -- 行转换 ------------------------------------------------------------ #
    @staticmethod
    def _task_row(row) -> dict:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.update({"id": row["id"], "state": row["state"],
                        "owner": row["owner"], "retries": int(row["retries"]),
                        "updated": row["updated"]})
        return payload

    @staticmethod
    def _claim_row(row) -> dict:
        return {"task_id": row["task_id"], "lease_id": row["lease_id"],
                "lease_seq": int(row["lease_seq"]), "owner": row["owner"],
                "term": int(row["term"]), "lease_until": float(row["lease_until"]),
                "claimed_at": row["claimed_at"], "renewed_at": row["renewed_at"],
                "state": row["state"]}

    @staticmethod
    def _result_row(row) -> dict:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            payload = {}
        return {"task_id": row["task_id"], "status": row["status"],
                "agent": row["agent"], "term": int(row["term"]),
                "lease_id": row["lease_id"], "payload": payload,
                "accepted_at": row["accepted_at"],
                "archived_at": row["archived_at"],
                "archive_sha": row["archive_sha"]}
