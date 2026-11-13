"""本机代码仓路径解析：sync-repos 不能再拿到「错误的包信息」。

背景（用户现场反馈）：每台机器 repo 的位置和目录名都可能不一样，
`hive sync-repos` 有时会报出别的机器的路径，甚至对着无关目录做同步。
本文件把新的解析约定钉死：

- 本机 agent 登记必须是 git 仓，光「目录存在」不算数；
- 本机没有登记时，按工作区扫描 + git remote 认仓（目录名可以不同），
  同一工作区优先浅层；
- 多个同等候选 → needs-confirm，不猜、不同步；
- 用户确认过的路径写进本机记忆，下次直接用；
- 本机没有 agent 节点时，主控用 `hive repo remember` 替它记；
- 别的机器的登记路径永远不能当本机答案。
"""
from __future__ import annotations

import json

import hive

from conftest import sh


def _clone(world, dest):
    sh(["git", "clone", "-q", str(world.code_origin), str(dest)],
       env=world.git_env)
    return dest


def _add_remote_agent(world, repos, host="other-box"):
    """只登记在别的机器上：本机解析时不能拿它的路径当答案。"""
    return world.add_agent("remote-node", mode="manual", host=host, repos=repos)


def _results(out):
    return {r["repo"]: r for r in out["results"]}


# --------------------------------------------------------------------------- #
# 基础：remote 认仓 / 非 git 目录
# --------------------------------------------------------------------------- #
def test_scan_matches_by_remote_not_dir_name(world, tmp_path, monkeypatch):
    """目录叫 light-merge，只要 remote 是 light.git 就认（目录名不必等于仓名）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    clone = _clone(world, ws / "light-merge")
    _add_remote_agent(world, {"light": "/no/such/light"})
    monkeypatch.setenv("HIVE_WORKSPACE", str(ws))

    out = world.hive_json("repo", "list")
    item = out["repos"]["light"]
    assert item["status"] == "ok", item
    assert item["path"] == str(clone)
    assert item["source"] == "workspace-scan"
    assert item["verified"] is True
    assert "light" in item["notes"][0]

    # 扫描确认的路径写入本机记忆：下次（新进程）不用再扫
    data = json.loads((tmp_path / "repo-bindings.json").read_text(encoding="utf-8"))
    assert data["hosts"][hive.current_host()]["light"]["path"] == str(clone)


def test_existing_but_non_git_dir_is_ignored(world, tmp_path):
    """本机 agent 登记了一个存在但不是 git 的目录：必须忽略，不能拿去同步。"""
    fake = tmp_path / "fake-light"
    fake.mkdir()
    world.add_agent("fb", mode="manual", host=hive.current_host(),
                    repos={"light": str(fake)})

    out = world.hive_json("sync-repos")
    r = _results(out)["light"]
    assert r["status"] == "missing", r
    assert r["path"] == ""
    assert any("不是 git 仓" in n for n in r["notes"])


def test_foreign_registration_is_not_used(world):
    """别的机器登记的 G:/... 不能变成本机的「路径」再报 missing。"""
    _add_remote_agent(world, {"light": "G:/dswork/light"}, host="win-box")
    out = world.hive_json("sync-repos")
    r = _results(out)["light"]
    assert r["status"] == "missing", r
    assert r["path"] == ""
    assert "G:/dswork/light" not in r["path"]


# --------------------------------------------------------------------------- #
# 歧义：不猜，交给用户
# --------------------------------------------------------------------------- #
def test_multiple_equal_candidates_need_confirm(world, tmp_path, monkeypatch):
    """两个同层同名（remote 相同）候选 → needs-confirm，一个都不同步。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    a = _clone(world, ws / "a-light")
    b = _clone(world, ws / "b-light")
    _add_remote_agent(world, {"light": "/no/such/light"})
    monkeypatch.setenv("HIVE_WORKSPACE", str(ws))

    out = world.hive_json("sync-repos")
    r = _results(out)["light"]
    assert r["status"] == "needs-confirm", r
    paths = {c["path"] for c in r["candidates"]}
    assert paths == {str(a), str(b)}
    # 两个都没被快进过（HEAD 与 origin 一致；这里只验证命令没报成功）
    assert "sha" not in r


def test_shallow_candidate_wins(tmp_path, world, monkeypatch):
    """同一工作区里浅层优先：ws/light 胜过 ws/soak/light（「都放同一工作目录」）。"""
    ws = tmp_path / "ws"
    (ws / "soak").mkdir(parents=True)
    shallow = _clone(world, ws / "light")
    _clone(world, ws / "soak" / "light")
    _add_remote_agent(world, {"light": "/no/such/light"})
    monkeypatch.setenv("HIVE_WORKSPACE", str(ws))

    out = world.hive_json("repo", "list")
    item = out["repos"]["light"]
    assert item["path"] == str(shallow), item
    assert item["status"] == "ok"


