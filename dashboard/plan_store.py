#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务计划（Plan）的存储与领域逻辑 —— 看板「计划」Tab 的唯一真相源。

为什么单独一个模块而不是塞进 serve.py：
* 计划有自己的状态机（进行中 / 已完成 / 已关闭 / 已删除-软删）、排序规则和
  进度派生；这些规则要被 HTTP 接口、留言指令和测试同时用到，塞在请求处理器里
  就只能靠复制粘贴保持一致。
* serve.py 已经够长，且它的职责是「HTTP 外壳」；把领域逻辑挪出来后，
  解析留言指令这种纯函数就能单测，不用起服务器。

存储形态：一个 JSON 文件（默认 dashboard/plans.json，可用 HIVE_PLANS_FILE 覆盖），
结构 `{"version": 1, "seq": 12, "plans": [...]}`。选 JSON 而不是 SQLite，
是因为看板本来就是「零依赖、可拖走、可肉眼 diff」的东西；计划数量是几十条量级，
全量读写反而最简单。写入一律 tmp + os.replace 原子替换，并用进程内锁串行化，
避免两个请求同时改同一份文件时丢更新。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

# ---- 状态 ----
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_CLOSED = "closed"
STATUS_DELETED = "deleted"
STATUSES = (STATUS_ACTIVE, STATUS_COMPLETED, STATUS_CLOSED, STATUS_DELETED)
STATUS_LABEL = {
    STATUS_ACTIVE: "进行中",
    STATUS_COMPLETED: "已完成",
    STATUS_CLOSED: "已关闭",
    STATUS_DELETED: "已删除",
}
# 中文/别名 → 规范状态：留言指令里人更愿意写「完成」而不是 completed
STATUS_ALIASES = {
    "active": STATUS_ACTIVE, "进行中": STATUS_ACTIVE, "进行": STATUS_ACTIVE,
    "open": STATUS_ACTIVE, "doing": STATUS_ACTIVE,
    "completed": STATUS_COMPLETED, "complete": STATUS_COMPLETED,
    "done": STATUS_COMPLETED, "已完成": STATUS_COMPLETED, "完成": STATUS_COMPLETED,
    "closed": STATUS_CLOSED, "close": STATUS_CLOSED,
    "已关闭": STATUS_CLOSED, "关闭": STATUS_CLOSED,
    "deleted": STATUS_DELETED, "delete": STATUS_DELETED,
    "已删除": STATUS_DELETED, "删除": STATUS_DELETED,
}

PLAN_ID_RE = re.compile(r"^P-\d{3,6}$")
TASK_ID_RE = re.compile(r"^T-\d{3,6}$")
PREFIX_MAX = 16
TITLE_MAX = 200
DESC_MAX = 4000
MAX_TASKS = 200

# 任务算「已完成」的判定：与看板 classify() 的口径一致 —— done 归档、结果已出
# 但还没 close 的（待合并）也算这张卡干完了。closed 是「作废」，不算完成。
DONE_STATES = ("done",)


class PlanError(Exception):
    """带用户可读中文消息的领域错误；调用方据此回 400/403。"""

    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _epoch(value) -> float:
    """ISO 字符串 → epoch 秒；解析不了返回 0（排序时沉底）。"""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _clean(value, limit: int) -> str:
    """去掉控制字符并截断：这些字段会进 HTML 与日志，换行/控制符只添乱。"""
    text = str(value if value is not None else "")
    text = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32).strip()
    return text[:limit]


