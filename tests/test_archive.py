"""T-0219：冷热分离与归档。

四条判据各自独立反证（见 docs/冷热分离与归档.md §9）：

1. 迁移幂等 → 在 test_store.py（`test_migration_v2_is_idempotent`）；
2. 归档后 `status --summary` 体积腰斩，且冷卡仍可列出（`archived: true`）；
3. `--restore` 之后详情可读、`archived` 复位；
4. `--dry-run` 用目录快照前后 diff 证明零移动。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import hive_archive as ha
from hive import Hive, write_card


# --------------------------------------------------------------------------- #
# 造数：直接写工作区文件，不走 `hive task new`（60 张卡逐条提交太慢）
# --------------------------------------------------------------------------- #

def _iso_days_ago(days: float) -> str:
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(
        timespec="seconds")


def _make_card(hive_dir, tid: str, *, state: str = "done", when: str,
               title: str = "测试卡", repo: str = "light",
               priority: str = "P2", body: str = "正文\n") -> None:
    p = hive_dir / "tasks" / f"{tid}.md"
    data = {"id": tid, "title": title, "repo": repo, "base": "main",
            "state": state, "owner": "", "priority": priority,
            "created": when, "updated": when}
    write_card(p, data, body)


def _make_result(hive_dir, tid: str, *, when: str,
                 summary: str = "第一行结论\n第二行细节") -> None:
    """造一份「像真跑过」的结果：带 verify 的 tail 和整段 summary。

    这两样正是旧 `status --json` 体积的大头（列表页一个都用不到），
    摘要通道必须一个字节都不带——所以造数时要带上，否则体积对比是假的。
    """
    p = hive_dir / "results" / f"{tid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task": tid, "status": "done", "agent": "a", "head": "0" * 40,
        "finished_at": when,
        "verify": [{"cmd": "python3 -m pytest -q tests/test_store.py",
                    "code": 0,
                    "tail": "..........  [100%]\n10 passed in 6.61s\n" * 8}],
        "changed_files": ["hive_store.py", "hive_archive.py"],
        "summary": summary,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed(hive_dir, n_old: int, n_new: int, *, old_days: float = 40.0,
          new_days: float = 1.0) -> tuple[list[str], list[str]]:
    """n_old 张「完成于 old_days 天前」+ n_new 张「完成于 new_days 天前」的 done 卡。"""
    old, new = [], []
    for i in range(n_old):
        tid = f"T-01{i:02d}"
        when = _iso_days_ago(old_days)
        _make_card(hive_dir, tid, when=when, title=f"旧卡{i}")
        _make_result(hive_dir, tid, when=when, summary=f"旧卡{i}已收口\n细节")
        old.append(tid)
    for i in range(n_new):
        tid = f"T-02{i:02d}"
        when = _iso_days_ago(new_days)
        _make_card(hive_dir, tid, when=when, title=f"新卡{i}")
        _make_result(hive_dir, tid, when=when, summary=f"新卡{i}已收口\n细节")
        new.append(tid)
    return old, new


# --------------------------------------------------------------------------- #
# 判据 4：dry-run 零副作用
# --------------------------------------------------------------------------- #

def test_dry_run_moves_nothing(world):
    old, _ = _seed(world.hive1, 5, 2)
    before = ha.snapshot_tree(world.hive1)

    items = ha.plan(world.hive1, days=14)
    assert [i["id"] for i in items] == old            # 只挑旧卡
    assert all(i["target_rel"].startswith("tasks/archive/") for i in items)

    out = world.hive("archive", "--days", "14")        # 默认就是 dry-run
    after = ha.snapshot_tree(world.hive1)

    # 目录快照（路径 + 字节数 + mtime_ns）必须逐项相同 —— 连 touch 都不许发生
    assert after == before
    assert "待归档" in out.stdout
    assert "dry-run" in out.stdout
    for tid in old:
        assert (world.hive1 / "tasks" / f"{tid}.md").is_file()
    assert not (world.hive1 / "tasks" / "archive").exists()


def test_dry_run_json_reports_candidates_without_touching(world):
    old, _ = _seed(world.hive1, 3, 1)
    before = ha.snapshot_tree(world.hive1)
    out = world.hive("archive", "--days", "14", "--json")
    data = json.loads(out.stdout)
    assert data["applied"] is False and data["moved"] == 0
    assert [c["id"] for c in data["candidates"]] == old
    assert ha.snapshot_tree(world.hive1) == before


# --------------------------------------------------------------------------- #
# 判据 2 + 「归档 ≠ 删除」
# --------------------------------------------------------------------------- #

def test_archive_shrinks_summary_at_least_half_and_keeps_cold_cards(world):
    hive1 = world.hive1
    old, new = _seed(hive1, 50, 10)

    before = world.hive("status", "--summary", "--json").stdout
    before_rows = json.loads(before)
    assert len(before_rows) == 60
    assert all(r["archived"] is False for r in before_rows)

    items = ha.plan(hive1, days=14)
    assert len(items) == 50
    moved = ha.apply_archive(hive1, items)
    assert len(moved) == 50

    after = world.hive("status", "--summary", "--json").stdout
    after_rows = json.loads(after)
    assert len(after_rows) == 10                       # 默认只热卡
    assert [r["id"] for r in after_rows] == new
    # 判据 2：体积下降 ≥ 50%
    assert len(after.encode("utf-8")) <= 0.5 * len(before.encode("utf-8")), (
        f"摘要体积没腰斩：{len(before.encode())} -> {len(after.encode())}")

    # 冷卡没被删：50 个文件都在 tasks/archive/<YYYY-MM>/ 下
    archived_files = list((hive1 / "tasks" / "archive").glob("*/T-*.md"))
    assert len(archived_files) == 50
    assert len({p.name for p in archived_files}) == 50

    # 摘要仍能查到这 50 张，且 archived 为真（判据 2 的后半句）
    full = json.loads(world.hive("status", "--summary", "--json",
                                 "--include-archived").stdout)
    assert len(full) == 60
    cold = [r for r in full if r["archived"]]
    assert [r["id"] for r in cold] == old
    assert all(r["state"] == "done" for r in cold)
    assert all(r["summary_head"].startswith("旧卡") for r in cold)

    # 摘要行只有 10 个字段：详情（verify / result_summary / 正文）一律不下发
    assert set(full[0]) == {"id", "title", "state", "repo", "priority", "owner",
                            "created_at", "finished_at", "summary_head", "archived"}

    # 动机数字：旧全量通道确实比摘要通道大一个量级
    legacy = world.hive("status", "--json").stdout
    assert len(legacy.encode("utf-8")) > 3 * len(after.encode("utf-8"))


def test_archived_cards_drop_out_of_cards_but_detail_still_readable(world):
    old, _ = _seed(world.hive1, 2, 1)
    ha.apply_archive(world.hive1, ha.plan(world.hive1, days=14))

    h = Hive(str(world.hive1))
    assert set(h.cards()) == {"T-0200"}                # 热目录只剩新卡
    assert set(h.archived_cards()) == set(old)         # 冷目录两张都在
    card = h.card(old[0])                              # 详情按 id 现取，两处都查
    assert card["state"] == "done"
    assert card["archived"] is True
    assert card["_path"].parent.parent.name == "archive"


# --------------------------------------------------------------------------- #
# 判据 3：可回滚
# --------------------------------------------------------------------------- #

def test_restore_brings_card_back_with_detail_intact(world):
    hive1 = world.hive1
    tid = "T-0100"
    marker = "唯一正文标记-Z9Q7"
    _make_card(hive1, tid, when=_iso_days_ago(40), title="可回滚卡",
               body=f"{marker}\n第二段\n")
    _make_result(hive1, tid, when=_iso_days_ago(40))

    ha.apply_archive(hive1, ha.plan(hive1, days=14))
    assert not (hive1 / "tasks" / f"{tid}.md").exists()
    assert json.loads(world.hive("status", "--summary", "--json").stdout) == []

    out = ha.restore(hive1, tid)
    assert out["id"] == tid and out["restored_to"].endswith(f"{tid}.md")

    back = hive1 / "tasks" / f"{tid}.md"
    assert back.is_file()
    text = back.read_text(encoding="utf-8")
    assert marker in text                              # 正文逐字保留
    assert "archived" not in text.split("---")[1]      # 归档标记已清掉

    card = Hive(str(hive1)).card(tid)                  # 详情可读
    assert card["state"] == "done"
    assert not card.get("archived")

    rows = json.loads(world.hive("status", "--summary", "--json").stdout)
    assert [r["id"] for r in rows] == [tid]            # 复位后重新进默认列表
    assert rows[0]["archived"] is False
    # 空分片目录顺手清掉，但不删 tasks/archive 本体
    assert (hive1 / "tasks" / "archive").is_dir()


def test_restore_refuses_to_overwrite_hot_card(world):
    hive1 = world.hive1
    tid = "T-0101"
    _make_card(hive1, tid, when=_iso_days_ago(40), body="冷的那份\n")
    ha.apply_archive(hive1, ha.plan(hive1, days=14))
    _make_card(hive1, tid, when=_iso_days_ago(0), body="热的那份\n")  # 有人又建了一张

    with pytest.raises(FileExistsError):
        ha.restore(hive1, tid)
    # 拒绝之后两份都还在，谁都没被吞
    assert (hive1 / "tasks" / f"{tid}.md").read_text(encoding="utf-8").count("热的那份") == 1
    assert len(list((hive1 / "tasks" / "archive").glob(f"*/{tid}.md"))) == 1

    out = world.hive("archive", "--restore", tid, check=False)
    assert out.returncode == 1
    assert "拒绝覆盖" in out.stderr


def test_restore_missing_card_errors_cleanly(world):
    _seed(world.hive1, 1, 0)
    before = ha.snapshot_tree(world.hive1)
    out = world.hive("archive", "--restore", "T-9999", check=False)
    assert out.returncode == 1
    assert "冷目录里没有" in out.stderr
    assert ha.snapshot_tree(world.hive1) == before


# --------------------------------------------------------------------------- #
# 边界：不该动的卡一张都不许动
# --------------------------------------------------------------------------- #

def test_only_finished_cards_past_window_are_eligible(world):
    hive1 = world.hive1
    _make_card(hive1, "T-0300", state="done", when=_iso_days_ago(40))
    _make_result(hive1, "T-0300", when=_iso_days_ago(40))
    # 40 天前建的，但还在等人 → 不是「完事」
    _make_card(hive1, "T-0301", state="blocked", when=_iso_days_ago(40))
    _make_card(hive1, "T-0302", state="needs_review", when=_iso_days_ago(40))
    # done 但昨天才完成 → 还在热窗口
    _make_card(hive1, "T-0303", state="done", when=_iso_days_ago(40))
    _make_result(hive1, "T-0303", when=_iso_days_ago(1))
    # done 但没有任何时间可解析 → 宁可留在热区，不误移
    _make_card(hive1, "T-0304", state="done", when="")
    # 已经带 archived 标记的热卡（手工搬回来的残局）不再二次归档
    _make_card(hive1, "T-0305", state="done", when=_iso_days_ago(40))
    _make_result(hive1, "T-0305", when=_iso_days_ago(40))
    p = hive1 / "tasks" / "T-0305.md"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "---\n", "---\narchived: true\n", 1), encoding="utf-8")

    assert [i["id"] for i in ha.plan(hive1, days=14)] == ["T-0300"]


def test_days_zero_is_escape_valve(world):
    _seed(world.hive1, 5, 0)
    assert ha.plan(world.hive1, days=0) == []
    before = ha.snapshot_tree(world.hive1)
    out = world.hive("archive", "--days", "0", "--apply")
    assert "0 张" in out.stdout
    assert ha.snapshot_tree(world.hive1) == before


def test_archive_days_env_override(monkeypatch):
    assert ha.archive_days(None) == ha.DEFAULT_ARCHIVE_DAYS
    monkeypatch.setenv(ha.DAYS_ENV, "3")
    assert ha.archive_days(None) == 3
    assert ha.archive_days(30) == 30                   # 显式参数优先
    monkeypatch.setenv(ha.DAYS_ENV, "不是数字")
    assert ha.archive_days(None) == ha.DEFAULT_ARCHIVE_DAYS


def test_cli_apply_moves_and_reports_json(world):
    old, _ = _seed(world.hive1, 4, 1)
    out = world.hive("archive", "--days", "14", "--apply", "--json")
    data = json.loads(out.stdout)
    assert data["applied"] is True and data["moved"] == 4
    assert [c["id"] for c in data["candidates"]] == old
    assert len(list((world.hive1 / "tasks" / "archive").glob("*/T-*.md"))) == 4
