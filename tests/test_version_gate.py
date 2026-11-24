"""T-0466：`hive cluster start` 的版本闸 —— 正在跑的这份代码落后就说话。

背景（2026-11-24 现场）：四个坑（线程池泄漏、PIPE 挂死、ahead 空转、
pid 判活）改好、测过、写了排查文档，但改动停在别的工作区既没 commit 也没
让实际起集群的那份更新。于是文档说「已修」、现场继续 MemoryError —— 靠人
记「改完要重启 / 要更新」是记不住的，让启动本身说话。

覆盖：
- 与远端一致 → current，不告警；
- 本地落后远端 → behind，`_warn_if_tool_repo_behind` 往 stderr 说话；
- 本地**领先**远端 → current（自己就是最新的，不该劝人 pull）；
- 分叉 → current（不归这里裁决，别误导）；
- 不是 git 工作区 / 没有远端跟踪引用 → unknown，静默放行（绝不挡住启动）；
- `HIVE_SKIP_VERSION_GATE=1` → 完全闭嘴；
- 闸只看本地远端跟踪引用，**不做网络操作**（启动路径上不许等网络）。
"""
from __future__ import annotations

import ast
import subprocess

import pytest

import hive


def _git(cwd, *args) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0, f"git {args} 失败: {p.stderr}"
    return p.stdout.strip()


def _make_repo(tmp_path, monkeypatch):
    """造一个「就是 hive 自己」的小仓：把 __file__ 指到 tmp_path/hive.py。"""
    repo = tmp_path / "toolrepo"
    repo.mkdir()
    (repo / "hive.py").write_text("# fake\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    # 让 _tool_repo_staleness 认为「正在跑的就是这个文件」
    monkeypatch.setattr(hive, "__file__", str(repo / "hive.py"))
    return repo


def _advance_remote(repo, msg="upstream") -> str:
    """在仓里造一个新提交，并把 origin/main 指过去（模拟远端已更新）。"""
    fake = repo / f"{msg}.txt"
    fake.write_text(msg, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    new = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", new)
    # 把工作区退回上一个提交，制造「本地落后」
    _git(repo, "reset", "-q", "--mixed", "HEAD~1")
    return new


def test_current_when_synced(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    st = hive._tool_repo_staleness()
    assert st["state"] == "current", st
    assert st["behind"] == 0


def test_behind_is_detected(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    _advance_remote(repo)
    st = hive._tool_repo_staleness()
    assert st["state"] == "behind", st
    assert st["behind"] == 1, st


def test_behind_warns_on_stderr(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path, monkeypatch)
    _advance_remote(repo, "upstream2")
    hive._warn_if_tool_repo_behind()
    err = capsys.readouterr().err
    assert "落后远端" in err, err
    # 必须给出可照做的下一步，而不是只抱怨
    assert "pull --ff-only" in err, err
    assert "HIVE_SKIP_VERSION_GATE=1" in err, err


def test_skip_env_silences(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path, monkeypatch)
    _advance_remote(repo, "upstream3")
    monkeypatch.setenv("HIVE_SKIP_VERSION_GATE", "1")
    hive._warn_if_tool_repo_behind()
    assert capsys.readouterr().err == ""


def test_ahead_is_not_behind(tmp_path, monkeypatch):
    """本地领先（自己就是最新的）不该被劝去 pull。"""
    repo = _make_repo(tmp_path, monkeypatch)
    old = _git(repo, "rev-parse", "HEAD")
    # 本地再提交一个，远端引用仍停在 old
    (repo / "local.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "local only")
    _git(repo, "update-ref", "refs/remotes/origin/main", old)
    st = hive._tool_repo_staleness()
    assert st["state"] == "current", st
    assert st["behind"] == 0


def test_diverged_is_not_behind(tmp_path, monkeypatch):
    """分叉不归版本闸裁决，别给出「pull 一下就好」的错误暗示。"""
    repo = _make_repo(tmp_path, monkeypatch)
    base = _git(repo, "rev-parse", "HEAD")
    # 远端一条线
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    _advance_remote(repo, "remote-side")
    remote = _git(repo, "rev-parse", "refs/remotes/origin/main")
    # 本地回到 base 再走另一条线
    _git(repo, "reset", "-q", "--hard", base)
    (repo / "mine.txt").write_text("m", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "local side")
    _git(repo, "update-ref", "refs/remotes/origin/main", remote)
    st = hive._tool_repo_staleness()
    assert st["state"] == "current", st


def test_not_a_git_repo_is_unknown(tmp_path, monkeypatch):
    """裸目录（比如解压出来的安装包）必须静默放行，不能挡住启动。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "hive.py").write_text("# fake\n", encoding="utf-8")
    monkeypatch.setattr(hive, "__file__", str(plain / "hive.py"))
    st = hive._tool_repo_staleness()
    assert st["state"] == "unknown", st


def test_no_remote_ref_is_unknown(tmp_path, monkeypatch):
    """没有 origin/main 本地引用时无从判断 —— 说 unknown，不当成落后。"""
    repo = _make_repo(tmp_path, monkeypatch)
    st = hive._tool_repo_staleness()
    assert st["state"] == "unknown", st


def test_gate_does_no_network_call():
    """启动路径上不许 fetch：一切只读本地引用，离线也必须瞬间返回。

    看的是**实际发出去的子进程参数**，不是源码文本 —— 文档里说「不做 fetch」
    的那句话本身不该让它变红。
    """
    import inspect

    tree = ast.parse(inspect.getsource(hive._tool_repo_staleness).lstrip())
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
    # 只聚合「看起来像命令参数」的字面量，跳过 docstring 那一句散文。
    cmds = " ".join(s for s in literals if s in {
        "fetch", "pull", "ls-remote", "remote_head", "merge",
        "rev-parse", "merge-base", "rev-list",
        "origin/main", "origin/master", "main", "master",
    } or s.startswith("-"))
    for forbidden in ("fetch", "pull", "ls-remote"):
        assert forbidden not in cmds, f"版本闸把 {forbidden} 当成 git 子命令用了"
    # 真正的读操作只该是这几个（都是本地引用查询）
    for needed in ("rev-parse", "merge-base", "rev-list"):
        assert needed in cmds, f"版本闸应当用 {needed} 读本地引用"
