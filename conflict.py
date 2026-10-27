"""hive 的文件级锁：任务卡 `locks` 字段的冲突判定。

## 为什么要有这个模块

`paths` 只做**目录前缀**匹配（`Hive._path_overlap`）——声明 `src/` 就覆盖其下一切。
于是两张卡明明只分别改 `src/a.py` 和 `src/b.py`，只要有一张不得不写 `src/`，
dispatch 就判它们打架，只能排队跑。缺的是「我只碰这几个文件」这种说法。

`locks` 补上这层语义：卡面可以列一串**锁声明**，双方都写了 `locks` 时按精确集合判冲突，
没写的那一方仍走旧 `paths` 规则（完全向后兼容，旧卡一个字段都不用改）。

## 三种写法

| 写法 | 含义 | 与谁冲突 |
|---|---|---|
| `src/`（**带尾斜杠**） | 目录锁：覆盖该目录下所有文件 | `src/a.py`、`src/sub/b.py`、`src/*.py` |
| `src/a.py`（无尾斜杠、无通配） | 文件锁：只碰这一个文件 | 只有 `src/a.py` / `src/` |
| `src/*.py`、`**/test_*.py` | 简单通配（`fnmatch`） | 命中即冲突 |

## 判定规则

- `_lock_matches(lock, other)`：**双向**看 —— 任一方覆盖到另一方就算匹配，所以
  `_lock_matches(x, y) == _lock_matches(y, x)`（对称，有测试守着）。
- `_locks_conflict(a_locks, b_locks)`：任一对匹配即冲突；**任一方为空就保守判冲突**
  （没声明锁 = 可能改任何地方），与旧 `paths` 的保守默认一致。

## 两个刻意的取舍

1. **`src`（无尾斜杠）在 `locks` 里是「文件」，不是目录**。想覆盖整个目录必须写 `src/`。
   跟 shell 直觉一致，也消掉「`src` 到底是文件还是目录」的歧义；旧 `paths` 的
   `_path_overlap` 仍按目录处理，两条路语义不同是有意的（`locks` 更严格）。
2. **通配走 `fnmatch`，`*` 会跨 `/`**（`src/*.py` 能匹配 `src/sub/a.py`，
   `**/test_*.py` 能匹配 `a/b/test_x.py`）。够用即可：不实现 glob 的完整语义，
   也不引第三方依赖（标准库 `fnmatch` 足够）。

## 与其它机制的关系

- `Hive._paths_conflict` 是唯一入口：双方都有非空 `locks` → 走本模块；否则回退 `paths`。
- `Hive._path_overlap` 保留不动，旧卡继续用它。
- `locks` 目前**不参与** `out_of_paths`（越界检测）与 `audit`：那里仍以 `paths` 为准，
  写锁不代表允许改 `paths` 之外的文件。
"""

from __future__ import annotations

import fnmatch

# fnmatch 的通配元字符；出现任意一个就按「通配声明」处理
_GLOB_CHARS = ("*", "?", "[")


def _norm(lock) -> str:
    """规整一条锁声明：去空白、反斜杠转正斜杠、去掉开头的 `./`。

    只做「写法归一」，不改语义 —— 尾斜杠（目录锁）原样保留。
    """
    s = str(lock if lock is not None else "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def _is_glob(s: str) -> bool:
    return any(c in s for c in _GLOB_CHARS)


def _static_prefix(s: str) -> str:
    """通配声明里「第一个通配符之前」的静态前缀。

    `src/*.py` -> `src/`；`**/test_*.py` -> `""`（以通配开头，能伸到任何目录）；
    非通配声明原样返回。
    """
    idx = [s.find(c) for c in _GLOB_CHARS if c in s]
    if not idx:
        return s
    return s[:min(idx)]


def normalize_locks(value) -> list[str]:
    """把卡面 `locks` 字段规整成非空字符串列表。

    frontmatter 里 `locks:` 可能是空（解析成 `[]`）、单条标量（解析成字符串）、
    或列表，这里统一：`None` / `""` / `[]` -> `[]`，其余去空去重保序。
    """
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    out: list[str] = []
    for it in items:
        s = _norm(it)
        if s and s not in out:
            out.append(s)
    return out


def _covers(a: str, b: str) -> bool:
    """声明 `a` 覆盖到的文件集合，与声明 `b` 覆盖到的集合有没有交集。

    这是 `_lock_matches` 的内核；调用前两边都已 `_norm` 过。
    """
    if not a or not b:
        return True                 # 空声明 = 任何地方
    if a == b:
        return True

    a_dir, b_dir = a.endswith("/"), b.endswith("/")
    a_glob, b_glob = _is_glob(a), _is_glob(b)

    if a_dir:
        # 目录锁：看 b 的静态前缀落在哪儿
        pb = _static_prefix(b)
        if b_glob and not pb:
            return True             # 形如 `**/x`、`*/x`：能伸到任何目录下
        if pb == a or pb.startswith(a):
            return True             # b 在 a 目录下（`src/` vs `src/a.py` / `src/*.py`）
        if a.startswith(pb):
            return True             # a 在 b 的静态前缀下（`src/sub/` vs `src/*.py`）
        return False

    if b_dir:
        return _covers(b, a)        # 对称：换一边再看（此时首参是目录，不会再递归）

    if a_glob and fnmatch.fnmatchcase(b, a):
        return True
    if b_glob and fnmatch.fnmatchcase(a, b):
        return True
    return False


def _lock_matches(lock, other) -> bool:
    """两条锁声明是否相互覆盖（目录锁 / 文件锁 / 通配三种写法混着比）。

    对称：`_lock_matches(x, y)` 与 `_lock_matches(y, x)` 结果相同。
    """
    return _covers(_norm(lock), _norm(other))


def _locks_conflict(a_locks, b_locks) -> bool:
    """两组锁声明是否冲突：任一对匹配就算冲突。

    任一方为空（没声明 `locks`）时保守返回 True —— 跟旧 `paths`
    「未声明 = 可能改任何地方」的默认一致，不会因为少写字段就放开并行。
    """
    a = normalize_locks(a_locks)
    b = normalize_locks(b_locks)
    if not a or not b:
        return True
    return any(_covers(x, y) for x in a for y in b)
