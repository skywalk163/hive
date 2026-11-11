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
    # rollout（主控一键下发到各节点）是 0.16.02 新增的动作。
    assert ('UPGRADE_POST_ACTIONS = ("check", "apply", "publish", "rollout")'
            in text)
    # rollout 会改写整个集群的代码仓，必须和 publish 同级只许 controller。
    assert 'action in ("publish", "rollout") and not self.is_controller(who)' in text

    js = (Path(__file__).parents[1] / "dashboard" / "app.js").read_text(
        encoding="utf-8")
    for key in ("upgrade-check", "upgrade-apply", "upgrade-publish",
                "upgrade-rollout"):
        assert key in js, f"看板缺少 {key} 按钮接线"


# --------------------------------------------------------------------------- #
# 5. 主控一键节点升级（rollout）：权限闸门 + 只下发落后节点
# --------------------------------------------------------------------------- #

def _fake_state(monkeypatch, nodes, self_name="fb98", self_id="self-1"):
    """把 hive.cluster_state() 换成一个固定节点表，不起真集群。"""
    state = {
        "node": self_id,
        "self": {"id": self_id, "name": self_name, "version": "0.16.00"},
        "nodes": nodes,
    }
    monkeypatch.setattr(hive, "cluster_state", lambda: state)
    return state


def _node(nid, name, version, status="alive", url="http://10.0.0.9:8788"):
    return {"id": nid, "name": name, "version": version, "status": status,
            "url": url}


def test_rollout_requires_published_version(world, monkeypatch):
    """没有 release.json 时不能「一键升级」——否则下发的是个空版本。"""
    h = hive.Hive(world.hive1)
    monkeypatch.setattr(h, "_upgrade_info",
                        lambda: {"remote_version": "", "update_available": False})
    with pytest.raises(hive.HiveError) as ei:
        h._upgrade_rollout({"remote_version": ""})
    assert "publish" in str(ei.value)


def test_rollout_skips_up_to_date_and_offline(world, monkeypatch):
    """只对「落后且在线」的节点下发；版本一致/离线的跳过且不算失败。"""
    h = hive.Hive(world.hive1)
    _fake_state(monkeypatch, [
        _node("self-1", "fb98", "0.16.00"),
        _node("n-old", "win-old", "0.15.00"),
        _node("n-new", "win-new", "0.16.00"),
        _node("n-dead", "win-dead", "0.15.00", status="dead"),
    ])
    sent = []

    def fake_http(method, path, payload=None, timeout=1.0, url=""):
        sent.append((path, url, payload))
        return 200, {"ok": True}

    monkeypatch.setattr(hive, "_cluster_http", fake_http)
    # 本机（主控）自己也要升：替换成本地快进成功。
    monkeypatch.setattr(h, "cmd_upgrade", lambda *a, **kw: 0)

    rc = h._upgrade_rollout({"remote_version": "0.16.00"}, as_json=True)
    assert rc == 0
    # 只有落后的那台被下发；本机走本地分支，不发 HTTP
    assert [u for _p, u, _pl in sent] == ["http://10.0.0.9:8788"]
    assert sent[0][2]["requester"] == "self-1"
    assert sent[0][2]["version"] == "0.16.00"


def test_rollout_reports_failure_and_returns_nonzero(world, monkeypatch):
    """某台升级失败要如实报错并返回非 0，不能笼统说「完成」。"""
    h = hive.Hive(world.hive1)
    _fake_state(monkeypatch, [
        _node("self-1", "fb98", "0.16.00"),
        _node("n-old", "win-old", "0.15.00"),
    ])
    monkeypatch.setattr(hive, "_cluster_http",
                        lambda *a, **kw: (500, {"ok": False,
                                                "error": "脏工作区"}))
    monkeypatch.setattr(h, "cmd_upgrade", lambda *a, **kw: 0)
    rc = h._upgrade_rollout({"remote_version": "0.16.00"}, as_json=True)
    assert rc == 1


def test_rollout_restart_schedules_on_local_branch(world, monkeypatch):
    """主控本机分支也必须安排重启（远端那支由对端 handle_upgrade_trigger 安排）。

    现场：加了 --restart 却只有子节点重启，主控自己仍跑旧代码 —— 集群面板
    上主控那一行版本永远不变。根因是本地分支只调了 cmd_upgrade 就 continue，
    压根没走到安排重启那一步。
    """
    h = hive.Hive(world.hive1)
    _fake_state(monkeypatch, [_node("self-1", "fb98", "0.15.00")],
                self_name="fb98", self_id="self-1")
    scheduled = []
    monkeypatch.setattr(h, "cmd_upgrade", lambda *a, **kw: 0)
    monkeypatch.setattr(h, "_request_local_restart",
                        lambda: scheduled.append(1) or True)
    rc = h._upgrade_rollout({"remote_version": "0.16.00"}, as_json=True,
                            restart=True)
    assert rc == 0
    assert scheduled == [1], "主控自己没有安排重启"


