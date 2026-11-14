"""T-0354 看板同步健康度与增量订阅。

背景：交办方在「hive 集群面板」上看到的那句
「本地协调仓落后 origin/main……执行 git pull（或 hive sync-repos）后刷新看板」，
把三种完全不同的处境混成了一句话。这张卡把横幅改成**同步健康度指示**：

    ok       同步正常      → 横幅隐藏，顶栏显示 LSDB seq / 最后同步 / 档位
    behind   同步滞后      → 「状态同步中，已落后 N 条」+ 自动重试倒计时
    dirty    本地有未提交改动 → 「自动同步已跳过」+ 联系改动的主人（**不是**故障）
    diverged 本地与 origin 分叉 → 「需人工裁决」+ head_sha / remote_sha

三层断言，全部不需要浏览器：

1. **接口层**（真起 `http.server`，随机端口）：`X-Hive-LSDB-Seq` 响应头、
   `sync_health` 字段、`?since=` 增量订阅（无变更 → 轻量 `unchanged`）、
   `Cache-Control: no-store` 与 `X-Snapshot-Mtime` **原样保留**、
   落盘节流后的刷新心跳头 `X-Refresh-Mtime`；
2. **静态层**（`app.js` / `serve-dashboard.sh`）：四档文案齐、旧的无差别拉取
   提示**零命中**、增量订阅与旧后端降级路径在、20s 节拍保留且带 ±20% jitter、
   仅在 `sync_health.lsdb_seq` 变化时才重写 `status.json`；
3. **可选行为层**（有 node 就跑）：`dashboard/dev/hlsa_banner_test.js` 把 app.js
   里的纯函数段抠出来逐条断言四档分支；没装 node 就 skip，不挡 Python 门禁。

为什么接口测试用临时 `status.json`：真实的 dashboard 快照会随集群刷新，
断言会跟着假红。这里所有快照都是本文件现造的。
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
sys.path.insert(0, str(DASH))

import serve  # noqa: E402

APP_JS = DASH / "app.js"
SERVE_PY = DASH / "serve.py"
SCRIPT_SH = ROOT / "scripts" / "serve-dashboard.sh"
DEV_JS_TEST = DASH / "dev" / "hlsa_banner_test.js"

# 四档文案的**关键片段**（不钉整句，允许以后润色措辞；钉的是分支语义在不在）
TEXT_OK_ABSENT = "请执行 git pull"
TEXT_BEHIND = "状态同步中"
TEXT_BEHIND_COUNT = "已落后"
TEXT_BEHIND_RETRY = "自动重试"
TEXT_DIRTY = "本地有未提交改动，自动同步已跳过"
TEXT_DIRTY_OWNER = "改动的主人"
TEXT_DIVERGED = "人工裁决"
TEXT_LEGACY = "同步健康度不可用（旧版后端）"


# --------------------------------------------------------------------------- #
# 快照构造
# --------------------------------------------------------------------------- #

def row(tid="T-9001", **over):
    """一条最小但字段真实的任务行（前端 normalize 是白名单，字段要齐）。"""
    base = {
        "id": tid, "title": "示例卡", "repo": "hive", "priority": "P1",
        "state": "ready", "owner": "", "claim_agent": "", "lease_until": "",
        "stale": False, "local_stale": False, "result_status": "",
        "finished_at": "", "verify": [], "depends_on": [],
    }
    base.update(over)
    return base


def health(**over):
    """T-0353 约定的 sync_health 形状。"""
    base = {
        "lsdb_seq": 7,
        "last_sync_at": "2026-11-14T12:00:00+08:00",
        "head_sha": "a" * 40,
        "remote_sha": "b" * 40,
        "behind": 0,
        "tier": "T1",
        "reason": "synced",
    }
    base.update(over)
    return base


def snapshot(seq=7, rows=None, **over):
    """新后端形态：{"tasks": [...], "sync_health": {...}}。"""
    return {"tasks": rows if rows is not None else [row("T-9001"), row("T-9002")],
            "sync_health": health(lsdb_seq=seq, **over)}


def write_status(directory: Path, payload) -> Path:
    path = directory / "status.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 起真服务 / 发请求（沿用 tests/test_dashboard_dispatch_ui.py 的写法）
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def board(status_path):
    srv = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.build_handler(str(status_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def get(base, path):
    """GET 一次 → (status, body, headers)；4xx/5xx 不抛。"""
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            raw = r.read()
            return r.status, _maybe_json(raw), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, _maybe_json(exc.read()), dict(exc.headers)


def _maybe_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


@pytest.fixture(autouse=True)
def isolate_overlays(monkeypatch, tmp_path):
    """把「叠加层」隔离到临时目录：不碰生产 ~/.hive/cluster，也不往协调仓写文件。

    `serve` 的 /api/push 会写 PUSHED_STATUS_PATH、overlay_epoch 会读集群
    state.json；本文件的接口测试必须自洽，不能依赖真机上有没有集群在跑。
    """
    monkeypatch.setattr(serve, "PUSHED_STATUS_PATH", tmp_path / "pushed-status.json")
    monkeypatch.setenv(serve.CLUSTER_STATE_ENV, str(tmp_path / "cluster"))
    (tmp_path / "cluster").mkdir(exist_ok=True)


@pytest.fixture()
def tmp_status(tmp_path):
    return write_status(tmp_path, snapshot())


# --------------------------------------------------------------------------- #
# 1. 接口契约：header / sync_health / no-store / 快照时刻
# --------------------------------------------------------------------------- #

def test_status_exposes_seq_header_and_sync_health(tmp_status):
    """/api/status 必须带 X-Hive-LSDB-Seq 响应头与 sync_health 字段。"""
    with board(tmp_status) as base:
        code, body, headers = get(base, "/api/status")
    assert code == 200, body
    cursor = headers.get(serve.SYNC_SEQ_HEADER)
    assert cursor and cursor.isdigit() and int(cursor) > 0, (
        "缺少/错位的 %s：增量订阅没有游标可用，实际 %r"
        % (serve.SYNC_SEQ_HEADER, cursor))
    assert isinstance(body, dict), "新后端快照应回 {tasks, sync_health}"
    assert isinstance(body.get("sync_health"), dict), "缺少 sync_health 字段"
    assert body["sync_health"]["lsdb_seq"] == 7
    assert body["sync_health"]["tier"] == "T1"
    assert body["sync_health"]["seq_known"] is True
    assert len(body["tasks"]) == 2, "任务行必须原样透传"


def test_cache_control_no_store_is_kept(tmp_status):
    """不删 Cache-Control: no-store —— 看板是实时视图，缓存一份旧的等于撒谎。"""
    with board(tmp_status) as base:
        _code, _body, headers = get(base, "/api/status")
    assert "no-store" in headers.get("Cache-Control", ""), (
        "Cache-Control: no-store 被删了，实际 %r" % headers.get("Cache-Control"))


def test_snapshot_mtime_semantics_unchanged(tmp_status):
    """X-Snapshot-Mtime 仍是状态文件 mtime（T-0191 契约，10 分钟告警靠它）。"""
    with board(tmp_status) as base:
        _code, _body, headers = get(base, "/api/status")
    assert headers.get("X-Snapshot-Mtime") == "%d" % int(tmp_status.stat().st_mtime)


def test_refresh_stamp_header_when_present(tmp_status):
    """落盘节流后快照 mtime 会长时间不动，刷新循环另有心跳头证明自己活着。"""
    with board(tmp_status) as base:
        _code, _body, headers = get(base, "/api/status")
        assert serve.SYNC_REFRESH_HEADER not in headers, (
            "没有心跳文件时不该凭空造一个 %s" % serve.SYNC_REFRESH_HEADER)

    stamp = Path(str(tmp_status) + ".tmp.refresh")
    stamp.write_text("tick", encoding="utf-8")
    with board(tmp_status) as base:
        _code, _body, headers = get(base, "/api/status")
    assert headers.get(serve.SYNC_REFRESH_HEADER) == "%d" % int(stamp.stat().st_mtime)


# --------------------------------------------------------------------------- #
# 2. 增量订阅：seq 没变 → 轻量 unchanged；变了 → 全量
# --------------------------------------------------------------------------- #

def test_since_same_seq_returns_tiny_unchanged(tmp_path):
    """无变更时只回 {"unchanged": true}，且体积远小于全量快照。

    用一张接近真实规模的快照（40 张卡、带 summary/verify）比体积：
    默认夹具只有 2 行，几百字节的差值说明不了「避免 15s 全量重传」这件事。
    游标取响应头给的不透明令牌（前端就是这么拿的），不是手写死一个数字。
    """
    rows = [row("T-%04d" % (9000 + i), title="示例卡 " + "长标题" * 8,
                summary="一行结果摘要 " * 6,
                verify=[{"cmd": "python3 -m pytest -q tests/x.py", "code": 0}])
            for i in range(40)]
    path = write_status(tmp_path, snapshot(seq=7, rows=rows))
    with board(path) as base:
        _code, full, first_h = get(base, "/api/status")
        cursor = first_h[serve.SYNC_SEQ_HEADER]
        code, body, headers = get(base, "/api/status?since=" + cursor)
    assert code == 200, body
    assert body.get("unchanged") is True, "同游标应回 unchanged，实际 %r" % (body,)
    assert headers.get(serve.SYNC_SEQ_HEADER) == cursor, "没变化时游标必须稳定（否则会一直全量）"
    assert "tasks" not in body, "unchanged 响应不该再带整份任务列表"
    full_bytes = len(json.dumps(full, ensure_ascii=False).encode("utf-8"))
    tiny_bytes = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    assert tiny_bytes * 10 < full_bytes, (
        "unchanged 响应体应显著小于全量：%d vs %d 字节" % (tiny_bytes, full_bytes))


def test_overlay_change_invalidates_cursor(tmp_status):
    """/api/push 的加速通道不能被增量订阅吞掉（卡面明确要求保留）。

    叠加层（pushed-status.json）不在 status.json 的 lsdb_seq 里：如果游标只看
    lsdb_seq，一条刚推上来的 progress 会因为「序号没变」被 unchanged 吞掉。
    断言「/api/push 之后同游标必须回全量，且进度真的合并进了任务行」。
    """
    with board(tmp_status) as base:
        _code, _body, before = get(base, "/api/status")
        cursor_before = before[serve.SYNC_SEQ_HEADER]
        req = urllib.request.Request(
            base + "/api/push",
            data=json.dumps({"task": "T-9001", "progress": 55,
                             "message": "推一条新进度"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
        _code, after_body, after = get(base, "/api/status?since=" + cursor_before)
    assert after[serve.SYNC_SEQ_HEADER] != cursor_before, (
        "/api/push 之后游标必须变化，否则前端永远看不到新进度")
    assert after_body.get("unchanged") is not True, (
        "叠加层变了就不能回 unchanged（加速通道被吞了）")
    assert isinstance(after_body.get("tasks"), list)
    pushed_row = next(r for r in after_body["tasks"] if r["id"] == "T-9001")
    assert pushed_row["progress"] == 55, "直推的进度没合并进任务行"


def test_since_other_seq_returns_full_payload(tmp_status):
    """游标不匹配 = 有变化 → 回全量（并带上新的游标）。"""
    with board(tmp_status) as base:
        code, body, headers = get(base, "/api/status?since=6")
    assert code == 200
    assert body.get("unchanged") is None
    assert isinstance(body.get("tasks"), list) and len(body["tasks"]) == 2
    assert headers.get(serve.SYNC_SEQ_HEADER) not in ("0", "", None)


def test_cursor_stable_to_identical_overlay(tmp_status):
    """同一份叠加层重复读，游标不变（不能每次请求都抖，否则增量订阅退化成全量）。"""
    with board(tmp_status) as base:
        _code, _body, a = get(base, "/api/status")
        _code, _body, b = get(base, "/api/status")
    assert a[serve.SYNC_SEQ_HEADER] == b[serve.SYNC_SEQ_HEADER]


def test_cluster_heartbeat_noise_does_not_invalidate_cursor(tmp_status, tmp_path):
    """集群每次心跳都重写 state.json，但 term/leader 变化不该让游标抖动。

    如果游标把 state.json 的 mtime 算进去，unchanged 就永远不触发（退化成每 15s
    全量重传），增量订阅等于没做。只有 progress 段真变了才该让游标前进。
    """
    cluster_dir = tmp_path / "cluster"   # isolate_overlays 夹具设的目录
    state_file = cluster_dir / "state.json"
    snapshot_state = {
        "snapshot": {"term": 1, "leader_id": "node-A",
                     "progress": {"T-9001": {"progress": 10, "updated_at": "2026-11-14T12:00:00"}}}
    }
    with board(tmp_status) as base:
        state_file.write_text(json.dumps(snapshot_state), encoding="utf-8")
        _c, _b, first = get(base, "/api/status")
        # 心跳只改 term / leader / 时间戳，progress 段一字不动
        snapshot_state["snapshot"]["term"] = 2
        snapshot_state["snapshot"]["leader_id"] = "node-B"
        snapshot_state["snapshot"]["heartbeat_at"] = "2026-11-14T12:00:20"
        state_file.write_text(json.dumps(snapshot_state), encoding="utf-8")
        _c, _b, second = get(base, "/api/status")
        assert first[serve.SYNC_SEQ_HEADER] == second[serve.SYNC_SEQ_HEADER], (
            "心跳噪声把游标抖了 —— unchanged 永远不触发")

        # 真有新进度：游标必须前进
        snapshot_state["snapshot"]["progress"]["T-9001"]["progress"] = 42
        state_file.write_text(json.dumps(snapshot_state), encoding="utf-8")
        _c, _b, third = get(base, "/api/status")
        assert third[serve.SYNC_SEQ_HEADER] != second[serve.SYNC_SEQ_HEADER], (
            "集群 progress 变了却不换游标，前端会漏看新进度")


def test_since_garbage_falls_back_to_full(tmp_status):
    """?since=abc / 负数 一律当没给，不做增量判定（不猜）。"""
    with board(tmp_status) as base:
        for q in ("?since=abc", "?since=-1", "?since="):
            code, body, _h = get(base, "/api/status" + q)
            assert code == 200
            assert isinstance(body.get("tasks"), list), "%s 应回全量，实际 %r" % (q, body)


def test_legacy_backend_never_locked_into_unchanged(tmp_path):
    """旧后端没有可信序号：`?since=0` 也绝不能回 unchanged，否则永不刷新。"""
    legacy = [row("T-9001", local_stale=True), row("T-9002")]
    path = write_status(tmp_path, legacy)
    with board(path) as base:
        code, body, headers = get(base, "/api/status?since=0")
    assert code == 200
    assert isinstance(body, list), "旧后端的裸数组契约保持不变（T-0191）"
    assert len(body) == 2
    assert serve.SYNC_SEQ_HEADER in headers, "旧后端也要有这个头（值为 0 = 不可信）"
    assert headers[serve.SYNC_SEQ_HEADER] == "0"


# --------------------------------------------------------------------------- #
# 3. 旧后端优雅降级：没有 sync_health 也不能白屏
# --------------------------------------------------------------------------- #

def test_legacy_dict_root_degrades_from_local_stale(tmp_path):
    """dict 根但没 sync_health（滚动升级）→ degraded + legacy_local_stale。"""
    path = write_status(tmp_path, {"tasks": [row("T-9001", local_stale=True)]})
    with board(path) as base:
        code, body, _h = get(base, "/api/status")
    assert code == 200
    health = body["sync_health"]
    assert health["degraded"] is True, "旧后端必须标 degraded，前端才知道要降级"
    assert health["legacy_local_stale"] is True, "要把行上的 local_stale 收起来"
    assert health["reason"] == "legacy"
    assert health["seq_known"] is False and health["lsdb_seq"] == 0


def test_legacy_array_root_keeps_body_shape(tmp_path):
    """裸数组根（旧快照 / 手工导出）：响应体结构一个字节不改。"""
    path = write_status(tmp_path, [row("T-9001"), row("T-9002")])
    with board(path) as base:
        code, body, _h = get(base, "/api/status")
    assert code == 200
    assert isinstance(body, list) and len(body) == 2


def test_sync_health_of_normalizes_dirty_types():
    """后端字段可能是字符串（JSON 手改/旧版本），服务端负责归一而不是把脏值透给前端。"""
    health = serve.sync_health_of({
        "tasks": [],
        "sync_health": {"lsdb_seq": "12", "behind": "3", "tier": 2,
                        "reason": " dirty ", "local_dirty": 1},
    })
    assert health["lsdb_seq"] == 12 and health["seq"] == 12
    assert health["behind"] == 3
    assert health["tier"] == "2"
    assert health["reason"] == "dirty"
    assert health["local_dirty"] is True
    assert health["seq_known"] is True
    assert health["degraded"] is False


def test_sync_health_of_flags_missing_seq_as_unknown():
    """没有 lsdb_seq 的 sync_health 不能算「可信序号」（否则增量订阅会锁死）。"""
    health = serve.sync_health_of({"tasks": [], "sync_health": {"reason": "synced"}})
    assert health["seq_known"] is False
    assert serve.lsdb_seq_of({"tasks": [], "sync_health": {"reason": "synced"}}) is None


# --------------------------------------------------------------------------- #
# 4. 落盘节流（serve.py 的进程内刷新循环 + 脚本共用同一判据）
# --------------------------------------------------------------------------- #

def test_status_needs_rewrite_only_on_seq_change(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(snapshot(seq=7)), encoding="utf-8")

    same = json.dumps(snapshot(seq=7, rows=[row("T-9001", state="running")]))
    assert serve.status_needs_rewrite(path, same) is False, (
        "序号没变就不该重写 status.json（否则 20s 一次全量序列化）")

    newer = json.dumps(snapshot(seq=8))
    assert serve.status_needs_rewrite(path, newer) is True, "序号变了必须落盘"

    legacy = json.dumps([row("T-9001")])
    assert serve.status_needs_rewrite(path, legacy) is True, (
        "旧后端没有序号：宁可多写一次，也不能漏掉卡面变化")

    assert serve.status_needs_rewrite(tmp_path / "missing.json", newer) is True


def test_refresh_loop_source_is_throttled_and_atomic():
    """进程内刷新循环：先判序号（节流）再原子替换，且顺手 touch 心跳。"""
    src = SERVE_PY.read_text(encoding="utf-8")
    assert "status_needs_rewrite(" in src, "refresh_loop 没接落盘节流"
    assert "write_status_atomic(" in src and "os.replace" in src, "丢了原子替换"
    assert "touch_refresh_stamp(" in src, "没维护刷新心跳（节流后无法判停摆）"


# --------------------------------------------------------------------------- #
# 5. 前端静态层：四档文案 + 增量订阅 + 降级 + 旧提示零命中
# --------------------------------------------------------------------------- #

def test_old_pull_hint_is_gone():
    """完成标准 1：旧的「请执行 git pull（或 hive sync-repos）」在 app.js 零命中。"""
    app = APP_JS.read_text(encoding="utf-8")
    assert "git pull" not in app, "app.js 里还有「git pull」的无差别提示"
    assert "sync-repos" not in app, "app.js 里还有「hive sync-repos」的无差别提示"
    assert TEXT_OK_ABSENT not in app


def test_four_banner_branches_and_modes_exist():
    """四种状态各有独立分支：ok / behind / dirty / diverged（+ 旧后端 legacy）。"""
    app = APP_JS.read_text(encoding="utf-8")
    assert "SYNC HEALTH PURE BEGIN" in app and "SYNC HEALTH PURE END" in app, (
        "纯函数区块的 BEGIN/END 标记被删了：可选 jsdom/vm 测试就没法抠出来单测")
    for fn in ("function syncHealthMode", "function syncHealthBanner",
               "function syncHealthChip", "function syncRetryText"):
        assert fn in app, "缺少纯函数 %s" % fn
    for mode in ("'ok'", "'behind'", "'dirty'", "'diverged'", "'legacy'"):
        assert mode in app, "模式 %s 没出现" % mode

    # 四档各自的文案（关键片段）
    assert TEXT_BEHIND in app, "缺少「同步滞后」分支文案"
    assert TEXT_BEHIND_COUNT in app, "滞后横幅要给落后条数"
    assert TEXT_BEHIND_RETRY in app, "滞后横幅要给自动重试倒计时"
    assert TEXT_DIRTY in app, "缺少「本地有未提交改动」分支文案"
    assert TEXT_DIRTY_OWNER in app, "本地脏要提示联系改动的主人"
    assert TEXT_DIVERGED in app, "缺少「分叉需人工裁决」分支文案"
    assert TEXT_LEGACY in app, "缺少旧后端降级文案"
    # ok 档：横幅隐藏（返回 null），顶栏显示 seq / 最后同步 / 档位
    assert "LSDB seq " in app and "最后同步 " in app and "档位 " in app, (
        "顶栏同步健康度指示缺少 seq / 最后同步 / 档位")
    assert "return null" in app
    # 本地脏不许被说成故障：提示里不能出现 pull / reset / 一键修复
    assert "不提供一键 pull" not in app  # 只是防手滑写进 UI 文案
    assert "reset" in app  # 分叉文案里明确「不会自动 merge/reset」，是解释不是按钮


def test_frontend_uses_incremental_subscription():
    """增量订阅：带 since 取数、unchanged 走轻量分支、15s 兜底与不可见即暂停不变。"""
    app = APP_JS.read_text(encoding="utf-8")
    assert "?since=" in app, "前端没有走 ?since= 增量订阅"
    assert "'unchanged'" in app or '"unchanged"' in app, "前端没处理 unchanged"
    assert "X-Hive-LSDB-Seq" in app, "前端没读增量游标响应头（游标靠它回传）"
    assert "state.cursor" in app, "前端没保存/回传游标"
    assert "REFRESH_MS = 15000" in app, "15s 兜底档位被改了"
    assert "visibilityState === 'visible'" in app, "页面不可见时暂停的逻辑被删了"
    # 落盘节流后「快照 mtime 不动」不等于停摆：前端要用刷新心跳兜住 10 分钟告警
    assert "X-Refresh-Mtime" in app
    assert "state.refreshAt || state.snapshotAt" in app, "10 分钟告警没有改用刷新心跳"


def test_frontend_degrades_without_sync_health():
    """旧后端（只有 local_stale、无 sync_health）下不白屏：降级路径必须在。"""
    app = APP_JS.read_text(encoding="utf-8")
    assert "localStaleAny" in app, "旧后端的 local_stale 降级入口被删了"
    assert "legacyLocalStale" in app, "降级判定没把 local_stale 传进去"
    assert "state.syncHealth = (!Array.isArray(data) && data && data.sync_health) || null" in app, (
        "没处理「新后端有 sync_health / 旧后端没有」两种形态")


# --------------------------------------------------------------------------- #
# 6. 落盘节流（scripts/serve-dashboard.sh）与前端纯函数行为
# --------------------------------------------------------------------------- #

def test_dashboard_script_throttles_and_jitters():
    sh = SCRIPT_SH.read_text(encoding="utf-8")
    assert 'INTERVAL="${INTERVAL:-20}"' in sh, "20s 节拍被改了"
    assert "status_seq" in sh and "lsdb_seq" in sh, "脚本没按 sync_health.lsdb_seq 判变化"
    assert '"$new_seq" = "$old_seq"' in sh, "缺「序号相同 → 不重写」的判定"
    assert "0.2" in sh and "uniform" in sh, "节拍缺 ±20% jitter"
    assert 'STAMP="$STATUS.tmp.refresh"' in sh, "缺刷新心跳文件（节流后无法判停摆）"
    assert '"$STAMP"' in sh, "心跳文件没有每拍更新"
    assert '[ -n "$CI_REPOS" ] && refresh_ci' in sh, "CI 刷新通道被改了"
    assert '--host "$HOST"' in sh, "serve.py 启动参数被改了"


def _posix_shell():
    """找一个能 `-n` 语法检查的 POSIX shell：sh 优先，其次 bash（Windows git-bash）。"""
    for name in ("sh", "bash"):
        found = shutil.which(name)
        if found:
            return found
    for guess in (r"C:\Program Files\Git\bin\bash.exe",
                  r"C:\Program Files\Git\usr\bin\sh.exe"):
        if Path(guess).is_file():
            return guess
    return None


@pytest.mark.skipif(_posix_shell() is None, reason="本机没有 POSIX shell（sh/bash）")
def test_dashboard_script_syntax():
    """语法能过：`sh -n` 不执行任何一行（不碰生产 8787 / 不重启服务）。"""
    proc = subprocess.run([_posix_shell(), "-n", str(SCRIPT_SH)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)
    assert proc.returncode == 0, "serve-dashboard.sh 语法错误：%s" % proc.stderr


THROTTLE_BEGIN = "# --- THROTTLE FUNCTIONS BEGIN ---"
THROTTLE_END = "# --- THROTTLE FUNCTIONS END ---"

FAKE_HIVE_PY = '''#!/usr/bin/env python3
"""假 hive CLI：只回答 `status --json`，序号从同目录的 seq 文件读。

