"""多机 agent 路由（host 字段）。

背景：hive 早期只在单机跑，agent 配置里的 `repos` 路径是"这台机器"的本地
路径。多机协作后，同一仓名会在不同机器上登记成不同路径（FreeBSD 的
/home/... 与 Windows 的 G:/...），于是需要：

1. `host` 字段标明 agent 归属哪台机器（默认取 hostname）；
2. `repos()` 解析时优先本机 agent 声明的、真实存在的路径；
3. `dispatch --host <机器>` 只把卡派给指定机器上的 agent。

没有 host 时全部退化为旧行为（路径存在优先），所以这里既测新字段，也
保证老配置不炸。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import hive as H


def _git_init(directory: Path) -> None:
    subprocess.run(["git", "init", "-q", str(directory)], check=True)


def _write_agent(directory: Path, name: str, host: str, repo_path: str,
                 mode: str = "exec") -> None:
    (directory / "agents").mkdir(parents=True, exist_ok=True)
    cfg = {
        "name": name,
        "host": host,
        "mode": mode,
        "manual": mode == "manual",
        "abilities": ["code"],
        "capabilities": [],
        "repos": {"hive": repo_path},
    }
    (directory / "agents" / f"{name}.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def test_current_host_nonempty():
    assert H.current_host(), "current_host() 取不到机器名，多机路由会失效"


def test_every_agent_config_declares_host():
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "agents").glob("*.json"))
    assert files, "agents/ 为空"
    for p in files:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        assert cfg.get("host"), f"{p.name} 缺少 host 字段（多机路由会退化）"


def test_repos_prefers_local_host(tmp_path):
    """同名仓：本机路径即使不是第一个声明，也应被选中。"""
    _git_init(tmp_path)
    real = tmp_path / "real-hive"
    real.mkdir()
    # 故意先写"别的机器"的路径，且它不存在
    _write_agent(tmp_path, "zzz-remote", "another-machine", "/no/such/hive")
    _write_agent(tmp_path, "aaa-local", H.current_host(), str(real))

    repos = H.Hive(str(tmp_path)).repos()
    assert repos["hive"] == real


def test_repos_falls_back_when_no_path_exists(tmp_path):
    """本机路径都不存在时，退回首个声明，至少报错信息能带出路径。"""
    _git_init(tmp_path)
    _write_agent(tmp_path, "a", "another-machine", "/no/such/hive")
    _write_agent(tmp_path, "b", H.current_host(), "/also/missing")
    repos = H.Hive(str(tmp_path)).repos()
    assert str(repos["hive"]) in {"/no/such/hive", "/also/missing"}


def test_dispatch_only_host(world):
    """dispatch --host 只派给指定机器的 agent。"""
    world.add_agent("a-local", mode="exec", host="fb98",
                    repos={"hive": world.code_clone("a-local")})
    world.add_agent("a-remote", mode="exec", host="win10",
                    repos={"hive": world.code_clone("a-remote")})
    world.add_task("T-0001", repo="hive", paths=["src/a.py"])

    out = world.hive("dispatch", "--host", "win10").stdout
    assert "a-remote" in out, out
    assert "a-local" not in out, out

    out2 = world.hive("dispatch", "--host", "fb98").stdout
    assert "a-local" in out2, out2
    assert "a-remote" not in out2, out2
