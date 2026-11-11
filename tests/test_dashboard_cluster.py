"""看板对常驻集群的只读桥接：/api/cluster + /api/status 的进度合并。

集群是可选件：没起时 /api/cluster 返回 running=false，/api/status 仍要正常；
起了时集群里更新的进度要能覆盖 git 快照里的旧 pushed_at。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASH))

import serve  # noqa: E402

APP_JS = DASH / "app.js"
STYLE_CSS = DASH / "style.css"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve_once(status_path):
    srv = serve.ThreadingHTTPServer(("127.0.0.1", 0),
                                     serve.build_handler(str(status_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1], srv.shutdown


def _get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _write_cluster_state(state_dir: Path, snapshot: dict):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(
        json.dumps({"snapshot": snapshot}, ensure_ascii=False), encoding="utf-8")


def test_api_cluster_offline_returns_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_CLUSTER_STATE", str(tmp_path / "cluster"))
    monkeypatch.setenv("HIVE_CLUSTER_PORT", str(_free_port()))
    _write_cluster_state(tmp_path / "cluster", {
        "cluster_id": "hive", "term": 3, "role": "follower",
        "leader": {"id": "a", "name": "a", "url": "http://10.0.0.1:8788"},
        "self": {"id": "b", "name": "b"}, "nodes": [], "alive": 1, "known": 2,
        "progress": {}, "leases": {},
    })
    status = tmp_path / "status.json"
    status.write_text("[]", encoding="utf-8")
    base, shutdown = _serve_once(status)
    try:
        code, data = _get_json(base + "/api/cluster")
    finally:
        shutdown()
    assert code == 200
    assert data["running"] is False
    assert data["state"]["leader"]["name"] == "a"
    assert data["state"]["term"] == 3


def test_status_merges_cluster_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_CLUSTER_STATE", str(tmp_path / "cluster"))
    monkeypatch.setenv("HIVE_CLUSTER_PORT", str(_free_port()))
    _write_cluster_state(tmp_path / "cluster", {
        "cluster_id": "hive", "term": 4, "role": "leader",
        "leader": {"id": "a", "name": "a"},
        "self": {"id": "a", "name": "a"}, "nodes": [], "alive": 1, "known": 1,
        "progress": {
            "T-0001": {"task": "T-0001", "agent": "a", "state": "running",
                        "progress": 77, "step": "跑 verify",
                        "message": "集群实时进度",
                        "updated_at": "2099-01-01T00:00:00+08:00"},
        },
        "leases": {},
    })
    status = tmp_path / "status.json"
    status.write_text(json.dumps([{
        "id": "T-0001", "title": "示例", "state": "assigned", "owner": "a",
        "pushed_at": "2000-01-01T00:00:00+08:00",
    }], ensure_ascii=False), encoding="utf-8")
    base, shutdown = _serve_once(status)
    try:
        code, data = _get_json(base + "/api/status")
    finally:
        shutdown()
    assert code == 200
    row = data[0]
    assert row["progress"] == 77
    assert row["push_step"] == "跑 verify"
    assert row["push_message"] == "集群实时进度"
    assert row["push_source"] == "cluster"


# --------------------------------------------------------------------------- #
# T-0217：集群面板（节点表 + 心跳 age + 三档状态灯）
# --------------------------------------------------------------------------- #
NOW = 1_800_000_000.0
CLUSTER_JS_NAMES = [
    "esc", "clusterAgeSeconds", "clusterAge", "clusterThresholds",
    "clusterNodeStatus", "clusterStatusView", "clusterEndpoint",
    "clusterPriority", "clusterNodeIp", "clusterTaskCount",
    "renderClusterSummary", "renderClusterPanel",
]
CLUSTER_JS_CONSTS = ["CLUSTER_EST_PEER_S", "CLUSTER_EST_DEAD_S"]

_NODE_RENDER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.HIVE_APP_JS, 'utf8');
function grabConst(name) {
  const re = new RegExp('var\\s+' + name + '\\s*=\\s*([^;]+);');
  const m = re.exec(src);
  if (!m) throw new Error('app.js 里找不到常量 ' + name);
  return 'var ' + name + ' = ' + m[1] + ';';
}
function grab(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
  const m = re.exec(src);
  if (!m) throw new Error('app.js 里找不到函数 ' + name);
  let i = m.index + m[0].length - 1, depth = 0;
  for (; i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) { i++; break; } }
  }
  return src.slice(m.index, i);
}
const consts = JSON.parse(process.env.HIVE_CLUSTER_JS_CONSTS || '[]');
const names = JSON.parse(process.env.HIVE_CLUSTER_JS_NAMES);
const api = new Function(consts.map(grabConst).join('\n') + '\n' +
  names.map(grab).join('\n') +
  '\nreturn {' + names.join(',') + '};')();
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = api.renderClusterPanel(input.snapshot, input.tasks, input.now);
const summary = api.renderClusterSummary(input.snapshot, input.open);
const ages = (input.snapshot.nodes || []).map(function (n) {
  return api.clusterAge(n.last_seen_epoch, input.now);
});
process.stdout.write(JSON.stringify({html: html, summary: summary, ages: ages}));
"""