def test_rollout_without_restart_leaves_process_alone(world, monkeypatch):
    """不带 --restart 时不能去杀别人的守护进程。"""
    h = hive.Hive(world.hive1)
    _fake_state(monkeypatch, [_node("self-1", "fb98", "0.15.00")],
                self_name="fb98", self_id="self-1")
    scheduled = []
    monkeypatch.setattr(h, "cmd_upgrade", lambda *a, **kw: 0)
    monkeypatch.setattr(h, "_request_local_restart",
                        lambda: scheduled.append(1) or True)
    assert h._upgrade_rollout({"remote_version": "0.16.00"}, as_json=True) == 0
    assert scheduled == [], "没要 --restart 却安排了重启"


def test_rollout_refuses_when_local_version_not_published(world, monkeypatch):
    """本地改了 VERSION 却没 publish：拒绝下发，并指明先 publish。

    现场（2026-11-11）：顶栏显示 0.16.03，按「一键节点升级」却提示
    「升级到 0.16.02」。根因是 rollout 取 release.json 的 remote_version，
    而主控自己把 VERSION 提到 0.16.03 后没重新 publish。旧代码还会把主控
    无条件标成「落后」，打出 `fb98 0.16.03 → 0.16.02` 这种像降级的行，
    并真的跑一次 apply（只回「已是最新」）。
    """
    h = hive.Hive(world.hive1)
    _fake_state(monkeypatch, [_node("self-1", "fb98", "0.16.03")],
                self_name="fb98", self_id="self-1")
    sent = []
    monkeypatch.setattr(hive, "_cluster_http",
                        lambda *a, **kw: sent.append(1) or (200, {"ok": True}))
    monkeypatch.setattr(h, "cmd_upgrade", lambda *a, **kw: sent.append(1) or 0)

    rc = h._upgrade_rollout({"local_version": "0.16.03",
                             "remote_version": "0.16.02"}, as_json=True)
    assert rc == 1, "版本错位时不该继续下发"
    assert not sent, "版本错位时仍然下发了升级"


def test_rollout_allows_when_versions_match(world, monkeypatch):
    """版本一致时正常下发（别把上面那道闸做成一律拒绝）。"""
    h = hive.Hive(world.hive1)
    _fake_state(monkeypatch, [
        _node("self-1", "fb98", "0.16.03"),
        _node("n-old", "win-old", "0.16.02"),
    ], self_name="fb98", self_id="self-1")
    hits = []
    monkeypatch.setattr(hive, "_cluster_http",
                        lambda *a, **kw: hits.append(kw.get("url")) or
                        (200, {"ok": True}))
    monkeypatch.setattr(h, "cmd_upgrade", lambda *a, **kw: 0)
    rc = h._upgrade_rollout({"local_version": "0.16.03",
                             "remote_version": "0.16.03"}, as_json=True)
    assert rc == 0
    assert hits == ["http://10.0.0.9:8788"]


def test_rollout_skips_master_when_already_on_target(world, monkeypatch):
    """主控自身版本与发布版本相同就跳过 —— 不跑无意义的 apply，也不打
    `fb98 0.16.03 → 0.16.03` 这种多余行。"""
    h = hive.Hive(world.hive1)
    _fake_state(monkeypatch, [
        _node("self-1", "fb98", "0.16.03"),
        _node("n-old", "win-old", "0.16.02"),
    ], self_name="fb98", self_id="self-1")
    applied = []
    hits = []
    monkeypatch.setattr(h, "cmd_upgrade",
                        lambda *a, **kw: applied.append(1) or 0)
    monkeypatch.setattr(hive, "_cluster_http",
                        lambda *a, **kw: hits.append(kw.get("url")) or
                        (200, {"ok": True}))
    rc = h._upgrade_rollout({"local_version": "0.16.03",
                             "remote_version": "0.16.03"}, as_json=True)
    assert rc == 0
    assert applied == [], "主控已是最新却仍跑了 apply"
    assert hits == ["http://10.0.0.9:8788"], "只该下发真正落后的那台"
