"""pytest 夹具：每个测试一个临时世界（代码仓 + 协调仓 + 两个 worker 克隆）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HIVE = ROOT / "hive.py"
FAKE = ROOT / "fake_agent.py"


def sh(cmd, cwd=None, check=True, env=None):
    p = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                       text=True, capture_output=True, env=env)
    if check and p.returncode != 0:
        raise AssertionError(f"命令失败({p.returncode}): {cmd}\n{p.stdout}\n{p.stderr}")
    return p


def _tune_bare(path):
    """测试用裸仓：关掉自动 gc，避免 push 后的后台 gc 和 clone/fetch 打架。"""
    for k, v in (("gc.auto", "0"), ("receive.autogc", "false"),
                 ("maintenance.auto", "false"), ("gc.autoDetach", "false")):
        sh(["git", "--git-dir", path, "config", k, v], check=False)


def _tune_worktree(path):
    sh(["git", "-C", path, "config", "gc.auto", "0"], check=False)


class World:
    def __init__(self, root: Path):
        self.root = root
        self.git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        self.code_origin = root / "light.git"
        self.hive_origin = root / "hive.git"
        self.hive1 = root / "hive-a1"
        self.hive2 = root / "hive-a2"
        self._clones: dict[str, Path] = {}
        self._init_code()
        self._init_hive()

    # -- 仓库 ------------------------------------------------------------- #
    def _init_code(self):
        sh(["git", "init", "--bare", "-b", "main", self.code_origin])
        _tune_bare(self.code_origin)
        seed = self.root / "light-seed"
        sh(["git", "init", "-b", "main", seed])
        (seed / "README.md").write_text("# light\n", encoding="utf-8")
        (seed / "src").mkdir()
        (seed / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        sh(["git", "add", "-A"], cwd=seed)
        sh(["git", "commit", "-m", "init"], cwd=seed, env=self.git_env)
        sh(["git", "remote", "add", "origin", self.code_origin], cwd=seed)
        sh(["git", "push", "-u", "origin", "main"], cwd=seed)

    def _init_hive(self):
        sh(["git", "init", "--bare", "-b", "main", self.hive_origin])
        _tune_bare(self.hive_origin)
        self.hive("init", "--remote", self.hive_origin, dir=self.hive1)
        _tune_worktree(self.hive1)
        sh(["git", "clone", "-q", self.hive_origin, self.hive2])
        _tune_worktree(self.hive2)

    def code_clone(self, name: str) -> Path:
        if name not in self._clones:
            dst = self.root / f"light-{name}"
            sh(["git", "clone", "-q", self.code_origin, dst])
            _tune_worktree(dst)
            self._clones[name] = dst
        return self._clones[name]

    # -- hive 命令 --------------------------------------------------------- #
    def hive(self, *args, dir: Path | None = None, check=True, env=None):
        return sh([sys.executable, HIVE, "--dir", dir or self.hive1, *args],
                  check=check, env=env)

    def hive_json(self, *args, dir: Path | None = None):
        return json.loads(self.hive(*args, "--json", dir=dir).stdout)

    def add_agent(self, name: str, mode="exec", caps=(), repos=None,
                  command="", launch="", worktrees="", host=""):
        repos = repos or {"light": self.code_clone(name)}
        args = ["agent", "add", "--name", name, "--mode", mode]
        if host:
            args += ["--host", host]
        if caps:
            args += ["--capabilities", *caps]
        if command:
            args += ["--command", command]
        if launch:
            args += ["--launch", launch]
        if worktrees:
            args += ["--worktrees", worktrees]
        for repo, path in repos.items():
            args += ["--repo", f"{repo}={path}"]
        self.hive(*args)

    def add_task(self, tid: str, title="任务", repo="light", paths=None,
                 needs=None, depends=None, verify=None, base="main", body="测试任务"):
        args = ["task", "new", "--id", tid, "--title", title, "--repo", repo,
                "--base", base, "--body", body]
        if paths:
            args += ["--paths", *paths]
        if needs:
            args += ["--needs", *needs]
        if depends:
            args += ["--depends", *depends]
        for v in verify or []:
            args += ["--verify", v]
        self.hive(*args)

    def sync(self):
        """把 hive1 的工作区同步到 origin/main。

        卡片/agent 都存在工作区文件里，光 fetch 不更新；别的 clone 推了东西之后
        不 reset 就读到旧卡。
        """
        sh(["git", "-C", str(self.hive1), "fetch", "--prune", "origin"], check=False,
           env=self.git_env)
        sh(["git", "-C", str(self.hive1), "reset", "--hard", "-q", "origin/main"],
           check=False, env=self.git_env)

    def card(self, tid: str) -> dict:
        from hive import parse_card  # noqa: WPS433  (测试进程里动态导入)
        data, body = parse_card(self.hive1 / "tasks" / f"{tid}.md")
        data["_body"] = body
        return data

    # -- worker ------------------------------------------------------------ #
    def run_worker(self, agent: str, hive_dir: Path | None = None,
                   state: str | None = None, env=None):
        return sh([sys.executable, HIVE, "--dir", hive_dir or self.hive1,
                   "work", "--agent", agent, "--once",
                   "--state-dir", state or self.root / f"state-{agent}"],
                  check=False, env={**os.environ, "FAKE_SLEEP": "0", **(env or {})})

    def code_refs(self, pattern: str = "refs/heads/task/*") -> set[str]:
        """代码仓（light.git）里的分支。task/verify 分支推的是代码仓，不是协调仓。"""
        out = sh(["git", "--git-dir", self.code_origin, "for-each-ref",
                  "--format=%(refname)", pattern]).stdout
        return {r.rsplit("/", 1)[-1] for r in out.split()}

    def result_refs(self, tid: str) -> list[str]:
        return [r for r in self.refs("refs/heads/res/*") if r.endswith(f"/{tid}")]

    def refs(self, pattern: str) -> list[str]:
        out = sh(["git", "--git-dir", self.hive_origin, "for-each-ref",
                  "--format=%(refname)", pattern]).stdout
        return out.split()


@pytest.fixture()
def world(tmp_path) -> World:
    return World(tmp_path)


@pytest.fixture()
def fake_cmd() -> str:
    return f"{sys.executable} {FAKE}"
