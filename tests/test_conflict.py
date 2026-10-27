"""T-0163：文件级锁（`locks` 字段）的判定与 dispatch 接线。

分三层测，故意分开写，坏在哪一层一眼能看出来：

1. **单元**：`conflict._lock_matches` / `_locks_conflict` 的三种写法（目录锁 / 文件锁 / 通配）
   与对称性；
2. **接线**：`Hive._paths_conflict` 在「双方都有 locks」时走精确逻辑、否则回退旧 `paths`
   规则（旧卡行为不变）；
3. **端到端**：两张卡各写互不相干的 `locks` 时 dispatch 能同时派出去（这才是本卡要的效果），
   写 `src/` 的卡仍会被拦下。
"""

from __future__ import annotations

import textwrap

import pytest

import hive
from conflict import _lock_matches, _locks_conflict, normalize_locks

# --------------------------------------------------------------------------- #
# 1. 单元：两条锁声明是否相互覆盖
# --------------------------------------------------------------------------- #


def test_lock_matches_exact_files():
    """精确文件锁：同名才冲突，`src/a.py` 与 `src/b.py` 互不相干。"""
    assert _lock_matches("src/a.py", "src/a.py") is True
    assert _lock_matches("src/a.py", "src/b.py") is False
    # 没有尾斜杠就是「文件」：`src` 不是 `src/` 的简写
    assert _lock_matches("src", "src/a.py") is False


def test_lock_matches_dir_prefix():
    """目录锁（带尾斜杠）：覆盖其下所有层级，且尊重目录边界。"""
    assert _lock_matches("src/", "src/a.py") is True
    assert _lock_matches("src/", "src/sub/b.py") is True
    assert _lock_matches("src/", "src/") is True
    assert _lock_matches("src/", "src2/a.py") is False      # 前缀不是目录边界
    assert _lock_matches("src/sub/", "src/") is True        # 子目录也在 src/ 下


def test_lock_matches_glob():
    """通配锁：`fnmatch` 命中即冲突；`*` 跨 `/` 是有意为之（够用即可）。"""
    assert _lock_matches("src/*.py", "src/a.py") is True
    assert _lock_matches("src/*.py", "src/sub/a.py") is True   # * 跨目录
    assert _lock_matches("src/*.py", "docs/a.py") is False
    assert _lock_matches("**/test_*.py", "tests/test_a.py") is True
    assert _lock_matches("**/test_*.py", "a/b/test_x.py") is True
    assert _lock_matches("**/test_*.py", "tests/a.py") is False
    # 目录锁 vs 通配锁：通配的静态前缀落在目录下，就算覆盖
    assert _lock_matches("src/", "src/*.py") is True
    assert _lock_matches("src/", "docs/*.py") is False
    # 以通配开头的声明能伸到任何目录下
    assert _lock_matches("src/", "**/test_*.py") is True


@pytest.mark.parametrize("a,b", [
    ("src/a.py", "src/b.py"),
    ("src/", "src/a.py"),
    ("src/*.py", "src/a.py"),
    ("**/test_*.py", "tests/test_a.py"),
    ("src/", "docs/"),
    ("hive.py", "docs/"),
    ("src/sub/", "src/*.py"),
])
def test_lock_matches_is_symmetric(a, b):
    """判匹配必须对称：谁在左边不该影响结果（否则 dispatch 结论会随卡片顺序漂）。"""
    assert _lock_matches(a, b) == _lock_matches(b, a)


def test_normalize_locks():
    """规整：去空白、反斜杠转正斜杠、去 `./` 前缀；空值/空串过滤掉；单条标量也接。"""
    assert normalize_locks(None) == []
    assert normalize_locks([]) == []
    assert normalize_locks("  ") == []
    assert normalize_locks("src/a.py") == ["src/a.py"]           # frontmatter 写成标量
    assert normalize_locks([" src\\a.py ", "./docs/", ""]) == ["src/a.py", "docs/"]
    assert normalize_locks(["a.py", "a.py"]) == ["a.py"]          # 去重保序


