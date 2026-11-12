"""verify 命令的 POSIX → Windows 垫片（hive_compat.py，T-0208 / T-0292）。

背景：任务卡里的 verify 常写 `true` / `test -f x`。FreeBSD 上是内建命令，
Windows 的 cmd.exe 没有，于是同一张卡在验收机上**必然**红灯。
垫片只映射高频、语义明确的写法，未知写法原样交给 shell（绝不猜成功）。

T-0292 追加 `bash <script>` / `python3 <args>`：Windows 上分别探测
Git for Windows 的 bash.exe 与 `sys.executable`，真起子进程、退出码原样透传。
测试不要求 CI 装 Git Bash：识别/失败路径用 mock，执行路径用 `sys.executable`。

T-0343 追加三条口径：`python` 与 `python3` 同义；`VAR=value ...` 赋值前缀解析成
子进程 env（POSIX 上一个字节都不改写）；`make` 能力过滤（verify 含 make 的卡只派给
声明了 make 的 agent）。未知写法仍必须明确失败，绝不猜成功。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hive  # noqa: E402
import hive_compat  # noqa: E402


# --------------------------------------------------------------------------- #
# 纯求值层（与平台无关：直接调 evaluate）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cmd,want", [
    ("true", 0),
    ("false", 1),
    (":", 0),
    ("! true", 1),
    ("! false", 0),
    ("test -f hive.py", 0),
    ("test -f no_such_file_xyz", 1),
    ("test -d dashboard", 0),
    ("! test -f no_such_file_xyz", 0),
    ("test 2 -eq 2", 0),
    ("test 2 -ne 2", 1),
    ("test 3 -gt 1", 0),
    # T-0237：`test -x` 曾经不在映射表里，Windows 上整条命令丢给 cmd.exe 必红灯。
    ("test -x no_such_file_xyz", 1),
    ("! test -x no_such_file_xyz", 0),
])
def test_evaluate_known_forms(cmd, want, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert hive_compat.evaluate(cmd) == want, cmd


def test_evaluate_unquotes_single_quotes(monkeypatch):
    """cmd.exe 不剥单引号：`run 'true'` 到 Python 是 "'true'"。"""
    monkeypatch.chdir(ROOT)
    assert hive_compat.evaluate("'true'") == 0
    assert hive_compat.evaluate("'test -f hive.py'") == 0


def test_unknown_form_is_not_guessed():
    """不认识的写法返回 2（未处理），绝不冒充成功。"""
    assert hive_compat.evaluate("pytest -q tests") == 2
    assert hive_compat.evaluate("") == 2


def test_evaluate_x_flag_semantics(tmp_path, monkeypatch):
    """`test -x`：POSIX 看执行位，Windows 上没有执行位 → 退化为「存在」。

    这条把平台差异钉住，免得以后有人以为 Windows 上真能测出 exec 位。
    """
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "run.sh"
    f.write_text("#!/bin/sh\n", encoding="utf-8")
    f.chmod(0o644)          # 明确不可执行，别被 umask 带偏

    if os.name == "nt":
        # Windows：文件存在 → os.access(X_OK) 为真（CPython 的既定行为）
        assert hive_compat.evaluate("test -x run.sh") == 0
    else:
        assert hive_compat.evaluate("test -x run.sh") == 1
        f.chmod(0o755)
        assert hive_compat.evaluate("test -x run.sh") == 0
        f.chmod(0o644)      # 还原，别影响同目录里其它断言


def test_x_flag_is_wrapped_not_dropped_to_cmd(monkeypatch):
    """回归 T-0237：`test -x smoke.sh` 必须走垫片，不能原样丢给 cmd.exe。"""
    monkeypatch.setattr(hive_compat.os, "name", "nt")
    monkeypatch.delenv("HIVE_COMPAT", raising=False)
    assert hive_compat.should_wrap("test -x smoke.sh") is True
    assert hive_compat.should_wrap("! test -x smoke.sh") is True
    wrapped = hive.verify_command("test -x smoke.sh")
    assert "hive_compat.py" in wrapped and " run " in wrapped


# --------------------------------------------------------------------------- #
# 平台开关
# --------------------------------------------------------------------------- #

def test_should_wrap_only_on_windows(monkeypatch):
    monkeypatch.setattr(hive_compat.os, "name", "posix")
    assert hive_compat.should_wrap("true") is False
    monkeypatch.setattr(hive_compat.os, "name", "nt")
    assert hive_compat.should_wrap("true") is True
    assert hive_compat.should_wrap("test -f x") is True
    assert hive_compat.should_wrap("test -x x") is True
    assert hive_compat.should_wrap("! test -x x") is True
    assert hive_compat.should_wrap("pytest -q") is False
    # 已经包过的别再套娃
    assert hive_compat.should_wrap(
        f'{sys.executable} "{hive_compat._self_path()}" run true') is False


def test_rewrite_is_idempotent_and_quoted():
    text = "test -f docs/ok.md"
    out = hive_compat.rewrite(text)
    # 脚本路径是带引号的绝对路径，MARK 整串（含空格）对不上——只认文件名。
    assert "hive_compat.py" in out
    assert " run " in out
    assert text in out
    # 幂等：already-wrapped 的输入不再包一层
    assert hive_compat.should_wrap(out) is False


def test_hive_verify_command_respects_kill_switch(monkeypatch):
    monkeypatch.setattr(hive_compat.os, "name", "nt")
    assert hive.verify_command("true") != "true"
    monkeypatch.setenv("HIVE_COMPAT", "0")
    assert hive.verify_command("true") == "true"


def test_hive_verify_command_untouched_on_posix(monkeypatch):
    """T-0343 红线：POSIX 上一个字节都不改写（含 python / 赋值前缀 / make）。"""
    monkeypatch.setattr(hive_compat.os, "name", "posix")
    monkeypatch.delenv("HIVE_COMPAT", raising=False)
    for cmd in ("true", "test -f x", "pytest -q tests", "python -V",
                "python3 -V",
                "PYTHONPATH=src python scripts/acceptance.py --all",
                "A=1 B=2 python -c 'print(1)'", "make demo"):
        assert hive.verify_command(cmd) == cmd
        assert hive_compat.should_wrap(cmd) is False


# --------------------------------------------------------------------------- #
# T-0292：`bash <script>` / `python3 <args>` 前缀
#
# 卡面 verify 写 `bash tests/run_pytest.sh ...` / `python3 -V` 时，Windows 的
# cmd.exe 两样都没有（Git for Windows 只把 git.exe 放进 PATH），产物合格也假红。
# 这里锁住：认出这两类前缀 → 定位解释器 → 直接 exec → 退出码透传。
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cmd,want", [
    ("bash tests/run_pytest.sh tests/test_run_stdin.py", True),
    ("bash tests/x.sh", True),
    ("bash -c 'exit 0'", True),
    ("  bash   tests/x.sh  ", True),
    ("bash", False),                    # 没后续参数 = 找交互 shell，不是 verify
    ("bash   ", False),
    ("bashful tests/x.sh", False),      # 前缀必须整词
    ("echo bash tests/x.sh", False),
    ("python3 -V", True),
    ("python3 tests/x.py --flag 1", True),
    ("python3", False),
    ("python -V", True),                # T-0343：`python` 与 `python3` 同义
    ("python tests/x.py --flag 1", True),
    ("python", False),
    ("python3.12 -V", False),           # 带版本号不认，别静默跑错解释器
    ("pythonic -V", False),             # 前缀必须整词
    ("echo python -V", False),
    # T-0343：赋值前缀（一条或多条）+ 解释器前缀
    ("PYTHONPATH=src python scripts/acceptance.py --all", True),
    ("A=1 B=2 python3 -V", True),
    ('A=1 B="two words" python -c "print(1)"', True),
    ("A=1 make demo", False),           # make 不是解释器 → 未知写法，不猜成功
    ("A=1", False),
    ("1BAD=x python -V", False),        # 不是合法的变量名
    ("echo python3 -V", False),
])
def test_should_wrap_recognises_interpreter_prefix(cmd, want, monkeypatch):
    monkeypatch.setattr(hive_compat.os, "name", "nt")
    assert hive_compat.should_wrap(cmd) is want, cmd


def test_interpreter_prefix_is_not_wrapped_on_posix(monkeypatch):
    """平台边界不变：POSIX 上 should_wrap 仍恒 False（交还给系统 shell）。"""
    monkeypatch.setattr(hive_compat.os, "name", "posix")
    assert hive_compat.should_wrap("bash tests/x.sh") is False
    assert hive_compat.should_wrap("python3 -V") is False
    assert hive_compat.should_wrap("python -V") is False
    assert hive_compat.should_wrap(
        "PYTHONPATH=src python scripts/acceptance.py --all") is False


def test_interpreter_command_parses_prefix():
    assert hive_compat.interpreter_command(
        "bash tests/run_pytest.sh tests/test_run_stdin.py") == (
            "bash", "tests/run_pytest.sh tests/test_run_stdin.py")
    assert hive_compat.interpreter_command("python3 -V") == ("python3", "-V")
    assert hive_compat.interpreter_command("python -V") == ("python", "-V")
    assert hive_compat.interpreter_command(
        "PYTHONPATH=src python scripts/acceptance.py --all") == (
            "python", "scripts/acceptance.py --all")
    assert hive_compat.interpreter_command("bash") is None
    assert hive_compat.interpreter_command("pytest -q") is None


def test_bare_interpreter_is_not_executed():
    """`bash` / `python3` / `python` 单独一条：不执行、判未知（绝不冒充成功）。"""
    assert hive_compat.evaluate("bash") == 2
    assert hive_compat.evaluate("python3") == 2
    assert hive_compat.evaluate("python") == 2


# -- 解释器定位 ------------------------------------------------------------- #

def test_resolve_python3_is_sys_executable():
    """`python3` 映射到跑 hive 的解释器，不去 PATH 里碰运气。"""
    assert hive_compat.resolve_python3() == sys.executable


def test_resolve_bash_honours_env_override(monkeypatch):
    monkeypatch.setenv("HIVE_BASH", sys.executable)
    assert hive_compat.resolve_bash() == sys.executable


def test_resolve_bash_falls_back_to_git_candidates(monkeypatch, tmp_path):
    """PATH 里没有 bash.exe（Git for Windows 的常态）→ 走常见安装位置。"""
    fake = tmp_path / "bash.exe"
    fake.write_text("", encoding="utf-8")
    fake.chmod(0o755)   # POSIX 上 X_OK 看执行位；Windows 上这一步是幂等的
    monkeypatch.delenv("HIVE_BASH", raising=False)
    monkeypatch.setattr(hive_compat.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(hive_compat, "GIT_BASH_CANDIDATES", (str(fake),))
    assert hive_compat.resolve_bash() == str(fake)


def test_resolve_bash_missing_returns_none(monkeypatch):
    monkeypatch.delenv("HIVE_BASH", raising=False)
    monkeypatch.setattr(hive_compat.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(hive_compat, "GIT_BASH_CANDIDATES", ())
    assert hive_compat.resolve_bash() is None


def test_resolve_bash_wrong_override_fails_loudly(monkeypatch, tmp_path):
    """显式指错就当场暴露，不偷偷退回别的 bash（否则排查方向会被带偏）。"""
    monkeypatch.setenv("HIVE_BASH", str(tmp_path / "no_such_bash.exe"))
    monkeypatch.setattr(hive_compat.shutil, "which", lambda *a, **k: sys.executable)
    assert hive_compat.resolve_bash() is None


# -- 真执行 ----------------------------------------------------------------- #

def test_evaluate_runs_python3_and_passes_exit_code(capsys):
    assert hive_compat.evaluate("python3 -V") == 0
    out = capsys.readouterr()
    assert "Python" in (out.out + out.err)


def test_evaluate_passes_through_nonzero_exit_code():
    """子进程非零 → 原样透传，不吞、不改 0。"""
    assert hive_compat.evaluate('python3 -c "import sys; sys.exit(7)"') == 7


def test_evaluate_missing_script_is_nonzero(capsys):
    """脚本不存在：rc≠0，且把解释器自己的报错留下来（可诊断）。"""
    rc = hive_compat.evaluate("python3 no_such_script_xyz.py")
    assert rc != 0
    assert "no_such_script_xyz.py" in capsys.readouterr().err


def test_evaluate_missing_bash_is_nonzero_and_explains(monkeypatch, capsys, tmp_path):
    """找不到 bash：非零 + 可照做的提示，**绝不返回 0**。"""
    monkeypatch.setenv("HIVE_BASH", str(tmp_path / "no_such_bash.exe"))
    rc = hive_compat.evaluate("bash tests/x.sh")
    assert rc == hive_compat.MISSING_INTERPRETER_RC
    assert rc != 0
    err = capsys.readouterr().err
    assert "HIVE_BASH" in err and "bash" in err


def test_bash_path_arg_converts_drive_paths(monkeypatch):
    """Git Bash 把反斜杠当转义：`C:\\a\\b` 原样传会变成 `C:ab`（实测踩到）。"""
    monkeypatch.setattr(hive_compat.os, "name", "nt")
    assert hive_compat.bash_path_arg(r"C:\tools\x.sh") == "C:/tools/x.sh"
    assert hive_compat.bash_path_arg("C:/tools/x.sh") == "C:/tools/x.sh"
    assert hive_compat.bash_path_arg("tests/run_pytest.sh") == "tests/run_pytest.sh"
    # `-c` 里的脚本内容不是路径，不能被改写
    assert hive_compat.bash_path_arg(r"echo C:\tools\x.sh") == r"echo C:\tools\x.sh"


def test_interpreter_argv_split_handles_spaces_and_quotes(monkeypatch):
    """按 POSIX shlex 拆 argv 直接 exec：带空格/引号的参数不被 cmd.exe 咬。"""
    seen = {}

    def fake_run(argv, cwd=None):
        seen["argv"] = argv
        seen["cwd"] = cwd
        return 0

    monkeypatch.setattr(hive_compat, "_run_argv", fake_run)
    monkeypatch.setenv("HIVE_BASH", sys.executable)
    assert hive_compat.evaluate('bash -c "echo a b" --flag "x y"') == 0
    assert seen["argv"] == [sys.executable, "-c", "echo a b", "--flag", "x y"]


def test_run_uses_no_window_kwargs(monkeypatch):
    """Windows 侧仍走 no_window_kwargs（T-0206 的黑窗不能带回来）。"""
    calls = []

    def fake_no_window():
        calls.append(True)
        return {}

    monkeypatch.setattr(hive_compat, "no_window_kwargs", fake_no_window)
    assert hive_compat.evaluate("python3 -V") == 0
    assert calls


@pytest.mark.skipif(hive_compat.resolve_bash() is None, reason="本机没有 bash")
def test_bash_runs_script_and_passes_rc(tmp_path):
    """退出码原样透传。路径按 POSIX 语义加引号：未加引号的反斜杠会被 shlex
    （以及 bash 自己）当转义吃掉，这是 POSIX 的既定语义，垫片不擅自改写。"""
    script = tmp_path / "x.sh"
    script.write_text("exit 3\n", encoding="utf-8")
    assert hive_compat.evaluate(f'bash "{script}"') == 3


@pytest.mark.skipif(hive_compat.resolve_bash() is None, reason="本机没有 bash")
def test_bash_handles_quoted_script_path(tmp_path, capsys):
    """路径带空格时仍能跑（引号由 shlex 剥，不被二次拼接）。"""
    script = tmp_path / "with space.sh"
    script.write_text("echo quoted-ok\n", encoding="utf-8")
    assert hive_compat.evaluate(f'bash "{script}"') == 0
    assert "quoted-ok" in capsys.readouterr().out


# -- 接线：hive.verify_command 必须把这两类前缀转进垫片 --------------------- #

def test_verify_command_wraps_interpreter_prefix(monkeypatch):
    monkeypatch.setattr(hive_compat.os, "name", "nt")
    monkeypatch.delenv("HIVE_COMPAT", raising=False)
    for cmd in ("bash tests/run_pytest.sh tests/test_run_stdin.py", "python3 -V",
                "python -V",
                "PYTHONPATH=src python scripts/acceptance.py --all"):
        out = hive.verify_command(cmd)
        assert "hive_compat.py" in out and " run " in out, cmd
        assert cmd in out, cmd                          # 原文原样带给垫片
        assert hive_compat.should_wrap(out) is False     # 幂等，不套娃


def test_legacy_idioms_unchanged(monkeypatch):
    """T-0208/T-0237 的既有惯用法行为一字不变（回归锁）。"""
    monkeypatch.setattr(hive_compat.os, "name", "nt")
    for cmd in ("true", "false", ":", "! true", "! false", "test -f hive.py",
                "! test -e no_such_file_xyz", "test 2 -eq 2", "test 5 -gt 3",
                "test -x hive.py"):
        assert hive_compat.should_wrap(cmd) is True, cmd

    monkeypatch.chdir(ROOT)
    assert hive_compat.evaluate("true") == 0
    assert hive_compat.evaluate("false") == 1
    assert hive_compat.evaluate("test -f hive.py") == 0
    assert hive_compat.evaluate("test 2 -ne 2") == 1
    assert hive_compat.evaluate("pytest -q tests") == 2   # 未知写法仍不猜


def test_rewrite_quoting_for_interpreter_prefix():
    text = "bash tests/x.sh --flag 'a b'"
    out = hive_compat.rewrite(text)
    assert "hive_compat.py" in out and " run " in out
    assert text in out
    assert hive_compat.should_wrap(out) is False


# --------------------------------------------------------------------------- #
# T-0343：`python` 前缀 / `VAR=value` 赋值前缀 / `make` 能力过滤
#
# 事故复盘：T-0334 的 verify 是 `PYTHONPATH=src python scripts/acceptance.py --all`，
# Windows 上 should_wrap 不认 → 原样交 cmd.exe → "'PYTHONPATH' 不是内部或外部命令"；
# 就算包进垫片，interpreter_command 也只认 bash/python3 → rc=2。
# T-0335 的 `make demo` 则是 Windows 根本没装 make —— 那类卡不该派给无 make 的机器。
# --------------------------------------------------------------------------- #

T0334_VERIFY = "PYTHONPATH=src python scripts/acceptance.py --all"


# -- 1. `python` 前缀 -------------------------------------------------------- #

def test_python_prefix_maps_to_sys_executable(capsys):
    """`python` 与 `python3` 同义：都映射到当前解释器，不去 PATH 里碰运气。"""
    assert hive_compat.resolve_python() == sys.executable
    assert hive_compat.resolve_python() == hive_compat.resolve_python3()
    assert hive_compat.evaluate("python -V") == 0
    out = capsys.readouterr()
    assert "Python" in (out.out + out.err)


def test_python_prefix_passes_through_nonzero_exit_code():
    assert hive_compat.evaluate('python -c "import sys; sys.exit(7)"') == 7


def test_python_and_python3_share_interpreter(monkeypatch):
    seen = []

    def fake_run(argv, cwd=None, env=None):
        seen.append((argv, cwd, env))
        return 0

    monkeypatch.setattr(hive_compat, "_run_argv", fake_run)
    for cmd in ("python3 -V", "python -V"):
        assert hive_compat.evaluate(cmd) == 0
    assert seen[0][0] == [sys.executable, "-V"], seen
    assert seen[1][0] == [sys.executable, "-V"], seen
    # 没有赋值前缀时保持 T-0292 的调用形态，别多传 env
    assert seen[0][2] is None and seen[1][2] is None


def test_versioned_python_is_unknown_not_guessed():
    """认不出的解释器写法返回 2，绝不「猜一个成功」。"""
    assert hive_compat.interpreter_command("python3.12 -V") is None
    assert hive_compat.evaluate("python3.12 -V") == 2


# -- 2. `VAR=value` 赋值前缀 ------------------------------------------------- #

def test_env_prefix_command_parses_single_and_multiple():
    assert hive_compat.env_prefix_command("python -V") is None
    assert hive_compat.env_prefix_command("") is None
    assert hive_compat.env_prefix_command(T0334_VERIFY) == (
        {"PYTHONPATH": "src"}, "python scripts/acceptance.py --all")
    assert hive_compat.env_prefix_command(
        'A=1 B="two words" C=\'x y\' python -V') == (
            {"A": "1", "B": "two words", "C": "x y"}, "python -V")
    assert hive_compat.env_prefix_command("VAR= python -V") == (
        {"VAR": ""}, "python -V")
    # 变量名不合法 / 引号不成对 → 不认，交回「未知写法」
    assert hive_compat.env_prefix_command("1BAD=x python -V") is None
    assert hive_compat.env_prefix_command('A="oops python -V') is None


def test_parse_command_returns_env_kind_and_args():
    assert hive_compat.parse_command(T0334_VERIFY) == (
        {"PYTHONPATH": "src"}, "python", "scripts/acceptance.py --all")
    assert hive_compat.parse_command("python3 -V") == ({}, "python3", "-V")
    assert hive_compat.parse_command("A=1 make demo") is None
    assert hive_compat.parse_command("pytest -q tests") is None


def test_evaluate_env_prefix_reaches_child_only(monkeypatch):
    """env 真的传给子进程，且不污染 hive 自己的 os.environ。"""
    monkeypatch.delenv("HIVE_T0343_VAR", raising=False)
    cmd = ("HIVE_T0343_VAR=src python -c "
           "\"import os,sys; sys.exit(0 if os.environ.get('HIVE_T0343_VAR')=='src' else 9)\"")
    assert hive_compat.evaluate(cmd) == 0
    assert "HIVE_T0343_VAR" not in os.environ


def test_evaluate_multiple_assignments_argv_and_env(monkeypatch):
    """多赋值 + 带空格的引号值：argv 按 POSIX shlex 拆，env 原样解析。"""
    seen = {}

    def fake_run(argv, cwd=None, env=None):
        seen.update(argv=argv, cwd=cwd, env=env)
        return 0

    monkeypatch.setattr(hive_compat, "_run_argv", fake_run)
    cmd = 'A=1 B="x y" C=\'z w\' python -c "print(1)" --flag "a b"'
    assert hive_compat.evaluate(cmd) == 0
    assert seen["env"] == {"A": "1", "B": "x y", "C": "z w"}, seen
    assert seen["argv"] == [sys.executable, "-c", "print(1)", "--flag", "a b"], seen


def test_evaluate_env_prefix_exit_code_passthrough():
    assert hive_compat.evaluate('A=1 python -c "import sys; sys.exit(7)"') == 7


@pytest.mark.parametrize("cmd", [
    "make demo",                       # Windows 没 make：这是未知写法，明确失败
    "A=1 make demo",
    "A=1",                             # 只有赋值、没有命令
    "1BAD=x python -V",                # 非法变量名
    "PYTHONPATH=src python",           # 有解释器没参数
    "pytest -q tests",
    "",
])
def test_unknown_forms_still_fail_explicitly(cmd):
    """红线：未知写法仍要明确失败，不许猜成功。"""
    assert hive_compat.evaluate(cmd) == 2, cmd
    assert hive_compat.parse_command(cmd) is None, cmd


def test_posix_never_rewrites_a_single_byte(monkeypatch):
    """红线：POSIX 上 verify_command 原样返回（一个字节都不动）。"""
    monkeypatch.setattr(hive_compat.os, "name", "posix")
    monkeypatch.delenv("HIVE_COMPAT", raising=False)
    for cmd in ("true", "test -f x", "python3 -V", "python -V", T0334_VERIFY,
                "A=1 B=2 python -c 'print(1)'", "make demo", "pytest -q tests"):
        assert hive_compat.should_wrap(cmd) is False, cmd
        assert hive.verify_command(cmd) == cmd, cmd


def test_windows_assignment_prefix_does_not_false_fail(monkeypatch, capsys):
    """回归 T-0334：monkeypatch os.name 成 nt，赋值前缀必须走垫片而不是 cmd.exe。"""
    cmd = "PYTHONPATH=src python -c 'print(1)'"
    with monkeypatch.context() as m:
        m.setattr(hive_compat.os, "name", "nt")
        m.delenv("HIVE_COMPAT", raising=False)
        assert hive_compat.should_wrap(cmd) is True
        assert hive_compat.should_wrap(T0334_VERIFY) is True
        wrapped = hive.verify_command(T0334_VERIFY)
        assert "hive_compat.py" in wrapped and " run " in wrapped
        assert T0334_VERIFY in wrapped
        assert hive_compat.should_wrap(wrapped) is False     # 不套娃
        # make 仍不认识：不包、不猜成功
        assert hive_compat.should_wrap("make demo") is False
    # 垫片自己求值（等价于 cmd.exe 起的子进程），平台名已还原
    assert hive_compat.evaluate(cmd) == 0
    assert "1" in capsys.readouterr().out


def test_cli_run_assignment_prefix_exits_zero():
    """卡面验收：python3 hive_compat.py run "PYTHONPATH=src python -c 'print(1)'"。"""
    cmd = "PYTHONPATH=src python -c 'print(1)'"
    p = subprocess.run(
        [sys.executable, str(ROOT / "hive_compat.py"), "run", cmd],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **hive_compat.no_window_kwargs())
    assert p.returncode == 0, p.stdout + p.stderr
    assert "1" in p.stdout


def test_cli_run_unknown_form_exits_nonzero():
    p = subprocess.run(
        [sys.executable, str(ROOT / "hive_compat.py"), "run", "make demo"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **hive_compat.no_window_kwargs())
    assert p.returncode != 0
    assert "不支持" in p.stderr


# -- 3. make 能力过滤 -------------------------------------------------------- #

@pytest.mark.parametrize("cmd,want", [
    ("make demo", True),
    ("make bench", True),
    ("gmake check", True),
    ("cd src && make check", True),
    ("PYTHONPATH=src make demo", True),
    ("bash -c 'make check'", True),
    ("echo make", False),                 # 只是个参数，不是命令
    ("grep -q make README.md", False),
    ("cmake .", False),
    ("test -f Makefile", False),
    ("python3 -m pytest -q tests/test_compat.py", False),
])
def test_command_requires_make(cmd, want):
    assert hive_compat.command_requires_make(cmd) is want, cmd


def test_required_capabilities_infers_make_from_verify():
    assert hive_compat.required_capabilities(
        {"needs": ["make"], "verify": ["make demo"]}) == {"make"}
    # 卡面漏标 needs、但 verify 里有 make —— 照样算需要 make
    assert hive_compat.required_capabilities(
        {"needs": [], "verify": ["make demo"]}) == {"make"}
    assert hive_compat.required_capabilities(
        {"needs": ["python"], "verify": ["python3 -m pytest -q"]}) == {"python"}
    assert hive_compat.card_needs_make({"needs": [], "verify": ["make bench"]})


@pytest.mark.parametrize("agent", [
    {"capabilities": ["windows"]},                       # 压根没声明 make
    {"capabilities": ["windows", "make"], "no_capabilities": ["make"]},
    {"capabilities": ["windows", "make"], "missing_capabilities": ["make"]},
    {"capabilities": ["windows", "make"], "no_make": True},
    {"capabilities": ["windows", "make"], "has_make": False},
    {"capabilities": ["windows", "no-make"]},
    {"capabilities": ["windows", "!make"]},
])
def test_agent_can_explicitly_declare_no_make(agent):
    assert hive_compat.agent_has_make(agent) is False, agent


@pytest.mark.parametrize("agent", [
    {"capabilities": ["windows", "make"]},
    {"abilities": ["code"], "capabilities": ["make"]},
    {"capabilities": ["make"], "no_make": False},
])
def test_agent_with_make(agent):
    assert hive_compat.agent_has_make(agent) is True, agent


def test_filter_agents_for_card_returns_only_make_agents():
    card = {"needs": [], "verify": ["make demo"]}
    agents = {
        "win11": {"capabilities": ["windows", "python"], "no_make": True},
        "fb88": {"capabilities": ["freebsd", "make"]},
        "win10": {"capabilities": ["windows", "python"]},
    }
    assert hive_compat.filter_agents_for_card(card, agents) == ["fb88"]
    # 不需要 make 的卡：能力过滤不该把普通 agent 误杀
    assert hive_compat.filter_agents_for_card(
        {"needs": ["python"], "verify": ["python3 -V"]}, agents) == ["win10", "win11"]


def test_dispatch_routes_make_cards_to_make_agents(world, fake_cmd):
    """真派发层回归：needs: make 的卡只给声明了 make 的 agent（hive.py 原有判据）。"""
    world.add_agent("win", "exec", caps=["windows", "python"], command=fake_cmd,
                    repos={"hive": world.code_clone("win")})
    world.add_task("T-0001", repo="hive", needs=["make"], verify=["make demo"])

    out = world.hive_json("dispatch")
    assert out["plan"] == [], out
    reasons = {s["task"]: s["reason"] for s in out["skipped"]}
    assert "能力缺失" in reasons["T-0001"], out

    world.add_agent("fb", "exec", caps=["freebsd", "make"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb")})
    out = world.hive_json("dispatch")
    plan = {p["task"]: p["agent"] for p in out["plan"]}
    assert plan == {"T-0001": "fb"}, out


# --------------------------------------------------------------------------- #
# 端到端：worker 在 Windows 上跑 `true` 必须 done
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(os.name != "nt", reason="Windows 专用回归")
def test_worker_true_verify_is_done(tmp_path):
    """跨平台卡的 `verify: [true]` 在 Windows 上必须绿（不是平台性失败）。"""
    p = subprocess.run(
        [sys.executable, str(ROOT / "hive_compat.py"), "run", "true"],
        capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr

    from conftest import World  # noqa: WPS433
    world = World(tmp_path)
    world.add_agent("win", "verify", caps=["windows"])
    world.add_task("T-0001", needs=["windows"], verify=["true"])
    world.hive("assign", "T-0001", "--agent", "win")
    r = world.run_worker("win")
    assert r.returncode == 0, r.stdout + r.stderr
    res = world.hive_json("result", "T-0001")
    assert res["status"] == "done", res
    assert res["verify"][0]["code"] == 0, res
