#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hive —— 多机 AI Agent 协同开发的任务总线（git-as-queue）。

设计要点
--------
* 协调仓库（hive repo）只存三样东西：任务卡 tasks/、收件箱 inbox/、结果 results/。
* 领取 = 向协调仓库 push 一个独占分支 claim/<task>；push 被拒 = 被别人抢走。
  依赖 git 服务端的引用更新原子性，不需要额外的锁服务。
* 结果 = push 分支 res/<task>，内容是 results/<task>.json；不碰 main。
* 所有 worker 都是「拉取式」：只要能 git fetch/push，就能工作，不需要被 SSH 进来。
* 代码仓库（light / lightmem / lightharness …）各自独立，任务卡里写 repo 名字，
  worker 用自己的本地克隆 + 每任务一个 git worktree 干活。

子命令
------
  init        初始化协调仓库
  agent add   登记一台机器/一个 agent（能力、仓库路径、启动命令）
  task new    新建任务卡
  assign      把任务放进某个 agent 的收件箱
  work        worker 主循环（领取 → 干活 → 提交 → 写结果）
  status      看板
  close       标记任务完成
  claim       手工领取（调试用）
  gc          回收过期租约

只用 Python 标准库 + git，Windows / FreeBSD / Linux 同一份代码。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

VERSION = "0.2.0"
IDENT = ["-c", "user.name=hive-worker", "-c", "user.email=hive@localhost"]
DEFAULT_STATE = Path.home() / ".hive"

TASK_FIELDS = [
    "id", "title", "repo", "base", "state", "owner", "agent", "fresh",
    "priority", "needs", "paths", "verify", "depends_on",
    "lease_minutes", "timeout_minutes", "created",
]

