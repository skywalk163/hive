"""verify 命令的 POSIX → Windows 垫片（hive_compat.py，T-0208 / T-0292）。

背景：任务卡里的 verify 常写 `true` / `test -f x`。FreeBSD 上是内建命令，
Windows 的 cmd.exe 没有，于是同一张卡在验收机上**必然**红灯。
垫片只映射高频、语义明确的写法，未知写法原样交给 shell（绝不猜成功）。

T-0292 追加 `bash <script>` / `python3 <args>`：Windows 上分别探测
Git for Windows 的 bash.exe 与 `sys.executable`，真起子进程、退出码原样透传。
测试不要求 CI 装 Git Bash：识别/失败路径用 mock，执行路径用 `sys.executable`。
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
    monkeypatch.setattr(hive_compat.os, "name", "posix")
    monkeypatch.delenv("HIVE_COMPAT", raising=False)
    for cmd in ("true", "test -f x", "pytest -q tests"):
        assert hive.verify_command(cmd) == cmd


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
    ("python -V", False),               # `python` 不是 `python3`，口径不混
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


def test_interpreter_command_parses_prefix():
    assert hive_compat.interpreter_command(
        "bash tests/run_pytest.sh tests/test_run_stdin.py") == (
            "bash", "tests/run_pytest.sh tests/test_run_stdin.py")
    assert hive_compat.interpreter_command("python3 -V") == ("python3", "-V")
    assert hive_compat.interpreter_command("bash") is None
    assert hive_compat.interpreter_command("pytest -q") is None


def test_bare_interpreter_is_not_executed():
    """`bash` / `python3` 单独一条：不执行、判未知（绝不冒充成功）。"""
    assert hive_compat.evaluate("bash") == 2
    assert hive_compat.evaluate("python3") == 2


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
    for cmd in ("bash tests/run_pytest.sh tests/test_run_stdin.py", "python3 -V"):
        out = hive.verify_command(cmd)
        assert "hive_compat.py" in out and " run " in out, cmd
        assert hive_compat.should_wrap(out) is False      # 幂等，不套娃


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