def test_locks_conflict_cases():
    """本卡要求的五条判定 + 保守默认。"""
    # 精确文件互不相干 -> 不冲突
    assert _locks_conflict(["src/a.py"], ["src/b.py"]) is False
    # 目录覆盖文件 -> 冲突
    assert _locks_conflict(["src/"], ["src/a.py"]) is True
    # 通配命中 -> 冲突
    assert _locks_conflict(["src/*.py"], ["src/a.py"]) is True
    # 两张卡各改一组互不相干的文件 -> 不冲突
    assert _locks_conflict(["src/a.py", "tests/x.py"],
                           ["src/b.py", "tests/y.py"]) is False
    # 任一方没写 locks -> 保守冲突（少写字段不能变成绕过并发保护的捷径）
    assert _locks_conflict(["src/a.py"], []) is True
    assert _locks_conflict([], ["src/b.py"]) is True
    assert _locks_conflict(None, None) is True


# --------------------------------------------------------------------------- #
# 2. 接线：Hive._paths_conflict 的两条分支
# --------------------------------------------------------------------------- #


def test_paths_conflict_uses_locks_when_both_declared(world):
    """双方都有 locks 时按精确文件集合判：只看 paths 会判冲突的组合，现在能并行。"""
    h = hive.Hive(world.hive1)
    # paths 相同（都写 src/），但 locks 精确到互不相干的文件 -> 不冲突
    assert h._paths_conflict(["src/"], ["src/"],
                             ["src/a.py"], ["src/b.py"]) is False
    # locks 真打架 -> 冲突
    assert h._paths_conflict(["src/"], ["src/"],
                             ["src/"], ["src/a.py"]) is True
    assert h._paths_conflict(["src/"], ["src/"],
                             ["src/*.py"], ["src/a.py"]) is True


def test_paths_conflict_falls_back_to_paths(world):
    """只有一方写了 locks 时回退旧 paths 规则 —— 旧卡一个字段都不用改。"""
    h = hive.Hive(world.hive1)
    # 一方有 locks、另一方 paths 为空 -> 保守冲突（旧默认）
    assert h._paths_conflict([], [], ["src/a.py"], None) is True
    assert h._paths_conflict(["src/a.py"], [], ["src/a.py"], []) is True
    # 一方有 locks、另一方只有 paths -> 按目录前缀判，locks 不参与
    assert h._paths_conflict(["src/"], ["src/b.py"], ["src/a.py"], None) is True
    assert h._paths_conflict(["src/"], ["docs/"], ["src/a.py"], None) is False


def test_paths_conflict_legacy_unchanged(world):
    """不传 locks 时与 T-0163 之前完全一致（旧签名调用仍然合法）。"""
    h = hive.Hive(world.hive1)
    assert h._paths_conflict(["hive.py"], ["hive.py"]) is True
    assert h._paths_conflict(["docs/"], ["docs/操作手册.md"]) is True
    assert h._paths_conflict(["hive.py"], ["docs/"]) is False
    assert h._paths_conflict([], ["hive.py"]) is True
    assert h._paths_conflict(["tests/"], ["testing/"]) is False


# --------------------------------------------------------------------------- #
# 3. 卡面：locks 字段能存住（write_card 只写 TASK_FIELDS，漏登记就会被静默抹掉）
# --------------------------------------------------------------------------- #


def test_card_locks_survive_rewrite(tmp_path):
    """卡上写了 locks，被 hive 重写（assign/close 都会走 write_card）之后不能丢。"""
    p = tmp_path / "T-9001.md"
    p.write_text(textwrap.dedent("""\
        ---
        id: T-9001
        title: 锁字段回归
        repo: hive
        base: main
        state: ready
        priority: P1
        paths:
          - src/
        locks:
          - src/a.py
          - "src/*.py"
        verify:
          - python3 -m pytest -q
        ---

        正文
        """), encoding="utf-8")

    data, body = hive.parse_card(p)
    assert data["locks"] == ["src/a.py", "src/*.py"]
    data["state"], data["owner"] = "assigned", "someone"
    hive.write_card(p, data, body)

    again, _ = hive.parse_card(p)
    assert again["locks"] == ["src/a.py", "src/*.py"], "locks 被 write_card 抹掉了"
    assert again["state"] == "assigned" and again["owner"] == "someone"


