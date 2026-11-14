"""T-0349 开发隔离自检：dev-cluster 实例与生产集群逐项错开。

本文件是纯 Python、可注入路径、**不依赖真实网络/真实集群**：
- 解析 ``scripts/dev-cluster.sh`` 的默认值与环境变量覆盖项，校验配置层隔离；
- 校验 ``HIVE_DEV_REPO`` 解析出的 ``hive.py`` 不在生产工作区目录下；
- 对照模块 ``Limits`` 的生产默认组播组/端口，确认开发候选值逐项错开。

``assert_isolated()`` 供后续第 1 批卡片复用：传入一组配置（dict，键与
dev-cluster.sh 导出/解析出的字段一致）即逐项断言，违例抛 ``AssertionError``。

生产基线常量（只用于比对，不读生产工作区文件）：
  * 端口 8787（看板）/ 8788（集群 HTTP）/ 8789（UDP 发现）/ 8790（MCP）
  * 组播组 239.255.42.1:47899
  * cluster_id = hive；state_dir = ~/.hive/cluster
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dev-cluster.sh"

# ---- 生产基线（来源 hive_ports.py / hive_cluster.py / hive_control/config.py） ----
PROD_PORTS = (8787, 8788, 8789, 8790)
PROD_MC_GROUP = "239.255.42.1"
PROD_MC_PORT = 47899
PROD_CLUSTER_ID = "hive"
PROD_STATE_DIR_NAME = Path(".hive") / "cluster"
# 生产协调仓工作区（任务卡约定的路径；开发副本不得落在它下面）。
PROD_REPO_CANDIDATES = (
    Path.home() / "dswork" / "hive",
    Path("G:/dswork/hive"),
    ROOT.parent / "hive",
)

# dev 脚本默认值（与脚本内 ``:-`` 默认一一对应；改脚本默认值要同步这里）。
DEV_DEFAULTS = {
    "port": 8888,
    "discovery_port": 8889,
    "cluster_id": "hive-dev",
    "state_dir": str(Path.home() / ".hive" / "dev-cluster"),
    "mc_group": "239.255.42.2",
    "mc_port": 47900,
    "project_tag": "hive-dev",
}


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _script_default(var: str, script: str | None = None) -> str:
    """从脚本里取 ``VAR="${...:-默认值}"`` 的默认值。"""
    text = script or _script_text()
    pattern = r'^\s*' + re.escape(var) + r'="\$\{[A-Z_]+:-([^}]+)\}"'
    m = re.search(pattern, text, re.MULTILINE)
    if not m:
        raise AssertionError(f"脚本里找不到 {var} 的默认值定义")
    return m.group(1)


def _resolve_repo_dir(env: dict[str, str] | None = None,
                       script: str | None = None) -> Path:
    """复刻脚本的 REPO_DIR 解析：HIVE_DEV_REPO 优先，否则脚本上级目录。"""
    text = script or _script_text()
    env = env or {}
    raw = env.get("HIVE_DEV_REPO")
    if raw:
        return Path(raw).expanduser().resolve()
    return (ROOT / "scripts" / "..").resolve()


def assert_isolated(cfg: dict[str, object]) -> None:
    """逐项断言一份 dev 实例配置与生产隔离。

    ``cfg`` 支持的键（缺省取 :data:`DEV_DEFAULTS`）：
      ``port`` / ``discovery_port`` / ``cluster_id`` / ``state_dir`` /
      ``mc_group`` / ``mc_port`` / ``project_tag`` / ``repo_dir``。

    违例抛 ``AssertionError``；全部通过返回 ``None``。供后续卡直接复用。
    """
    merged = {**DEV_DEFAULTS, **{k: v for k, v in cfg.items() if v is not None}}

    # 1. 端口：不出现生产 8787/8788/8789/8790。
    for key in ("port", "discovery_port"):
        port = int(merged[key])
        assert port not in PROD_PORTS, f"开发实例 {key}={port} 命中生产端口 {PROD_PORTS}"

    # 2. 组播：不出现生产组播组 / 端口。
    assert merged["mc_group"] != PROD_MC_GROUP, (
        f"开发实例组播组 {merged['mc_group']} 仍是生产组 {PROD_MC_GROUP}")
    assert int(merged["mc_port"]) != PROD_MC_PORT, (
        f"开发实例组播端口 {merged['mc_port']} 仍是生产 {PROD_MC_PORT}")

    # 3. cluster_id：必须是 hive-dev，且不等于生产 hive。
    assert merged["cluster_id"] != PROD_CLUSTER_ID, "cluster_id 仍是生产 hive"
    assert merged["cluster_id"] == "hive-dev"

    # 4. state_dir：不得是生产 ~/.hive/cluster。
    state_dir = Path(str(merged["state_dir"])).expanduser().resolve()
    assert str(PROD_STATE_DIR_NAME) not in str(state_dir), (
        f"state_dir {state_dir} 指向生产目录 ~/.hive/cluster")

    # 5. 代码隔离：repo_dir 下的 hive.py 不得在生产工作区目录里。
    repo_dir = Path(str(merged["repo_dir"])).expanduser().resolve() if merged.get("repo_dir") else None
    if repo_dir is not None:
        hive_py = (repo_dir / "hive.py").resolve()
        for prod in PROD_REPO_CANDIDATES:
            prod = prod.resolve()
            try:
                hive_py.relative_to(prod)
            except ValueError:
                continue
            raise AssertionError(
                f"HIVE_DEV_REPO 解析出的 {hive_py} 落在生产工作区 {prod} 下")

    # 6. project_tag 不得为空 / 不得与生产标签混淆（生产无 tag，dev 固定 hive-dev）。
    assert str(merged["project_tag"]), "project_tag 不能为空"


# --------------------------------------------------------------------------- #
# 脚本静态解析
# --------------------------------------------------------------------------- #

def test_script_defaults_match_dev_baseline():
    """脚本默认值必须与隔离基线一致（8888 / hive-dev / .2:47900 等）。"""
    text = _script_text()
    assert _script_default("DEV_PORT", text) == str(DEV_DEFAULTS["port"])
    assert _script_default("DEV_DISCOVERY_PORT", text) == str(DEV_DEFAULTS["discovery_port"])
    assert _script_default("DEV_CLUSTER_ID", text) == DEV_DEFAULTS["cluster_id"]
    assert _script_default("DEV_MC_GROUP", text) == DEV_DEFAULTS["mc_group"]
    assert _script_default("DEV_MC_PORT", text) == str(DEV_DEFAULTS["mc_port"])
    assert _script_default("DEV_PROJECT_TAG", text) == DEV_DEFAULTS["project_tag"]


def test_script_does_not_reference_prod_addrs():
    """脚本正文（除注释里的生产对照说明外）不得写死生产组播组/端口。"""
    text = _script_text()
    # 生产对照只允许出现在注释（# 开头行）与「生产 xxxx」说明里；
    # 实参/默认值不得出现生产地址。
    for prod_token in (PROD_MC_GROUP, str(PROD_MC_PORT)):
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert prod_token not in line, f"脚本可执行行出现生产组播 {prod_token}: {line}"
    for prod_port in PROD_PORTS:
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            # 允许「生产 8788」这类注释性对照行（含「生产」字样）。
            if "生产" in line:
                continue
            assert str(prod_port) not in line, (
                f"脚本可执行行出现生产端口 {prod_port}: {line}")


def test_env_overrides_resolve():
    """环境变量覆盖项能被解析，且覆盖后仍保持隔离。"""
    env = {
        "HIVE_DEV_REPO": str(ROOT),
        "HIVE_DEV_MC_GROUP": "239.255.42.3",
        "HIVE_DEV_MC_PORT": "47901",
        "HIVE_DEV_CLUSTER_ID": "hive-dev",
        "HIVE_DEV_PORT": "8899",
    }
    repo_dir = _resolve_repo_dir(env)
    cfg = {
        "repo_dir": repo_dir,
        "mc_group": env["HIVE_DEV_MC_GROUP"],
        "mc_port": int(env["HIVE_DEV_MC_PORT"]),
        "cluster_id": env["HIVE_DEV_CLUSTER_ID"],
        "port": int(env["HIVE_DEV_PORT"]),
    }
    assert_isolated(cfg)


def test_default_dev_repo_is_not_prod_when_env_unset(tmp_path):
    """不传 HIVE_DEV_REPO 时默认指向脚本上级目录；在 worktree/开发副本下不属生产。"""
    repo_dir = _resolve_repo_dir({})
    hive_py = repo_dir / "hive.py"
    assert hive_py.exists(), f"默认 REPO_DIR 下找不到 hive.py: {hive_py}"
    for prod in PROD_REPO_CANDIDATES:
        prod = prod.resolve()
        try:
            hive_py.resolve().relative_to(prod)
        except ValueError:
            continue
        pytest.fail(f"默认 REPO_DIR 落在生产工作区 {prod} 下")


def test_prod_repo_windows_path_is_rejected():
    """显式把 HIVE_DEV_REPO 指到 Windows 现场生产工作区时必须拦下。"""
    prod = Path("G:/dswork/hive")
    with pytest.raises(AssertionError, match="生产工作区"):
        assert_isolated({"repo_dir": prod})


def test_prod_cluster_id_is_rejected():
    with pytest.raises(AssertionError, match="cluster_id"):
        assert_isolated({"cluster_id": PROD_CLUSTER_ID})


def test_prod_mc_group_is_rejected():
    with pytest.raises(AssertionError, match="组播组"):
        assert_isolated({"mc_group": PROD_MC_GROUP})


def test_prod_port_is_rejected():
    with pytest.raises(AssertionError, match="生产端口"):
        assert_isolated({"port": 8788})


# --------------------------------------------------------------------------- #
# 控制面组播配置口径（纯配置断言；Limits 环境变量装配归 T-0351 落地）
# --------------------------------------------------------------------------- #

def test_limits_defaults_keep_prod_baseline():
    """未被覆盖时，模块 Limits 默认组播组/端口就是生产基线（对照口径）。"""
    Limits = pytest.importorskip("hive_control.config").Limits
    defaults = Limits()
    assert defaults.multicast_group == PROD_MC_GROUP
    assert defaults.multicast_port == PROD_MC_PORT


def test_dev_env_values_do_not_match_prod_baseline():
    """开发侧候选组播组/端口与生产基线逐项不等（脚本默认值口径）。"""
    assert DEV_DEFAULTS["mc_group"] != PROD_MC_GROUP
    assert int(DEV_DEFAULTS["mc_port"]) != PROD_MC_PORT
