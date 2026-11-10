"""T-0189：看板的「复制派发指令」按钮 + dispatched 阶段列的回归。

三层断言，全部不需要浏览器 / GUI：

1. **接口层** —— 真起一个 `http.server`（随机端口），用 HTTP 打
   `/api/dispatch-prompt`，覆盖 200 / 400 / 404 与两种 format；
2. **静态层** —— `app.js` 里两条剪贴板路径、接口地址、stage 徽标渲染必须在；
3. **降级层** —— 旧快照缺 `board` / `stage` 时，JS 的取值都有兜底，
   不许把 `undefined` 渲染到页面上。

为什么接口测试用临时 tasks 目录而不是真卡：真卡会被关闭/改名，测试跟着假红；
唯一的例外是 `test_real_card_T0184_renders`，它按验收口径打真实仓库的卡，
卡没了就 skip 而不是红。
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASH))

import serve  # noqa: E402

APP_JS = DASH / "app.js"
INDEX_HTML = DASH / "index.html"
STYLE_CSS = DASH / "style.css"
SERVE_PY = DASH / "serve.py"

# 一张最小但字段完整的卡：paths / verify / timeout_minutes / 禁止行为 都有，
# 这样派发指令的四段契约都能被渲染出来（不会退化成回退文案）。
CARD_MD = """---
id: T-0001
title: 看板复制按钮
repo: hive
priority: P2
paths:
  - dashboard/app.js
  - tests/test_dashboard_dispatch_ui.py
verify:
  - python -m pytest -q tests/test_dashboard_dispatch_ui.py
timeout_minutes: 30
---

## 背景

看板上点一下就能把派发指令复制出来，不用再人肉抄任务号。

## 不要做什么