# --------------------------------------------------------------------------- #
# 4. 端到端：dispatch 真的能并行派「文件不重叠」的两张卡
# --------------------------------------------------------------------------- #


def _set_locks(world, tid: str, locks) -> None:
    """给已建好的卡补上 locks（`task new` 还没有 --locks，这里直接改 frontmatter）。"""
    p = world.hive1 / "tasks" / f"{tid}.md"
    data, body = hive.parse_card(p)
    data["locks"] = list(locks)
    hive.write_card(p, data, body)


def test_dispatch_parallel_by_file_locks(world):
    """两张卡都声明了互不相干的 locks -> 同时派出；写 `src/` 的第三张被拦下。"""
    # 三台机器：前两张各占一台，第三张还有空机器可用，所以被拦下的原因只会是
    # 「paths/locks 冲突」而不是「负载已满」—— 判据才咬得住。
    for name in ("fb", "fb2", "fb3"):
        world.add_agent(name, "exec", caps=["freebsd"],
                        repos={"hive": world.code_clone(name)})
    # paths 故意写成同一个目录（旧逻辑会判冲突），靠 locks 区分开
    world.add_task("T-0001", repo="hive", paths=["src/", "tests/"])
    world.add_task("T-0002", repo="hive", paths=["src/", "tests/"])
    world.add_task("T-0003", repo="hive", paths=["src/"])
    _set_locks(world, "T-0001", ["src/a.py", "tests/x.py"])
    _set_locks(world, "T-0002", ["src/b.py", "tests/y.py"])
    _set_locks(world, "T-0003", ["src/"])

    out = world.hive_json("dispatch")
    assert {p["task"] for p in out["plan"]} == {"T-0001", "T-0002"}, out
    reason = {s["task"]: s["reason"] for s in out["skipped"]}["T-0003"]
    assert "冲突" in reason and "T-0001" in reason, reason


def test_dispatch_locks_conflict_blocked(world):
    """locks 真重叠（同一文件 / 目录覆盖）时仍然拦住，不会因为写了 locks 就放开。"""
    world.add_agent("fb", "exec", caps=["freebsd"], repos={"hive": world.code_clone("fb")})
    world.add_agent("fb2", "exec", caps=["freebsd"], repos={"hive": world.code_clone("fb2")})
    world.add_task("T-0001", repo="hive", paths=["src/"])
    world.add_task("T-0002", repo="hive", paths=["src/"])
    _set_locks(world, "T-0001", ["src/a.py"])
    _set_locks(world, "T-0002", ["src/a.py"])

    out = world.hive_json("dispatch")
    assert [p["task"] for p in out["plan"]] == ["T-0001"], out
    assert {s["task"] for s in out["skipped"]} == {"T-0002"}, out


def test_dispatch_legacy_card_without_locks_still_conflicts(world):
    """一张写了 locks、一张只写 paths：回退旧规则，仍然判冲突（保守优先没被削弱）。"""
    world.add_agent("fb", "exec", caps=["freebsd"], repos={"hive": world.code_clone("fb")})
    world.add_agent("fb2", "exec", caps=["freebsd"], repos={"hive": world.code_clone("fb2")})
    world.add_task("T-0001", repo="hive", paths=["docs/"])
    world.add_task("T-0002", repo="hive", paths=["docs/a.md"])
    _set_locks(world, "T-0002", ["docs/a.md"])   # 只有一方有 locks

    out = world.hive_json("dispatch")
    assert [p["task"] for p in out["plan"]] == ["T-0001"], out
    assert {s["task"] for s in out["skipped"]} == {"T-0002"}, out