PROMPT_HEADER = """\
你是「蜂巢（Hive）」的一名任务执行者。你只负责下面这一张任务卡，不要越界。

## 硬性规则

1. 工作目录就是为你单独创建的 git worktree，分支 `task/{tid}`；不要 cd 到别的目录。
2. 只修改任务卡 `paths` 允许的路径；需要改其它文件时，停下来在 `.hive/summary.md` 里说明原因。
3. 不要 `git push`，不要切分支，不要动 `main`。提交由 worker 统一完成（你也可以自行小步 commit）。
4. 完成后必须写 `.hive/summary.md`（中文，5–15 行）：做了什么、改了哪些文件、跑了什么验证、遗留什么风险。
5. 如果做不完，也要写 `.hive/summary.md`，把卡住的原因和下一步写清楚，不要沉默退出。

## 任务卡
"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def log(msg: str) -> None:
    print(f"[hive {now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


class HiveError(Exception):
    pass


def run(cmd, cwd=None, check=True, input=None, timeout=None, shell=False, env=None):
    p = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, text=True, input=input,
        capture_output=True, timeout=timeout, shell=shell, env=env,
    )
    if check and p.returncode != 0:
        raise HiveError(
            f"命令失败({p.returncode}): {cmd if isinstance(cmd, str) else ' '.join(cmd)}\n"
            f"{p.stderr.strip()[-2000:]}"
        )
    return p


def git(args, cwd, check=True, input=None):
    return run(["git", *args], cwd=cwd, check=check, input=input)


def git_ok(args, cwd) -> bool:
    return git(args, cwd, check=False).returncode == 0


def rev_parse(cwd, ref) -> str | None:
    p = git(["rev-parse", "--verify", "-q", ref], cwd, check=False)
    return p.stdout.strip() if p.returncode == 0 else None


# --------------------------------------------------------------------------- #
# 任务卡：极简 Markdown frontmatter（只支持标量与标量列表）
# --------------------------------------------------------------------------- #

def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v in ("null", "~", ""):
        return None
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def parse_card(path: Path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise HiveError(f"{path} 缺少 frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise HiveError(f"{path} frontmatter 没有结束标记")
    head, body = text[4:end], text[end + 5:]
    data: dict = {}
    key = None
    for lineno, line in enumerate(head.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") or line.startswith("- "):
            if key is None or not isinstance(data.get(key), list):
                raise HiveError(f"{path}:{lineno} 列表项没有归属: {line!r}")
            # 列表项一律按字符串处理：verify 里的 "true"/"false" 是命令，不是布尔值
            data[key].append(_unquote(line.split("- ", 1)[1]))
        elif ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if not v or v == "[]":
                data[k], key = [], (k if not v else None)
            else:
                data[k], key = _scalar(v), None
        else:
            raise HiveError(f"{path}:{lineno} 无法解析: {line!r}")
    return data, body


def _fmt_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def write_card(path: Path, data: dict, body: str) -> None:
    lines = ["---"]
    for k in TASK_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}:")
            else:
                lines.append(f"{k}:")
                lines.extend(f"  - {_fmt_scalar(x)}" for x in v)
        else:
            lines.append(f"{k}: {_fmt_scalar(v)}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n" + body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# git 底层：把「一个文件」提交成某个分支，不打断当前工作区
# --------------------------------------------------------------------------- #

def commit_file_to_ref(repo: Path, path: str, content: str, ref: str,
                       parent: str = "origin/main", message: str = "") -> str:
    """构造 commit(parent=origin/main, tree=main+<path>) 并 push 到 refs/heads/<ref>。

    push 非快进会被 git 服务端拒绝 —— 这正是 claim 的原子性来源。
    返回 commit sha；失败抛 HiveError。
    """
    parent_sha = rev_parse(repo, parent)
    if not parent_sha:
        raise HiveError(f"{repo} 找不到 {parent}，先 git fetch")
    blob = git(["hash-object", "-w", "--stdin"], repo, input=content).stdout.strip()
    # 用独立 index：即使多个 worker 共用同一个 clone，也不会互相踩 index
    fd, idx = tempfile.mkstemp(prefix="hive-index-")
    os.close(fd)
    env = {**os.environ, "GIT_INDEX_FILE": idx}
    try:
        run(["git", "read-tree", parent_sha], cwd=repo, env=env)
        run(["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
            cwd=repo, env=env)
        tree = run(["git", "write-tree"], cwd=repo, env=env).stdout.strip()
        commit = run(
            ["git", *IDENT, "commit-tree", tree, "-p", parent_sha,
             "-m", message or f"hive: {path}"],
            cwd=repo, env=env,
        ).stdout.strip()
        run(["git", "push", "origin", f"{commit}:refs/heads/{ref}"], cwd=repo)
        return commit
    finally:
        try:
            os.unlink(idx)
        except OSError:
            pass


def read_ref_file(repo: Path, ref: str, path: str):
    p = git(["show", f"{ref}:{path}"], repo, check=False)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.stdout


PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _pct(values, p: int) -> float:
    if not values:
        return 0.0
    k = max(0, min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1)))))
    return float(values[k])


def _fmt_dur(seconds: float) -> str:
    if not seconds:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


# --------------------------------------------------------------------------- #
# 协调仓库
# --------------------------------------------------------------------------- #

class Hive:
    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory).expanduser().resolve()
        if not (self.dir / ".git").exists():
            raise HiveError(f"{self.dir} 不是 git 仓库（先 hive init）")

    # -- 基本读写 ---------------------------------------------------------- #
    def fetch(self) -> None:
        git(["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"], self.dir)

    def commit_and_push(self, message: str) -> bool:
        git(["add", "-A"], self.dir)
        if git(["diff", "--cached", "--quiet"], self.dir, check=False).returncode == 0:
            return False
        git([*IDENT, "commit", "-m", message], self.dir)
        git(["push", "origin", "main"], self.dir)
        return True

    def cards(self) -> dict[str, dict]:
        out = {}
        for p in sorted((self.dir / "tasks").glob("T-*.md")):
            data, body = parse_card(p)
            data["_body"], data["_path"] = body, p
            out[data["id"]] = data
        return out

    def card(self, tid: str) -> dict:
        p = self.dir / "tasks" / f"{tid}.md"
        if not p.exists():
            raise HiveError(f"没有任务 {tid}")
        data, body = parse_card(p)
        data["_body"], data["_path"] = body, p
        return data

    def save_card(self, data: dict, body: str | None = None) -> None:
        p = data["_path"]
        write_card(p, data, data["_body"] if body is None else body)

    def agent(self, name: str) -> dict:
        p = self.dir / "agents" / f"{name}.json"
        if not p.exists():
            raise HiveError(f"没有登记 agent {name}")
        return json.loads(p.read_text(encoding="utf-8"))

    def claim_info(self, tid: str):
        return read_ref_file(self.dir, f"origin/claim/{tid}", f"claims/{tid}.json")

    def result_info(self, tid: str):
        # 先看进行中的 res 分支，再看 close 后归档到 main 的 results/
        data = read_ref_file(self.dir, f"origin/res/{tid}", f"results/{tid}.json")
        if data is not None:
            return data
        local = self.dir / "results" / f"{tid}.json"
        if local.exists():
            try:
                return json.loads(local.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        return read_ref_file(self.dir, "origin/main", f"results/{tid}.json")

    # -- 命令 -------------------------------------------------------------- #
    def cmd_assign(self, tid: str, agent: str) -> None:
        self._assign_local(tid, agent)
        self.commit_and_push(f"assign {tid} -> {agent}")
        log(f"{tid} 已分派给 {agent}")

    def cmd_close(self, tid: str, note: str = "") -> None:
        card = self.card(tid)
        self.fetch()
        res = read_ref_file(self.dir, f"origin/res/{tid}", f"results/{tid}.json")
        if res is not None:
            # 归档到 main：分支可以删，审计不会丢
            (self.dir / "results").mkdir(exist_ok=True)
            (self.dir / "results" / f"{tid}.json").write_text(
                json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        card["state"] = "done"
        body = card["_body"] + (f"\n\n---\n\n**完成备注（{iso()}）**：{note}\n" if note else "")
        self.save_card(card, body)
        self.commit_and_push(f"close {tid}" + (f": {note}" if note else ""))
        # 收口：删掉 claim/res 分支，Gitea 分支列表里只剩进行中的任务
        for ref in (f"claim/{tid}", f"res/{tid}"):
            if rev_parse(self.dir, f"origin/{ref}"):
                p = git(["push", "origin", f":refs/heads/{ref}"], self.dir, check=False)
                if p.returncode == 0:
                    log(f"已删除分支 {ref}")
                else:
                    log(f"删不掉 {ref}（可能受保护），保留不影响结果")
        log(f"{tid} 已关闭")

    # -- 自动分派 / 体检 / 统计 ------------------------------------------- #
    def agents(self) -> dict[str, dict]:
        out = {}
        for p in sorted((self.dir / "agents").glob("*.json")):
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out[cfg.get("name") or p.stem] = cfg
        return out

    def _load_counts(self, cards: dict) -> dict[str, int]:
        # 已分派但还没 claim 的也要算负载，否则 dispatch 会把同一台机器塞爆
        load: dict[str, int] = {}
        for tid, c in cards.items():
            # 只扫在飞的卡，别为几百张历史卡逐个 git show
            if c.get("state") not in ("assigned", "running"):
                continue
            if self.result_info(tid):
                continue
            claim = self.claim_info(tid)
            if claim:
                a = claim.get("agent") or "?"
            elif c.get("state") == "assigned" and c.get("owner"):
                a = c["owner"]
            else:
                continue
            load[a] = load.get(a, 0) + 1
        return load

    def dep_done(self, dep: str) -> bool:
        res = self.result_info(dep)
        return bool(res and res.get("status") == "done")

    def _assign_local(self, tid: str, agent: str) -> None:
        self.agent(agent)  # 校验已登记
        card = self.card(tid)
        card["state"] = "assigned"
        card["owner"] = agent
        self.save_card(card)
        inbox = self.dir / "inbox" / agent
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{tid}.md").write_text(
            f"# {tid} -> {agent}\n\n"
            f"assigned_at: {iso()}\n\n"
            f"任务卡：`tasks/{tid}.md`\n",
            encoding="utf-8",
        )

    def cmd_dispatch(self, apply: bool = False, limit: int | None = None,
                     only_agent: str | None = None, as_json: bool = False) -> None:
        self.fetch()
        cards = self.cards()
        agents = self.agents()
        if not agents:
            raise HiveError("还没有登记任何 agent")
        load = self._load_counts(cards)
        ready = [c for c in cards.values()
                 if c.get("state") == "ready" and not c.get("owner")]
        ready.sort(key=lambda c: (PRIORITY_ORDER.get(c.get("priority"), 9), c["id"]))

        plan: list[tuple[dict, str]] = []
        skipped: list[tuple[str, str]] = []
        planned = dict(load)
        for c in ready:
            if limit and len(plan) >= limit:
                break
            missing = [d for d in (c.get("depends_on") or []) if not self.dep_done(d)]
            if missing:
                skipped.append((c["id"], "依赖未完成: " + ", ".join(missing)))
                continue
            needs = set(c.get("needs") or [])
            cands = []
            for name, a in agents.items():
                if only_agent and name != only_agent:
                    continue
                if c.get("agent") and name != c.get("agent"):
                    continue
                if c.get("repo") not in (a.get("repos") or {}):
                    continue
                if needs - set(a.get("capabilities") or []):
                    continue
                mode = a.get("mode") or ("manual" if a.get("manual") else "exec")
                if mode == "verify" and (c.get("paths") or []):
                    continue  # 验收机不改代码
                if planned.get(name, 0) >= int(a.get("max_concurrency") or 1):
                    continue
                cands.append(name)
            if not cands:
                skipped.append((c["id"], "没有空闲且能力匹配的 agent"))
                continue
            cands.sort(key=lambda n: (planned.get(n, 0),
                                      0 if c.get("agent") == n else 1, n))
            name = cands[0]
            plan.append((c, name))
            planned[name] = planned.get(name, 0) + 1

        if as_json:
            print(json.dumps({
                "plan": [{"task": c["id"], "agent": n, "repo": c.get("repo"),
                          "priority": c.get("priority")} for c, n in plan],
                "skipped": [{"task": t, "reason": r} for t, r in skipped],
                "applied": bool(apply),
            }, ensure_ascii=False, indent=2))
            return

        if plan:
            print(f"{'TASK':<9}{'PRI':<5}{'REPO':<14}-> {'AGENT':<14}LOAD")
            for c, n in plan:
                print(f"{c['id']:<9}{c.get('priority', ''):<5}{c.get('repo', ''):<14}"
                      f"-> {n:<14}{planned.get(n, 0)}/{int(agents[n].get('max_concurrency') or 1)}")
        else:
            print("没有可分派的任务")
        for tid, reason in skipped:
            print(f"  skip {tid}: {reason}")

        if apply and plan:
            for c, n in plan:
                self._assign_local(c["id"], n)
            summary = ", ".join(f"{c['id']}->{n}" for c, n in plan)
            self.commit_and_push(f"dispatch: {summary}")
            log(f"已分派 {len(plan)} 张卡")
        elif plan:
            print("（dry-run；加 --apply 才写入）")

    def cmd_doctor(self) -> bool:
        errors: list[str] = []
        warns: list[str] = []
        if not (self.dir / ".git").exists():
            print("[ERR] 不是 git 仓库")
            return False
        try:
            self.fetch()
        except HiveError as e:
            warns.append(f"fetch 失败（离线？）: {str(e).splitlines()[0]}")
        if not rev_parse(self.dir, "origin/main"):
            errors.append("缺少 origin/main")

        cards: dict[str, dict] = {}
        for p in sorted((self.dir / "tasks").glob("T-*.md")):
            try:
                data, _body = parse_card(p)
            except HiveError as e:
                errors.append(str(e))
                continue
            tid = data.get("id")
            if tid != p.stem:
                errors.append(f"{p.name}: id={tid!r} 与文件名不一致")
            for k in ("title", "repo", "state"):
                if not data.get(k):
                    errors.append(f"{p.name}: 缺字段 {k}")
            if tid in cards:
                errors.append(f"重复 id {tid}")
            cards[tid] = data

        agents = self.agents()
        if not agents:
            warns.append("还没有登记 agent")
        for name, a in agents.items():
            mode = a.get("mode") or ("manual" if a.get("manual") else "exec")
            if mode not in ("exec", "verify", "manual"):
                errors.append(f"agent {name}: mode={mode!r} 非法")
            if mode == "exec" and not a.get("command"):
                errors.append(f"agent {name}: exec 模式没有 command")
            if not a.get("repos"):
                warns.append(f"agent {name}: 没有配置 repos")
            for repo, path in (a.get("repos") or {}).items():
                if not Path(path).expanduser().exists():
                    warns.append(f"agent {name}: 本机看不到 {repo}={path}（可能不在本机）")

        claim_refs = git(["for-each-ref", "--format=%(refname)",
                          "refs/remotes/origin/claim/"], self.dir).stdout.split()
        running_claims = 0
        for ref in claim_refs:
            tid = ref.rsplit("/", 1)[-1]
            info = read_ref_file(self.dir, ref, f"claims/{tid}.json")
            if tid not in cards:
                warns.append(f"租约 {tid} 没有对应任务卡")
            if not isinstance(info, dict) or not info.get("agent"):
                errors.append(f"租约 {ref} 内容不合法")
                continue
            has_res = self.result_info(tid)
            if not has_res:
                running_claims += 1
            lease = info.get("lease_until")
            if lease and not has_res:
                try:
                    if parse_iso(lease) < now():
                        warns.append(f"任务 {tid} 租约已过期（{info.get('agent')}）")
                except ValueError:
                    errors.append(f"租约 {tid} 的 lease_until 无法解析")

        res_refs = git(["for-each-ref", "--format=%(refname)",
                        "refs/remotes/origin/res/"], self.dir).stdout.split()
        for ref in res_refs:
            tid = ref.rsplit("/", 1)[-1]
            data = read_ref_file(self.dir, ref, f"results/{tid}.json")
            if tid not in cards:
                warns.append(f"结果 {tid} 没有对应任务卡")
            if not isinstance(data, dict):
                errors.append(f"结果 {ref} 不是合法 JSON")
                continue
            if data.get("status") not in ("done", "failed"):
                errors.append(f"结果 {tid} status={data.get('status')!r} 非法")
            if data.get("status") == "done" and any(v.get("code") for v in data.get("verify") or []):
                errors.append(f"结果 {tid} 标了 done 但 verify 有非零退出码")

        inbox = self.dir / "inbox"
        if inbox.is_dir():
            for d in sorted(inbox.iterdir()):
                if d.is_dir() and d.name not in agents:
                    warns.append(f"inbox/{d.name} 没有对应的 agent 登记")

        for p in sorted((self.dir / "results").glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                errors.append(f"归档结果 {p.name} 不是合法 JSON")
                continue
            if p.stem not in cards:
                warns.append(f"归档结果 {p.stem} 没有对应任务卡")
            if data.get("status") not in ("done", "failed"):
                errors.append(f"归档结果 {p.name} status={data.get('status')!r} 非法")

        for tid, c in cards.items():
            if c.get("state") == "done" and not self.result_info(tid):
                warns.append(f"任务 {tid} 已关闭但没有结果记录")
            if c.get("state") == "ready" and c.get("owner"):
                warns.append(f"任务 {tid} 是 ready 但有 owner")
            for n in c.get("needs") or []:
                if not any(n in (a.get("capabilities") or []) for a in agents.values()):
                    warns.append(f"任务 {tid} 需要能力 {n!r}，但没有 agent 声明")

        for w in warns:
            print(f"[WARN] {w}")
        for e in errors:
            print(f"[ERR ] {e}")
        if errors:
            print(f"[FAIL] {len(errors)} 个错误，{len(warns)} 个警告")
            return False
        print(f"[ OK ] {len(cards)} 张任务，{len(agents)} 个 agent，"
              f"{running_claims} 个进行中租约，{len(res_refs)} 个待合并结果")
        return True

    def cmd_stats(self, as_json: bool = False, days: int = 0) -> None:
        self.fetch()
        cards = self.cards()
        nowdt = now()
        rows = []
        for tid, c in cards.items():
            claim = self.claim_info(tid) or {}
            res = self.result_info(tid) or {}
            finished = res.get("finished_at") or ""
            if days and finished:
                try:
                    if (nowdt - parse_iso(finished)).days > days:
                        continue
                except ValueError:
                    pass
            rows.append({
                "id": tid,
                "state": c.get("state") or "",
                "repo": c.get("repo") or "",
                "priority": c.get("priority") or "",
                "agent": claim.get("agent") or res.get("agent") or "",
                "status": res.get("status") or "",
                "running": bool(not res.get("status") and
                                (claim or (c.get("state") == "assigned" and c.get("owner")))),
                "duration_s": float(res.get("duration_s") or 0),
                "finished_at": finished,
            })

        done = [r for r in rows if r["status"] == "done"]
        failed = [r for r in rows if r["status"] == "failed"]
        durations = sorted(r["duration_s"] for r in done if r["duration_s"] > 0)
        rate = (len(done) / (len(done) + len(failed)) * 100) if (done or failed) else 0.0
        median = statistics.median(durations) if durations else 0.0
        p90 = _pct(durations, 90)

        by_agent: dict[str, dict] = defaultdict(
            lambda: {"done": 0, "failed": 0, "running": 0, "durations": []})
        for r in rows:
            key = r["agent"] or "(未领取)"
            slot = by_agent[key]
            if r["status"] == "done":
                slot["done"] += 1
                if r["duration_s"]:
                    slot["durations"].append(r["duration_s"])
            elif r["status"] == "failed":
                slot["failed"] += 1
            elif r["running"]:
                slot["running"] += 1
        by_repo = Counter(r["repo"] or "(未填)" for r in rows)

        if as_json:
            print(json.dumps({
                "summary": {
                    "tasks": len(rows), "done": len(done), "failed": len(failed),
                    "rate": round(rate, 1), "median_s": round(median, 1),
                    "p90_s": round(p90, 1),
                    "running": sum(1 for r in rows if r["running"]),
                    "merge": sum(1 for r in rows
                                 if r["status"] == "done" and r["state"] != "done"),
                    "ready": sum(1 for r in rows if r["state"] == "ready" and not r["agent"]),
                    "closed": sum(1 for r in rows if r["state"] == "done"),
                },
                "agents": {k: {"done": v["done"], "failed": v["failed"],
                               "running": v["running"]} for k, v in by_agent.items()},
                "repos": dict(by_repo),
                "tasks": rows,
            }, ensure_ascii=False, indent=2))
            return

        merge = sum(1 for r in rows if r["status"] == "done" and r["state"] != "done")
        print(f"任务 {len(rows)} ｜ 完成 {len(done)} ｜ 失败 {len(failed)} ｜ "
              f"进行中 {sum(1 for r in rows if r['running'])} ｜ 待合并 {merge} ｜ "
              f"待领取 {sum(1 for r in rows if r['state'] == 'ready' and not r['agent'])} ｜ "
              f"已关闭 {sum(1 for r in rows if r['state'] == 'done')}")
        print(f"一次通过率 {rate:.1f}% ｜ 中位耗时 {_fmt_dur(median)} ｜ P90 {_fmt_dur(p90)}")
        if by_agent:
            print("按 agent:")
            for name in sorted(by_agent, key=lambda n: -(by_agent[n]["done"] + by_agent[n]["failed"])):
                v = by_agent[name]
                med = statistics.median(v["durations"]) if v["durations"] else 0
                print(f"  {name:<16} done {v['done']:<3} failed {v['failed']:<3} "
                      f"running {v['running']:<3} 中位 {_fmt_dur(med)}")
        if by_repo:
            print("按仓库: " + "  ".join(f"{k} {v}" for k, v in by_repo.most_common()))

    def cmd_status(self, as_json: bool = False) -> None:
        self.fetch()
        cards = self.cards()
        rows = []
        for tid, c in cards.items():
            claim = self.claim_info(tid) or {}
            res = self.result_info(tid) or {}
            state = c.get("state")
            if claim and not res and state not in ("done", "blocked"):
                state = "running"  # 看板推导，不写回任务卡
            lease_until = claim.get("lease_until")
            stale = bool(lease_until and not res and parse_iso(lease_until) < now())
            rows.append({
                "id": tid,
                "state": state,
                "owner": c.get("owner") or "",
                "title": c.get("title", ""),
                "repo": c.get("repo") or "",
                "priority": c.get("priority") or "",
                "depends_on": c.get("depends_on") or [],
                "claim": f"{claim.get('agent')}@{claim.get('claimed_at', '')[5:16]}"
                         if claim else "",
                "claim_agent": claim.get("agent") or "",
                "claimed_at": claim.get("claimed_at") or "",
                "lease_until": lease_until or "",
                "stale": stale,
                "result": res.get("status", ""),
                "result_status": res.get("status", ""),
                "duration_s": res.get("duration_s", 0),
                "head": res.get("head") or "",
                "verify": res.get("verify") or [],
                "summary": (res.get("summary") or "").splitlines()[0][:60] if res else "",
                "result_summary": res.get("summary") or "",
            })
        if as_json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        if not rows:
            print("（还没有任务）")
            return
        w = [4, 9, 12, 8, 16, 8]
        print(f"{'ID':<{w[0]}} {'STATE':<{w[1]}} {'OWNER':<{w[2]}} "
              f"{'RESULT':<{w[4]}} {'CLAIM':<{w[5]}} TITLE")
        for r in rows:
            print(f"{r['id']:<{w[0]}} {r['state']:<{w[1]}} {r['owner']:<{w[2]}} "
                  f"{r['result']:<{w[4]}} {r['claim']:<{w[5]}} {r['title'][:50]}")

    def cmd_retry(self, tid: str, agent: str | None = None, fresh: bool = False) -> None:
        card = self.card(tid)
        if fresh:
            card["fresh"] = True
        self.fetch()
        for ref in (f"res/{tid}", f"claim/{tid}"):
            if rev_parse(self.dir, f"origin/{ref}"):
                git(["push", "origin", f":refs/heads/{ref}"], self.dir, check=False)
                log(f"已删除 {ref}")
        card["state"] = "assigned"
        if agent:
            card["owner"] = agent
        self.save_card(card)
        target = card.get("owner")
        if target:
            inbox = self.dir / "inbox" / target
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / f"{tid}.md").write_text(
                f"# {tid} -> {target}\n\nretry_at: {iso()}\n\n任务卡：`tasks/{tid}.md`\n",
                encoding="utf-8")
        archived = self.dir / "results" / f"{tid}.json"
        if archived.exists():
            archived.unlink()  # 重新入队时把归档也撤掉，否则 worker 会以为已完成
        self.commit_and_push(f"retry {tid}")
        log(f"{tid} 已重新入队 -> {target}")

    def cmd_gc(self, minutes: int) -> None:
        self.fetch()
        cutoff = now() - timedelta(minutes=minutes)
        p = git(["for-each-ref", "--format=%(refname)", "refs/remotes/origin/claim/"], self.dir)
        removed = 0
        for ref in p.stdout.split():
            tid = ref.rsplit("/", 1)[-1]
            if self.result_info(tid):
                continue
            info = read_ref_file(self.dir, ref, f"claims/{tid}.json") or {}
            claimed = info.get("claimed_at")
            if not claimed or parse_iso(claimed) > cutoff:
                continue
            git(["push", "origin", f":refs/heads/claim/{tid}"], self.dir, check=False)
            log(f"回收过期租约 {tid}（{info.get('agent')} @ {claimed}）")
            removed += 1
        log(f"gc 完成，回收 {removed} 个")


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #

class Worker:
    def __init__(self, hive_dir: str, agent_name: str, keep_worktree=False,
                 hive_state: str | None = None):
        self.h = Hive(hive_dir)
        self.dir = self.h.dir
        self.name = agent_name
        # 先同步再读配置：agent 的登记信息可能刚由总控推到 main
        self.h.fetch()
        git(["reset", "--hard", "-q", "origin/main"], self.dir)
        self.cfg = self.h.agent(agent_name)
        self.keep = keep_worktree
        self.state = Path(hive_state or os.environ.get("HIVE_STATE")
                          or DEFAULT_STATE / "agents" / agent_name).expanduser()
        (self.state / "done").mkdir(parents=True, exist_ok=True)
        (self.state / "logs").mkdir(parents=True, exist_ok=True)
        self.cmd = self.cfg.get("command") or ""
        self.launch = self.cfg.get("launch") or ""
        self.mode = self.cfg.get("mode") or ("manual" if self.cfg.get("manual") else "exec")
        self.manual = self.mode == "manual"
        if self.mode == "exec" and not self.cmd:
            raise HiveError(f"agent {agent_name} 没配置 command（或设 mode: verify / manual）")

    # -- 队列 -------------------------------------------------------------- #
    def pending(self) -> list[tuple[str, dict]]:
        inbox = self.dir / "inbox" / self.name
        if not inbox.is_dir():
            return []
        out = []
        cards = self.h.cards()
        for p in sorted(inbox.glob("T-*.md")):
            tid = p.stem
            marker = self.state / "done" / tid
            if marker.exists():
                if self.h.result_info(tid):
                    continue
                marker.unlink()  # 总控已 retry，远端结果被清掉，允许重跑
            if self.h.result_info(tid):
                continue
            claim = self.h.claim_info(tid)
            if claim and claim.get("agent") != self.name:
                continue
            card = cards.get(tid)
            if not card or card.get("state") in ("done", "blocked"):
                continue
            deps = card.get("depends_on") or []
            if any(not self._dep_done(d) for d in deps):
                log(f"{tid} 依赖未完成: {deps}")
                continue
            out.append((tid, card))
        return out

    def _dep_done(self, dep: str) -> bool:
        res = self.h.result_info(dep)
        return bool(res and res.get("status") == "done")

    # -- 领取 -------------------------------------------------------------- #
    def claim(self, tid: str) -> bool:
        info = self.h.claim_info(tid)
        if info and info.get("agent") != self.name:
            return False
        lease = int(self.h.card(tid).get("lease_minutes") or 180)
        payload = json.dumps({
            "task": tid, "agent": self.name,
            "claimed_at": iso(),
            "lease_until": iso(now() + timedelta(minutes=lease)),
            "host": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", ""),
        }, ensure_ascii=False, indent=2)
        try:
            commit_file_to_ref(self.dir, f"claims/{tid}.json", payload,
                               f"claim/{tid}", "origin/main",
                               f"claim {tid} by {self.name}")
            log(f"{tid} 领取成功")
            return True
        except HiveError as e:
            self.h.fetch()
            info = self.h.claim_info(tid)
            if info and info.get("agent") == self.name:
                return True
            log(f"{tid} 领取失败（{info.get('agent') if info else '未知'} 抢先）")
            if "non-fast-forward" not in str(e) and "rejected" not in str(e):
                log(str(e).splitlines()[0])
            return False

    # -- 执行 -------------------------------------------------------------- #
    def run_task(self, tid: str) -> dict:
        card = self.h.card(tid)
        repo_name = card.get("repo") or ""
        repo_path = (self.cfg.get("repos") or {}).get(repo_name)
        if not repo_path:
            return self._fail(tid, card, None, f"agent {self.name} 没有配置仓库 {repo_name!r} 的本地路径")

        repo = Path(repo_path).expanduser()
        if not (repo / ".git").exists():
            return self._fail(tid, card, None, f"本地仓库不存在: {repo}")

        started = iso()
        t0 = time.time()
        git(["fetch", "--prune", "origin"], repo)
        wt_root = Path(self.cfg.get("worktrees") or (self.state / "wt")).expanduser()
        wt_root.mkdir(parents=True, exist_ok=True)
        wt = wt_root / tid

        if wt.exists():
            git(["worktree", "remove", "--force", str(wt)], repo, check=False)
            shutil.rmtree(wt, ignore_errors=True)

        verify_only = self.mode == "verify"
        if verify_only and not (card.get("verify") or []):
            return self._fail(tid, card, None, "verify 模式的任务卡必须至少有一条 verify 命令")

        base = f"origin/{card.get('base') or 'main'}"
        branch = f"task/{tid}"
        if verify_only:
            branch = f"verify/{tid}"  # 只读检出，不产出代码分支
        elif not card.get("fresh") and rev_parse(repo, f"origin/task/{tid}"):
            base = f"origin/task/{tid}"  # 续跑上次的活；retry --fresh 时从 main 重来
        try:
            git(["worktree", "add", "-B", branch, str(wt), base], repo)
        except HiveError as e:
            return self._fail(tid, card, None, f"创建 worktree 失败: {e}")

        # .hive/ 只给 agent 和 worker 用，不进 git
        ex = git(["rev-parse", "--git-path", "info/exclude"], wt).stdout.strip()
        ex_path = (wt / ex).resolve() if not os.path.isabs(ex) else Path(ex)
        try:
            old = ex_path.read_text(encoding="utf-8") if ex_path.exists() else ""
            if ".hive/" not in old:
                ex_path.write_text(old + "\n.hive/\n", encoding="utf-8")
        except OSError:
            pass

        (wt / ".hive").mkdir(exist_ok=True)
        prompt = PROMPT_HEADER.format(tid=tid) + self._render_card(card)
        (wt / ".hive" / "task.md").write_text(prompt, encoding="utf-8")
        summary_file = wt / ".hive" / "summary.md"

        logfile = self.state / "logs" / f"{tid}.log"
        out, timed_out, code = "", False, 0

        if verify_only:
            log(f"{tid} 验证模式：只跑 verify，不调用 agent")
        elif self.manual:
            if os.name == "nt" and self.cfg.get("copy_prompt", True):
                try:
                    subprocess.run("clip", input=prompt, text=True, shell=True,
                                   check=False, capture_output=True)
                    log("任务提示词已复制到剪贴板")
                except OSError:
                    pass
            if self.launch:
                cmdline = (self.launch.replace("{worktree}", str(wt))
                           .replace("{task}", tid)
                           .replace("{prompt}", str(wt / ".hive" / "task.md")))
                try:
                    subprocess.Popen(cmdline, shell=True, cwd=str(wt))
                    log(f"已启动 GUI：{cmdline}")
                except OSError as e:
                    log(f"启动 GUI 失败（继续手动）: {e}")
            log(f"{tid} 手动模式：请在 {wt} 里让 GUI agent 完成，然后写 {summary_file}")
            print(f"\n=== 任务 {tid} 已就绪 ===\n工作目录: {wt}\n任务卡:   {wt/'.hive'/'task.md'}"
                  f"\n完成后写: {summary_file}（或在此按 Ctrl-C 放弃）\n")
            deadline = time.time() + int(card.get("timeout_minutes") or 120) * 60
            while time.time() < deadline:
                if summary_file.exists() and summary_file.stat().st_size > 0:
                    break
                time.sleep(5)
            else:
                timed_out = True
        else:
            code, out, timed_out = self._exec(self.cmd, wt, prompt, logfile,
                                              int(card.get("timeout_minutes") or 60) * 60, tid)

        # 提交 + push（agent 自己 commit 过也没关系）；验证模式不产出代码
        head = None
        if verify_only:
            head = rev_parse(wt, "HEAD")
        else:
            try:
                git(["add", "-A"], wt)
                if git(["diff", "--cached", "--quiet"], wt, check=False).returncode != 0:
                    git([*IDENT, "commit", "-m", f"hive({tid}): {card.get('title', '')}"], wt)
                head = rev_parse(wt, "HEAD")
                git(["push", "--force", "origin", f"HEAD:refs/heads/task/{tid}"], wt)
            except HiveError as e:
                log(f"{tid} 推送代码分支失败: {e}")

        verify = []
        for cmd in (card.get("verify") or []):
            p = run(cmd, cwd=wt, check=False, shell=True, timeout=1800)
            verify.append({"cmd": cmd, "code": p.returncode,
                           "tail": (p.stdout + p.stderr).strip().splitlines()[-5:]})
            log(f"{tid} 验证 {p.returncode}: {cmd}")

        ok = code == 0 and not timed_out and all(v["code"] == 0 for v in verify)
        if summary_file.exists() and summary_file.stat().st_size > 0:
            summary = summary_file.read_text(encoding="utf-8").strip()
        elif verify_only:
            summary = "verify-only 模式（无 agent 参与）\n" + "\n".join(
                f"$ {v['cmd']}  ->  {v['code']}\n" + "\n".join(v["tail"]) for v in verify)
        else:
            summary = "\n".join(out.strip().splitlines()[-30:])

        result = {
            "task": tid, "agent": self.name, "status": "done" if ok else "failed",
            "branch": branch, "base": card.get("base") or "main", "mode": self.mode,
            "head": head, "exit_code": code, "timed_out": timed_out,
            "started_at": started, "finished_at": iso(),
            "duration_s": round(time.time() - t0, 1),
            "verify": verify, "summary": summary,
            "error": "" if ok else (
                "超时" if timed_out else
                "verify 未通过" if verify_only else f"agent 退出码 {code}"),
        }
        self._write_result(tid, result)

        if not self.keep:
            git(["worktree", "remove", "--force", str(wt)], repo, check=False)
            shutil.rmtree(wt, ignore_errors=True)

        (self.state / "done" / tid).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        log(f"{tid} 完成: {result['status']}（{result['duration_s']}s）")
        return result

    def _render_card(self, card: dict) -> str:
        lines = [f"- id: {card['id']}", f"- 标题: {card.get('title', '')}",
                 f"- 仓库: {card.get('repo')}（基线 {card.get('base', 'main')}）",
                 f"- 允许修改: {', '.join(card.get('paths') or []) or '（未声明，尽量最小改动）'}"]
        if card.get("depends_on"):
            lines.append(f"- 依赖: {', '.join(card['depends_on'])}")
        if card.get("verify"):
            lines.append("- 验收命令（worker 会跑，必须全绿）:")
            lines += [f"    {c}" for c in card["verify"]]
        return "\n".join(lines) + "\n\n## 任务说明\n\n" + card.get("_body", "").strip() + "\n"

    def _exec(self, cmd: str, cwd: Path, prompt: str, logfile: Path, timeout_s: int, tid: str):
        env = os.environ.copy()
        env.update({
            "HIVE_TASK": tid, "HIVE_AGENT": self.name,
            "HIVE_PROMPT": str(cwd / ".hive" / "task.md"),
            "HIVE_WORKTREE": str(cwd),
        })
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        p = subprocess.Popen(
            cmd, shell=True, cwd=str(cwd), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, **kwargs,
        )
        timed_out = False
        try:
            out, _ = p.communicate(prompt, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True)
            else:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            out, _ = p.communicate()
        logfile.write_text(out or "", encoding="utf-8")
        return p.returncode or 0, out or "", timed_out

    def _fail(self, tid: str, card: dict, wt, error: str) -> dict:
        result = {
            "task": tid, "agent": self.name, "status": "failed", "branch": f"task/{tid}",
            "base": card.get("base") or "main", "head": None, "exit_code": 1,
            "timed_out": False, "started_at": iso(), "finished_at": iso(),
            "duration_s": 0, "verify": [], "summary": error, "error": error,
        }
        self._write_result(tid, result)
        (self.state / "done" / tid).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        log(f"{tid} 失败: {error}")
        return result

    def _write_result(self, tid: str, result: dict) -> None:
        commit_file_to_ref(
            self.dir, f"results/{tid}.json",
            json.dumps(result, ensure_ascii=False, indent=2),
            f"res/{tid}", "origin/main",
            f"result {tid}: {result['status']} by {self.name}",
        )

    # -- 主循环 ------------------------------------------------------------ #
    def loop(self, once=False, interval=30) -> None:
        log(f"worker {self.name} 启动（{self.dir}），轮询间隔 {interval}s")
        while True:
            try:
                self.h.fetch()
                git(["reset", "--hard", "-q", "origin/main"], self.dir)
                pending = self.pending()
                for tid, _card in pending:
                    if self.claim(tid):
                        self.run_task(tid)
                if once:
                    if not pending:
                        log("没有待办任务")
                    return
            except KeyboardInterrupt:
                log("收到中断，退出")
                return
            except HiveError as e:
                log(f"循环出错（继续）: {e}")
            time.sleep(interval)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def cmd_init(args) -> None:
    d = Path(args.dir).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    if not (d / ".git").exists():
        git(["init", "-b", "main"], d)
    for sub in ("tasks", "inbox", "results", "agents"):
        (d / sub).mkdir(exist_ok=True)
        (d / sub / ".gitkeep").touch()
    # 工具随仓库走：每台机器 clone 下来就能 python3 hive.py
    tool = d / "hive.py"
    if not tool.exists():
        shutil.copy2(Path(__file__).resolve(), tool)
        os.chmod(tool, 0o755)
    readme = d / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Hive 协同仓库\n\n"
            "这个仓库是任务总线，不放代码。\n\n"
            "- `tasks/T-*.md`：任务卡（总控写）\n"
            "- `inbox/<agent>/`：分派信号（总控写）\n"
            "- `results/T-*.json`：结果（worker 写，在 `res/<task>` 分支）\n"
            "- `claims/<task>.json`：租约（worker 写，在 `claim/<task>` 分支）\n"
            "- `agents/<agent>.json`：机器/agent 配置\n\n"
            "看板：`python3 hive.py status`\n",
            encoding="utf-8",
        )
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*.log\n.hive/\n__pycache__/\n", encoding="utf-8")
    git(["add", "-A"], d)
    if git(["diff", "--cached", "--quiet"], d, check=False).returncode != 0:
        git([*IDENT, "commit", "-m", "hive: init"], d)
    if args.remote:
        if not git_ok(["remote", "get-url", "origin"], d):
            git(["remote", "add", "origin", args.remote], d)
        git(["push", "-u", "origin", "main"], d)
    log(f"协调仓库就绪: {d}")


def cmd_agent(args) -> None:
    h = Hive(args.dir)
    if args.action == "list":
        for p in sorted((h.dir / "agents").glob("*.json")):
            cfg = json.loads(p.read_text(encoding="utf-8"))
            print(f"{cfg.get('name'):<12} {cfg.get('capabilities', [])} "
                  f"{'manual' if cfg.get('manual') else cfg.get('command', '')[:60]}")
        return
    if not args.name:
        raise HiveError("agent add 需要 --name")
    repos = {}
    for item in args.repo or []:
        if "=" not in item:
            raise HiveError(f"--repo 需要 name=path 形式: {item}")
        k, v = item.split("=", 1)
        repos[k] = v
    mode = args.mode or ("manual" if args.manual else "exec")
    cfg = {
        "name": args.name,
        "command": args.command or "",
        "mode": mode,
        "manual": mode == "manual",
        "capabilities": args.capabilities or [],
        "repos": repos,
        "launch": args.launch or "",
        "worktrees": args.worktrees or "",
        "max_concurrency": args.max_concurrency or 1,
        "created": iso(),
    }
    p = h.dir / "agents" / f"{args.name}.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    h.commit_and_push(f"agent: register {args.name}")
    log(f"已登记 {args.name}")


def cmd_task(args) -> None:
    h = Hive(args.dir)
    if args.action == "list":
        for tid, c in h.cards().items():
            print(f"{tid} {c.get('state'):<10} {c.get('owner') or '-':<12} {c.get('title')}")
        return
    if not args.title or not args.repo:
        raise HiveError("task new 需要 --title 和 --repo")
    ids = [int(p.stem.split("-")[1]) for p in (h.dir / "tasks").glob("T-*.md")
           if p.stem.split("-")[1].isdigit()]
    tid = args.id or f"T-{max(ids, default=0) + 1:04d}"
    if (h.dir / "tasks" / f"{tid}.md").exists():
        raise HiveError(f"{tid} 已存在")
    body = ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body:
        body = args.body
    card = {
        "id": tid, "title": args.title, "repo": args.repo, "base": args.base,
        "state": "ready", "owner": None, "agent": None, "priority": args.priority,
        "needs": args.needs or [], "paths": args.paths or [], "verify": args.verify or [],
        "depends_on": args.depends or [], "fresh": False, "lease_minutes": args.lease,
        "timeout_minutes": args.timeout, "created": iso(),
    }
    write_card(h.dir / "tasks" / f"{tid}.md", card, body or f"# {args.title}\n")
    h.commit_and_push(f"task: add {tid} {args.title}")
    log(f"已创建 {tid}")


def cmd_assign(args) -> None:
    Hive(args.dir).cmd_assign(args.task, args.agent)


def cmd_dispatch(args) -> None:
    Hive(args.dir).cmd_dispatch(args.apply, args.limit, args.agent, args.json)


def cmd_doctor(args) -> None:
    if not Hive(args.dir).cmd_doctor():
        sys.exit(1)


def cmd_stats(args) -> None:
    Hive(args.dir).cmd_stats(args.json, args.days)


def cmd_close(args) -> None:
    Hive(args.dir).cmd_close(args.task, args.note or "")


def cmd_status(args) -> None:
    Hive(args.dir).cmd_status(args.json)


def cmd_gc(args) -> None:
    Hive(args.dir).cmd_gc(args.minutes)


def cmd_retry(args) -> None:
    Hive(args.dir).cmd_retry(args.task, args.agent, args.fresh)


def cmd_result(args) -> None:
    h = Hive(args.dir)
    h.fetch()
    res = h.result_info(args.task)
    if not res:
        raise HiveError(f"{args.task} 还没有结果")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"{res['task']}  {res['status']}  by {res['agent']}  ({res['duration_s']}s)")
        print(f"branch: {res.get('branch')}  head: {res.get('head')}")
        print("-" * 60)
        print(res.get("summary", ""))


def cmd_claim(args) -> None:
    w = Worker(args.dir, args.agent)
    sys.exit(0 if w.claim(args.task) else 1)


def cmd_work(args) -> None:
    w = Worker(args.dir, args.agent, keep_worktree=args.keep, hive_state=args.state_dir)
    w.loop(once=args.once, interval=args.interval)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hive", description="多机 AI Agent 协同任务总线")
    p.add_argument("--dir", default=os.environ.get("HIVE_DIR", "."),
                   help="协调仓库路径（默认 $HIVE_DIR 或当前目录）")
    p.add_argument("-V", "--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="初始化协调仓库")
    s.add_argument("--remote", help="git 远端 URL")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("agent", help="登记 / 查看 agent")
    s.add_argument("action", choices=["add", "list"])
    s.add_argument("--name")
    s.add_argument("--command", help="执行命令（prompt 从 stdin 传入）")
    s.add_argument("--manual", action="store_true", help="GUI 半自动模式（等价 --mode manual）")
    s.add_argument("--mode", choices=["exec", "verify", "manual"],
                   help="exec=调用 agent；verify=只跑验收；manual=等人")
    s.add_argument("--capabilities", nargs="*")
    s.add_argument("--repo", action="append", metavar="NAME=PATH")
    s.add_argument("--worktrees", help="worktree 根目录")
    s.add_argument("--launch", help="manual 模式下打开 GUI 的命令，支持 {worktree}/{task}/{prompt}")
    s.add_argument("--max-concurrency", type=int, default=1)
    s.set_defaults(func=cmd_agent)

    s = sub.add_parser("task", help="任务卡")
    s.add_argument("action", choices=["new", "list"])
    s.add_argument("--id")
    s.add_argument("--title")
    s.add_argument("--repo", required=False)
    s.add_argument("--base", default="main")
    s.add_argument("--priority", default="P1")
    s.add_argument("--needs", nargs="*", help="能力标签，如 windows exe")
    s.add_argument("--paths", nargs="*")
    s.add_argument("--verify", action="append")
    s.add_argument("--depends", nargs="*")
    s.add_argument("--lease", type=int, default=180)
    s.add_argument("--timeout", type=int, default=60)
    s.add_argument("--body")
    s.add_argument("--body-file")
    s.set_defaults(func=cmd_task)

    s = sub.add_parser("dispatch", help="自动分派（能力/负载/依赖感知）")
    s.add_argument("--apply", action="store_true", help="真正写入；默认 dry-run")
    s.add_argument("--limit", type=int, help="最多分派几张")
    s.add_argument("--agent", help="只分派给这个 agent")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_dispatch)

    s = sub.add_parser("doctor", help="检查协调仓库/任务/租约/结果")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("stats", help="吞吐与成功率统计")
    s.add_argument("--json", action="store_true")
    s.add_argument("--days", type=int, default=0, help="只看最近 N 天完成的结果")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("assign", help="分派任务")
    s.add_argument("task")
    s.add_argument("--agent", required=True)
    s.set_defaults(func=cmd_assign)

    s = sub.add_parser("close", help="关闭任务")
    s.add_argument("task")
    s.add_argument("--note")
    s.set_defaults(func=cmd_close)

    s = sub.add_parser("status", help="看板")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("retry", help="重试任务（清掉结果与租约）")
    s.add_argument("task")
    s.add_argument("--agent")
    s.add_argument("--fresh", action="store_true", help="从 main 重来，不续用 task 分支")
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("result", help="查看任务结果")
    s.add_argument("task")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_result)

    s = sub.add_parser("gc", help="回收过期租约")
    s.add_argument("--minutes", type=int, default=180)
    s.set_defaults(func=cmd_gc)

    s = sub.add_parser("claim", help="手工领取")
    s.add_argument("task")
    s.add_argument("--agent", required=True)
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("work", help="worker 主循环")
    s.add_argument("--agent", required=True)
    s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=int, default=30)
    s.add_argument("--keep", action="store_true", help="保留 worktree 便于排查")
    s.add_argument("--state-dir")
    s.set_defaults(func=cmd_work)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
        return 0
    except HiveError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