def _snapshot():
    """三节点快照：alive / suspect / dead 各一，age 分别为 2s / 30s / 2m。"""
    return {
        "protocol": "1",
        "cluster_id": "hive-prod",
        "term": 7,
        "role": "follower",
        "leader": {"id": "n1", "name": "fb98", "url": "http://10.0.0.11:8788"},
        "self": {"id": "n2", "name": "fb99"},
        "nodes": [
            {"id": "n1", "name": "fb98", "url": "http://10.0.0.11:8788", "port": 8788,
             "role": "leader", "status": "alive", "last_seen_epoch": NOW - 2,
             "electable": True, "version": "1.0"},
            {"id": "n2", "name": "fb99", "url": "http://10.0.0.12:8788", "port": 8788,
             "role": "follower", "status": "suspect", "last_seen_epoch": NOW - 30,
             "electable": True, "version": "1.0"},
            {"id": "n3", "name": "fb9a", "url": "http://10.0.0.13:8788", "port": 8788,
             "role": "follower", "status": "dead", "last_seen_epoch": NOW - 120,
             "electable": True, "version": "1.0"},
        ],
        "alive": 1,
        "known": 3,
        "quorum": 2,
    }


TASKS = [
    {"id": "T-1", "owner": "fb98", "state": "running"},
    {"id": "T-2", "owner": "fb98", "state": "assigned"},
    {"id": "T-3", "owner": "fb98", "state": "done"},
    {"id": "T-4", "owner": "fb99", "state": "running"},
    {"id": "T-5", "owner": "fb9a", "state": "blocked"},
]


def _render_cluster(snapshot, tasks=None, open_=False):
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，无法执行 app.js 里的纯渲染函数")
    payload = json.dumps({"snapshot": snapshot, "tasks": tasks or [],
                          "now": NOW, "open": open_}, ensure_ascii=False)
    env = dict(os.environ)
    env["HIVE_APP_JS"] = str(APP_JS)
    env["HIVE_CLUSTER_JS_NAMES"] = json.dumps(CLUSTER_JS_NAMES)
    env["HIVE_CLUSTER_JS_CONSTS"] = json.dumps(CLUSTER_JS_CONSTS)
    proc = subprocess.run([node, "-e", _NODE_RENDER], input=payload,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=30)
    assert proc.returncode == 0, (
        "node 渲染集群面板失败：\nstdout=%s\nstderr=%s" % (proc.stdout, proc.stderr))
    return json.loads(proc.stdout)


def test_cluster_panel_renders_three_nodes_and_ages():
    """3 节点快照必须渲染出三档状态灯 + 2s/30s/2m 心跳 age + 每台任务数。"""
    out = _render_cluster(_snapshot(), TASKS)
    html = out["html"]

    assert html.count('class="cluster-lamp"') == 3, "每个节点必须有一盏状态灯"
    for cls, label in (("alive", "在线"), ("suspect", "可疑"), ("dead", "离线")):
        assert 'class="cluster-status %s"' % cls in html, "缺 %s 状态灯" % cls
        assert label in html, "缺 %s 文案" % label

    assert out["ages"] == ["2s", "30s", "2m"], (
        "心跳 age 换算错误：%r" % (out["ages"],))
    for age in ("2s", "30s", "2m"):
        assert ">%s<" % age in html, "渲染结果里没有 age 文案 %s" % age

    # 本机标记只出现在 self 那一行（fb99）
    assert html.count('class="cluster-self"') == 1
    assert "fb99</b><span class=\"cluster-self\">本机</span>" in html

    # 头部：主节点地址可复制 + term / quorum / 集群 ID
    assert 'data-cluster-copy="http://10.0.0.11:8788"' in html
    assert "fb98" in html and "hive-prod" in html
    assert "<dd>7</dd>" in html and "<dd>2</dd>" in html

    # 任务数：owner 对得上的在飞卡；done / blocked 不算
    assert '<span class="cluster-tasks">2</span>' in html
    assert '<span class="cluster-tasks">1</span>' in html
    assert '<span class="cluster-tasks">0</span>' in html

    # 后端没下发阈值时，卡片上必须注明估算口径
    assert "按 15s / 60s 估" in html

    summary = out["summary"]
    assert 'aria-expanded="false"' in summary and "集群：fb98" in summary
    opened = _render_cluster(_snapshot(), TASKS, open_=True)
    assert 'aria-expanded="true"' in opened["summary"]