- 不改 hive.py。
- 不发写请求。
"""


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def board(status_path):
    """起一个真实看板服务（随机端口），退出时关掉。"""
    srv = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.build_handler(str(status_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def get(base, path):
    """GET 一次，返回 (status, body_dict, headers)；4xx/5xx 不抛。"""
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw), dict(exc.headers)


@pytest.fixture()
def tasks_dir(tmp_path, monkeypatch):
    """把 serve.TASKS_DIR 指到临时目录，只放一张 T-0001。"""
    d = tmp_path / "tasks"
    d.mkdir()
    (d / "T-0001.md").write_text(CARD_MD, encoding="utf-8")
    monkeypatch.setattr(serve, "TASKS_DIR", d)
    return d


@pytest.fixture()
def status_file(tmp_path):
    p = tmp_path / "status.json"
    p.write_text(json.dumps([{"id": "T-0001", "state": "ready", "stage": "pending"}],
                            ensure_ascii=False), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# 1. 接口层
# --------------------------------------------------------------------------- #

def test_dispatch_prompt_markdown_ok(tasks_dir, status_file):
    """默认 format=markdown：200，正文含任务号、契约与 hive_claim。"""
    with board(status_file) as base:
        code, body, headers = get(base, "/api/dispatch-prompt?task=T-0001")

    assert code == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert body["task"] == "T-0001"
    assert body["format"] == "markdown"
    text = body["text"]
    assert "请领取任务 T-0001" in text
    assert "hive_claim" in text                 # 验收口径：能直接粘给 agent
    assert "dashboard/app.js" in text           # 允许修改路径来自卡面
    assert "30 分钟内未完成即放弃" in text      # timeout_minutes 进了失败策略
    assert "不改 hive.py" in text               # 「不要做什么」进了禁止行为


def test_dispatch_prompt_json_format(tasks_dir, status_file):
    """format=json：text 是一段可 json.loads 的字符串，且与 markdown 同源。"""
    with board(status_file) as base:
        code, body, _ = get(base, "/api/dispatch-prompt?task=T-0001&format=json")

    assert code == 200
    assert body["format"] == "json"
    payload = json.loads(body["text"])
    assert payload["task_id"] == "T-0001"
    assert payload["claim_tool"]["name"] == "hive_claim"
    assert payload["contract"]["allowed_paths"] == ["dashboard/app.js",
                                                    "tests/test_dashboard_dispatch_ui.py"]


def test_dispatch_prompt_with_agent(tasks_dir, status_file):
    """带 agent：正文多一行「领取身份」，payload 的 agent 字段同步。"""
    with board(status_file) as base:
        _, body, _ = get(base, "/api/dispatch-prompt?task=T-0001&agent=trae-win")
        _, jbody, _ = get(base, "/api/dispatch-prompt?task=T-0001&agent=trae-win&format=json")

    assert "领取身份：trae-win" in body["text"]
    assert json.loads(jbody["text"])["agent"] == "trae-win"


@pytest.mark.parametrize("bad", ["T-18", "T-12345", "t-0001", "../etc/passwd",
                                 "T-0001/../../hive.py", "", "T-ABCD"])
def test_dispatch_prompt_rejects_bad_task_id(tasks_dir, status_file, bad):
    """任务号必须严格 ^T-\\d{4}$：拼进文件名，所以不能放行任何路径字符。"""
    with board(status_file) as base:
        code, body, _ = get(base, "/api/dispatch-prompt?task=" + bad)

    assert code == 400
    assert "error" in body and "task" in body["error"]


def test_dispatch_prompt_missing_card_is_404(tasks_dir, status_file):
    """号合法但卡不存在：404，不是 500。"""
    with board(status_file) as base:
        code, body, _ = get(base, "/api/dispatch-prompt?task=T-9999")

    assert code == 404
    assert "T-9999" in body["error"]


def test_dispatch_prompt_rejects_bad_agent(tasks_dir, status_file):
    """agent 沿用 AGENT_NAME_RE；不合法的名字 400。"""
    with board(status_file) as base:
        code, body, _ = get(base, "/api/dispatch-prompt?task=T-0001&agent=../x")

    assert code == 400
    assert "agent" in body["error"]


def test_dispatch_prompt_rejects_bad_format(tasks_dir, status_file):
    """format 只认 markdown/json；乱传 400 而不是静默降级。"""
    with board(status_file) as base:
        code, body, _ = get(base, "/api/dispatch-prompt?task=T-0001&format=yaml")

    assert code == 400
    assert "format" in body["error"]


def test_dispatch_prompt_broken_card_is_json_400(tasks_dir, status_file):
    """卡面坏掉（缺 frontmatter）也只能回 JSON 400，不许甩异常栈。"""
    (tasks_dir / "T-0002.md").write_text("没有 frontmatter 的卡\n", encoding="utf-8")
    with board(status_file) as base:
        code, body, _ = get(base, "/api/dispatch-prompt?task=T-0002")

    assert code == 400
    assert "渲染失败" in body["error"]


def test_real_card_T0184_renders():
    """验收口径：真实仓库的 T-0184 必须能渲染出含 hive_claim 的指令。"""
    if not (serve.TASKS_DIR / "T-0184.md").is_file():
        pytest.skip("真实仓库里没有 tasks/T-0184.md")
    with board(DASH / "sample-status.json") as base:
        code, body, _ = get(base, "/api/dispatch-prompt?task=T-0184")

    assert code == 200
    assert "请领取任务 T-0184" in body["text"]
    assert "hive_claim" in body["text"]


# --------------------------------------------------------------------------- #
# 1.5 状态感知（2026-11-03）：待合入 → 给主控的合入指令；异常 → 附溯源块
# --------------------------------------------------------------------------- #

def write_status(tmp_path, records):
    """落一份临时状态快照，返回路径（serve 按此判断卡的运行时状态）。"""
    p = tmp_path / "status-mode.json"
    p.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return p


def test_dispatch_prompt_merging_is_merge_prompt(tasks_dir, tmp_path):
    """待合入卡：复制出的是给主控的合入指令，不再是「请领取任务」。"""
    status = write_status(tmp_path, [{"id": "T-0001", "state": "assigned",
                                      "result_status": "done",
                                      "claim_agent": "deepseek-win",
                                      "head": "2c905a6fc1cd69923a1b",
                                      "result_summary": "已修，实测见摘要。"}])
    with board(status) as base:
        code, body, _ = get(base, "/api/dispatch-prompt?task=T-0001")

    assert code == 200
    assert body["mode"] == "merge"
    text = body["text"]
    assert "任务 T-0001 已完成，请把它合入" in text
    assert "hive land T-0001 --apply" in text       # 主控要跑的命令
    assert "执行 agent：deepseek-win" in text        # 溯源：谁做完的
    assert "res/T-0001" in text                     # 结果分支
    assert "head 2c905a6fc1" in text                # head 截到 10 位
    assert "hive_claim" not in text                 # 不该再喊人领取
    assert "冲突处理" in text


def test_dispatch_prompt_merging_json_payload(tasks_dir, tmp_path):
    """format=json：merge 载荷带 mode / land_cmd / branch，text 与 markdown 同源。"""
    status = write_status(tmp_path, [{"id": "T-0001", "state": "assigned",
                                      "result_status": "done",
                                      "claim_agent": "deepseek-win"}])
    with board(status) as base:
        _, body, _ = get(base, "/api/dispatch-prompt?task=T-0001&format=json")

    payload = json.loads(body["text"])
    assert payload["mode"] == "merge"
    assert payload["land_cmd"] == "hive land T-0001 --apply"
    assert payload["branch"] == "res/T-0001"
    assert payload["agent"] == "deepseek-win"
    assert "任务 T-0001 已完成，请把它合入" in payload["text"]


def test_dispatch_prompt_abnormal_carries_trace(tasks_dir, tmp_path):
    """异常卡：仍是派发指令，但末尾带异常溯源块（agent / 状态 / 摘要）。"""
    status = write_status(tmp_path, [{"id": "T-0001", "state": "assigned",
                                      "result_status": "failed",
                                      "claim_agent": "fb88-dsh",
                                      "claimed_at": "2026-10-27T22:00:00+08:00",
                                      "result_summary": "agent fb88-dsh 没有配置仓库 'light' 的本地路径"}])
    with board(status) as base:
        code, body, _ = get(base, "/api/dispatch-prompt?task=T-0001")

    assert code == 200
    assert body["mode"] == "abnormal"
    text = body["text"]
    assert "请领取任务 T-0001" in text              # 重派语义保留
    assert "异常信息（供溯源）" in text
    assert "此前领取 agent：fb88-dsh" in text        # 用户要的溯源信息
    assert "异常状态：failed" in text
    assert "agent fb88-dsh 没有配置仓库" in text


def test_dispatch_prompt_stale_is_abnormal(tasks_dir, tmp_path):
    """租约过期也归异常：带溯源块，异常状态标成「租约过期」。"""
    status = write_status(tmp_path, [{"id": "T-0001", "state": "running",
                                      "claim_agent": "trae-win",
                                      "stale": True}])
    with board(status) as base:
        _, body, _ = get(base, "/api/dispatch-prompt?task=T-0001")

    assert body["mode"] == "abnormal"
    assert "异常状态：租约过期" in body["text"]
    assert "此前领取 agent：trae-win" in body["text"]


def test_dispatch_prompt_ready_keeps_claim_text(tasks_dir, tmp_path):
    """待领取 / 进行中 / 已关闭：mode=claim，正文与旧行为逐字一致。"""
    for rec in ({"id": "T-0001", "state": "ready"},
                {"id": "T-0001", "state": "running", "claim_agent": "trae-win"},
                {"id": "T-0001", "state": "done", "result_status": "done"}):
        status = write_status(tmp_path, [rec])
        with board(status) as base:
            code, body, _ = get(base, "/api/dispatch-prompt?task=T-0001")
        assert code == 200
        assert body["mode"] == "claim"
        assert "请领取任务 T-0001" in body["text"]
        assert "异常信息" not in body["text"]


def test_dispatch_prompt_without_status_snapshot_falls_back(tasks_dir, tmp_path):
    """快照缺失 / 坏掉：降级成 claim 渲染，不 500。"""
    missing = tmp_path / "no-such.json"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    for status in (missing, broken):
        with board(status) as base:
            code, body, _ = get(base, "/api/dispatch-prompt?task=T-0001")
        assert code == 200
        assert body["mode"] == "claim"


def test_detect_mode_matches_board_columns():
    """detect_mode 的判定与看板 classify 对齐（单测，不起服务）。"""
    import dispatch_prompt
    assert dispatch_prompt.detect_mode(None) == "claim"
    assert dispatch_prompt.detect_mode({}) == "claim"
    assert dispatch_prompt.detect_mode({"state": "assigned",
                                        "result_status": "done"}) == "merge"
    # 已经 close 的不算待合入（合完的卡再喊主控合入就是空转）
    assert dispatch_prompt.detect_mode({"state": "done",
                                        "result_status": "done"}) == "claim"
    assert dispatch_prompt.detect_mode({"state": "closed",
                                        "result_status": "done"}) == "claim"
    assert dispatch_prompt.detect_mode({"state": "assigned",
                                        "result_status": "failed"}) == "abnormal"
    assert dispatch_prompt.detect_mode({"state": "blocked"}) == "abnormal"
    assert dispatch_prompt.detect_mode({"state": "running", "stale": True}) == "abnormal"
    # 搁置卡绝不能被生成领取指令，哪怕旧快照里还留着 done 结果
    assert dispatch_prompt.detect_mode({"state": "shelved"}) == "shelved"
    assert dispatch_prompt.detect_mode({"state": "shelved",
                                        "result_status": "done"}) == "shelved"


def test_shelved_prompt_never_asks_agent_to_claim():
    import dispatch_prompt
    text = dispatch_prompt.render_status_prompt(
        {"id": "T-0001", "title": "等上游"},
        {"state": "shelved", "shelve_reason": "上游没发版",
         "shelved_at": "2026-11-04T10:00:00+00:00"})
    assert "不要领取" in text and "hive_claim" in text  # 明确写出禁止动作
    assert "上游没发版" in text
    assert "hive unshelve T-0001" in text
    assert "hive close T-0001" in text


def test_dispatch_route_never_shells_out():
    """接口必须是进程内渲染：一旦 subprocess 调 hive.py，git fetch 会拖死 HTTP 线程。"""
    src = SERVE_PY.read_text(encoding="utf-8")
    start = src.index("def serve_dispatch_prompt")
    end = src.index("def serve_status", start)
    block = src[start:end]
    assert "subprocess" not in block
    assert "hive.py" not in block
    # 进程内路径：parse_card 读卡 + dispatch_prompt 渲染
    assert "parse_card" in src and "dispatch_prompt" in src
    # 路由必须显式判断，不许通配
    assert 'API_DISPATCH_PROMPT = "/api/dispatch-prompt"' in src
    assert "if path == API_DISPATCH_PROMPT:" in src


def test_api_status_accepts_board_object(tmp_path):
    """`hive status --json --board` 的 {tasks, board} 形态要能原样透传。"""
    p = tmp_path / "status.json"
    payload = {"tasks": [{"id": "T-0001", "state": "ready", "stage": "pending"}],
               "board": {"total": 1, "counts": {"pending": 1}}}
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with board(p) as base:
        code, body, _ = get(base, "/api/status")

    assert code == 200
    assert body["board"]["counts"] == {"pending": 1}
    assert body["tasks"][0]["stage"] == "pending"


def test_api_status_object_without_tasks_is_500(tmp_path):
    """对象形态但没 tasks：明确 500 + 人话错误，不猜。"""
    p = tmp_path / "status.json"
    p.write_text(json.dumps({"board": {"total": 0}}, ensure_ascii=False), encoding="utf-8")

    with board(p) as base:
        code, body, _ = get(base, "/api/status")

    assert code == 500
    assert "tasks" in body["error"]


# --------------------------------------------------------------------------- #
# 2. 静态层：前端源码里必须有这些路径
# --------------------------------------------------------------------------- #

def js_function(name):
    """粗取 app.js 里某个函数体（到同缩进的收尾 `}` 为止）。"""
    src = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"function %s\([^)]*\)\s*\{" % re.escape(name), src)
    assert m, "app.js 里找不到函数 %s" % name
    end = src.find("\n  }", m.end())
    assert end > m.end(), "函数 %s 的收尾大括号没找到" % name
    return src[m.end():end]


def test_app_js_has_both_clipboard_paths():
    """Clipboard API 与 execCommand 回退两条路径都要在（HTTP 非安全上下文必走回退）。"""
    src = APP_JS.read_text(encoding="utf-8")
    assert "navigator.clipboard.writeText" in src
    assert 'document.execCommand("copy")' in src
    legacy = js_function("legacyCopy")
    assert "textarea" in legacy and "appendChild" in legacy and "removeChild" in legacy


def test_app_js_calls_dispatch_prompt_api():
    """卡片按钮打的是只读接口，不是 /api/task。"""
    body = js_function("copyDispatch")
    assert "api/dispatch-prompt?task=" in body
    assert "/api/task" not in body
    assert "writeClipboard" in body


def test_app_js_merge_button_for_merging_cards():
    """待合入卡的按钮文案是「复制合入指令」，复制提示按 mode 区分收件人。"""
    card = js_function("cardInner")
    assert "复制合入指令" in card
    assert "col === 'merging'" in card
    drawer = js_function("drawerHTML")
    assert "复制合入指令" in drawer
    copy = js_function("copyDispatch")
    assert "body.mode === 'merge'" in copy
    assert "主控 agent" in copy


def test_app_js_renders_stage_badge_and_dispatched_to():
    """stage 徽标 + dispatched 卡的 dispatched_to 都要渲染。"""
    badge = js_function("stageBadge")
    assert "stage-badge" in badge
    assert "stageLabel" in badge
    assert "t.dispatched_to" in badge
    # 兼容「有 dispatched_to 但没 stage」的旧卡
    assert "!s || s === 'dispatched'" in badge
    # 卡片头部确实把徽标拼进去了
    card = js_function("cardInner")
    assert "stageBadge(t)" in card
    assert 'data-copy="' in card
    # 抽屉里也有一份
    drawer = js_function("drawerHTML")
    assert "stageBadge(t)" in drawer
    assert "已推送给" in drawer


def test_index_has_board_slot():
    """表头的 board 摘要槽位必须在，且默认隐藏（旧快照不占位）。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="board"' in html
    m = re.search(r'<span class="board" id="board"[^>]*>', html)
    assert m and "hidden" in m.group(0)


