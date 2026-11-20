"""判据在 Windows 上的引号问题（T-0225 / T-0228 事故回归）。

背景 1（T-0225）：`git grep -qE 'v4|v7' -- CHANGELOG.md` 在 cmd.exe 下不是「正则里的竖线」，
而是「把一个命令劈成两段」——单引号在 cmd 里不是引号，`|` 被当管道符。
实测报 `'v7'' 不是内部或外部命令`，exit 255，于是产物合格却判 failed。

背景 2（T-0228）：`git grep -q 'version = "0.4.1"' -- pyproject.toml` 里没有 `|`，
老垫片直接放过，但空格同样会把参数切成 `'version` / `=` / `0.4.1'`，
git 报 `fatal: unable to resolve revision: =`，exit 128。

修法：`verify_command` 在 Windows 上先把「cmd 保护不了的 POSIX 单引号片段」换成双引号
（含空格或 `|&<>^`），内层双引号按 `_requote_span` 的口径转义成 `\\"`。
本文件锁住四件事：
  1. 该换的换（空格、`|`、`&` 都换）；
  2. 不该换的一个字节都不动（无空格无元字符、已用双引号、双引号区域内的 `'`）；
  3. 一段命令里出现多组单引号时，不能把闭合引号当开引号跨段吃（第一版正则就错在这）；
  4. 内层含 `"` 且同时含 cmd 元字符时保持原样（转义后元字符会暴露成管道，做不到安全）。
"""

from __future__ import annotations

import os
import subprocess

import pytest

import hive_compat
from hive import _win_single_quote_fix, verify_command


@pytest.mark.parametrize("src, want", [
    # 该换的：元字符
    ("git grep -qE 'v4|v7' -- CHANGELOG.md", 'git grep -qE "v4|v7" -- CHANGELOG.md'),
    ("git grep -qE 'a&b' -- f", 'git grep -qE "a&b" -- f'),
    ("git grep -qE 'a|b' -- x && test -f y", 'git grep -qE "a|b" -- x && test -f y'),
    # 该换的：空格（T-0228）
    ("git grep -q 'version = 0.4.1' -- pyproject.toml",
     'git grep -q "version = 0.4.1" -- pyproject.toml'),
    ("test -f 'a b'", 'test -f "a b"'),
    # 该换的：内层含双引号但没有 cmd 元字符 —— 转义后裹双引号
    ('git grep -q \'version = "0.4.1"\' -- pyproject.toml',
     'git grep -q "version = \\"0.4.1\\"" -- pyproject.toml'),
    # 不该换的
    ("git grep -q \"a|b\" -- f", 'git grep -q "a|b" -- f'),
    ("git grep -q plain -- f", "git grep -q plain -- f"),
    ("git grep -q 'plain' -- f", "git grep -q 'plain' -- f"),
    # 片段内既有双引号又有 cmd 元字符：换也换不干净，宁可原样交给 shell
    ("git grep -qE 'a\"|b' -- f", "git grep -qE 'a\"|b' -- f"),
    # 落单的引号：原样收尾，不吞字符
    ("echo 'unclosed |x", "echo 'unclosed |x"),
])
def test_single_quote_fix(src, want):
    assert _win_single_quote_fix(src) == want


def test_quote_inside_double_quotes_is_not_a_quote():
    """`"don't"` 里的 `'` 在 cmd 眼里是普通字符，不能拿它去配对后面的引号。"""
    src = 'git grep -q "don\'t" -- f && git grep -q \'a b\' -- g'
    want = 'git grep -q "don\'t" -- f && git grep -q "a b" -- g'
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
    for src in ("git grep -qE 'v4|v7' -- f",
                "git grep -q 'version = \"0.4.1\"' -- pyproject.toml",
                "git grep -q 'a b' -- f"):
        once = _win_single_quote_fix(src)
        assert _win_single_quote_fix(once) == once


def _crt_split(line: str) -> list[str]:
    """按 Windows CRT（msvcrt / mingw 共用口径）切 argv：`\\"` 是字面双引号，`"` 翻转引号态。

    cmd.exe 不会替程序剥引号，它把整行交给 CreateProcess，真正切词的是程序自己的 CRT。
    """
    args: list[str] = []
    cur, has, in_q, i = "", False, False, 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == '"':
            cur += '"'
            has = True
            i += 2
            continue
        if ch == '"':
            in_q = not in_q
            has = True
            i += 1
            continue
        if ch.isspace() and not in_q:
            if cur or has:
                args.append(cur)
            cur, has = "", False
            i += 1
            continue
        cur += ch
        has = True
        i += 1
    if cur or has:
        args.append(cur)
    return args


@pytest.mark.parametrize("src", [
    "git grep -q 'version = \"0.4.1\"' -- pyproject.toml",   # T-0228 原判据
    "test -f 'a b'",
    "git grep -qE 'v4|v7' -- CHANGELOG.md",
    "git grep -q 'version = 0.4.1' -- pyproject.toml",
])
def test_requoted_line_splits_into_same_argv(src):
    """换完的串按 Windows CRT 口径切词，必须与 POSIX `sh` 切出来的 argv 逐字相同。

    这就是 T-0228 的判据形态：老版垫片产出的 `git grep -q 'version = "0.4.1"' -- pyproject.toml`
    在 CRT 下切成 `['git','grep','-q',"'version",'=','0.4.1\'','--','pyproject.toml']`，
    git 把 `'version` 当 pattern、把 `=` 当版本号 → `unable to resolve revision: =`。
    """
    import shlex

    assert _crt_split(_win_single_quote_fix(src)) == shlex.split(src)


