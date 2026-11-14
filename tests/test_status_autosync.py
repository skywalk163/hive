"""T-0353：协调仓自动同步 + local_stale 语义升级。

覆盖（卡面「要做什么」第 5 条 + 完成标准）：
- 干净 ff-only：自动同步成功、local_stale 转 false、卡面读到新状态；
- 干净分叉：不 merge、behind 被标出、给出人工裁决提示；
- **脏工作区：git status --porcelain 前后完全一致、零写操作**（本卡最重要回归）；
- 30 分钟窗口内不重复触发、jitter 落在 ±20% 且不与整周期对齐、T0 提前同步、
  T1 head_sha 相同时零 Gitea 流量；
- local_stale 旧字段仍在、--json / --sync-health 输出可解析且含契约字段；
- sync-repos / orchestrate 复用同一节流器，60s tick 不再每次打 Gitea。

夹具刻意只造「bare origin + 两个干净 clone」，不跑 `hive init`：卡片格式是
`tasks/T-*.md` frontmatter，cmd_status 只读这些文件；这样单个用例在 Windows
上不必付全套 World 初始化的十几轮 git 子进程开销。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

import hive
import hive_repos
from conftest import sh

ROOT = Path(__file__).resolve().parents[1]
HIVE_PY = ROOT / "hive.py"

# 只给 git 提交身份；conftest 的 sh() 会把 os.environ（含 monkeypatch 设的
# HIVE_* 隔离变量）补进来，千万别在这里存一份 os.environ 快照覆盖它们。
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
}

CARD = """---
id: {tid}
title: {title}
state: pending
repo: light
priority: P2
---
同步测试卡
"""

WRITE_VERBS = {
    "merge", "reset", "checkout", "stash", "clean", "fetch", "pull",
    "rebase", "commit", "push", "add", "rm", "mv",
}

SYNC_ENV_KEYS = (
    hive_repos.SYNC_ENV_TIER, hive_repos.SYNC_ENV_HEAD_SHA,
    hive_repos.SYNC_ENV_EVENT, hive_repos.SYNC_ENV_FORCE,
    hive_repos.SYNC_ENV_STATE, hive_repos.SYNC_ENV_DISABLE,
    hive.FETCH_ALWAYS_ENV,
)


def _git(args, cwd, check=True):
    return sh(["git", "-C", str(cwd), *args], check=check, env=GIT_ENV)


def _head(path) -> str:
    return _git(["rev-parse", "HEAD"], path).stdout.strip()


def _porcelain(path) -> str:
    return _git(["status", "--porcelain"], path).stdout


class Repos:
    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "hive.git"
        self.seed = root / "seed"          # 模拟「别的节点」往 origin 推
        self.a = root / "hive-a"           # 被测协调仓（干净 clone）
        sh(["git", "init", "--bare", "-b", "main", str(self.origin)], env=GIT_ENV)
        sh(["git", "init", "-b", "main", str(self.seed)], env=GIT_ENV)
        (self.seed / "tasks").mkdir()
        self._write_card(self.seed, "T-9000", "基线卡")
        (self.seed / ".gitignore").write_text(".hive/\n__pycache__/\n",
                                              encoding="utf-8")
        _git(["add", "-A"], self.seed)
        _git(["commit", "-q", "-m", "init"], self.seed)
        _git(["remote", "add", "origin", str(self.origin)], self.seed)
        _git(["push", "-q", "-u", "origin", "main"], self.seed)
        self.clone(self.a)

    @staticmethod
    def _write_card(path, tid, title):
        (path / "tasks").mkdir(exist_ok=True)
        (path / "tasks" / f"{tid}.md").write_text(
            CARD.format(tid=tid, title=title), encoding="utf-8")

    def clone(self, dst: Path) -> Path:
        sh(["git", "clone", "-q", str(self.origin), str(dst)], env=GIT_ENV)
        return dst

    def push_card(self, tid: str, title: str = "远端新卡") -> None:
        self._write_card(self.seed, tid, title)
        _git(["add", "-A"], self.seed)
        _git(["commit", "-q", "-m", f"add {tid}"], self.seed)
        _git(["push", "-q", "origin", "main"], self.seed)

    def local_commit(self, filename: str = "local.txt") -> str:
        (self.a / filename).write_text("local only\n", encoding="utf-8")
        _git(["add", "-A"], self.a)
        _git(["commit", "-q", "-m", "local commit"], self.a)
        return _head(self.a)


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    # 每个用例独立一份节流状态文件：既能断言「脏工作区不落盘」，也避免串窗口。
    hive._ls_remote_cache.clear()
    for key in SYNC_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(hive_repos.SYNC_ENV_STATE,
                       str(tmp_path / "sync-state.json"))
    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "git")
    yield Repos(tmp_path)
    hive._ls_remote_cache.clear()


def _status_json(h, capsys, **kw) -> object:
    h.cmd_status(as_json=True, **kw)
    return json.loads(capsys.readouterr().out)


def _health(row: dict) -> dict:
    assert "local_stale" in row, "旧布尔字段必须保留"
    assert "sync_health" in row, "新结构化对象必须存在"
    return row["sync_health"]


def _state(tmp_path) -> dict:
    p = tmp_path / "sync-state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _age_state(tmp_path, seconds: float = 120.0) -> None:
    """把上次对账时间往前拨，模拟最小防抖间隔已经过去。"""
    p = tmp_path / "sync-state.json"
    st = json.loads(p.read_text(encoding="utf-8"))
    st["last_sync_at"] = time.time() - seconds
    p.write_text(json.dumps(st), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 三态判定
# --------------------------------------------------------------------------- #
def test_clean_worktree_autosync_fast_forwards(repos, capsys, tmp_path):
    """干净 + 落后：自动 ff-only，卡面读到新卡，local_stale 转 false。"""
    before = _head(repos.a)
    repos.push_card("T-9001")
    assert not (repos.a / "tasks" / "T-9001.md").exists()

    rows = _status_json(hive.Hive(repos.a), capsys)
    assert isinstance(rows, list), "默认 --json 顶层仍是数组（旧看板兼容）"
    assert any(r["id"] == "T-9001" for r in rows), "自动同步后应看到新卡面"
    for row in rows:
        health = _health(row)
        assert row["local_stale"] is False
        assert health["behind"] is False
        assert health["autosynced"] is True
        assert health["state"] == hive_repos.UP_TO_DATE
    assert _head(repos.a) != before, "HEAD 应被 ff-only 推进"
    assert (repos.a / "tasks" / "T-9001.md").exists(), "Git 仍是事实账"
    assert _state(tmp_path)["lsdb_seq"] == 1, "冷启动只记一次对账"


def test_clean_diverged_no_merge_and_manual_banner(repos, capsys):
    """干净 + 分叉：绝不自动 merge，behind 标出并给出人工裁决提示。"""
    local_head = repos.local_commit()
    repos.push_card("T-9002")

    rows = _status_json(hive.Hive(repos.a), capsys)
    for row in rows:
        health = _health(row)
        assert row["local_stale"] is True
        assert health["behind"] is True
        assert health["state"] == hive_repos.CLEAN_DIVERGED
        assert health["action"] == "manual"
        assert "人工裁决" in health["reason"]
        assert "人工裁决" in health["banner"]
    assert _head(repos.a) == local_head, "分叉时本地提交必须原样保留"

    # 人类可读输出里的横幅也要换成三态文案
    hive.Hive(repos.a).cmd_status()
    assert "本地与 origin/main 分叉，需人工裁决" in capsys.readouterr().out


def test_dirty_worktree_zero_writes(repos, capsys, monkeypatch, tmp_path):
    """**本卡最重要回归**：脏工作区只读不改，porcelain 前后字节级一致。"""
    tracked = _git(["ls-files"], repos.a).stdout.splitlines()[0]
    target = repos.a / tracked
    original = target.read_text(encoding="utf-8")
    target.write_text(original + "\n# 别人的在途改动\n", encoding="utf-8")
    (repos.a / "untracked-note.txt").write_text("in flight\n", encoding="utf-8")
    repos.push_card("T-9003")

    before = _porcelain(repos.a)
    head_before = _head(repos.a)
    remote_before = _git(["rev-parse", "origin/main"], repos.a).stdout.strip()

    remote_probes, git_writes, net_calls = [], [], []
    real_fic = hive.fetch_if_changed
    real_git = hive.git

    def spy_fic(path, ttl=hive.FETCH_IF_CHANGED_TTL_S):
        remote_probes.append(str(path))
        return real_fic(path, ttl)

    def spy_git(args, cwd, check=True, input=None):
        if args and args[0] in WRITE_VERBS:
            git_writes.append(list(args))
        return real_git(args, cwd, check=check, input=input)

    monkeypatch.setattr(hive, "fetch_if_changed", spy_fic)
    monkeypatch.setattr(hive, "git", spy_git)
    monkeypatch.setattr(hive, "git_net",
                        lambda a, c, **k: net_calls.append(list(a)))

    rows = _status_json(hive.Hive(repos.a), capsys)
    for row in rows:
        health = _health(row)
        assert health["state"] == hive_repos.DIRTY_WORKTREE
        assert health["action"] == "skip-dirty"
        assert "自动同步已跳过" in health["reason"]
        assert "自动同步已跳过" in health["banner"]

    # 零写：连一次 ls-remote / fetch / merge / reset / stash 都不许有
    assert remote_probes == [], f"脏工作区不该探测远端：{remote_probes}"
    assert net_calls == [], f"脏工作区不该发网络 git：{net_calls}"
    assert git_writes == [], f"脏工作区不该有写操作：{git_writes}"
    assert not (tmp_path / "sync-state.json").exists(), "连节流记账都不落盘"
    assert _porcelain(repos.a) == before, "git status --porcelain 前后必须完全一致"
    assert _head(repos.a) == head_before
    assert _git(["rev-parse", "origin/main"], repos.a).stdout.strip() == remote_before
    assert target.read_text(encoding="utf-8") == original + "\n# 别人的在途改动\n"

    hive.Hive(repos.a).cmd_status()
    assert "本地有未提交改动，自动同步已跳过" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# T2 / T0 / T1 节拍
# --------------------------------------------------------------------------- #
def test_t2_window_no_repeat_within_30_minutes(repos, capsys, monkeypatch,
                                               tmp_path):
    """30 分钟窗口内重复 status：ls-remote / fetch 总共 ≤1 次。"""
    probes, fetches = [], []
    real_remote_head, real_git_net = hive.remote_head, hive.git_net

    def spy_remote_head(path):
        probes.append(str(path))
        return real_remote_head(path)

    def spy_git_net(args, cwd, **kw):
        if "fetch" in args:
            fetches.append(list(args))
        return real_git_net(args, cwd, **kw)

    monkeypatch.setattr(hive, "remote_head", spy_remote_head)
    monkeypatch.setattr(hive, "git_net", spy_git_net)

    h = hive.Hive(repos.a)
    for _ in range(5):
        # 清掉 10s TTL 缓存：证明「不连 Gitea」来自 T2 节流，不是 TTL 碰巧命中
        hive._ls_remote_cache.clear()
        _status_json(h, capsys)

    assert len(probes) == 1, f"30 分钟窗口内只该探一次远端：{probes}"
    assert fetches == [], "远端没变时不该 fetch"
    assert _state(tmp_path)["lsdb_seq"] == 1, "窗口内不重复记对账"


def test_t2_jitter_bounds_and_not_aligned(tmp_path):
    """T2 = 1800s ±20%，且不落在整周期上（避免节点整点对齐）。"""
    interval = hive_repos.SYNC_T2_INTERVAL_S
    assert interval == 1800.0
    assert hive_repos.SYNC_JITTER == 0.20
    for u in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 0.999999):
        t = hive_repos.SyncThrottle(
            tmp_path, state_path=tmp_path / "j.json",
            clock=lambda: 1000.0, rng=lambda: u)
        delta = t.next_t2_at(1000.0) - 1000.0
        assert interval * 0.8 - 1e-6 <= delta <= interval * 1.2 + 1e-6, (
            f"u={u} 时抖动出界: {delta}")
        assert delta != interval, f"u={u} 时不该退回无抖动的整周期"
    # 最小间隔只是事件防抖下限，不是同步周期
    assert hive_repos.SYNC_MIN_INTERVAL_S < interval / 10


def test_t0_event_early_sync(repos, capsys, monkeypatch, tmp_path):
    """T0 触发 LSA 可在 T2 窗口到期前提前同步（仍受最小间隔约束）。"""
    h = hive.Hive(repos.a)
    _status_json(h, capsys)                 # 冷启动基线对账，seq=1
    assert _state(tmp_path)["lsdb_seq"] == 1

    repos.push_card("T-9004")
    hive._ls_remote_cache.clear()
    _age_state(tmp_path)                    # 让 60s 防抖间隔过去
    monkeypatch.setenv(hive_repos.SYNC_ENV_TIER, "T0")
    monkeypatch.setenv(hive_repos.SYNC_ENV_EVENT, "1")

    rows = _status_json(h, capsys)
    assert any(r["id"] == "T-9004" for r in rows), "T0 应提前拉到新卡面"
    for row in rows:
        health = _health(row)
        assert health["tier"] == "T0"
        assert "T0" in health["reason"]
        assert row["local_stale"] is False
    assert _state(tmp_path)["lsdb_seq"] == 2


def test_t1_same_head_sha_zero_gitea(repos, capsys, monkeypatch):
    """T1 组播 head_sha 与本地一致：本轮零 ls-remote 流量。"""
    h = hive.Hive(repos.a)
    _status_json(h, capsys)
    probes = []
    real = hive.remote_head

    def spy(path):
        probes.append(str(path))
        return real(path)

    monkeypatch.setattr(hive, "remote_head", spy)
    hive._ls_remote_cache.clear()
    known = _git(["rev-parse", "origin/main"], repos.a).stdout.strip()
    monkeypatch.setenv(hive_repos.SYNC_ENV_TIER, "T1")
    monkeypatch.setenv(hive_repos.SYNC_ENV_HEAD_SHA, known)

    rows = _status_json(h, capsys)
    assert probes == [], f"组播 sha 一致时不该问 Gitea：{probes}"
    for row in rows:
        health = _health(row)
        assert health["tier"] == "T1"
        assert "无需问 Gitea" in health["reason"]


# --------------------------------------------------------------------------- #
# --json 契约
# --------------------------------------------------------------------------- #
def test_status_json_keeps_local_stale_and_sync_health(repos, capsys, tmp_path):
    """--json 同时含旧 local_stale 与新 sync_health；对象形态可解析。"""
    h = hive.Hive(repos.a)
    rows = _status_json(h, capsys)
    assert isinstance(rows, list) and rows
    contract = {"lsdb_seq", "last_sync_at", "head_sha", "remote_sha",
                "behind", "tier", "reason"}
    for row in rows:
        health = _health(row)
        assert contract <= set(health), f"sync_health 缺契约字段: {health}"
        assert health["tier"] in ("T0", "T1", "T2")
        assert isinstance(row["local_stale"], bool)
        assert isinstance(health["behind"], bool)
        assert health["t2_interval_s"] == 1800.0
        assert health["jitter"] == 0.20

    obj = _status_json(h, capsys, sync_health_only=True)
    assert isinstance(obj, dict)
    assert isinstance(obj["tasks"], list)
    assert "sync_health" in obj and "local_stale" in obj
    assert contract <= set(obj["sync_health"])

    # CLI 层也要通：--sync-health 把顶层切成 {tasks, sync_health, local_stale}
    p = sh([sys.executable, str(HIVE_PY), "--dir", str(repos.a),
            "status", "--json", "--sync-health"], env=GIT_ENV)
    parsed = json.loads(p.stdout)
    assert isinstance(parsed, dict) and "sync_health" in parsed
    assert "local_stale" in parsed


# --------------------------------------------------------------------------- #
# sync-repos / orchestrate 复用同一节流器
# --------------------------------------------------------------------------- #
def test_sync_repos_throttled_skips_second_tick(repos, monkeypatch, tmp_path):
    """orchestrate 路径的 sync-repos：窗口内整段跳过，连路径解析都不做。"""
    resolve_calls = []
    monkeypatch.setattr(hive.Hive, "resolve_repos",
                        lambda self: (resolve_calls.append(1), {})[1])
    h = hive.Hive(repos.a)
    h._sync_repos_throttled = True
    try:
        first = h.cmd_sync_repos(quiet=True)
        second = h.cmd_sync_repos(quiet=True)
    finally:
        h._sync_repos_throttled = False

    assert "skipped" not in first, "冷启动那一轮仍要做一次对账"
    assert "skipped" in second, "30 分钟窗口内第二个 tick 必须跳过"
    assert "T2 窗口内" in second["skipped"]
    assert len(resolve_calls) == 1, "跳过时连 resolve_repos 都不该跑"
    assert _state(tmp_path)["lsdb_seq"] == 1

    # 显式人工调用（CLI 语义）不受节流影响
    monkeypatch.delenv(hive_repos.SYNC_ENV_STATE, raising=False)
    assert "skipped" not in h.cmd_sync_repos(quiet=True)


def test_orchestrate_marks_sync_repos_throttled(repos, monkeypatch):
    """orchestrate 每 tick 给 sync 阶段打上节流标记。"""
    seen = []

    def fake(self, repo=None, as_json=False, quiet=False):
        seen.append(bool(getattr(self, "_sync_repos_throttled", False)))
        return {"results": []}

    monkeypatch.setattr(hive.Hive, "cmd_sync_repos", fake)
    monkeypatch.setattr(hive, "cluster_ensure", lambda d: False)
    monkeypatch.setattr(hive.Hive, "_cluster_gate",
                        lambda self: {"mode": "no-cluster", "term": 0})
    h = hive.Hive(repos.a)
    h.cmd_orchestrate(once=True, dispatch=False, land=False, watchdog=False,
                      doctor=False, sync=True)
    assert seen == [True]
    assert getattr(h, "_sync_repos_throttled", False) is False, "标记必须复位"


def test_sync_one_repo_reuses_three_state_diverged(repos, monkeypatch):
    """sync-repos 复用三态判定：干净分叉直接交人工裁决，连 merge 都不试。"""
    repos.local_commit()
    repos.push_card("T-9005")
    hive._ls_remote_cache.clear()

    attempts = []
    real_git = hive.git

    def spy_git(args, cwd, check=True, input=None):
        if args and args[0] == "merge":
            attempts.append(list(args))
        return real_git(args, cwd, check=check, input=input)

    monkeypatch.setattr(hive, "git", spy_git)
    out = hive.Hive(repos.a)._sync_one_repo("light", repos.a)
    assert out["status"] == "diverged"
    assert "人工裁决" in out["reason"]
    assert attempts == [], "干净分叉不该尝试 merge --ff-only"
