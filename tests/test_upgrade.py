"""集群升级：版本比较、主控发布、子节点检索与升级（2026-11-11）。

钉住四件事：
1. 版本按数值比较，不是字符串比较（0.16.10 > 0.16.9）；格式非法直接报错。
2. 主控 `upgrade publish` 把版本/commit 写进协调仓 release.json 并推送。
3. 落后节点 `upgrade check` 能看出可升级，`upgrade apply` 只做 ff 快进。
4. 三道安全闸门：脏工作区拒绝、policy=never 拒绝、policy=manual 需 --yes。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import hive
import pytest


@pytest.fixture(autouse=True)
def _isolate_upgrade_state(tmp_path, monkeypatch):
    """回滚点写到临时目录，别污染开发机的 ~/.hive/upgrade。"""
    monkeypatch.setenv("HIVE_UPGRADE_STATE", str(tmp_path / "upgrade"))


def _git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if check:
        assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout.strip()


def _commit_version(hive_dir, version: str, msg: str) -> str:
    """改协调仓里的 hive.py VERSION 并提交推送，返回新 commit。"""
    p = Path(hive_dir) / "hive.py"
    text = p.read_text(encoding="utf-8")
    text = re.sub(r'^VERSION\s*=\s*"[^"]+"', f'VERSION = "{version}"',
                  text, flags=re.M)
    p.write_text(text, encoding="utf-8")
    _git(hive_dir, "add", "hive.py")
    _git(hive_dir, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", msg)
    _git(hive_dir, "push", "origin", "HEAD:refs/heads/main")
    return _git(hive_dir, "rev-parse", "HEAD")


def _publish_and_rewind(world):
    """在 hive1 上发布 0.16.00，再把 hive2 退回 0.15.00 的旧提交。

    返回 (old_head, new_head)。old_head 是 hive2 升级前的 HEAD。
    """
    old_head = _commit_version(world.hive1, "0.15.00", "old version")
    world.sync()
    new_head = _commit_version(world.hive1, "0.16.00", "new version")
    world.sync()
    out = world.hive("upgrade", "publish", "--notes", "测试发布")
    assert "已发布" in out.stdout, out.stdout + out.stderr
    # hive2 回到旧版本，工作区干净
    _git(world.hive2, "fetch", "-q", "origin")
    _git(world.hive2, "reset", "--hard", "-q", old_head)
    return old_head, new_head


# --------------------------------------------------------------------------- #
# 1. 版本解析与比较
# --------------------------------------------------------------------------- #

def test_version_parse_format_compare():
    assert hive.parse_version("0.16.00") == (0, 16, 0)
    assert hive.parse_version("0.16.1") == (0, 16, 1)
    assert hive.format_version((1, 2, 3)) == "1.02.03"
    # 字符串比较会在这里出错（"0.16.10" < "0.16.9"），数值比较才对
    assert hive.compare_versions("0.16.10", "0.16.9") == 1
    assert hive.compare_versions("0.16.00", "0.15.99") == 1
    assert hive.compare_versions("0.16.00", "0.16.00") == 0


def test_version_rejects_bad_format():
    for bad in ("0.16", "v0.16.00", "0.16.00.1", "", None):
        with pytest.raises(hive.HiveError):
            hive.parse_version(bad)


def test_current_version_is_two_digit_patch():
    assert re.fullmatch(r"\d+\.\d{1,2}\.\d{2}", hive.VERSION), hive.VERSION


# --------------------------------------------------------------------------- #
# 2/3. 发布 → 检索 → 升级
# --------------------------------------------------------------------------- #

def test_publish_writes_release_and_visible_to_others(world):
    world.sync()
    before = _git(world.hive1, "rev-parse", "HEAD")
    out = world.hive("upgrade", "publish", "--notes", "首个集群版本")
    assert "已发布" in out.stdout

    data = json.loads(world.hive("upgrade", "status", "--json",
                                 dir=world.hive2).stdout)
    assert data["remote_version"] == hive.VERSION
    assert data["remote_commit"] == before, "release.commit 应是发布前的代码提交"
    assert data["notes"] == "首个集群版本"


def test_publish_refuses_unpushed_head(world):
    # 本地造一个未推送的提交：HEAD != origin/main，发布应被拒
    (world.hive1 / "local-only.txt").write_text("x\n", encoding="utf-8")
    _git(world.hive1, "add", "local-only.txt")
    _git(world.hive1, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", "local only")
    r = world.hive("upgrade", "publish", check=False)
    assert r.returncode != 0
    assert "origin/main" in (r.stdout + r.stderr)


def test_check_and_apply_upgrade(world, tmp_path):
    old_head, new_head = _publish_and_rewind(world)

    info = json.loads(world.hive("upgrade", "check", "--json",
                                 dir=world.hive2).stdout)
    assert info["local_version"] == "0.15.00"
    assert info["remote_version"] == "0.16.00"
    assert info["update_available"] is True
    assert info["behind"] is True

    r = world.hive("upgrade", "apply", "--yes", dir=world.hive2)
    assert r.returncode == 0, r.stdout + r.stderr
    # 升级到主控当前 HEAD（publish 之后又多了一个 release.json 提交）
    assert _git(world.hive2, "rev-parse", "HEAD") == \
        _git(world.hive1, "rev-parse", "HEAD")
    assert new_head != ""

    after = json.loads(world.hive("upgrade", "status", "--json",
                                  dir=world.hive2).stdout)
    assert after["local_version"] == "0.16.00"
    assert after["update_available"] is False

    # 回滚点落在 HIVE_UPGRADE_STATE 指向的目录（本测试 fixture 已隔离到 tmp）
    record = json.loads((tmp_path / "upgrade" / "last.json").read_text(
        encoding="utf-8"))
    assert record["from_version"] == "0.15.00"
    assert record["to_version"] == "0.16.00"
    assert record["from_commit"] == old_head


def test_apply_dry_run_does_not_move_head(world):
    _publish_and_rewind(world)
    head_before = _git(world.hive2, "rev-parse", "HEAD")
    r = world.hive("upgrade", "apply", "--yes", "--dry-run", dir=world.hive2)
    assert r.returncode == 0
    assert "预演" in r.stdout
    assert _git(world.hive2, "rev-parse", "HEAD") == head_before


# --------------------------------------------------------------------------- #
# 4. 安全闸门
# --------------------------------------------------------------------------- #

def test_apply_refuses_dirty_worktree(world):
    _publish_and_rewind(world)
    target = world.hive2 / "hive.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# dirty\n",
                     encoding="utf-8")
    r = world.hive("upgrade", "apply", "--yes", dir=world.hive2, check=False)
    assert r.returncode != 0
    assert "未提交改动" in (r.stdout + r.stderr)


def test_apply_respects_never_policy(world):
    _publish_and_rewind(world)
    r = world.hive("upgrade", "apply", "--yes", dir=world.hive2, check=False,
                   env={"HIVE_UPGRADE_POLICY": "never"})
    assert r.returncode != 0
    assert "never" in (r.stdout + r.stderr)


def test_apply_manual_policy_needs_yes(world):
    _publish_and_rewind(world)
    env = {"HIVE_UPGRADE_POLICY": "manual"}
    r = world.hive("upgrade", "apply", dir=world.hive2, check=False, env=env)
    assert r.returncode != 0
    assert "manual" in (r.stdout + r.stderr) and "--yes" in (r.stdout + r.stderr)
    # 确认后放行
    ok = world.hive("upgrade", "apply", "--yes", dir=world.hive2, env=env)
    assert ok.returncode == 0


def test_default_policy_is_follow(monkeypatch):
    monkeypatch.delenv("HIVE_UPGRADE_POLICY", raising=False)
    monkeypatch.setattr(hive, "_cluster_saved_config", lambda: {})
    assert hive.upgrade_policy() == "follow"


# --------------------------------------------------------------------------- #
# 5. 看板接口接线（静态断言，避免起真实 HTTP 服务）
# --------------------------------------------------------------------------- #

def test_dashboard_exposes_upgrade_endpoint():
    text = (Path(__file__).parents[1] / "dashboard" / "serve.py").read_text(
        encoding="utf-8")
    assert 'API_UPGRADE = "/api/upgrade"' in text
    assert "def serve_upgrade_get" in text
    assert "def serve_upgrade_post" in text
    assert 'UPGRADE_POST_ACTIONS = ("check", "apply", "publish")' in text

    js = (Path(__file__).parents[1] / "dashboard" / "app.js").read_text(
        encoding="utf-8")
    for key in ("upgrade-check", "upgrade-apply", "upgrade-publish"):
        assert key in js, f"看板缺少 {key} 按钮接线"