def plans_path() -> Path:
    raw = (os.environ.get("HIVE_PLANS_FILE") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent / "plans.json"


_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def _empty() -> dict:
    return {"version": 1, "seq": 0, "plans": []}


def load_all(path=None) -> dict:
    """读整份文件；文件不存在/坏掉都返回空表（不能让坏 JSON 把看板打挂）。"""
    p = Path(path) if path else plans_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    plans = raw.get("plans")
    if not isinstance(plans, list):
        plans = []
    try:
        seq = int(raw.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    data = {k: v for k, v in raw.items() if k not in ("plans",)}
    data["version"] = 1
    data["seq"] = seq
    data["plans"] = [p for p in plans if isinstance(p, dict)]
    # `applied`（已执行的留言 id 账本）等附加键要原样带回来，
    # 否则每次 load 都把它抹掉，同一指令会被反复执行。
    if not isinstance(data.get("applied"), list):
        data.pop("applied", None)
    return data


def save_all(data: dict, path=None) -> None:
    """原子写：先落到同目录 tmp，再 os.replace。"""
    p = Path(path) if path else plans_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# --------------------------------------------------------------------------- #
# 归一化 / 派生
# --------------------------------------------------------------------------- #
def normalize_plan(raw: dict) -> dict:
    """把磁盘上的一条记录补全成前端可直接用的形状（旧记录缺字段也不炸）。"""
    state = STATUS_ALIASES.get(str(raw.get("status") or "").strip().lower(),
                               str(raw.get("status") or STATUS_ACTIVE))
    if state not in STATUSES:
        state = STATUS_ACTIVE
    tasks = raw.get("task_ids")
    if not isinstance(tasks, list):
        tasks = []
    return {
        "id": str(raw.get("id") or ""),
        "prefix": str(raw.get("prefix") or ""),
        "title": str(raw.get("title") or ""),
        "description": str(raw.get("description") or ""),
        "status": state,
        "status_label": STATUS_LABEL.get(state, state),
        "pinned": bool(raw.get("pinned")),
        "task_ids": [str(t) for t in tasks if t],
        "created_by": str(raw.get("created_by") or ""),
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
        "completed_at": str(raw.get("completed_at") or ""),
        "closed_at": str(raw.get("closed_at") or ""),
        "deleted_at": str(raw.get("deleted_at") or ""),
        "auto_complete": bool(raw.get("auto_complete", True)),
    }


def task_done(task: dict) -> bool:
    if not isinstance(task, dict):
        return False
    return (str(task.get("state") or "") in DONE_STATES
            or str(task.get("result_status") or "") == "done")


def progress_of(plan: dict, tasks_by_id: dict) -> dict:
    """关联任务的进度：总数 / 已完成数 / 完成率。

    `tasks_by_id` 来自 /api/status 快照。快照里没出现的任务号不算完成，
    并单独列进 missing —— 「计划里有 5 张卡但看板只认识 3 张」这件事本身
    就是要让人看见的信号，不能静默当成 0/5。
    """
    ids = plan.get("task_ids") or []
    done = ok = 0
    missing = []
    for tid in ids:
        task = tasks_by_id.get(tid)
        if task is None:
            missing.append(tid)
            continue
        ok += 1
        if task_done(task):
            done += 1
    total = len(ids)
    return {
        "total": total,
        "done": done,
        "rate": (round(done * 100.0 / total, 1) if total else 0.0),
        "missing": missing,
        "known": ok,
    }


def sort_plans(items: list) -> list:
    """进行中：置顶在前，其余按更新时间倒序；已完成/已关闭：按完成时间倒序。

    调用方传入的通常是同一状态的一组，但这里对混合输入也保持确定性：
    先按状态分组顺序（active → completed → closed → deleted），组内再按各自规则。
    """
    order = {STATUS_ACTIVE: 0, STATUS_COMPLETED: 1, STATUS_CLOSED: 2, STATUS_DELETED: 3}

    def key(p):
        # 时间只到秒，同一秒内的多条更新会并列；用 id 倒序兜底，
        # 让「后建的排在前面」，结果不随列表顺序漂移。
        tie = _id_rank(p.get("id") or "")
        status = p.get("status")
        if status == STATUS_ACTIVE:
            pin_rank = 0 if p.get("pinned") else 1
            return (0, pin_rank, -_epoch(p.get("updated_at")), tie)
        when = p.get("completed_at") or p.get("closed_at") or p.get("updated_at")
        return (order.get(status, 9), 0, -_epoch(when), tie)

    return sorted(items, key=key)


def _id_rank(pid: str) -> int:
    """P-0007 → -7：排序时数值越大越靠前（同秒并列的确定性兜底）。"""
    m = re.search(r"(\d+)$", str(pid))
    return -int(m.group(1)) if m else 0


def is_auto_completable(plan: dict, tasks_by_id: dict) -> bool:
    """自动完成规则：开了 auto_complete、关联了任务、且全部关联任务都已完成。"""
    if not plan.get("auto_complete"):
        return False
    if plan.get("status") != STATUS_ACTIVE:
        return False
    ids = plan.get("task_ids") or []
    if not ids:
        return False
    for tid in ids:
        if not task_done(tasks_by_id.get(tid)):
            return False
    return True


# --------------------------------------------------------------------------- #
# 权限
# --------------------------------------------------------------------------- #
def can_write(plan: dict, who: dict) -> bool:
    """创建者本人或 controller/admin 可写；其余（普通成员）只读。"""
    actor = str((who or {}).get("agent") or "")
    role = str((who or {}).get("role") or "")
    if role in ("controller", "admin"):
        return True
    owner = str((plan or {}).get("created_by") or "")
    return bool(actor) and actor == owner


def require_write(plan: dict, who: dict, verb: str) -> None:
    if not can_write(plan, who):
        raise PlanError("%s 计划 %s 需要创建者 %s 或 controller 角色（当前 %s）"
                        % (verb, plan.get("id") or "?", plan.get("created_by") or "?",
                           (who or {}).get("agent") or "匿名"), code=403)


# --------------------------------------------------------------------------- #
# 变更
# --------------------------------------------------------------------------- #
def _actor(who: dict) -> str:
    agent = str((who or {}).get("agent") or "").strip()
    return agent or "anonymous"


def _touch(plan: dict) -> None:
    plan["updated_at"] = _now_iso()


def _find(data: dict, plan_id: str) -> dict:
    pid = str(plan_id or "").strip().upper()
    if not PLAN_ID_RE.match(pid):
        raise PlanError("计划号不合法：%s（应形如 P-0001）" % (plan_id or ""))
    for plan in data["plans"]:
        if str(plan.get("id") or "").upper() == pid:
            return plan
    raise PlanError("没有这个计划：%s" % pid, code=404)


def _write(plan_data: dict, path=None) -> None:
    save_all(plan_data, path)


def create_plan(prefix, title, description="", task_ids=None, created_by="",
                pinned=False, auto_complete=True, path=None) -> dict:
    """新建计划：分配 P-0001 形式的号，前缀是给人看的短码（如 PAY / INFRA）。"""
    prefix = _clean(prefix, PREFIX_MAX)
    title = _clean(title, TITLE_MAX)
    if not prefix:
        raise PlanError("前缀不能为空（例：/plan create PAY 支付改造）")
    if not title:
        raise PlanError("标题不能为空（例：/plan create PAY 支付改造）")
    ids = _norm_task_ids(task_ids)
    with _LOCK:
        data = load_all(path)
        seq = int(data.get("seq") or 0)
        # 跳过已被占用的号（手改过文件时也不会撞号）
        used = {str(p.get("id") or "") for p in data["plans"]}
        while True:
            seq += 1
            pid = "P-%04d" % seq
            if pid not in used:
                break
        now = _now_iso()
        plan = {
            "id": pid,
            "prefix": prefix,
            "title": title,
            "description": _clean(description, DESC_MAX),
            "status": STATUS_ACTIVE,
            "pinned": bool(pinned),
            "task_ids": ids,
            "created_by": _clean(created_by, 64) or "anonymous",
            "created_at": now,
            "updated_at": now,
            "completed_at": "",
            "closed_at": "",
            "deleted_at": "",
            "auto_complete": bool(auto_complete),
        }
        data["seq"] = seq
        data["plans"].append(plan)
        _write(data, path)
        return normalize_plan(plan)


def _norm_task_ids(task_ids) -> list:
    if task_ids is None:
        return []
    if isinstance(task_ids, str):
        task_ids = [x for x in re.split(r"[,\s]+", task_ids) if x]
    if not isinstance(task_ids, (list, tuple)):
        raise PlanError("关联任务必须是任务号数组")
    out, seen = [], set()
    for raw in task_ids:
        tid = str(raw or "").strip().upper()
        if not tid:
            continue
        if not TASK_ID_RE.match(tid):
            raise PlanError("任务号不合法：%s（应形如 T-0231）" % tid)
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    if len(out) > MAX_TASKS:
        raise PlanError("一个计划最多关联 %d 个任务" % MAX_TASKS)
    return out


def _apply_status(plan: dict, status: str) -> None:
    """状态迁移 + 时间戳维护。已删除是终态：不允许从别的入口复活。"""
    if plan.get("status") == STATUS_DELETED:
        raise PlanError("计划 %s 已删除，不能再修改" % plan.get("id"), code=409)
    if status not in STATUSES:
        raise PlanError("未知状态：%s" % status)
    if status == STATUS_DELETED:
        raise PlanError("请用 delete 操作删除计划（软删除）")
    plan["status"] = status
    if status == STATUS_COMPLETED:
        plan["completed_at"] = plan.get("completed_at") or _now_iso()
        plan["closed_at"] = ""
    elif status == STATUS_CLOSED:
        plan["closed_at"] = plan.get("closed_at") or _now_iso()
        plan["completed_at"] = ""
    elif status == STATUS_ACTIVE:
        plan["completed_at"] = ""
        plan["closed_at"] = ""
    if status != STATUS_ACTIVE:
        plan["pinned"] = False   # 置顶只对进行中生效，离开进行中自动撤销
    _touch(plan)


def update_plan(plan_id, who, fields: dict, path=None) -> dict:
    """改标题/描述/前缀/关联任务/置顶/自动完成；状态迁移走 complete/close。"""
    with _LOCK:
        data = load_all(path)
        plan = _find(data, plan_id)
        require_write(plan, who, "编辑")
        if plan.get("status") == STATUS_DELETED:
            raise PlanError("计划 %s 已删除，不能再修改" % plan.get("id"), code=409)

        if "prefix" in fields and fields["prefix"] is not None:
            prefix = _clean(fields["prefix"], PREFIX_MAX)
            if not prefix:
                raise PlanError("前缀不能为空")
            plan["prefix"] = prefix
        if "title" in fields and fields["title"] is not None:
            title = _clean(fields["title"], TITLE_MAX)
            if not title:
                raise PlanError("标题不能为空")
            plan["title"] = title
        if "description" in fields and fields["description"] is not None:
            plan["description"] = _clean(fields["description"], DESC_MAX)
        if "task_ids" in fields and fields["task_ids"] is not None:
            plan["task_ids"] = _norm_task_ids(fields["task_ids"])
        if "auto_complete" in fields and fields["auto_complete"] is not None:
            plan["auto_complete"] = bool(fields["auto_complete"])
        if "pinned" in fields and fields["pinned"] is not None:
            plan["pinned"] = bool(fields["pinned"]) and plan.get("status") == STATUS_ACTIVE
        if "status" in fields and fields["status"]:
            _apply_status(plan, STATUS_ALIASES.get(str(fields["status"]).strip().lower(),
                                                   str(fields["status"]).strip()))
        _touch(plan)
        _write(data, path)
        return normalize_plan(plan)


def set_pinned(plan_id, who, pinned: bool, path=None) -> dict:
    return update_plan(plan_id, who, {"pinned": bool(pinned)}, path=path)


def complete_plan(plan_id, who, path=None, auto=False) -> dict:
    with _LOCK:
        data = load_all(path)
        plan = _find(data, plan_id)
        if not auto:
            require_write(plan, who, "完成")
        if plan.get("status") == STATUS_COMPLETED:
            return normalize_plan(plan)      # 幂等：重复点完成不该报错
        if plan.get("status") == STATUS_DELETED:
            raise PlanError("计划 %s 已删除" % plan.get("id"), code=409)
        _apply_status(plan, STATUS_COMPLETED)
        _write(data, path)
        return normalize_plan(plan)


def close_plan(plan_id, who, path=None) -> dict:
    with _LOCK:
        data = load_all(path)
        plan = _find(data, plan_id)
        require_write(plan, who, "关闭")
        if plan.get("status") == STATUS_CLOSED:
            return normalize_plan(plan)
        if plan.get("status") == STATUS_DELETED:
            raise PlanError("计划 %s 已删除" % plan.get("id"), code=409)
        _apply_status(plan, STATUS_CLOSED)
        _write(data, path)
        return normalize_plan(plan)


def delete_plan(plan_id, who, confirm=False, path=None) -> dict:
    """软删除：只改状态和时间戳，记录仍留在文件里（可追溯、可恢复）。"""
    with _LOCK:
        data = load_all(path)
        plan = _find(data, plan_id)
        require_write(plan, who, "删除")
        admin = str((who or {}).get("role") or "") in ("controller", "admin")
        # 「删除需确认或仅管理员可执行」：前端做二次确认后带 confirm=true；
        # token 侧的 controller 可免确认；两者都不满足就拒绝并说明怎么继续。
        if not confirm and not admin:
            raise PlanError("删除计划 %s 需要二次确认（confirm=true）或 controller 角色"
                            % plan.get("id"), code=409)
        if plan.get("status") == STATUS_DELETED:
            return normalize_plan(plan)
        plan["status"] = STATUS_DELETED
        plan["pinned"] = False
        plan["deleted_at"] = _now_iso()
        _touch(plan)
        _write(data, path)
        return normalize_plan(plan)


def sync_progress(tasks_by_id: dict, path=None, return_completed=False):
    """按快照刷新：把满足规则的计划自动完成。

    返回 {plan_id: progress}，供 GET /api/plans 直接挂到每个计划上；
    `return_completed=True` 时返回 (progress, [被自动完成的 id])，让调用方
    能把这些「规则触发的状态变更」也写进审计（它同样是一次写操作）。
    注意这里会写盘，所以整段在锁里做，且只在真的有待完成计划时才写。
    """
    with _LOCK:
        data = load_all(path)
        changed = False
        completed = []
        for plan in data["plans"]:
            norm = normalize_plan(plan)
            if is_auto_completable(norm, tasks_by_id):
                plan["status"] = STATUS_COMPLETED
                plan["completed_at"] = plan.get("completed_at") or _now_iso()
                plan["pinned"] = False
                _touch(plan)
                changed = True
                completed.append(norm["id"])
        if changed:
            _write(data, path)
        out = {}
        for plan in data["plans"]:
            norm = normalize_plan(plan)
            out[norm["id"]] = progress_of(norm, tasks_by_id)
        return (out, completed) if return_completed else out


# ---- 入站指令（从集群留言里消费）----
#
# 为什么需要这条路径：agent 用的是 `hive chat post` / MCP `hive_chat_post`，
# 消息直接进集群，根本不经过看板的留言输入框。计划真身只在本机，所以由
# 「拥有计划的看板」在读到这些消息时执行它们 —— 这样任意机器上的 agent
# 发一条 `@command:plan complete P-0001` 也能生效。
#
# 幂等靠 applied 账本：同一条消息 id 只执行一次（GET /api/chat 会被反复调用）。
LEDGER_MAX = 500


def _role_of(name: str) -> str:
    """按本机 token 表把 agent 名翻成角色；查不到就是普通成员。"""
    try:
        import mcp_auth  # noqa: WPS433  同仓模块
    except ImportError:
        return "worker"
    try:
        tokens = mcp_auth.load().get("tokens") or {}
    except Exception:  # noqa: BLE001  坏 token 文件不等于给了权限
        return "worker"
    for rec in tokens.values():
        if isinstance(rec, dict) and str(rec.get("agent") or "") == name:
            return str(rec.get("role") or "worker")
    return "worker"


def apply_inbound(messages, tasks_by_id=None, path=None) -> list:
    """消费留言里的计划指令，返回执行结果列表。

    只认以 /plan 或 @command:plan 开头的消息；身份取消息的 from（去掉
    `human:` 前缀）—— 查不到 token 记录的一律按 worker 处理，删不掉东西。
    """
    tasks_by_id = tasks_by_id or {}
    results = []
    with _LOCK:
        data = load_all(path)
        applied = data.get("applied")
        if not isinstance(applied, list):
            applied = []
        seen = set(applied)
        changed = False
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            mid = str(msg.get("id") or "")
            text = str(msg.get("text") or "")
            if not mid or mid in seen or not looks_like_plan_command(text):
                continue
            sender = str(msg.get("from") or "").strip()
            if sender.startswith("human:"):
                sender = sender[len("human:"):]
            who = {"agent": sender or "anonymous", "role": _role_of(sender)}
            # 指令由「看板」代发执行，scope 与发送者原频道一致，回复也回那里
            out = run_command(text, who, tasks_by_id, path=path)
            out["message_id"] = mid
            out["scope"] = str(msg.get("scope") or "hall")
            out["from"] = sender
            results.append(out)
            applied.append(mid)
            seen.add(mid)
            changed = True
        if changed:
            data = load_all(path)          # run_command 可能已经改过文件
            data["applied"] = applied[-LEDGER_MAX:]
            _write(data, path)
    return results


def list_plans(tasks_by_id=None, include_deleted=False, status=None, path=None):
    """列计划（已排序）+ 进度。已删除默认不出现。"""
    tasks_by_id = tasks_by_id or {}
    data = load_all(path)
    items = []
    for raw in data["plans"]:
        plan = normalize_plan(raw)
        if plan["status"] == STATUS_DELETED and not include_deleted:
            continue
        if status and plan["status"] != status:
            continue
        plan["progress"] = progress_of(plan, tasks_by_id)
        items.append(plan)
    return sort_plans(items)


def get_plan(plan_id, tasks_by_id=None, path=None):
    data = load_all(path)
    for raw in data["plans"]:
        plan = normalize_plan(raw)
        if plan["id"].upper() == str(plan_id or "").strip().upper():
            plan["progress"] = progress_of(plan, tasks_by_id or {})
            return plan
    return None


# --------------------------------------------------------------------------- #
# 留言指令解析
#
# 为什么要做成「指令」而不是让人点按钮：agent 没有浏览器，它只有留言通道。
# 但 agent 也没有「确认删除」这种交互，所以删除指令只对 controller 开放。
# 语法刻意保持两套写法：
#   /plan create PAY 支付改造            —— 人手的习惯
#   @command:plan update P-0001 标题=…   —— 机器/agent 的习惯，字段显式
# 解析失败一律回一段带示例的说明，而不是只说「格式不对」。
# --------------------------------------------------------------------------- #
USAGE = (
    "用法："
    "/plan create <前缀> <标题>\n"
    "@command:plan create 前缀=PAY 标题=支付改造 关联=T-0001,T-0002 置顶=on 自动完成=on\n"
    "@command:plan update <计划ID> [标题=…] [描述=…] [前缀=…] [关联=T-0001,T-0002] [置顶=on|off] [自动完成=on|off]\n"
    "@command:plan complete <计划ID>\n"
    "@command:plan close <计划ID>\n"
    "@command:plan delete <计划ID>\n"
    "@command:plan pin <计划ID> ｜ @command:plan unpin <计划ID>"
)

PLAN_CMD_RE = re.compile(r"^\s*(?:/plan|@command:plan)\b\s*(.*)$", re.S | re.I)
FIELD_ALIASES = {
    "标题": "title", "title": "title",
    "描述": "description", "说明": "description", "description": "description", "desc": "description",
    "前缀": "prefix", "prefix": "prefix",
    "关联": "task_ids", "任务": "task_ids", "tasks": "task_ids", "task_ids": "task_ids",
    "置顶": "pinned", "pinned": "pinned", "pin": "pinned",
    "自动完成": "auto_complete", "auto_complete": "auto_complete",
    "状态": "status", "status": "status",
    "完成时间": "completed_at", "completed_at": "completed_at",
}
_TRUE = ("1", "on", "yes", "true", "是", "开", "置顶")
_FALSE = ("0", "off", "no", "false", "否", "关", "取消")


def looks_like_plan_command(text: str) -> bool:
    return bool(PLAN_CMD_RE.match(str(text or "")))


def _bool(value, field):
    raw = str(value or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise PlanError("%s 只接受 on/off（收到 %s）" % (field, value))


def parse_command(text: str) -> dict:
    """把一条留言解析成 {action, ...}；不是计划指令返回 None；格式错抛 PlanError。"""
    m = PLAN_CMD_RE.match(str(text or ""))
    if not m:
        return None
    body = (m.group(1) or "").strip()
    if not body:
        raise PlanError("计划指令缺少动作。" + USAGE)
    parts = body.split(None, 1)
    action = parts[0].lower()
    rest = (parts[1] if len(parts) > 1 else "").strip()

    if action in ("create", "new", "新建"):
        # 两套写法都收：
        #   /plan create PAY 支付链路改造                —— 人手习惯，标题可含空格
        #   @command:plan create 前缀=PAY 标题=… 关联=T-0001,T-0002
        #                                                —— agent 习惯，字段显式，
        #                                                   一条命令就能带上关联任务
        if "=" in rest or "：" in rest:
            try:
                fields = _parse_fields(rest)
            except PlanError as exc:
                raise PlanError(exc.message + "\n" + USAGE) from None
            prefix = str(fields.get("prefix") or "").strip()
            title = str(fields.get("title") or "").strip()
            if not prefix or not title:
                raise PlanError("新建计划需要 前缀= 与 标题=。" + USAGE)
            return {"action": "create", "prefix": prefix, "title": title,
                    "description": str(fields.get("description") or ""),
                    "task_ids": fields.get("task_ids") or [],
                    "pinned": bool(fields.get("pinned")),
                    "auto_complete": fields.get("auto_complete", True)}
        bits = rest.split(None, 1)
        if len(bits) < 2:
            raise PlanError("新建计划需要「前缀 + 标题」。" + USAGE)
        return {"action": "create", "prefix": bits[0], "title": bits[1],
                "description": ""}

    if action in ("list", "ls", "列表"):
        return {"action": "list"}

    if action in ("help", "?", "帮助"):
        return {"action": "help"}

    # 其余动作都要计划号
    bits = rest.split(None, 1)
    if not bits or not bits[0]:
        raise PlanError("%s 需要计划号（如 P-0001）。" % action + USAGE)
    plan_id = bits[0].strip().upper()
    tail = (bits[1] if len(bits) > 1 else "").strip()

    if action in ("update", "edit", "set", "修改", "编辑"):
        try:
            fields = _parse_fields(tail)
        except PlanError as exc:
            # 字段级错误（未知字段 / bool 值不合法）也要把正确格式带上：
            # agent 只看到一句「置顶只接受 on/off」仍然不知道整条命令长什么样。
            raise PlanError(exc.message + "\n" + USAGE) from None
        if not fields:
            raise PlanError("update 至少要给一个字段（标题=… 描述=… 关联=…）。" + USAGE)
        return {"action": "update", "plan_id": plan_id, "fields": fields}
    if action in ("complete", "done", "完成"):
        return {"action": "complete", "plan_id": plan_id}
    if action in ("close", "关闭"):
        return {"action": "close", "plan_id": plan_id}
    if action in ("delete", "del", "rm", "删除"):
        return {"action": "delete", "plan_id": plan_id}
    if action in ("pin", "置顶"):
        return {"action": "pin", "plan_id": plan_id, "pinned": True}
    if action in ("unpin", "取消置顶"):
        return {"action": "unpin", "plan_id": plan_id, "pinned": False}
    raise PlanError("不认识的计划动作：%s。" % action + USAGE)


def _parse_fields(tail: str) -> dict:
    """解析 `标题=新标题 关联=T-0001,T-0002 置顶=on`；也接受 `key:value`。"""
    fields = {}
    if not tail:
        return fields
    # 逐段扫描：值里允许空格（描述=先做支付 再做退款），所以按「下一个 key=」切分
    pattern = re.compile(r"([\w\u4e00-\u9fff]+)\s*[=:]\s*")
    matches = list(pattern.finditer(tail))
    if not matches:
        raise PlanError("字段格式应为 key=value（如 标题=支付改造）。" + USAGE)
    for idx, mo in enumerate(matches):
        key = FIELD_ALIASES.get(mo.group(1).lower())
        start = mo.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(tail)
        value = tail[start:end].strip()
        if key is None:
            raise PlanError("不认识的字段：%s（可用：标题 / 描述 / 前缀 / 关联 / 置顶 / 自动完成）"
                            % mo.group(1))
        if key == "pinned":
            fields["pinned"] = _bool(value, "置顶")
        elif key == "auto_complete":
            fields["auto_complete"] = _bool(value, "自动完成")
        elif key == "task_ids":
            fields["task_ids"] = _norm_task_ids(value)
        elif key == "status":
            status = STATUS_ALIASES.get(value.lower(), value)
            if status not in (STATUS_ACTIVE, STATUS_COMPLETED, STATUS_CLOSED):
                raise PlanError("状态只能是 进行中 / 已完成 / 已关闭")
            fields["status"] = status
        elif key == "completed_at":
            # 完成时间只读派生，显式写也算「改成已完成」，但不接受伪造时间
            fields["status"] = STATUS_COMPLETED
        else:
            fields[key] = value
    return fields


def run_command(text: str, who: dict, tasks_by_id=None, path=None) -> dict:
    """执行一条留言指令，返回 {ok, message, plan?}。

    权限在这里统一收口：写动作都走 require_write，删除额外要求 controller
    （留言里没有「二次确认」这一步可用）。
    """
    tasks_by_id = tasks_by_id or {}
    try:
        cmd = parse_command(text)
    except PlanError as exc:
        return {"ok": False, "error": exc.message, "usage": USAGE}
    if cmd is None:
        return {"ok": False, "error": "不是计划指令", "usage": USAGE}

    action = cmd["action"]
    actor = _actor(who)
    try:
        if action == "help":
            return {"ok": True, "message": USAGE}
        if action == "list":
            items = list_plans(tasks_by_id, path=path)
            return {"ok": True, "message": "共 %d 个计划" % len(items), "plans": items}
        if action == "create":
            plan = create_plan(cmd["prefix"], cmd["title"], cmd.get("description") or "",
                               task_ids=cmd.get("task_ids"),
                               pinned=bool(cmd.get("pinned")),
                               auto_complete=cmd.get("auto_complete", True),
                               created_by=actor, path=path)
            extra = ""
            if plan["task_ids"]:
                extra = "，关联 %d 个任务" % len(plan["task_ids"])
            return {"ok": True, "plan": plan,
                    "message": "已创建计划 %s（%s）%s%s"
                               % (plan["id"], plan["prefix"], plan["title"], extra)}
        if action == "update":
            plan = update_plan(cmd["plan_id"], who, cmd["fields"], path=path)
            return {"ok": True, "plan": plan,
                    "message": "已更新计划 %s：%s" % (plan["id"], plan["title"])}
        if action == "complete":
            plan = complete_plan(cmd["plan_id"], who, path=path)
            return {"ok": True, "plan": plan, "message": "计划 %s 已完成" % plan["id"]}
        if action == "close":
            plan = close_plan(cmd["plan_id"], who, path=path)
            return {"ok": True, "plan": plan, "message": "计划 %s 已关闭" % plan["id"]}
        if action == "delete":
            # 留言通道没有二次确认：只允许 controller
            plan = get_plan(cmd["plan_id"], tasks_by_id, path=path)
            if plan is None:
                raise PlanError("没有这个计划：%s" % cmd["plan_id"], code=404)
            if str((who or {}).get("role") or "") not in ("controller", "admin"):
                raise PlanError("留言里删除计划只有 controller 能执行（当前 %s）；"
                                "创建者请在页面上确认删除" % ((who or {}).get("agent") or "匿名"),
                                code=403)
            plan = delete_plan(cmd["plan_id"], who, confirm=True, path=path)
            return {"ok": True, "plan": plan, "message": "计划 %s 已删除（软删除）" % plan["id"]}
        if action in ("pin", "unpin"):
            plan = set_pinned(cmd["plan_id"], who, cmd["pinned"], path=path)
            return {"ok": True, "plan": plan,
                    "message": "计划 %s 已%s" % (plan["id"], "置顶" if cmd["pinned"] else "取消置顶")}
    except PlanError as exc:
        return {"ok": False, "error": exc.message, "code": exc.code, "usage": USAGE}
    return {"ok": False, "error": "未实现的动作：%s" % action, "usage": USAGE}
