"""卡面自定义字段的往返保真（T-0227 事故回归）。

背景：`write_card` 只按 `TASK_FIELDS` 输出，卡面上任何没登记的字段都会在重写时被吞掉。
`retry` / `take` / 关闭流程都会重写卡片，于是出现这样的死循环：

    给 T-0227 加 allow_empty_diff: true → retry（卡片被重写，字段消失）
    → submit 读不到开关 → 判零改动 failed → 又要加开关 …

现规则：`TASK_FIELDS` 之外的键**原样保留**，只跳过下划线开头的内部字段（`_body` / `_path`）。
"""

from __future__ import annotations

from pathlib import Path

from hive import TASK_FIELDS, parse_card, write_card


def _roundtrip(tmp_path: Path, data: dict) -> dict:
    p = tmp_path / "T-9999.md"
    write_card(p, dict(data), "# 正文\n")
    back, body = parse_card(p)
    assert body.strip() == "# 正文"
    return back


def test_custom_field_survives_roundtrip(tmp_path):
    """allow_empty_diff 这类自定义开关不能被写回时吞掉。"""
    data = {
        "id": "T-9999", "title": "只打 tag", "repo": "hive", "base": "main",
        "state": "ready", "owner": None, "paths": [], "priority": "P1",
        "verify": ["git rev-parse -q --verify refs/tags/v0.1.0"],
        "allow_empty_diff": True,
    }
    back = _roundtrip(tmp_path, data)
    assert back["allow_empty_diff"] is True


def test_arbitrary_custom_field_also_survives(tmp_path):
    """不只白名单里的那一个：任何非内部字段都该保留（未来加开关不用改两处）。"""
    back = _roundtrip(tmp_path, {
        "id": "T-9999", "title": "t", "state": "ready",
        "custom_note": "keep-me", "x_flag": False,
    })
    assert back["custom_note"] == "keep-me"
    assert back["x_flag"] is False


def test_internal_fields_are_not_written(tmp_path):
    """`_body` / `_path` 是内存里的辅助字段，不能写进 frontmatter。"""
    p = tmp_path / "T-9999.md"
    write_card(p, {"id": "T-9999", "title": "t", "_body": "内部", "_path": "/x"}, "# b\n")
    text = p.read_text(encoding="utf-8")
    assert "_body" not in text and "_path" not in text


def test_known_fields_keep_their_order(tmp_path):
    """已知字段仍按 TASK_FIELDS 的顺序输出，自定义字段排在它们之后（diff 好读）。"""
    p = tmp_path / "T-9999.md"
    write_card(p, {"priority": "P1", "id": "T-9999", "title": "t", "zz_custom": 1}, "# b\n")
    lines = [ln.split(":", 1)[0] for ln in p.read_text(encoding="utf-8").splitlines()[1:]]
    known = [k for k in TASK_FIELDS if k in lines]
    assert lines[:len(known)] == known
    assert "zz_custom" in lines[len(known):]
