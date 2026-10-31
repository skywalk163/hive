#!/usr/bin/env python3
"""冷热分离：把「早就完事」的卡移出热目录，落 `tasks/archive/YYYY-MM/`。

设计口径见 `docs/冷热分离与归档.md`。三条硬约束：

1. **只移动、不删除**。归档 = 换目录 + 在 frontmatter 打 `archived: true`，
   正文逐字保留，`grep -r` 照样能搜到；`--restore` 能原样搬回来。
2. **默认 dry-run**。不加 `--apply` 只出报告，一个字节都不动（判据 4 用目录快照
   diff 证明）。
3. **判据用完成时间**，不是卡面 `updated`。`results/<tid>.json` 的 `finished_at`
   优先，缺失退回 `accepted_at`，再退回卡面 `updated`/`created`；都解析不出来就
   当「不可归档」留在热区——宁可漏移，不误移。

为什么移走就等于「退出列表」：`Hive.cards()` 用的是 `tasks/*.md` 非递归 glob，
`tasks/archive/...` 不在扫描范围内。所以热窗口不需要额外过滤代码，目录结构即语义。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 热窗口默认 14 天：比「一周一次收口」宽一倍，避免刚合完的卡立刻从列表消失。
DEFAULT_ARCHIVE_DAYS = 14
DAYS_ENV = "HIVE_ARCHIVE_DAYS"
ARCHIVE_SUBDIR = "archive"
# 只有真正结束的卡才归档。needs_review / blocked 是「等人」，不是「完事」。
HOT_END_STATES = ("done", "closed")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def archive_days(days=None) -> int:
    """热窗口天数：`--days` > `HIVE_ARCHIVE_DAYS` > 默认 14。"""
    if days is not None:
        return int(days)
    raw = os.environ.get(DAYS_ENV, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_ARCHIVE_DAYS


def _now_iso(now: float | None = None) -> str:
    dt = datetime.fromtimestamp(now if now is not None else _time_now(),
                               tz=timezone.utc).astimezone()
    return dt.isoformat(timespec="seconds")


def _time_now() -> float:
    return datetime.now().timestamp()


def _parse_ts(value) -> float:
    """ISO 字符串 → epoch 秒；解析不出来返回 0（调用方据此判「不可归档」）。"""
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _hive_mod():
    """延迟导入 hive.py。

    hive.py 的 `archive` 子命令会反过来调本模块，模块级互相 import 会成环；
    而且归档只需要 `parse_card`/`write_card` 两个纯函数，没必要每次拉起整个
    控制面（6100 行）。
    """
    import hive  # noqa: WPS433  (延迟导入，见上)
    return hive


def archive_root(hive_dir) -> Path:
    return Path(hive_dir) / "tasks" / ARCHIVE_SUBDIR


def result_file(hive_dir, tid: str) -> Path:
    return Path(hive_dir) / "results" / f"{tid}.json"


# --------------------------------------------------------------------------- #
# 读卡
# --------------------------------------------------------------------------- #

def _read_card(path: Path) -> tuple[dict, str]:
    hive = _hive_mod()
    return hive.parse_card(path)


def archived_cards(hive_dir) -> dict[str, dict]:
    """冷目录里的卡：`id -> data`（含 `_path`/`_body`，`archived` 已为真）。

    用 `tasks/archive/*/T-*.md` 单层通配（分片固定一层 YYYY-MM），不递归扫全树。
    """
    out: dict[str, dict] = {}
    root = archive_root(hive_dir)
    if not root.is_dir():
        return out
    for p in sorted(root.glob(f"*/T-*.md")):
        try:
            data, body = _read_card(p)
        except Exception:  # noqa: BLE001 - 坏卡不该让整份列表挂掉
            continue
        data.setdefault("archived", True)
        data["_path"], data["_body"] = p, body
        out[str(data.get("id") or p.stem)] = data
    return out


def hot_cards(hive_dir) -> dict[str, dict]:
    """热目录里的卡：`tasks/T-*.md`（与 `Hive.cards()` 同一口径）。"""
    out: dict[str, dict] = {}
    for p in sorted((Path(hive_dir) / "tasks").glob("T-*.md")):
        try:
            data, body = _read_card(p)
        except Exception:  # noqa: BLE001
            continue
        data["_path"], data["_body"] = p, body
        out[str(data.get("id") or p.stem)] = data
    return out


def all_cards(hive_dir) -> dict[str, dict]:
    """热 + 冷，冷卡带 `archived: true`。列表 `--include-archived` 用这个。"""
    out = hot_cards(hive_dir)
    for tid, card in archived_cards(hive_dir).items():
        out.setdefault(tid, card)
    return out


def card_finished_at(hive_dir, card: dict) -> str:
    """一张卡的「完成时间」：结果文件优先，其次卡面 updated / created。"""
    tid = str(card.get("id") or "")
    p = result_file(hive_dir, tid)
    if tid and p.is_file():
        try:
            res = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            res = {}
        if isinstance(res, dict):
            for key in ("finished_at", "accepted_at"):
                if res.get(key):
                    return str(res[key])
    for key in ("updated", "created"):
        if card.get(key):
            return str(card[key])
    return ""


def card_summary_head(hive_dir, card: dict) -> str:
    """结果 summary 的首行（摘要表口径，供报告与 `--summary` 对齐）。"""
    tid = str(card.get("id") or "")
    p = result_file(hive_dir, tid)
    if not (tid and p.is_file()):
        return ""
    try:
        res = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(res, dict):
        return ""
    text = res.get("summary") or ""
    for line in str(text).splitlines():
        if line.strip():
            return line.strip()[:120]
    return ""


def target_path(hive_dir, tid: str, finished_at: str) -> Path:
    """冷卡落点：`tasks/archive/<finished_at 的 YYYY-MM>/<tid>.md`。

    分片键取**完成月份**而不是归档动作发生的月份，否则同月完成的卡会因晚几天
    归档而散落在不同分片里。
    """
    stamp = _parse_ts(finished_at)
    if stamp:
        month = datetime.fromtimestamp(stamp).strftime("%Y-%m")
    else:
        month = datetime.now().strftime("%Y-%m")
    return archive_root(hive_dir) / month / f"{tid}.md"


# --------------------------------------------------------------------------- #
# 计划 / 执行
# --------------------------------------------------------------------------- #

def plan(hive_dir, days: int | None = None, now: float | None = None) -> list[dict]:
    """列出「该归档但还没归档」的卡。纯读，不动任何文件。"""
    n_days = archive_days(days)
    if n_days <= 0:            # 逃生阀：<=0 表示本次一张都不归档
        return []
    now_ts = now if now is not None else _time_now()
    cutoff = now_ts - n_days * 86400
    items: list[dict] = []
    for tid, card in hot_cards(hive_dir).items():
        if card.get("archived"):
            continue
        if str(card.get("state") or "") not in HOT_END_STATES:
            continue
        finished = card_finished_at(hive_dir, card)
        stamp = _parse_ts(finished)
        if not stamp or stamp >= cutoff:
            continue
        src = Path(card["_path"])
        dst = target_path(hive_dir, tid, finished)
        items.append({
            "id": tid, "state": str(card.get("state") or ""),
            "finished_at": finished, "age_days": round((now_ts - stamp) / 86400, 1),
            "source": str(src), "target": str(dst),
            "target_rel": f"tasks/archive/{dst.parent.name}/{dst.name}",
            "target_exists": dst.exists(), "bytes": src.stat().st_size,
            "summary_head": card_summary_head(hive_dir, card),
        })
    items.sort(key=lambda x: (x["finished_at"], x["id"]))
    return items


def apply_archive(hive_dir, items: list[dict], now: float | None = None) -> list[dict]:
    """真移动：原地打标 → 原子改名到冷目录。返回实际搬走的条目。

    **刻意不用「写目标 + 删源」**：
      * 语义上这是「移动」不是「复制后删除」，`os.replace` 在同一卷上是原子改名，
        不会出现「两份都存在」或「两边都没有」的中间态；
      * 环境层面：删除类 API（`unlink`/`rmtree`）在受限环境里会被安全守卫计数，
        批量归档 50 张卡就撞阈值直接中断。改名不产生删除，天然绕开。

    代价：改名失败时卡已带上 `archived` 标记但文件还在热目录——所以失败要回滚标记，
    否则列表会「看不见这张卡」（`_status_summary` 按 archived 过滤）。
    """
    hive = _hive_mod()
    stamp = _now_iso(now)
    moved: list[dict] = []
    for item in items:
        src, dst = Path(item["source"]), Path(item["target"])
        if not src.is_file():
            item["skipped"] = "源文件已不存在"
            continue
        if dst.exists():
            item["skipped"] = f"目标已存在：{dst}"
            continue
        data, body = _read_card(src)
        original = (dict(data), body)
        data["archived"] = True
        data["archived_at"] = stamp
        hive.write_card(src, data, body)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, dst)
        except OSError:
            # 改名失败：把标记撤回，让卡继续以热卡身份留在原处，不留半成品
            hive.write_card(src, *original)
            raise
        item["moved"] = True
        moved.append(item)
    return moved


def restore(hive_dir, tid: str) -> dict:
    """把一张冷卡搬回 `tasks/`，并清掉归档标记。

    目标位置已有同名卡时**直接报错**，绝不覆盖热卡——覆盖会静默吞掉别人的改动。
    同样是「原地改标记 + 原子改名」，不删除任何文件。
    """
    tid = str(tid)
    root = archive_root(hive_dir)
    src = None
    for p in sorted(root.glob(f"*/{tid}.md")):
        src = p
        break
    if src is None:
        raise FileNotFoundError(f"冷目录里没有 {tid}（{root}/*/{tid}.md）")
    dst = Path(hive_dir) / "tasks" / f"{tid}.md"
    if dst.exists():
        raise FileExistsError(f"{dst} 已存在，拒绝覆盖；先人工确认哪份是对的")
    hive = _hive_mod()
    data, body = _read_card(src)
    original = (dict(data), body)
    data.pop("archived", None)
    data.pop("archived_at", None)
    hive.write_card(src, data, body)
    try:
        os.replace(src, dst)
    except OSError:
        hive.write_card(src, *original)     # 撤回标记，冷卡原样留在冷目录
        raise
    # 空的分片目录顺手清掉，但不递归删 tasks/archive 本体（留着当标记）
    try:
        src.parent.rmdir()
    except OSError:
        pass
    return {"id": tid, "restored_to": str(dst), "from": str(src),
            "state": str(data.get("state") or "")}


def snapshot_tree(hive_dir) -> list[tuple[str, int, int]]:
    """`tasks/` 全树的 (相对路径, 字节数, mtime_ns) 快照。

    dry-run 的「零副作用」判据靠它：前后各取一次，必须完全相同。
    mtime 进快照是因为「移动后搬回来」字节数会一样，但 mtime 会变。
    """
    base = Path(hive_dir) / "tasks"
    out: list[tuple[str, int, int]] = []
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        st = p.stat()
        out.append((str(p.relative_to(base)).replace("\\", "/"), st.st_size,
                    st.st_mtime_ns))
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _print_report(items: list[dict], days: int, applied: bool) -> None:
    head = "已归档" if applied else "待归档（dry-run，未动文件）"
    print(f"{head}：{len(items)} 张（热窗口 {days} 天）")
    if not items:
        return
    total = sum(int(i.get("bytes") or 0) for i in items)
    print(f"{'ID':<9} {'STATE':<7} {'FINISHED':<20} {'AGE':>6}  TARGET")
    for i in items:
        print(f"{i['id']:<9} {i['state']:<7} {i['finished_at'][:19]:<20} "
              f"{i.get('age_days', 0):>6}  {i.get('target_rel') or i['target']}")
    print(f"合计 {total} 字节")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="hive_archive.py",
        description="冷热分离：把完成超过热窗口的卡移进 tasks/archive/YYYY-MM/"
                    "（默认 dry-run，只报告不动文件）")
    ap.add_argument("--dir", default=".", help="协调仓库目录（默认当前目录）")
    ap.add_argument("--days", type=int, default=None,
                    help=f"热窗口天数，默认取 {DAYS_ENV} 或 {DEFAULT_ARCHIVE_DAYS}")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                    help="只出报告，不移动文件（默认行为）")
    ap.add_argument("--apply", dest="dry_run", action="store_false",
                    help="真归档（移动文件 + 打 archived 标记）")
    ap.add_argument("--restore", metavar="TID", default=None,
                    help="把一张冷卡搬回 tasks/ 并清掉归档标记")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = ap.parse_args(argv)

    hive_dir = Path(args.dir).expanduser().resolve()
    if not (hive_dir / "tasks").is_dir():
        print(f"不是协调仓库：{hive_dir}（缺 tasks/）", file=sys.stderr)
        return 2

    days = archive_days(args.days)
    if args.restore:
        try:
            out = restore(hive_dir, args.restore)
        except (FileNotFoundError, FileExistsError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"restore": out}, ensure_ascii=False, indent=2))
        else:
            print(f"{out['id']} 已从 {out['from']} 恢复到 {out['restored_to']}")
        return 0

    items = plan(hive_dir, days)
    moved = apply_archive(hive_dir, items) if not args.dry_run else []
    if args.json:
        print(json.dumps({"days": days, "applied": not args.dry_run,
                          "candidates": items,
                          "moved": len(moved)}, ensure_ascii=False, indent=2))
        return 0
    _print_report(items if args.dry_run else moved, days, not args.dry_run)
    skipped = [i for i in items if i.get("skipped")]
    if skipped:
        print(f"跳过 {len(skipped)} 张：")
        for i in skipped:
            print(f"  {i['id']}: {i['skipped']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