# --------------------------------------------------------------------------- #
# 记忆：用户指定 / 主控代记
# --------------------------------------------------------------------------- #
def test_bind_wins_and_persists(world, tmp_path, monkeypatch):
    """`hive repo bind` 写本机记忆，后续 sync 直接用（新进程仍生效）。"""
    bindings = tmp_path / "mem" / "repo-bindings.json"
    monkeypatch.setenv("HIVE_REPO_BINDINGS", str(bindings))
    clone = _clone(world, tmp_path / "somewhere" / "weird-dir-name")

    world.hive("repo", "bind", "light", str(clone))
    assert bindings.exists()

    out = world.hive_json("repo", "list")
    item = out["repos"]["light"]
    assert item["source"] == "local-memory"
    assert item["path"] == str(clone)

    sync = _results(world.hive_json("sync-repos"))["light"]
    assert sync["status"] == "up-to-date", sync
    assert sync["path"] == str(clone)


def test_controller_memory_for_host_without_agent(world, tmp_path):
    """本机没有 agent 节点：主控 remember 的 repo-paths.json 就是本机记忆。"""
    clone = _clone(world, tmp_path / "ctl-light")
    (world.hive1 / "repo-paths.json").write_text(json.dumps({
        "hosts": {hive.current_host(): {"light": {"path": str(clone)}}},
    }, ensure_ascii=False), encoding="utf-8")

    h = hive.Hive(world.hive1)
    item = h.resolve_repos()["light"]
    assert item["status"] == "ok", item
    assert item["source"] == "controller-memory"
    assert item["path"] == str(clone)


def test_repo_remember_cli(world, tmp_path):
    """`hive repo remember --host ... --repo name=path` 落盘并提交。"""
    clone = _clone(world, tmp_path / "r-light")
    world.hive("repo", "remember", "--host", "dsh-jail",
               "--repo", f"light={clone}")
    data = json.loads((world.hive1 / "repo-paths.json").read_text(encoding="utf-8"))
    assert data["hosts"]["dsh-jail"]["light"]["path"] == str(clone)
    # 已进入协调仓历史（主控的记忆要能跟着仓库走）
    log = sh(["git", "-C", str(world.hive1), "log", "-1", "--name-only",
              "--format=%s"], env=world.git_env).stdout
    assert "repo-paths.json" in log
    assert "remember dsh-jail" in log


# --------------------------------------------------------------------------- #
# 纯函数：URL 形态
# --------------------------------------------------------------------------- #
def test_repo_basename_variants():
    import hive_repos as R
    assert R.repo_basename("http://192.168.1.5:3000/skywalk/hive.git") == "hive"
    assert R.repo_basename("git@github.com:skywalk163/hive.git") == "hive"
    assert R.repo_basename("/tmp/light.git") == "light"
    assert R.repo_basename("G:/dswork/light") == "light"
    assert R.repo_basename("G:\\dswork\\light.git") == "light"
    assert R.repo_basename("") == ""
    assert R.matches_name("light", "/x/light-merge",
                          "http://h:3000/s/light.git") == "remote"
    assert R.matches_name("light", "/x/light-merge", "") == ""
    assert R.matches_name("light", "/x/light", "") == "dir"

# --------------------------------------------------------------------------- #
# 交互式问路（不猜的最后一步）
# --------------------------------------------------------------------------- #
class _TTY:
    def isatty(self):
        return True


def test_ask_path_picks_candidate(monkeypatch):
    """终端里列出候选并让用户选编号；非 TTY / --json / HIVE_NO_PROMPT 不问。"""
    import hive_repos as R
    monkeypatch.delenv("HIVE_NO_PROMPT", raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "2")
    res = {"candidates": [{"path": "/x/a-light", "kind": "remote"},
                          {"path": "/x/b-light", "kind": "remote"}]}
    assert R.ask_path("light", res, stream=_TTY()) == "/x/b-light"


def test_ask_path_respects_no_prompt(monkeypatch):
    import hive_repos as R
    monkeypatch.setenv("HIVE_NO_PROMPT", "1")
    monkeypatch.setattr("builtins.input",
                        lambda _prompt="": (_ for _ in ()).throw(AssertionError("不该问")))
    res = {"candidates": [{"path": "/x/a-light"}, {"path": "/x/b-light"}]}
    assert R.ask_path("light", res, stream=_TTY()) is None


def test_ask_path_accepts_pasted_path(monkeypatch):
    import hive_repos as R
    monkeypatch.delenv("HIVE_NO_PROMPT", raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "/my/own/light")
    assert R.ask_path("light", {"candidates": []}, stream=_TTY()) == "/my/own/light"