用来在临时沙箱里复现「同一序号刷新两次」与「序号变化」，不碰真仓库、不启服务。
"""
import json
import pathlib
import sys

here = pathlib.Path(__file__).resolve().parent
seq_file = here / "seq"
mode_file = here / "legacy"
args = [a for a in sys.argv[1:] if a != "--json"]
seq = int(seq_file.read_text().strip()) if seq_file.is_file() else 7
legacy = mode_file.is_file()
if legacy:
    # 旧后端形态：裸数组、没有 sync_health
    print(json.dumps([{"id": "T-9001", "state": "ready", "local_stale": True}]))
else:
    print(json.dumps({"tasks": [{"id": "T-9001", "state": "ready"}],
                      "sync_health": {"lsdb_seq": seq, "tier": "T1",
                                      "reason": "synced"}}))
'''


def _throttle_block() -> str:
    """从 serve-dashboard.sh 里抠出节流函数段（status_seq / jitter_seconds / refresh）。"""
    sh = SCRIPT_SH.read_text(encoding="utf-8")
    start = sh.index(THROTTLE_BEGIN)
    stop = sh.index(THROTTLE_END)
    block = sh[start + len(THROTTLE_BEGIN):stop]
    assert "status_seq" in block and "refresh()" in block, "节流区块内容不完整"
    return block


@pytest.mark.skipif(_posix_shell() is None, reason="本机没有 POSIX shell（sh/bash）")
def test_dashboard_script_throttles_disk_writes(tmp_path):
    """完成标准 4：无变更窗口内 status.json 的重写次数为 0，只动心跳文件。

    真跑 `refresh()`（不是读源码）：沙箱里放一份假 `hive.py`，序号可控。
    三次刷新里 status.json 的 mtime 必须只在序号变化那一次前进。
    """
    repo = tmp_path / "repo"
    (repo / "dashboard").mkdir(parents=True)
    (repo / "hive.py").write_text(FAKE_HIVE_PY, encoding="utf-8")
    (repo / "seq").write_text("7", encoding="utf-8")
    harness = repo / "throttle.sh"
    harness.write_text(
        "#!/bin/sh\nset -u\n"
        'DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        'HIVE="$DIR"\nDASH="$DIR/dashboard"\n'
        'STATUS="$DASH/status.json"\nSTAMP="$STATUS.tmp.refresh"\n'
        + _throttle_block()
        + '\nrefresh || true\nprintf "%s" "$(status_seq "$STATUS")"\n',
        encoding="utf-8")

    shell = _posix_shell()

    def run():
        proc = subprocess.run([shell, str(harness)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=120)
        assert proc.returncode == 0, "refresh 脚本失败：%s" % proc.stderr
        return proc.stdout.strip()

    status = repo / "dashboard" / "status.json"
    assert run() == "7", "首次刷新应落盘并识别出序号 7"
    first = json.loads(status.read_text(encoding="utf-8"))
    mtime_1 = status.stat().st_mtime_ns
    stamp_mtime_1 = (repo / "dashboard" / "status.json.tmp.refresh").stat().st_mtime_ns

    # 同序号再刷 2 次：status.json 一个字节都不该动（只更新心跳）
    for _ in range(2):
        assert run() == "7"
    assert status.stat().st_mtime_ns == mtime_1, (
        "序号没变却重写了 status.json —— 落盘节流没生效（20s 一次全量序列化仍在）")
    assert json.loads(status.read_text(encoding="utf-8")) == first, "快照内容不该被改写"
    assert (repo / "dashboard" / "status.json.tmp.refresh").stat().st_mtime_ns >= stamp_mtime_1, (
        "心跳文件没更新：节流后 /api/status 就无法区分「安静」与「停摆」")

    # 序号前进：必须落盘
    (repo / "seq").write_text("8", encoding="utf-8")
    assert run() == "8"
    assert status.stat().st_mtime_ns > mtime_1, "序号变了却不落盘（看板会停在旧卡面）"
    assert json.loads(status.read_text(encoding="utf-8"))["sync_health"]["lsdb_seq"] == 8

    # 旧后端（没有 sync_health）：宁可多写，也不能漏掉卡面变化
    (repo / "legacy").write_text("1", encoding="utf-8")
    assert run() == "", "旧后端不该识别出序号"
    mtime_legacy = status.stat().st_mtime_ns
    for _ in range(2):
        assert run() == ""
        assert status.stat().st_mtime_ns >= mtime_legacy
    assert json.loads(status.read_text(encoding="utf-8"))[0]["local_stale"] is True, (
        "旧后端的内容必须整份落盘")


def test_frontend_pure_functions_behaviour():
    """可选行为层：把 app.js 的纯函数段抠出来，在 node 里逐条断言四档分支。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过可选的纯函数行为测试（静态断言已覆盖文案）")
    proc = subprocess.run([node, str(DEV_JS_TEST)], cwd=str(DASH),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=90,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 0, (
        "同步健康度纯函数测试失败：\nstdout=%s\nstderr=%s"
        % (proc.stdout, proc.stderr))
    assert "ALL SYNC HEALTH UI CHECKS PASSED" in proc.stdout
