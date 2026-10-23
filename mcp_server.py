#!/usr/bin/env python3
"""hive MCP server —— 让任意 agent 通过 MCP 查阅任务、申请任务、提交、终止任务。

协议层零第三方依赖：用标准库直接实现 MCP 的 stdio JSON-RPC 传输
（换行分隔的 JSON-RPC 2.0）。日志一律走 stderr，stdout 只吐协议报文。

启动：

    python3 mcp_server.py --agent fb88-dsh
    python3 mcp_server.py --agent workbuddy --dir ~/.hive/clones/mcp-workbuddy

身份模型：一个 server 进程 = 一个 agent。**所有写操作只作用在这个 agent 自己身上**
——不能替别人领卡、不能替别人提交、不能终止别人的租约。这点由代码强制，不靠自觉。

安全约定：server 默认用自己独占的协调仓库 clone（`~/.hive/clones/mcp-<agent>`），
因为 worker 初始化时会 `reset --hard origin/main`；共用主工作区会把人未提交的改动冲掉。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hive import (  # noqa: E402
    DEFAULT_STATE, IDENT, PRIORITY_ORDER, VERSION, Hive, HiveError, Worker,
    commit_file_to_ref, git, iso, now, parse_iso, rev_parse,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "hive", "version": VERSION}

# --------------------------------------------------------------------------- #
# 工具定义
# --------------------------------------------------------------------------- #

TOOLS: list[dict] = [
    {
        "name": "hive_whoami",
        "description": "我是谁：agent 登记信息（模式/能力/仓库/并发上限）＋ 我名下和我能领的任务。干活前先调一次。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "hive_list_tasks",
        "description": "列出任务卡。默认列全部，可按 state / repo 过滤，或只看自己的。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string",
                          "description": "按状态过滤：ready / assigned / running / done（空=全部）"},
                "repo": {"type": "string", "description": "按代码仓过滤（空=全部）"},
                "mine_only": {"type": "boolean", "default": False,
                              "description": "只看 owner 或 claim 属于我的卡"},
                "limit": {"type": "integer", "default": 50, "description": "最多返回几条"},
            },
            "required": [],
        },
    },
    {
        "name": "hive_get_task",
        "description": "看一张卡的完整内容：frontmatter、任务说明正文、允许修改的 paths、验收命令、当前租约与结果。",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "任务号，如 T-0110"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "hive_next",
        "description": "给我推荐一张现在就能干的卡：按优先级排序，跳过依赖未就绪、能力不匹配、仓库没登记、已被别人领走的。只推荐，不领取。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "hive_claim",
        "description": "申请并领取一张卡：把卡指派给我、推 claim 租约分支（抢锁，抢不到会明确告诉你）、备好独立 worktree 和任务提示词。返回工作目录与文件路径。",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "任务号，如 T-0110"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "hive_submit",
        "description": "提交成果：把 worktree 里的改动 commit 并推到 task/<任务号> 分支，跑卡片上的 verify 命令，把结果写进 res/<任务号> 分支。提交前把工作说明写进 <worktree>/.hive/summary.md（也可以在这里用 note 传）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务号"},
                "note": {"type": "string",
                         "description": "没写 summary.md 时用这段作为工作说明（会写进结果）"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "hive_abort",
        "description": "终止/放弃一张卡：释放我持有的租约，默认把卡退回 ready 让别人能领。已经出结果的卡不能终止（要先用 retry）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务号"},
                "reason": {"type": "string", "description": "为什么放弃（会写进提交信息，便于审计）"},
                "requeue": {"type": "boolean", "default": True,
                            "description": "是否把卡退回 ready 重新排队（False=只释放租约，卡仍归我）"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "hive_extend_lease",
        "description": "给租约续期。GUI agent 干活慢、租约快到期时用，否则会被 gc 当成跑飞的卡回收掉。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务号"},
                "minutes": {"type": "integer", "default": 60, "description": "再续多少分钟"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "hive_progress",
        "description": "中途记一笔进度，写在 worktree 的 .hive/progress.md 里（不进 git、不影响验收）。长时间任务建议定期记，方便别人接手。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务号"},
                "text": {"type": "string", "description": "这一笔进度的内容（追加写入）"},
            },
            "required": ["task_id", "text"],
        },
    },
    {
        "name": "hive_result",
        "description": "查一张卡的结果：状态、耗时、每条 verify 命令的退出码与输出尾部、agent 写的总结、产物分支与 head。",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "任务号"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "hive_stats",
        "description": "吞吐与成功率：任务数、完成/失败、一次通过率、中位与 P90 耗时、按 agent 和按仓库分布。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "hive_doctor",
        "description": "给协调仓库做体检：任务卡格式、租约、结果、归档、agent 配置、inbox 是否自洽。返回问题清单。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


# --------------------------------------------------------------------------- #
# 业务逻辑
# --------------------------------------------------------------------------- #

def ok(message: str, **details) -> dict:
    out = {"status": "success", "message": message}
    if details:
        out["details"] = details
    return out


def err(message: str, **details) -> dict:
    out = {"status": "error", "message": message}
    if details:
        out["details"] = details
    return out


@contextlib.contextmanager
def _redirect_stdout(to):
    orig = sys.stdout
    sys.stdout = to
    try:
        yield
    finally:
        sys.stdout = orig


def state_root(agent: str) -> Path:
    """worker 状态根目录。和 Worker.prepare / Hive.cmd_submit 保持同一套推导。"""
    return Path(os.environ.get("HIVE_STATE")
                or DEFAULT_STATE / "agents" / agent).expanduser()


class HiveMcp:
    """一个 agent 视角下的 hive。读操作用 Hive，写操作用 Worker（会先同步 main）。"""

    def __init__(self, hive_dir: Path, agent: str):
        self.hive_dir = hive_dir
        self.agent = agent

    # -- 底层 -------------------------------------------------------------- #
    def hive(self) -> Hive:
        """每次调用都拉最新的 main。

        卡片和 agent 配置是工作区文件，光 fetch 只更新远端引用 —— 必须 reset 才能真正看到
        别人刚推的卡。这个 clone 是 MCP 独占的，reset 不会冲掉任何人的东西。
        """
        h = Hive(self.hive_dir)
        h.fetch()
        git(["reset", "--hard", "-q", "origin/main"], h.dir)
        return h

    def worker(self) -> Worker:
        # Worker.__init__ 会 fetch + reset --hard origin/main，保证写之前是最新的
        return Worker(str(self.hive_dir), self.agent)

    def worktree(self, cfg: dict, tid: str) -> Path:
        """worktree 路径必须和 Worker.prepare / Hive.cmd_submit 推导出的一样。"""
        root = Path(cfg.get("worktrees") or (state_root(self.agent) / "wt"))
        return root.expanduser() / tid

    def _rows(self, h: Hive) -> list[dict]:
        rows = []
        for tid, c in h.cards().items():
            claim = h.claim_info(tid) or {}
            res = h.result_info(tid) or {}
            state = c.get("state")
            if claim and not res and state != "done":
                state = "running"
            lease_until = claim.get("lease_until")
            rows.append({
                "id": tid,
                "state": state,
                "owner": c.get("owner") or "",
                "claim_agent": claim.get("agent") or "",
                "lease_until": lease_until or "",
                "stale": bool(lease_until and not res and parse_iso(lease_until) < now()),
                "repo": c.get("repo") or "",
                "priority": c.get("priority") or "",
                "needs": c.get("needs") or [],
                "title": c.get("title") or "",
                "result": res.get("status") or "",
                "duration_s": res.get("duration_s") or 0,
                "depends_on": c.get("depends_on") or [],
            })
        rows.sort(key=lambda r: (PRIORITY_ORDER.get(r["priority"], 9), r["id"]))
        return rows

    def _can_do(self, cfg: dict, card: dict, h: Hive) -> tuple[bool, str]:
        """我能不能干这张卡。返回 (能/不能, 原因)。"""
        repo = card.get("repo") or ""
        if repo not in (cfg.get("repos") or {}):
            return False, f"仓库 {repo or '(未填)'} 没登记在我名下"
        have = set(cfg.get("abilities") or []) | set(cfg.get("capabilities") or [])
        lack = set(card.get("needs") or []) - have
        if lack:
            return False, f"能力缺失：缺 {', '.join(sorted(lack))}"
        missing = [d for d in (card.get("depends_on") or []) if not h.dep_done(d)]
        if missing:
            return False, f"依赖未就绪：{', '.join(missing)}"
        return True, ""

    # -- 工具实现 ---------------------------------------------------------- #
    def t_whoami(self, _a: dict) -> dict:
        h = self.hive()
        cfg = h.agent(self.agent)
        rows = self._rows(h)
        mine = [r for r in rows if self.agent in (r["owner"], r["claim_agent"])]
        claimable = []
        for tid, c in h.cards().items():
            if c.get("state") != "ready" or c.get("owner"):
                continue
            if h.result_info(tid) or h.claim_info(tid):
                continue
            good, _ = self._can_do(cfg, c, h)
            if good:
                claimable.append(tid)
        return ok(
            f"我是 {self.agent}（{cfg.get('mode') or 'exec'} 模式）",
            agent=self.agent,
            mode=cfg.get("mode") or ("manual" if cfg.get("manual") else "exec"),
            capabilities=cfg.get("capabilities") or [],
            abilities=cfg.get("abilities") or [],
            repos=cfg.get("repos") or {},
            worktrees=str(Path(cfg.get("worktrees") or (state_root(self.agent) / "wt")).expanduser()),
            max_concurrency=cfg.get("max_concurrency") or 1,
            hive_dir=str(self.hive_dir),
            mine=mine,
            claimable=sorted(claimable),
        )

    def t_list_tasks(self, a: dict) -> dict:
        h = self.hive()
        state = (a.get("state") or "").strip()
        repo = (a.get("repo") or "").strip()
        mine_only = bool(a.get("mine_only"))
        limit = int(a.get("limit") or 50)
        rows = self._rows(h)
        if state:
            rows = [r for r in rows if r["state"] == state]
        if repo:
            rows = [r for r in rows if r["repo"] == repo]
        if mine_only:
            rows = [r for r in rows if self.agent in (r["owner"], r["claim_agent"])]
        rows = rows[:limit]
        return ok(f"{len(rows)} 张卡", tasks=rows)

    def t_get_task(self, a: dict) -> dict:
        tid = a["task_id"]
        h = self.hive()
        card = h.card(tid)
        body = card.pop("_body", "")
        card.pop("_path", None)
        return ok(f"{tid}：{card.get('title')}",
                  card=card,
                  task_md=body.strip(),
                  claim=h.claim_info(tid),
                  result=h.result_info(tid))

    def t_next(self, _a: dict) -> dict:
        h = self.hive()
        cfg = h.agent(self.agent)
        for tid, c in sorted(h.cards().items(),
                             key=lambda kv: (PRIORITY_ORDER.get(kv[1].get("priority"), 9), kv[0])):
            if c.get("state") != "ready" or c.get("owner"):
                continue
            if h.result_info(tid) or h.claim_info(tid):
                continue
            good, why = self._can_do(cfg, c, h)
            if good:
                return ok(f"建议接 {tid}：{c.get('title')}",
                          task_id=tid, title=c.get("title"), repo=c.get("repo"),
                          priority=c.get("priority"), paths=c.get("paths") or [],
                          verify=c.get("verify") or [],
                          hint=f"下一步调用 hive_claim(task_id=\"{tid}\")")
        return err("现在没有我能接的卡（都已被领走、依赖未就绪，或我的能力/仓库不匹配）",
                   hint="用 hive_list_tasks 看看全貌，或 hive_stats 看整体进度")

    def t_claim(self, a: dict) -> dict:
        tid = a["task_id"]
        h = self.hive()
        card = h.card(tid)
        if card.get("state") == "done" or h.result_info(tid):
            return err(f"{tid} 已经有结果或已关闭")
        claim = h.claim_info(tid)
        if claim and claim.get("agent") != self.agent:
            return err(f"{tid} 已被 {claim.get('agent')} 领取，抢不到")

        h._assign_local(tid, self.agent)
        h.commit_and_push(f"take {tid} -> {self.agent} (via mcp)")

        w = self.worker()
        if not claim and not w.claim(tid):
            return err(f"{tid} 领取失败（可能刚被别的 agent 抢走）")
        wt = self.worktree(w.cfg, tid)
        if not (wt / ".hive" / "task.md").exists():
            w.prepare(tid)

        return ok(f"{tid} 已领取，worktree 备好",
                  task_id=tid,
                  worktree=str(wt),
                  task_md=str(wt / ".hive" / "task.md"),
                  summary_md=str(wt / ".hive" / "summary.md"),
                  branch=f"task/{tid}",
                  paths=card.get("paths") or [],
                  verify=card.get("verify") or [],
                  hint=("在 worktree 里改代码，把工作说明写进 summary.md，"
                        "然后调 hive_submit；干不下去就调 hive_abort 把卡还回去"))

    def t_submit(self, a: dict) -> dict:
        tid = a["task_id"]
        h = self.hive()
        card = h.card(tid)
        if h.result_info(tid):
            return err(f"{tid} 已经有结果（要重跑先用 hive_retry / 让总控 retry）")
        owner = card.get("owner") or ""
        if owner != self.agent:
            return err(f"{tid} 不归我（owner={owner or '无'}），只能提交自己的卡",
                       hint=f"先 hive_claim(task_id=\"{tid}\") 把它领过来")
        cfg = h.agent(owner)
        wt = self.worktree(cfg, tid)
        if not wt.exists():
            return err(f"worktree 不存在：{wt}", hint="先 hive_claim 领取")

        note = a.get("note") or ""
        if note and not (wt / ".hive" / "summary.md").exists():
            (wt / ".hive").mkdir(parents=True, exist_ok=True)
            (wt / ".hive" / "summary.md").write_text(note, encoding="utf-8")

        h.cmd_submit(tid, keep=True, note=note)
        res = self.hive().result_info(tid) or {}
        return ok(f"{tid} 提交完成：{res.get('status')}",
                  task_id=tid,
                  status=res.get("status"),
                  branch=res.get("branch"),
                  head=res.get("head"),
                  duration_s=res.get("duration_s"),
                  verify=res.get("verify") or [],
                  error=res.get("error") or "",
                  hint=("verify 全绿就等总控合并；有红就改完再 hive_submit 一次"
                        if res.get("status") == "done" else
                        "verify 没过，看 verify 里的 tail 改代码后重新 hive_submit"))

    def t_abort(self, a: dict) -> dict:
        tid = a["task_id"]
        reason = (a.get("reason") or "").strip() or "未说明"
        requeue = a.get("requeue")
        requeue = True if requeue is None else bool(requeue)
        h = self.hive()
        card = h.card(tid)
        if h.result_info(tid):
            return err(f"{tid} 已经有结果，不能终止（要重跑找总控 retry）")
        claim = h.claim_info(tid)
        owner = card.get("owner") or ""
        if claim and claim.get("agent") != self.agent:
            return err(f"{tid} 的租约在 {claim.get('agent')} 手上，不能替别人终止")
        if not claim and owner != self.agent:
            return err(f"{tid} 不归我（owner={owner or '无'}），没什么可终止的")

        if rev_parse(h.dir, f"origin/claim/{tid}"):
            git(["push", "origin", f":refs/heads/claim/{tid}"], h.dir, check=False)
        if requeue and owner == self.agent:
            card["owner"] = None
            card["state"] = "ready"
            h.save_card(card)
            h.commit_and_push(f"abort {tid} by {self.agent}: {reason}")
        return ok(f"{tid} 已终止（{'退回 ready 重新排队' if requeue else '保留归属，仅释放租约'}）",
                  task_id=tid, requeued=requeue and owner == self.agent, reason=reason)

    def t_extend_lease(self, a: dict) -> dict:
        tid = a["task_id"]
        minutes = int(a.get("minutes") or 60)
        h = self.hive()
        claim = h.claim_info(tid)
        if not claim:
            return err(f"{tid} 当前没有租约，先 hive_claim")
        if claim.get("agent") != self.agent:
            return err(f"{tid} 的租约属于 {claim.get('agent')}，不能替别人续期")
        if h.result_info(tid):
            return err(f"{tid} 已经有结果，不需要续租")
        # 从「原到期时间」往后加，不是从现在：原租约 180 分钟、续 60 分钟，
        # 从现在算反而会把到期时间提前，等于没续
        until = parse_iso(claim["lease_until"]) if claim.get("lease_until") else now()
        if until < now():
            until = now()  # 已经过期的就按现在重新起算
        until = until + timedelta(minutes=minutes)
        payload = json.dumps({
            "task": tid, "agent": self.agent,
            "claimed_at": claim.get("claimed_at") or iso(),
            "lease_until": iso(until),
            "host": claim.get("host") or (os.uname().nodename if hasattr(os, "uname") else ""),
            "extended": True,
        }, ensure_ascii=False, indent=2)
        try:
            commit_file_to_ref(h.dir, f"claims/{tid}.json", payload, f"claim/{tid}",
                               "origin/main", f"extend lease {tid} +{minutes}m", force=True)
        except HiveError as e:
            return err(f"续租失败：{e}")
        return ok(f"{tid} 租约加了 {minutes} 分钟，新的到期时间 {iso(until)}",
                  task_id=tid, lease_until=iso(until), added_minutes=minutes)

    def t_progress(self, a: dict) -> dict:
        tid = a["task_id"]
        text = a.get("text") or ""
        h = self.hive()
        card = h.card(tid)
        cfg = h.agent(card.get("owner") or self.agent)
        wt = self.worktree(cfg, tid)
        if not wt.exists():
            return err(f"worktree 不存在：{wt}", hint="先 hive_claim 领取")
        (wt / ".hive").mkdir(parents=True, exist_ok=True)
        p = wt / ".hive" / "progress.md"
        with p.open("a", encoding="utf-8") as f:
            f.write(f"\n## {iso()}\n\n{text.strip()}\n")
        return ok(f"{tid} 进度已记录", path=str(p))

    def t_result(self, a: dict) -> dict:
        tid = a["task_id"]
        res = self.hive().result_info(tid)
        if not res:
            return err(f"{tid} 还没有结果")
        return ok(f"{tid}：{res.get('status')}", result=res)

    def t_stats(self, _a: dict) -> dict:
        h = self.hive()
        rows = self._rows(h)
        done = [r for r in rows if r["result"] == "done"]
        failed = [r for r in rows if r["result"] == "failed"]
        running = [r for r in rows if r["state"] == "running"]
        ready = [r for r in rows if r["state"] == "ready" and not r["owner"]]
        durs = sorted(r["duration_s"] for r in done if r["duration_s"])
        total = len(done) + len(failed)
        return ok(f"共 {len(rows)} 张卡，完成 {len(done)}，失败 {len(failed)}，进行中 {len(running)}",
                  tasks=len(rows), done=len(done), failed=len(failed),
                  running=len(running), ready=len(ready),
                  merge=sum(1 for r in rows if r["result"] == "done" and r["state"] != "done"),
                  pass_rate=round(len(done) / total * 100, 1) if total else 0.0,
                  median_s=durs[len(durs) // 2] if durs else 0,
                  mine=[r["id"] for r in rows if self.agent in (r["owner"], r["claim_agent"])])

    def t_doctor(self, _a: dict) -> dict:
        h = self.hive()
        issues: list[str] = []
        cards = h.cards()
        for tid, c in cards.items():
            if not c.get("repo"):
                issues.append(f"{tid}: 没填 repo")
            if not c.get("verify") and c.get("state") != "done":
                issues.append(f"{tid}: 没有 verify 命令（验收机没法接）")
            for d in (c.get("depends_on") or []):
                if d not in cards:
                    issues.append(f"{tid}: 依赖了不存在的卡 {d}")
            claim = h.claim_info(tid)
            res = h.result_info(tid)
            if claim and res and tid not in [r for r in cards if cards[r].get("state") == "done"]:
                issues.append(f"{tid}: 有结果但没 close（待合并）")
            if claim and not res and parse_iso(claim.get("lease_until") or iso()) < now():
                issues.append(f"{tid}: 租约已过期（{claim.get('agent')}），该 gc 回收")
        for name in h.agents():
            cfg = h.agent(name)
            if not (cfg.get("repos") or {}):
                issues.append(f"agent {name}: 没登记任何仓库")
        return ok("体检完成" + (f"，{len(issues)} 个问题" if issues else "，没发现问题"),
                  issues=issues, healthy=not issues)

    HANDLERS = {
        "hive_whoami": t_whoami,
        "hive_list_tasks": t_list_tasks,
        "hive_get_task": t_get_task,
        "hive_next": t_next,
        "hive_claim": t_claim,
        "hive_submit": t_submit,
        "hive_abort": t_abort,
        "hive_extend_lease": t_extend_lease,
        "hive_progress": t_progress,
        "hive_result": t_result,
        "hive_stats": t_stats,
        "hive_doctor": t_doctor,
    }


# --------------------------------------------------------------------------- #
# MCP stdio 传输层（JSON-RPC 2.0）
# --------------------------------------------------------------------------- #

class McpServer:
    def __init__(self, impl: HiveMcp):
        self.impl = impl

    def result(self, rid, payload: dict) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": payload}

    def error(self, rid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": code, "message": message}}

    def handle(self, msg: dict):
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return self.result(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    f"hive 任务总线。你的身份是 agent {self.impl.agent}。"
                    "典型流程：hive_whoami 看自己 → hive_next 挑卡 → hive_claim 领取 → "
                    "在返回的 worktree 里干活并写 .hive/summary.md → hive_submit 提交 → "
                    "hive_result 看验收结果。干不下去就 hive_abort 把卡还回去，别干耗到租约过期。"
                ),
            })
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return self.result(rid, {})
        if method == "tools/list":
            return self.result(rid, {"tools": TOOLS})
        if method == "resources/list":
            return self.result(rid, {"resources": []})
        if method == "prompts/list":
            return self.result(rid, {"prompts": []})
        if method == "tools/call":
            return self._call_tool(rid, params)
        if rid is None:
            return None
        return self.error(rid, -32601, f"不支持的方法：{method}")

    def _call_tool(self, rid, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HiveMcp.HANDLERS.get(name)
        if handler is None:
            return self.error(rid, -32602, f"没有工具 {name}")
        try:
            # stdout 被协议独占：底层 hive 代码里有 print（比如 cmd_submit 的提交回执），
            # 不接住就直接把 JSON-RPC 流搅坏了。统一赶到 stderr。
            with _redirect_stdout(sys.stderr):
                envelope = handler(self.impl, args)
        except HiveError as e:
            envelope = err(str(e))
        except Exception as e:  # 兜底：MCP 客户端不该看到裸 traceback
            envelope = err(f"{type(e).__name__}: {e}")
        text = json.dumps(envelope, ensure_ascii=False, indent=2)
        return self.result(rid, {
            "content": [{"type": "text", "text": text}],
            "isError": envelope.get("status") == "error",
        })


def serve(impl: HiveMcp) -> int:
    server = McpServer(impl)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            print(json.dumps(server.error(None, -32700, f"JSON 解析失败：{e}")), flush=True)
            continue
        resp = server.handle(msg if isinstance(msg, dict) else {})
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def resolve_dir(agent: str, given: str | None) -> Path:
    """MCP server 必须用独占 clone：worker 初始化会 reset --hard origin/main。"""
    if given:
        return Path(given).expanduser().resolve()
    return (DEFAULT_STATE / "clones" / f"mcp-{agent}").expanduser().resolve()


def ensure_clone(src: Path, dst: Path) -> None:
    """从源仓的 origin URL 建独占 clone。

    不能直接 `git clone <本地路径>`：那样 origin 指向别人的工作区，push main 会被
    `receive.denyCurrentBranch` 拒掉。必须克隆真正的远端（Gitea）。
    """
    if (dst / ".git").exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    from hive import git as _git
    url = ""
    if (src / ".git").exists():
        try:
            url = _git(["remote", "get-url", "origin"], src).stdout.strip()
        except Exception:
            url = ""
    _git(["clone", "--quiet", url or str(src), str(dst)], Path(dst.parent))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="hive MCP server（stdio，零第三方依赖）")
    p.add_argument("--agent", default=os.environ.get("HIVE_AGENT", ""),
                   help="以哪个 agent 的身份运行（默认取 HIVE_AGENT）")
    p.add_argument("--dir", default=os.environ.get("HIVE_DIR", ""),
                   help="协调仓库路径；不给就用 ~/.hive/clones/mcp-<agent>")
    p.add_argument("--source", default=None,
                   help="自动建 clone 时从哪拷（默认同目录下的 hive 仓）")
    args = p.parse_args(argv)

    if not args.agent:
        print("hive mcp: 必须指定 --agent（或设 HIVE_AGENT）——server 以某个 agent 的身份运行",
              file=sys.stderr)
        return 2

    dst = resolve_dir(args.agent, args.dir or None)
    src = Path(args.source).expanduser().resolve() if args.source else None
    if not (dst / ".git").exists():
        if src is None:
            # 默认从本文件所在仓（通常就是 hive 工具仓）拷
            src = Path(__file__).resolve().parent
        try:
            ensure_clone(src, dst)
        except Exception as e:
            print(f"hive mcp: 准备 clone 失败：{e}\n"
                  f"  手动来一次：git clone <hive 仓> {dst}", file=sys.stderr)
            return 2

    try:
        # 先 fetch + reset 再查：卡片/agent 都存在工作区文件里，光 fetch 不会更新它
        # （Worker.__init__ 每次也是这么干的，MCP 的每次工具调用同样如此）
        probe = Hive(dst)
        probe.fetch()
        git(["reset", "--hard", "-q", "origin/main"], dst)
        probe.agent(args.agent)  # 早失败：agent 没登记就直接退出
        impl = HiveMcp(dst, args.agent)
    except HiveError as e:
        print(f"hive mcp: {e}", file=sys.stderr)
        return 2

    print(f"hive mcp: agent={args.agent} dir={dst}", file=sys.stderr, flush=True)
    return serve(impl)


if __name__ == "__main__":
    sys.exit(main())