def test_cluster_panel_shows_leader_priority_and_ip():
    """节点表新增「优先级 / IP」两列：读自述 priority 与 host，不新增接口。"""
    snap = _snapshot()
    snap["nodes"][0].update({"priority": 2, "host": "10.0.0.11"})
    snap["nodes"][1].update({"priority": 0, "host": "0.0.0.0"})
    snap["nodes"][2].update({"priority": None})   # 老版本节点没上报优先级
    snap["nodes"][2].pop("host", None)            # 只带 url + port，走兜底

    html = _render_cluster(snap, TASKS)["html"]
    assert '>优先级</th>' in html and '>IP</th>' in html
    rows = html.split('<tr class="cluster-node">')[1:]
    assert len(rows) == 3

    # 值大者优先：2 原样显示，IP 来自 host
    assert 'class="cluster-prio mono"' in rows[0] and '>2</td>' in rows[0]
    assert 'title="http://10.0.0.11:8788">10.0.0.11</span>' in rows[0]

    # 0 = 不参选主控，单独标色；只拿到 0.0.0.0 时不展示 IP（"那就算了"）
    assert 'cluster-prio mono zero' in rows[1] and '>0</td>' in rows[1]
    assert '<span class="muted"' in rows[1] and '>0.0.0.0</span>' not in rows[1]

    # 没上报优先级按默认 1；host 缺失但 url 端口与自述 port 一致时兜底出 IP
    assert '该节点未上报，按默认 1 显示' in rows[2] and '>1</td>' in rows[2]
    assert '>10.0.0.13</span>' in rows[2]

    assert html.count('class="cluster-prio') == 3
    assert html.count('class="cluster-ip mono"') == 3


def test_cluster_panel_ip_fallback_rejects_mismatched_port():
    """残留/伪造记录只有 url 时，端口对不上自述 port 就不拿它的 IP 冒充。"""
    snap = _snapshot()
    snap["nodes"][0].pop("host", None)
    snap["nodes"][0]["port"] = 8788
    snap["nodes"][0]["url"] = "http://10.0.0.99:9999"
    html = _render_cluster(snap)["html"]
    assert "10.0.0.99" not in html
    assert "没有可用 IP" in html


def test_cluster_status_falls_back_to_age_with_backend_timeouts():
    """旧快照缺 status 时，用后端下发的 peer_timeout/dead_timeout 按 age 分档。"""
    snap = _snapshot()
    snap["peer_timeout"] = 15
    snap["dead_timeout"] = 60
    for n in snap["nodes"]:
        n.pop("status", None)

    out = _render_cluster(snap, TASKS)
    html = out["html"]
    assert 'class="cluster-status alive"' in html
    assert 'class="cluster-status suspect"' in html
    assert 'class="cluster-status dead"' in html
    assert "peer_timeout / dead_timeout 分档" in html
    assert "按 15s / 60s 估" not in html


def test_cluster_status_estimates_15s_60s_when_timeouts_missing():
    snap = _snapshot()
    for n in snap["nodes"]:
        n.pop("status", None)
    out = _render_cluster(snap, TASKS)
    html = out["html"]
    assert 'class="cluster-status alive"' in html   # 2s
    assert 'class="cluster-status suspect"' in html  # 30s
    assert 'class="cluster-status dead"' in html     # 120s
    assert "按 15s / 60s 估" in html


def test_cluster_panel_css_is_wired_and_mobile_keeps_priority_and_ip():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".cluster-panel" in css
    assert ".cluster-status.alive" in css and ".cluster-status.dead" in css
    assert ".cluster-lamp" in css
    assert "@media (max-width: 768px)" in css
    # 八列都有宽度规则
    for idx in ("1", "2", "3", "4", "5", "6", "7", "8"):
        assert ".cluster-nodes th:nth-child(%s)" % idx in css
        assert ".cluster-nodes td:nth-child(%s)" % idx in css
    # ≤768px 隐藏角色/心跳/任务/版本，保留名称 + 状态 + 优先级 + IP
    mobile = css.split("@media (max-width: 768px) {")[1].split("@media")[0]
    hidden = ""
    for line in mobile.splitlines():
        if line.strip().startswith(".cluster-nodes"):
            hidden += line
        if "display: none" in line:
            break
    for idx in ("2", "6", "7", "8"):
        assert "nth-child(%s)" % idx in hidden
    for idx in ("4", "5"):
        assert "nth-child(%s)" % idx not in hidden
    assert ".cluster-nodes th:nth-child(4), .cluster-nodes td:nth-child(4) { width: 12%; }" in mobile
    assert ".cluster-nodes th:nth-child(5), .cluster-nodes td:nth-child(5) { width: 30%; }" in mobile
    assert ".cluster-prio.zero" in css and ".cluster-ip" in css
    assert "overflow-x: hidden" in css


def test_cluster_panel_reuses_existing_api_and_fields():
    """不新增接口：只读 /api/cluster，age/status 都从已有字段来。"""
    src = APP_JS.read_text(encoding="utf-8")
    assert "fetch('/api/cluster'" in src
    for field in ("last_seen_epoch", "peer_timeout", "dead_timeout",
                  "cluster_id", "quorum", "status", "priority", "host", "url"):
        assert field in src
    assert "/api/cluster/nodes" not in src, "不许新增后端接口"
