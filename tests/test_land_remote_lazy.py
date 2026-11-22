"""T-0437：land 的远端探测必须是「只读引用、命中才搬对象」的惰性探测。

现场：light 仓 3 个远端 / 32 个 worktree / 101.71 MiB packs，`hive land T-0434`
不给 `--remote` 时裸抛 `MemoryError`。根因是 `_pick_remote()` 对**每个**远端都
`git fetch --prune` 一遍再探测分支，等于先把所有远端对象拉一遍。加 `--remote`
立刻就好，因为那条路只 fetch 指定的一个。

这里用 mock 记录 `git_net` 的调用序列（不造大仓、不搬对象），断言：
1. 3 个远端、第 2 个命中时，只有命中那个被 fetch，其余只做 ls-remote / 不碰；
2. 显式 `--remote` 完全不进探测循环（唯一一次 fetch 就是它）；
3. 远端名打错仍然直接报错并列出现有远端（安全语义不变）；
4. 探测/fetch 期间 MemoryError 被转成带 `--remote` 用法和远端清单的 HiveError。
"""
import pytest

import hive as H


class _FakeProc:
    """够用的 CompletedProcess 替身：`_pick_remote` 只看 stdout。"""

    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _hive(world):
    return H.Hive(world.hive1)


def _fake_git_net(calls, has_branch_on=(), ls_error=None, fetch_error=None):
    """记录调用序列的假 `git_net`；`has_branch_on` 里的远端算「有分支」。"""

    def _run(args, cwd, check=True, input=None):
        calls.append(list(args))
        if args[0] == "ls-remote":
            if ls_error is not None:
                raise ls_error
            name = args[2]
            ref = args[3] if len(args) > 3 else ""
            if name in has_branch_on:
                return _FakeProc(f"{name}-sha\t{ref}\n")
            return _FakeProc("")
        if args[0] == "fetch":
            if fetch_error is not None:
                raise fetch_error
            return _FakeProc()
        raise AssertionError(f"未预期的 git_net 调用: {args}")

    return _run


def test_probe_only_reads_refs_and_fetches_the_hit_remote(world, monkeypatch):
    """3 远端、第 2 个命中：只 fetch 命中那个，后面的远端不碰。"""
    repo = world.code_clone("probe")
    calls = []
    monkeypatch.setattr(H, "code_remotes", lambda r: ["origin", "gitcode", "gitea"])
    monkeypatch.setattr(H, "git_net", _fake_git_net(calls, {"gitcode"}))

    assert _hive(world)._pick_remote(repo, "task/T-0001") == "gitcode"

    fetches = [c for c in calls if c[0] == "fetch"]
    probes = [c for c in calls if c[0] == "ls-remote"]
    assert [c[2] for c in fetches] == ["gitcode"], calls
    assert [c[2] for c in probes] == ["origin", "gitcode"], calls
    # gitea 在命中后不再探测，更不承担任何对象传输
    assert not any("gitea" in c for c in calls), calls


def test_first_remote_hit_never_touches_others(world, monkeypatch):
    """第 1 个就命中时，其它远端连 ls-remote 都不该有。"""
    repo = world.code_clone("probe1")
    calls = []
    monkeypatch.setattr(H, "code_remotes", lambda r: ["origin", "gitcode", "gitea"])
    monkeypatch.setattr(H, "git_net", _fake_git_net(calls, {"origin"}))

    assert _hive(world)._pick_remote(repo, "task/T-0001") == "origin"
    assert calls == [["ls-remote", "--heads", "origin", "refs/heads/task/T-0001"],
                     ["fetch", "--prune", "origin"]], calls


