"""判据在 Windows 上的引号问题（T-0225 事故回归）。

背景：`git grep -qE 'v4|v7' -- CHANGELOG.md` 在 cmd.exe 下不是「正则里的竖线」，
而是「把一个命令劈成两段」——单引号在 cmd 里不是引号，`|` 被当管道符。
实测报 `'v7'' 不是内部或外部命令`，exit 255，于是产物合格却判 failed。

修法：`verify_command` 在 Windows 上先把「含 | 或 & 的单引号片段」换成双引号。
本文件锁住三件事：
  1. 该换的换（含 & 也换，因为 cmd 的 `&` 是命令连接符）；
  2. 不该换的一个字节都不动（无管道、已用双引号、含双引号）；
  3. 一段命令里出现多组单引号时，不能把闭合引号当开引号跨段吃（第一版正则就错在这）。
"""

from __future__ import annotations

import os

import pytest

from hive import _win_single_quote_fix, verify_command


@pytest.mark.parametrize("src, want", [
    # 该换的
    ("git grep -qE 'v4|v7' -- CHANGELOG.md", 'git grep -qE "v4|v7" -- CHANGELOG.md'),
    ("git grep -qE 'a&b' -- f", 'git grep -qE "a&b" -- f'),
    ("git grep -qE 'a|b' -- x && test -f y", 'git grep -qE "a|b" -- x && test -f y'),
    # 不该换的
    ("git grep -q \"a|b\" -- f", 'git grep -q "a|b" -- f'),
    ("test -f 'a b'", "test -f 'a b'"),
    ("git grep -q plain -- f", "git grep -q plain -- f"),
    # 片段内已有双引号：换引号会破坏语义，宁可原样交给 shell
    ("git grep -qE 'a\"|b' -- f", "git grep -qE 'a\"|b' -- f"),
    # 落单的引号：原样收尾，不吞字符
    ("echo 'unclosed |x", "echo 'unclosed |x"),
])
def test_single_quote_fix(src, want):
    assert _win_single_quote_fix(src) == want


def test_two_quote_groups_do_not_span():
    """两组单引号：只有含管道的那组被换，`&&` 保持原样。

    第一版用正则 `'([^']*[|&][^']*)'`，会把闭合引号当开引号、从第一个引号吃到最后一个，
    产出 `echo 'x" && echo "y|z'` —— 整条命令被改坏。这条断言就是钉住它。
    """
    src = "echo 'x' && echo 'y|z'"
    assert _win_single_quote_fix(src) == 'echo \'x\' && echo "y|z"'


def test_idempotent():
    """已是双引号的命令，再跑一遍不变（重复执行不漂移）。"""
    once = _win_single_quote_fix("git grep -qE 'v4|v7' -- f")
    assert _win_single_quote_fix(once) == once


@pytest.mark.skipif(os.name == "nt", reason="这条断言 POSIX 口径不被改动")
def test_non_windows_verify_command_untouched():
    """非 Windows 上 `verify_command` 一个字节都不该动（FreeBSD/Linux 口径）。"""
    cmd = "git grep -qE 'v4|v7' -- CHANGELOG.md"
    assert verify_command(cmd) == cmd
