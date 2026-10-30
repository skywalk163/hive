#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify 命令的跨平台垫片（Windows 上用）。

为什么需要
----------
任务卡里的 verify 常写成 POSIX 风格：`true`、`test -f x`、`! grep -q ...`。
在 FreeBSD/Linux 上这些是 shell 内建或 /usr/bin 里的命令；在 Windows 上
`cmd.exe` 没有 `true`，于是同一张卡在验收机上**必然**红灯——这不是业务失败，
是平台口径不一致。hive 在这里补一个最小映射，只覆盖高频、语义明确的写法，
未知写法原样交给 shell（绝不"猜一个成功"）。

用法（hive.py 内部的 run_verify 会调）::

    should_wrap(cmd)     -> bool   是否需要垫片
    rewrite(cmd)         -> str    `cmd` 或 `python hive_compat.py run "<cmd>"`
    run(cmd, cwd)         -> 退出码

单独调试::

    python hive_compat.py run "test -f docs/ok.md"
"""

from __future__ import annotations

import os
import re
import shlex
import sys

MARK = "hive_compat.py run"

# 只处理「整条命令就是这一种 POSIX 惯用法」的卡，避免误伤真实脚本。
_PLAIN = re.compile(r"^\s*(true|false|:)\s*$")
_NEG_PLAIN = re.compile(r"^\s*!\s*(true|false|:)\s*$")
_TEST_SUB = re.compile(r"^\s*(?:!\s*)?test\s+-(?P<flag>[efdrs])\s+(?P<arg>.+?)\s*$")
_NEG_TEST_SUB = re.compile(r"^\s*!\s*test\s+-(?P<flag>[efdrs])\s+(?P<arg>.+?)\s*$")
_COMPARE = re.compile(r'^\s*(?:test\s+)?"?(?P<a>[^"\s]+)"?\s*(?P<op>-eq|-ne|-lt|-gt|-le|-ge)\s*"?(?P<b>[^"\s]+)"?\s*$')


def should_wrap(cmd: str) -> bool:
    """这条命令是否需要垫片（只在 Windows 上返回 True）。"""
    if os.name != "nt":
        return False
    text = str(cmd or "")
    # 已经包过了（同一张卡重复执行时别套娃）。脚本路径是带引号的绝对路径，
    # 不能用 MARK 整串去匹配，只认文件名。
    if "hive_compat.py" in text:
        return False
    return bool(_PLAIN.match(text) or _NEG_PLAIN.match(text)
                or _TEST_SUB.match(text) or _NEG_TEST_SUB.match(text)
                or _COMPARE.match(text))


def _split_arg(arg: str) -> str:
    try:
        parts = shlex.split(arg, posix=False)
    except ValueError:
        parts = [arg]
    return parts[0] if parts else arg


def rewrite(cmd: str) -> str:
    """把单条 POSIX 惯用法改写成 `python hive_compat.py run "<cmd>"`。"""
    text = str(cmd or "")
    quote = "'" if "'" not in text else '"'
    return f'{sys.executable} "{_self_path()}" run {quote}{text}{quote}'


def _self_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "hive_compat.py")


def _unquote(text: str) -> str:
    """去掉一层包裹引号。

    Windows 的 cmd.exe 不会剥单引号：`run 'true'` 传到 Python 是 `'true'`
    （带引号），直接匹配正则永远失败。只剥一层，且要求首尾同一种引号。
    """
    t = (text or "").strip()
    for q in ("'", '"'):
        if len(t) >= 2 and t.startswith(q) and t.endswith(q):
            inner = t[1:-1]
            if q not in inner:
                return inner.strip()
    return t


def evaluate(cmd: str) -> int:
    """真正求值：返回 0/1。只支持 :func:`should_wrap` 认得的语法。"""
    text = _unquote(str(cmd or ""))
    if _PLAIN.match(text):
        return 0 if text.strip() in ("true", ":") else 1
    if _NEG_PLAIN.match(text):
        inner = text.strip().lstrip("!").strip()
        return 0 if inner == "false" else 1

    m = _NEG_TEST_SUB.match(text) or _TEST_SUB.match(text)
    if m:
        negated = bool(_NEG_TEST_SUB.match(text))
        flag = m.group("flag")
        arg = _split_arg(m.group("arg"))
        try:
            if flag == "f":
                ok = os.path.isfile(arg)
            elif flag == "e":
                ok = os.path.exists(arg)
            elif flag == "d":
                ok = os.path.isdir(arg)
            elif flag == "s":
                ok = os.path.getsize(arg) > 0 if os.path.exists(arg) else False
            elif flag == "r":
                ok = os.access(arg, os.R_OK)
            else:  # pragma: no cover - 正则里只有这几个
                return 2
        except OSError:
            ok = False
        if negated:
            ok = not ok
        return 0 if ok else 1

    m = _COMPARE.match(text)
    if m:
        a, b = _split_arg(m.group("a")), _split_arg(m.group("b"))
        try:
            ai, bi = int(a), int(b)
        except ValueError:
            ai, bi = a, b
        op = m.group("op")
        table = {"-eq": ai == bi, "-ne": ai != bi, "-lt": ai < bi,
                 "-gt": ai > bi, "-le": ai <= bi, "-ge": ai >= bi}
        return 0 if table.get(op) else 1

    # 没有映射到的写法：判为「未知」，让调用方回退到原命令
    return 2


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] != "run":
        print(f"用法: {MARK} \"<POSIX 命令>\"", file=sys.stderr)
        return 2
    cmd = " ".join(args[1:])
    rc = evaluate(cmd)
    if rc == 2:
        print(f"hive_compat: 不支持的写法 {cmd!r}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