def test_explicit_remote_skips_probe_entirely(world, monkeypatch):
    """显式 --remote：不进探测循环，唯一一次网络操作就是 fetch 它。"""
    repo = world.code_clone("explicit")
    calls = []
    monkeypatch.setattr(H, "code_remotes", lambda r: ["origin", "gitcode", "gitea"])
    monkeypatch.setattr(H, "git_net", _fake_git_net(calls, {"gitcode"}))

    assert _hive(world)._pick_remote(
        repo, "task/T-0001", want="gitea") == "gitea"
    assert calls == [["fetch", "--prune", "gitea"]], calls


def test_explicit_typo_still_fatal_without_any_network_call(world, monkeypatch):
    """安全语义不变：远端名打错 → 立刻报错并列出现有远端，不静默回落。"""
    repo = world.code_clone("typo")
    calls = []
    monkeypatch.setattr(H, "code_remotes", lambda r: ["origin", "gitcode", "gitea"])
    monkeypatch.setattr(H, "git_net", _fake_git_net(calls))

    with pytest.raises(H.HiveError) as ei:
        _hive(world)._pick_remote(repo, "task/T-0001", want="gtcode")
    msg = str(ei.value)
    assert "gtcode" in msg and "gitcode" in msg, msg
    assert calls == [], "远端名不对时不该发任何网络请求"


def test_probe_memory_error_becomes_actionable_hint(world, monkeypatch):
    """裸 MemoryError 不再漏到上层：换成「内存不足 + 改用 --remote + 远端清单」。"""
    repo = world.code_clone("oom")
    monkeypatch.setattr(H, "code_remotes", lambda r: ["origin", "gitcode", "gitea"])
    monkeypatch.setattr(H, "git_net",
                        _fake_git_net([], ls_error=MemoryError()))

    with pytest.raises(H.HiveError) as ei:
        _hive(world)._pick_remote(repo, "task/T-0001")
    msg = str(ei.value)
    assert "内存不足" in msg, msg
    assert "--remote" in msg and "hive land" in msg, msg
    for name in ("origin", "gitcode", "gitea"):
        assert name in msg, f"提示里应列出可用远端 {name}: {msg}"
    assert not isinstance(ei.value, MemoryError)


def test_fetch_memory_error_also_becomes_hint(world, monkeypatch):
    """命中之后真正 fetch 时 OOM：同样给可照抄的提示，而不是裸抛。"""
    repo = world.code_clone("oom2")
    calls = []
    monkeypatch.setattr(H, "code_remotes", lambda r: ["origin", "gitcode", "gitea"])
    monkeypatch.setattr(H, "git_net", _fake_git_net(
        calls, {"gitcode"}, fetch_error=MemoryError()))

    with pytest.raises(H.HiveError) as ei:
        _hive(world)._pick_remote(repo, "task/T-0001")
    msg = str(ei.value)
    assert "内存不足" in msg and "gitcode" in msg and "--remote" in msg, msg
    # 探测阶段仍然是「只读引用」：先把 2 个远端探完，只在命中那个 fetch
    assert [c[2] for c in calls if c[0] == "ls-remote"] == ["origin", "gitcode"]
    assert [c[2] for c in calls if c[0] == "fetch"] == ["gitcode"]


def test_probe_network_error_skips_remote_and_continues(world, monkeypatch):
    """某个远端断网/没权限时跳过它，不能在第一个远端上把 land 堵死。"""
    repo = world.code_clone("skip")
    calls = []

    def _run(args, cwd, check=True, input=None):
        calls.append(list(args))
        if args[0] == "ls-remote":
            if args[2] == "origin":
                raise H.HiveError("origin 连接超时")
            return _FakeProc(f"sha\t{args[3]}\n" if args[2] == "gitcode" else "")
        if args[0] == "fetch":
            return _FakeProc()
        raise AssertionError(args)

    monkeypatch.setattr(H, "code_remotes", lambda r: ["origin", "gitcode", "gitea"])
    monkeypatch.setattr(H, "git_net", _run)

    assert _hive(world)._pick_remote(repo, "task/T-0001") == "gitcode"
    assert [c[2] for c in calls if c[0] == "fetch"] == ["gitcode"], calls