@pytest.mark.skipif(os.name == "nt", reason="这条断言 POSIX 口径不被改动")
def test_non_windows_verify_command_untouched():
    """非 Windows 上 `verify_command` 一个字节都不该动（FreeBSD/Linux 口径）。"""
    cmd = "git grep -qE 'v4|v7' -- CHANGELOG.md"
    assert verify_command(cmd) == cmd


# --------------------------------------------------------------------------- #
# T-0409：原文同时含单双引号时，包装层不能靠嵌套双引号传递
#
# 事故：T-0406 的判据
#     python3 -c "import json;json.load(open('docs/功能对标/对标清单.json'))"
# 经 `hive.verify_command()` 改写后命令行是
#     "...python.exe" "...hive_compat.py" run "python3 -c "import json;…""
# cmd.exe 只认「开/关」两态、不认 `\"`，在内层第一个 `"` 处就截断，
# `run` 收到 `python3 -c import` → `SyntaxError: import` rc=1，
# 与「产物真坏了」长得一模一样（假红）。
#
# 修法：原文含双引号时改走不依赖引号的载荷通道（`--b64` / 超长时 `--file`），
# 验收口径是「run 收到的文本与入参逐字节相等」，不是「看起来像」。
# --------------------------------------------------------------------------- #

BOTH_QUOTES_CMD = 'python3 -c "import json;json.load(open(\'tests/x.json\'))"'


def test_rewrite_carries_quote_free_payload():
    """含双引号的原文不再裹外层双引号：命令行只剩两个带引号的绝对路径。"""
    out = hive_compat.rewrite(BOTH_QUOTES_CMD)
    assert "hive_compat.py" in out and " run " in out
    # 反证：改回「原文裹一层双引号」时这里是 6 个（原文里那 2 个也进来了）→ 立红
    assert out.count('"') == 4, out
    # 还原性：载荷逐字节还原原文，不是「看起来像」
    assert hive_compat.payload_text(out) == BOTH_QUOTES_CMD
    # 幂等：包过的不再套娃
    assert hive_compat.should_wrap(out) is False


@pytest.mark.parametrize("text", [
    BOTH_QUOTES_CMD,
    'python3 -c "print(\'a b\')"',
    'git grep -q "x\'y" -- f',
    'python3 -c "print(1)"\t--flag',
    "python3 -c \"print('多字节 文本')\"",     # 非 ASCII：base64 走 UTF-8，绕开代码页
])
def test_payload_roundtrips_byte_exact(text):
    """任何引号组合/空白/非 ASCII 都逐字节还原（载荷通道不吃字节）。"""
    out = hive_compat.rewrite(text)
    assert hive_compat.payload_text(out) == text


def test_plain_command_keeps_legacy_single_quote_wrapper():
    """没有双引号的命令仍走老包装（T-0225/T-0228 的既有行为一字不变）。"""
    out = hive_compat.rewrite("test -f docs/ok.md")
    assert out.endswith("run 'test -f docs/ok.md'"), out
    # 含单引号但不含双引号：仍用 `"` 包一层（与历史行为一致）
    assert hive_compat.rewrite("bash tests/x.sh --flag 'a b'").endswith(
        'run "bash tests/x.sh --flag \'a b\'"')


def test_long_command_falls_back_to_file_channel(tmp_path, monkeypatch):
    """base64 会顶到 cmd 单行上限时落盘：仍是逐字节还原，不是截断。"""
    monkeypatch.setenv(hive_compat.PAYLOAD_DIR_ENV, str(tmp_path))
    text = 'python3 -c "print(\'' + "x" * (hive_compat.MAX_B64_PAYLOAD + 100) + "')\""
    out = hive_compat.rewrite(text)
    assert " run --file " in out, out[:120]
    assert hive_compat.payload_text(out) == text
    # 同一条原文重复改写落到同一个文件（按内容哈希取名，幂等）
    assert hive_compat.rewrite(text) == out


def test_missing_payload_file_fails_explicitly(tmp_path, monkeypatch, capsys):
    """载荷文件读不到：明确失败（rc=2），绝不悄悄当成成功。"""
    monkeypatch.setenv(hive_compat.PAYLOAD_DIR_ENV, str(tmp_path))
    text = 'python3 -c "print(\'' + "y" * (hive_compat.MAX_B64_PAYLOAD + 100) + "')\""
    out = hive_compat.rewrite(text)
    assert hive_compat.payload_text(out) == text
    path = hive_compat._payload_path(text)
    os.unlink(path)
    assert hive_compat.main(["run", "--file", path]) == 2
    assert "--file" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["run", "--b64", "!!!not-base64!!!"],
    ["run", "--b64"],
    ["run", "--b64", "AAAA", "extra"],
])
def test_bad_payload_args_fail_explicitly(argv, capsys):
    assert hive_compat.main(argv) == 2, argv
    assert capsys.readouterr().err


@pytest.mark.skipif(os.name != "nt", reason="Windows 专用回归：真过 cmd.exe")
def test_windows_verify_with_both_quote_kinds_really_runs(tmp_path):
    """端到端钉住 T-0406 的假红：改写后经 cmd.exe 真跑，rc=0 且 stdout 是预期片段。

    反证：把 `rewrite()` 改回「原文裹一层双引号」，这条立刻回到
    `SyntaxError: import` / rc=1（本卡提交前实测）。
    """
    data = tmp_path / "quote_probe.json"
    data.write_text('{"tag": "t0409"}', encoding="utf-8")
    cmd = ('python3 -c "import json;d=json.load(open(\'%s\'));print(d[\'tag\'])"'
           % data.as_posix())
    wrapped = verify_command(cmd)
    p = subprocess.run(wrapped, shell=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert p.returncode == 0, (wrapped, p.stdout, p.stderr)
    assert "t0409" in p.stdout

