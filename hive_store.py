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

SCHEMA_VERSION = 2
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
# 摘要行里 summary_head 的长度上限：列表只用它做一行提示，不是正文。
SUMMARY_HEAD_MAX = 120


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


def summary_head(text, limit: int = SUMMARY_HEAD_MAX) -> str:
    """结果 summary 的「一行提示」：取首行非空文本，截到 limit 字。

    列表页只要「这张卡干完了什么」的一句话；整段结论属于详情，不进摘要。
    """
    if not text:
        return ""
    for line in str(text).splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


class StoreError(Exception):
    """store 业务错误。code 给上层做机器可读的分类，别再去解析中文文案。

    E_NO_TASK     leader store 里没有这张卡（常见于启动后新增的卡没被摄入）
    E_TAKEN       已有其他 agent 持有有效租约
    E_CLOSED      卡已 done/closed
    E_HAS_RESULT  卡已有结果，不该再领
    """

    def __init__(self, message: str, code: str = "E_STORE"):
        super().__init__(message)
        self.code = code


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
            ts TEXT NOT NULL DEFAULT '',
            committed INTEGER NOT NULL DEFAULT 1
        );
        """)
        self._db.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),))
        self._db.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('last_seq','0')")
        self._db.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('commit_seq','0')")
        self._db.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('compacted_through','0')")
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(ops)")}
        if "committed" not in cols:
            self._db.execute("ALTER TABLE ops ADD COLUMN committed INTEGER NOT NULL DEFAULT 1")
        self._migrate_v2()

    # -- v1 -> v2：列表摘要表 ---------------------------------------------- #
    def _migrate_v2(self) -> None:
        """加 `task_summary` 与两张索引，版本号落在 `PRAGMA user_version`。

        幂等性来自三处：DDL 全带 `IF NOT EXISTS`、`PRAGMA user_version = 2` 重复写
        不改变结果、老库不重建任何既有表也不搬数据。所以连跑两次（同一路径开两次
        `Store`）结构必须逐字一致 —— 这是 T-0219 判据 1 的直接来源。

        版本号用 SQLite 原生 `user_version` 而不是 `meta.schema_version`：前者跟着
        库文件走、不会被业务代码顺手改掉；`meta` 里那条同步写只为兼容旧读者。
        """
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS task_summary (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL DEFAULT '',
            state        TEXT NOT NULL DEFAULT '',
            repo         TEXT NOT NULL DEFAULT '',
            priority     TEXT NOT NULL DEFAULT '',
            owner        TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL DEFAULT '',
            finished_at  TEXT NOT NULL DEFAULT '',
            summary_head TEXT NOT NULL DEFAULT '',
            archived     INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_task_summary_state_finished
            ON task_summary(state, finished_at);
        CREATE INDEX IF NOT EXISTS idx_task_summary_repo_state
            ON task_summary(repo, state);
        """)
        current = int(self._db.execute("PRAGMA user_version").fetchone()[0] or 0)
        if current < SCHEMA_VERSION:
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if self._meta("schema_version", "") != str(SCHEMA_VERSION):
            self._set_meta("schema_version", str(SCHEMA_VERSION))

    def schema_version(self) -> int:
        """库文件自报的 schema 版本（`PRAGMA user_version`）。"""
        with self._lock:
            return int(self._db.execute("PRAGMA user_version").fetchone()[0] or 0)


    def close_conn(self) -> None:
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

    def commit_seq(self) -> int:
        with self._lock:
            return int(self._meta("commit_seq", "0") or 0)

    def compacted_through(self) -> int:
        with self._lock:
            return int(self._meta("compacted_through", "0") or 0)

    def compact(self, keep: int = 1000) -> int:
        """删除已提交且不再需要回放的旧 op；新节点用 snapshot 追平。"""
        with self._lock:
            cutoff = self.commit_seq() - max(0, int(keep))
            if cutoff <= self.compacted_through():
                return 0
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "DELETE FROM ops WHERE seq<=? AND committed=1", (cutoff,))
                self._set_meta("compacted_through", str(cutoff))
                self._db.execute("COMMIT")
                return cutoff
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def check(self) -> list[str]:
        with self._lock:
            rows = self._db.execute("PRAGMA integrity_check").fetchall()
            return [str(r[0]) for r in rows]

    def snapshot(self) -> dict:
        with self._lock:
            claims = {}
            for tid in self._db.execute("SELECT task_id FROM claims"):
                claim = self.claim_info(str(tid["task_id"]))
                if claim:
                    claims[str(tid["task_id"])] = claim
            results = {}
            for row in self._db.execute("SELECT task_id FROM results"):
                tid = str(row["task_id"])
                res = self.result_info(tid)
                if res:
                    results[tid] = res
            return {"commit_seq": self.commit_seq(), "last_seq": self.last_seq(),
                    "compacted_through": self.compacted_through(),
                    "tasks": self.tasks(), "claims": claims, "results": results,
                    "summaries": self.summaries()}

    def import_snapshot(self, snap: dict) -> int:
        if not isinstance(snap, dict):
            raise StoreError("snapshot 不是对象")
        commit = int(snap.get("commit_seq") or 0)
        with self._lock:
            if commit < self.commit_seq():
                return self.commit_seq()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM tasks")
                self._db.execute("DELETE FROM claims")
                self._db.execute("DELETE FROM results")
                self._db.execute("DELETE FROM ops")
                self._db.execute("DELETE FROM task_summary")
                for task in (snap.get("tasks") or {}).values():
                    self._upsert_task(task)
                for claim in (snap.get("claims") or {}).values():
                    self._upsert_claim(claim)
                for res in (snap.get("results") or {}).values():
                    self._upsert_result(res)
                # 摘要里的 archived 是生命周期标记，snapshot 带过来就还原，
                # 否则新节点会把「已移出热窗口」的卡重新算成热卡。
                for srow in (snap.get("summaries") or {}).values():
                    if isinstance(srow, dict) and srow.get("id") and srow.get("archived"):
                        self.set_summary_archived(str(srow["id"]), True)
                self._set_meta("commit_seq", str(commit))
                self._set_meta("last_seq", str(max(commit, int(
                    snap.get("last_seq") or commit))))
                self._set_meta("compacted_through", str(int(
                    snap.get("compacted_through") or commit)))
                self._db.execute("COMMIT")
                return commit
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def summary(self) -> dict:
        with self._lock:
            tasks = self._db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
            claims = self._db.execute(
                "SELECT COUNT(*) AS n FROM claims WHERE state='active'").fetchone()["n"]
            results = self._db.execute("SELECT COUNT(*) AS n FROM results").fetchone()["n"]
            pending = self._db.execute(
                "SELECT COUNT(*) AS n FROM results WHERE archived_at=''").fetchone()["n"]
            # 热/冷卡计数（T-0219）：列表页只看热卡，归档数单独报，便于对账。
            archived = self._db.execute(
                "SELECT COUNT(*) AS n FROM task_summary WHERE archived=1").fetchone()["n"]
            return {"tasks": int(tasks), "claims": int(claims), "results": int(results),
                    "archive_pending": int(pending),
                    "tasks_archived": int(archived),
                    "tasks_hot": int(tasks) - int(archived),
                    "schema_version": self.schema_version(),
                    "last_seq": self.last_seq(), "commit_seq": self.commit_seq(),
                    "compacted_through": self.compacted_through(),
                    "node": self.node_id, "path": str(self.path)}

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

    def pending_results(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM results WHERE archived_at='' ORDER BY accepted_at LIMIT ?",
                (int(limit),)).fetchall()
            return [self._result_row(r) for r in rows]

    def mark_archived(self, tid: str, sha: str = "", op_id: str = "",
                      term: int = 0, commit: bool = True) -> dict:
        with self._lock:
            cur = self.result_info(tid)
            if not cur:
                raise StoreError(f"没有结果 {tid}")
            if cur.get("archived_at"):
                return {"ok": True, "idempotent": True, "archived_at":
                        cur.get("archived_at"), "seq": 0}
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "archive",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"archive": {"task_id": tid,
                                          "archived_at": _iso(),
                                          "archive_sha": str(sha or "")}}}
            op["seq"] = self.apply_op(op) if commit else self.stage_op(op)
            return {"ok": True, "idempotent": False,
                    "archived_at": op["payload"]["archive"]["archived_at"],
                    "seq": op["seq"]}

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
                            "committed": int(row["committed"] or 0),
                            "payload": payload})
            return out

    # -- 任务定义（从 Git 摄入） -------------------------------------------- #
    def ingest_cards(self, cards: dict[str, dict], batch: int = 200) -> int:
        """把 Git 任务卡写进 store；已有运行时状态不覆盖。

        2026-11-10 实测（T-0327）：连接是 ``isolation_level=None``（autocommit），
        逐条 execute 等于逐条 fsync，PRAGMA synchronous=FULL 之下 194 张卡的
        补写要 **18 秒**，期间整条 store 锁被占住，持主锁的心跳/选举跟着排队，
        集群对外表现为「20 秒完全无响应」。改成单事务批量提交后同一批只要
        **66 毫秒**（约 274 倍）。

        ``batch`` 让超大批次分段提交：每段结束释放一次 store 锁，让 claim /
        选举 / 心跳有机会插进来，而不是一次性霸占整把锁。
        """
        if not cards:
            return 0
        items = list(cards.items())
        size = max(1, int(batch or 200))
        n = 0
        for start in range(0, len(items), size):
            chunk = items[start:start + size]
            with self._lock:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    for tid, card in chunk:
                        payload = {k: v for k, v in card.items()
                                   if not str(k).startswith("_")}
                        def_hash = str(card.get("updated") or card.get("created")
                                       or card.get("title") or "")
                        row = self._db.execute(
                            "SELECT id FROM tasks WHERE id=?", (tid,)).fetchone()
                        if row is None:
                            self._db.execute(
                                "INSERT INTO tasks(id,state,owner,retries,"
                                "payload_json,def_hash,updated) "
                                "VALUES(?,?,?,?,?,?,?)",
                                (tid, str(card.get("state") or "ready"),
                                 str(card.get("owner") or ""),
                                 int(card.get("retries") or 0),
                                 json.dumps(payload, ensure_ascii=False),
                                 def_hash, _iso()))
                        else:
                            self._db.execute(
                                "UPDATE tasks SET payload_json=?, def_hash=?, "
                                "updated=? WHERE id=?",
                                (json.dumps(payload, ensure_ascii=False),
                                 def_hash, _iso(), tid))
                        self._sync_summary_locked(tid)
                        n += 1
                    self._db.execute("COMMIT")
                except Exception:
                    try:
                        self._db.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
        return n

    # -- 写：leader 计算快照，op 里带结果行 -------------------------------- #
    def _next_seq_locked(self) -> int:
        return int(self._meta("last_seq", "0") or 0) + 1

    def _append_op_locked(self, op: dict, committed: int = 1) -> int:
        seq = int(op.get("seq") or self._next_seq_locked())
        try:
            self._db.execute(
                "INSERT INTO ops(seq,term,op_id,type,task_id,payload_json,ts,committed) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (seq, int(op.get("term") or 0), str(op["op_id"]),
                 str(op["type"]), str(op.get("task_id") or ""),
                 json.dumps(op.get("payload") or {}, ensure_ascii=False),
                 str(op.get("ts") or _iso()), int(committed)))
        except sqlite3.IntegrityError:
            row = self._db.execute("SELECT seq FROM ops WHERE op_id=?",
                                   (str(op["op_id"]),)).fetchone()
            return int(row["seq"]) if row else seq
        self._set_meta("last_seq", str(seq))
        return seq

    def apply_op(self, op: dict) -> int:
        """幂等应用一个 op（测试/兼容路径：追加即提交）。"""
        with self._lock:
            existing = self._db.execute(
                "SELECT seq FROM ops WHERE op_id=?", (str(op.get("op_id") or ""),)
            ).fetchone()
            if existing:
                return int(existing["seq"])
            self._db.execute("BEGIN IMMEDIATE")
            try:
                seq = self._append_op_locked(op, committed=1)
                self._apply_payload_locked(op)
                self._set_meta("commit_seq", str(max(self.commit_seq(), seq)))
                self._db.execute("COMMIT")
                return seq
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def stage_op(self, op: dict) -> int:
        """只落 op 日志，不改状态；leader 拿到多数 ACK 后再 commit。"""
        with self._lock:
            existing = self._db.execute(
                "SELECT seq FROM ops WHERE op_id=?", (str(op.get("op_id") or ""),)
            ).fetchone()
            if existing:
                return int(existing["seq"])
            self._db.execute("BEGIN IMMEDIATE")
            try:
                seq = self._append_op_locked(op, committed=0)
                self._db.execute("COMMIT")
                return seq
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def commit_op(self, seq: int) -> int:
        """leader/follower 把 staged op 真正应用到状态机。"""
        with self._lock:
            row = self._db.execute("SELECT * FROM ops WHERE seq=?",
                                   (int(seq),)).fetchone()
            if row is None:
                raise StoreError(f"没有 op seq={seq}")
            if int(row["committed"] or 0):
                self._set_meta("commit_seq", str(max(self.commit_seq(), int(seq))))
                return int(seq)
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except ValueError:
                payload = {}
            op = {"seq": int(seq), "term": int(row["term"]),
                  "op_id": row["op_id"], "type": row["type"],
                  "task_id": row["task_id"], "payload": payload}
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._apply_payload_locked(op)
                self._db.execute("UPDATE ops SET committed=1 WHERE seq=?", (int(seq),))
                self._set_meta("commit_seq", str(max(self.commit_seq(), int(seq))))
                self._db.execute("COMMIT")
                return int(seq)
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def truncate_uncommitted(self) -> int:
        """新 leader 上任：丢弃未提交的尾部 op，把 last_seq 对齐 commit_seq。"""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM ops WHERE committed=0")
                commit_seq = self.commit_seq()
                self._set_meta("last_seq", str(commit_seq))
                self._db.execute("COMMIT")
                return commit_seq
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _apply_payload_locked(self, op: dict) -> None:
        payload = op.get("payload") or {}
        if isinstance(payload.get("task"), dict):
            self._upsert_task(payload["task"])
        if "claim" in payload:
            if payload.get("claim") is None:
                self._db.execute("DELETE FROM claims WHERE task_id=?",
                                 (str(op.get("task_id") or ""),))
            else:
                self._upsert_claim(payload.get("claim"))
        if payload.get("clear_result"):
            self._db.execute("DELETE FROM results WHERE task_id=?",
                             (str(op.get("task_id") or ""),))
            self._sync_summary_locked(str(op.get("task_id") or ""))
        if "result" in payload:
            self._upsert_result(payload.get("result"))
        archive = payload.get("archive")
        if isinstance(archive, dict) and archive.get("task_id"):
            self._db.execute(
                "UPDATE results SET archived_at=?, archive_sha=? WHERE task_id=?",
                (str(archive.get("archived_at") or _iso()),
                 str(archive.get("archive_sha") or ""),
                 str(archive.get("task_id"))))
            self._sync_summary_locked(str(archive.get("task_id")))

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
        self._sync_summary_locked(str(task["id"]))

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
        self._sync_summary_locked(str(result["task_id"]))

    # -- 业务动作（leader） ------------------------------------------------- #
    def claim(self, tid: str, agent: str, term: int, lease_minutes: int,
              host: str = "", op_id: str = "", commit: bool = True) -> dict:
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
                # 以前这句和「被别人领走」在调用侧一样被翻译成「抢不到」，
                # 害人以为卡被占了，其实是 leader 压根没摄入这张新卡。
                raise StoreError(f"没有任务 {tid}", code="E_NO_TASK")
            if str(task.get("state") or "") in ("done", "closed"):
                raise StoreError(f"{tid} 已关闭", code="E_CLOSED")
            if self.result_info(tid):
                raise StoreError(f"{tid} 已经有结果", code="E_HAS_RESULT")
            cur = self.claim_info(tid)
            now = _now()
            if cur and cur.get("state") == "active" and cur.get("lease_until", 0) > now:
                if cur.get("owner") == agent:
                    return {"ok": True, "idempotent": True, "claim": cur,
                            "task": task, "seq": self.last_seq()}
                raise StoreError(f"{tid} 已被 {cur.get('owner')} 领取",
                                 code="E_TAKEN")
            seq = int(cur.get("lease_seq") or 0) + 1 if cur else 1
            claim = {
                "task_id": tid, "lease_id": uuid.uuid4().hex, "lease_seq": seq,
                "owner": agent, "term": int(term),
                "lease_until": now + lease_seconds,
                "claimed_at": _iso(), "renewed_at": _iso(), "state": "active",
            }
            task = dict(task)
            task.update({"state": "running", "owner": agent, "updated": _iso()})
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "claim",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"task": task, "claim": claim}}
            op["seq"] = self.apply_op(op) if commit else self.stage_op(op)
            return {"ok": True, "idempotent": False, "claim": claim,
                    "task": task, "seq": op["seq"]}

    def renew(self, tid: str, agent: str, lease_id: str, term: int,
              minutes: int, op_id: str = "", commit: bool = True) -> dict:
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
            op["seq"] = self.apply_op(op) if commit else self.stage_op(op)
            return {"ok": True, "claim": claim, "seq": op["seq"]}

    def release(self, tid: str, agent: str, lease_id: str, term: int,
                requeue: bool = True, op_id: str = "", commit: bool = True) -> dict:
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
            op["seq"] = self.apply_op(op) if commit else self.stage_op(op)
            return {"ok": True, "task": task, "seq": op["seq"]}

    def result(self, tid: str, agent: str, lease_id: str, term: int,
               payload: dict, status: str = "done", op_id: str = "",
               commit: bool = True) -> dict:
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
            op["seq"] = self.apply_op(op) if commit else self.stage_op(op)
            return {"ok": True, "task": task, "result": result, "seq": op["seq"]}

    def assign(self, tid: str, agent: str, term: int, op_id: str = "",
               commit: bool = True) -> dict:
        with self._lock:
            task = dict(self.task(tid) or {"id": tid})
            task.update({"state": "assigned", "owner": agent, "updated": _iso()})
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "assign",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"task": task}}
            op["seq"] = self.apply_op(op) if commit else self.stage_op(op)
            return {"ok": True, "task": task, "seq": op["seq"]}

    def ingest_card(self, tid: str, card: dict, term: int = 0,
                    op_id: str = "", commit: bool = True) -> dict:
        """把一张卡面登记进 store，走 op 日志（所以会复制到所有副本）。

        用途：leader 进程启动后新增的卡，启动那次 ingest_cards 没扫到；
        follower 手上有最新 Git 卡面，把定义发过来让 leader 补上。

        * 缺行 → 按卡面插入（state/owner/retries 用卡面值）；
        * 已有行 → **只补卡面定义**，保留 store 里的运行时 state/owner/retries，
          绝不把 running/done 翻回 ready（和 ingest_cards 同一口径）。
        """
        with self._lock:
            row = self.task(tid)
            src = {k: v for k, v in (card or {}).items()
                   if not str(k).startswith("_")}
            if row is None:
                task = {
                    "id": tid,
                    "state": str(src.get("state") or "ready"),
                    "owner": str(src.get("owner") or ""),
                    "retries": int(src.get("retries") or 0),
                }
            else:
                task = {
                    "id": tid,
                    "state": str(row.get("state") or "ready"),
                    "owner": str(row.get("owner") or ""),
                    "retries": int(row.get("retries") or 0),
                }
            keep = ("id", "state", "owner", "retries", "updated", "def_hash")
            task.update({k: v for k, v in src.items() if k not in keep})
            task["def_hash"] = str(src.get("updated") or src.get("created")
                                   or src.get("title") or "")
            task["updated"] = _iso()
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "ingest",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"task": task}}
            op["seq"] = self.apply_op(op) if commit else self.stage_op(op)
            return {"ok": True, "ingested": row is None, "task": task,
                    "seq": op["seq"]}

    def retry(self, tid: str, agent: str = "", term: int = 0, op_id: str = "",
              commit: bool = True, clear_result: bool = True) -> dict:
        with self._lock:
            task = dict(self.task(tid) or {"id": tid})
            task.update({"state": "assigned" if agent else "ready",
                         "owner": agent, "updated": _iso()})
            try:
                task["retries"] = int(task.get("retries") or 0) + 1
            except (TypeError, ValueError):
                task["retries"] = 1
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "retry",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"task": task, "claim": None,
                              "clear_result": bool(clear_result)}}
            op["seq"] = self.apply_op(op) if commit else self.stage_op(op)
            return {"ok": True, "task": task, "seq": op["seq"]}

    def close(self, tid: str, done: bool = True, note: str = "",
              term: int = 0, op_id: str = "", commit: bool = True) -> dict:
        with self._lock:
            task = dict(self.task(tid) or {"id": tid})
            task.update({"state": "done" if done else "closed", "owner": "",
                         "updated": _iso()})
            op = {"op_id": op_id or uuid.uuid4().hex, "type": "close",
                  "task_id": tid, "term": int(term), "ts": _iso(),
                  "payload": {"task": task, "claim": None,
                              "note": str(note or "")}}
            op["seq"] = self.apply_op(op) if commit else self.stage_op(op)
            return {"ok": True, "task": task, "seq": op["seq"]}

    # -- 摘要表（列表用物化视图，T-0219） ----------------------------------- #
    def _sync_summary_locked(self, tid: str) -> None:
        """按 tasks/results 当前内容重算一行摘要。

        `archived` **不由这里推导**：它是「这张卡已移出热窗口」的生命周期标记，
        只由 `set_summary_archived()`（归档/回滚时调用）改写。这里读写一遍保留原值，
        否则每次 claim/result 都会把刚打的归档标记抹掉。
        """
        tid = str(tid)
        row = self._db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        res = self._db.execute("SELECT * FROM results WHERE task_id=?",
                               (tid,)).fetchone()
        if row is None and res is None:
            self._db.execute("DELETE FROM task_summary WHERE id=?", (tid,))
            return
        try:
            payload = json.loads(row["payload_json"] or "{}") if row is not None else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        state = str(row["state"] or "") if row is not None else ""
        owner = str(row["owner"] or "") if row is not None else ""
        finished_at, head = "", ""
        if res is not None:
            finished_at = str(res["accepted_at"] or "")
            try:
                rp = json.loads(res["payload_json"] or "{}")
            except ValueError:
                rp = {}
            if isinstance(rp, dict):
                finished_at = str(rp.get("finished_at") or finished_at)
                head = summary_head(rp.get("summary"))
            if str(res["status"] or "") in ("done", "") and not state:
                state = "done"
        prev = self._db.execute("SELECT archived FROM task_summary WHERE id=?",
                                (tid,)).fetchone()
        archived = int(prev["archived"] or 0) if prev is not None else 0
        self._db.execute(
            "INSERT INTO task_summary(id,title,state,repo,priority,owner,"
            "created_at,finished_at,summary_head,archived) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "title=excluded.title, state=excluded.state, repo=excluded.repo, "
            "priority=excluded.priority, owner=excluded.owner, "
            "created_at=excluded.created_at, finished_at=excluded.finished_at, "
            "summary_head=excluded.summary_head, archived=excluded.archived",
            (tid, str(payload.get("title") or ""), state,
             str(payload.get("repo") or ""), str(payload.get("priority") or ""),
             owner, str(payload.get("created") or ""), finished_at, head, archived))

    def refresh_summaries(self) -> int:
        """从既有 tasks/results 全量重建摘要（老库升级、或怀疑漂移时手动跑）。

        保留已有 `archived` 标记：重建只负责「派生字段」，归档状态是人的决定。
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                ids = [str(r["id"]) for r in self._db.execute("SELECT id FROM tasks")]
                ids += [str(r["task_id"]) for r in self._db.execute(
                    "SELECT task_id FROM results")]
                seen, n = set(), 0
                for tid in ids:
                    if tid in seen:
                        continue
                    seen.add(tid)
                    self._sync_summary_locked(tid)
                    n += 1
                self._db.execute("COMMIT")
                return n
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def set_summary_archived(self, tid: str, archived: bool = True) -> bool:
        """打/清归档标记。找不到摘要行时返回 False（卡还没 ingest 过）。"""
        with self._lock:
            cur = self._db.execute(
                "UPDATE task_summary SET archived=? WHERE id=?",
                (1 if archived else 0, str(tid)))
            return cur.rowcount > 0

    def summary_rows(self, include_archived: bool = False,
                     limit: int | None = None) -> list[dict]:
        """列表用摘要行：10 个字段，按 id 排序（稳定顺序，前端不用再排）。"""
        sql = ("SELECT id,title,state,repo,priority,owner,created_at,finished_at,"
               "summary_head,archived FROM task_summary")
        args: list = []
        if not include_archived:
            sql += " WHERE archived=0"
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, tuple(args)).fetchall()
        return [{"id": str(r["id"]), "title": str(r["title"]),
                 "state": str(r["state"]), "repo": str(r["repo"]),
                 "priority": str(r["priority"]), "owner": str(r["owner"]),
                 "created_at": str(r["created_at"]),
                 "finished_at": str(r["finished_at"]),
                 "summary_head": str(r["summary_head"]),
                 "archived": bool(r["archived"])} for r in rows]

    def summaries(self) -> dict[str, dict]:
        """全部摘要行（含冷卡），按 id 索引；给集群复制/排障用。"""
        return {r["id"]: r for r in self.summary_rows(include_archived=True)}

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