def test_style_has_stage_and_copy_rules():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".badge.stage-badge" in css
    assert '[data-stage="dispatched"]' in css
    assert ".act-copy" in css
    assert ".board[hidden]" in css


# --------------------------------------------------------------------------- #
# 3. 降级层：缺字段时静默降级，绝不渲染 undefined
# --------------------------------------------------------------------------- #

def test_normalize_defaults_stage_fields():
    body = js_function("normalize")
    assert "stage: t.stage || ''" in body
    assert "dispatched_to: t.dispatched_to || ''" in body
    assert "dispatched_at: t.dispatched_at || ''" in body


def test_board_counts_falls_back_then_hides():
    """board 优先用服务端口径，其次按 stage 现算，都没有就返回 null（调用方隐藏）。"""
    body = js_function("boardCounts")
    assert "state.board && state.board.counts" in body
    assert "if (!t.stage) return;" in body
    assert "return seen ? { counts: counts, total: total } : null;" in body

    render = js_function("renderBoard")
    assert "el.board.hidden = true" in render       # 没数据就整条隐藏
    assert "el.board.textContent = ''" in render
    assert "|| 0" in render                          # 计数缺失按 0，不显示 undefined


def test_stage_label_has_unknown_fallback():
    """未登记的阶段名回落成原名，而不是 undefined。"""
    src = APP_JS.read_text(encoding="utf-8")
    assert "return STAGE_LABEL[s] || s || '';" in src


def test_old_snapshot_without_stage_renders_no_badge():
    """没有 stage 的旧行：徽标函数返回空串（不产出空 span）。"""
    body = js_function("stageBadge")
    assert "if (s) {" in body
    assert "var s = t.stage || '';" in body
