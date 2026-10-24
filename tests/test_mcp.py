"""hive MCP server 的端到端测试。

起一个真实的 `mcp_server.py` 子进程，走 stdio JSON-RPC，覆盖：
协议握手、工具清单、查任务、挑卡、领卡、提交、终止、续租、跨 agent 越权拦截、协议错误。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import HIVE, sh

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MCP = ROOT / "mcp_server.py"

EXPECTED_TOOLS = {
    "hive_whoami", "hive_list_tasks", "hive_get_task", "hive_next",
    "hive_claim", "hive_submit", "hive_abort", "hive_extend_lease",
    "hive_progress", "hive_result", "hive_stats", "hive_doctor",
}


class McpClient:
    """极简 MCP stdio 客户端，够测试用。"""

    def __init__(self, agent: str, hive_dir: Path, state_dir: Path, env: dict,
                 role: str = "worker"):
        env = {
            **os.environ,
            **env,
            "HIVE_AGENT": agent,
            "HIVE_ROLE": role,
            "HIVE_STATE": str(state_dir),
        }
        self.proc = subprocess.Popen(
            [sys.executable, str(MCP), "--agent", agent, "--role", role,
             "--dir", str(hive_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(ROOT), env=env, bufsize=1,
        )
        self._id = 0
        self.agent = agent

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method,
             "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"MCP server 提前退出：{self.proc.stderr.read()}"
        return json.loads(line)

    def initialize(self) -> dict:
        r = self.request("initialize", {})
        assert "result" in r, r
        return r["result"]

    def tools(self) -> list[dict]:
        return self.request("tools/list", {})["result"]["tools"]

    def call(self, name: str, **args):
        """返回 (信封 dict, isError)。"""
        r = self.request("tools/call", {"name": name, "arguments": args})
        assert "result" in r, r
        content = r["result"]["content"]
        assert content and content[0]["type"] == "text"
        return json.loads(content[0]["text"]), r["result"].get("isError", False)

    def ok(self, name: str, **args) -> dict:
        env, is_err = self.call(name, **args)
        assert env["status"] == "success", f"{name} 失败：{env}"
        assert not is_err
        return env

    def fail(self, name: str, **args) -> dict:
        env, is_err = self.call(name, **args)
        assert env["status"] == "error", f"{name} 本该失败：{env}"
        assert is_err, "失败的工具调用必须带 isError=true"
        return env

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture()
def mcp(world):
    """每起一个 MCP client 就配一个独立的 state 目录。"""
    clients: list[McpClient] = []

    def _make(agent: str, hive_dir: Path | None = None, role: str = "worker"):
        c = McpClient(agent, hive_dir or world.hive2,
                      world.root / f"mcp-state-{agent}", world.git_env, role=role)
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.close()


# --------------------------------------------------------------------------- #
# 协议层
# --------------------------------------------------------------------------- #

def test_mcp_handshake_and_tool_list(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    c = mcp("a1")
    init = c.initialize()
    assert init["protocolVersion"] == "2024-11-05"
    assert init["serverInfo"]["name"] == "hive"
    assert "tools" in init["capabilities"]
    assert "a1" in init["instructions"]  # 身份写进 instructions

    tools = c.tools()
    names = {t["name"] for t in tools}
    assert names == EXPECTED_TOOLS
    for t in tools:
        assert t["description"].strip(), f"{t['name']} 缺描述"
        assert t["inputSchema"]["type"] == "object"
        assert "properties" in t["inputSchema"]
    # 需要参数的工具必须标 required，否则客户端不知道要传什么
    for name in ("hive_get_task", "hive_claim", "hive_submit", "hive_abort",
                 "hive_extend_lease", "hive_result"):
        t = next(t for t in tools if t["name"] == name)
        assert "task_id" in t["inputSchema"]["required"], name


def test_mcp_unknown_tool_is_protocol_error(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    c = mcp("a1")
    c.initialize()
    r = c.request("tools/call", {"name": "hive_destroy_everything", "arguments": {}})
    assert "error" in r
    assert r["error"]["code"] == -32602


def test_mcp_bad_json_gets_parse_error(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    c = mcp("a1")
    c.proc.stdin.write("{not json\n")
    c.proc.stdin.flush()
    r = json.loads(c.proc.stdout.readline())
    assert "error" in r
    assert r["error"]["code"] == -32700


def test_mcp_stdout_carries_only_protocol(world, mcp):
    """stdout 被协议独占：任何 print 调试都会污染传输。stderr 随便写。"""
    world.add_agent("a1", caps=("python",), command="true")
    c = mcp("a1")
    c.initialize()
    c.ok("hive_stats")
    c.close()
    assert c.proc.stderr.read().strip(), "stderr 应该有启动日志"


# --------------------------------------------------------------------------- #
# 读操作
# --------------------------------------------------------------------------- #

def test_mcp_whoami_and_list_tasks(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    world.add_task("T-0100", title="写文档", paths=("docs/",), needs=("python",))
    world.add_task("T-0101", title="改代码", paths=("src/",), needs=("rust",))

    c = mcp("a1")
    c.initialize()
    who = c.ok("hive_whoami")["details"]
    assert who["agent"] == "a1"
    assert who["capabilities"] == ["python"]
    assert who["claimable"] == ["T-0100"], "rust 的卡不该出现在我能领的列表里"

    listed = c.ok("hive_list_tasks", limit=10)["details"]["tasks"]
    assert {t["id"] for t in listed} == {"T-0100", "T-0101"}
    assert all(t["state"] == "ready" for t in listed)

    got = c.ok("hive_get_task", task_id="T-0100")["details"]
    assert got["card"]["title"] == "写文档"
    assert got["card"]["paths"] == ["docs/"]
    assert got["claim"] is None and got["result"] is None

    c.fail("hive_get_task", task_id="T-9999")  # 不存在的卡


def test_mcp_next_skips_unqualified(world, mcp):
    world.add_agent("a1", caps=("python",))
    world.add_task("T-0100", title="够不着的卡", needs=("windows",))
    c = mcp("a1")
    c.initialize()
    env = c.fail("hive_next")
    assert "hint" in env["details"]

    # 卡是写在 hive1 再推上去的；同一个 client 再问一次也该看得见（每次调用都拉最新）
    world.add_task("T-0101", title="能干的卡", needs=("python",))
    nxt = c.ok("hive_next")["details"]
    assert nxt["task_id"] == "T-0101"
    assert "hive_claim" in nxt["hint"]


# --------------------------------------------------------------------------- #
# 写操作：领卡 → 干活 → 提交
# --------------------------------------------------------------------------- #

def test_mcp_claim_submit_roundtrip(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    world.add_task("T-0100", title="加一个文件", paths=("docs/",),
                   needs=("python",), verify=("test -f docs/mcp.txt",))

    c = mcp("a1")
    c.initialize()
    claimed = c.ok("hive_claim", task_id="T-0100")["details"]
    wt = Path(claimed["worktree"])
    assert (wt / ".hive" / "task.md").exists(), "提示词文件要备好"

    # 模拟 agent 干活：改代码 + 写 summary
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "mcp.txt").write_text("hello from mcp\n", encoding="utf-8")
    (wt / ".hive" / "summary.md").write_text("用 MCP 提交的第一张卡\n", encoding="utf-8")

    submitted = c.ok("hive_submit", task_id="T-0100")["details"]
    assert submitted["status"] == "done"
    assert submitted["branch"] == "task/T-0100"
    assert submitted["head"]
    assert submitted["verify"][0]["code"] == 0

    # 结果真的落进 res 分支了
    res = c.ok("hive_result", task_id="T-0100")["details"]["result"]
    assert res["status"] == "done"
    assert res["summary"].startswith("用 MCP 提交")
    assert "refs/heads/res/T-0100" in world.refs("refs/heads/res/*")

    # 已经出结果的卡不能再提交，也不能再终止
    c.fail("hive_submit", task_id="T-0100")
    c.fail("hive_abort", task_id="T-0100")


def test_mcp_claim_fails_when_taken(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    world.add_agent("a2", caps=("python",), command="true")
    world.add_task("T-0100", title="独占卡", needs=("python",))

    c1 = mcp("a1")
    c1.initialize()
    c1.ok("hive_claim", task_id="T-0100")

    c2 = mcp("a2")
    c2.initialize()
    env = c2.fail("hive_claim", task_id="T-0100")
    assert "a1" in env["message"], "要告诉 a2 是谁抢走的"


# --------------------------------------------------------------------------- #
# 越权拦截
# --------------------------------------------------------------------------- #

def test_mcp_cannot_submit_or_abort_others_task(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    world.add_agent("a2", caps=("python",), command="true")
    world.add_task("T-0100", title="a1 的卡", needs=("python",))

    c1 = mcp("a1")
    c1.initialize()
    c1.ok("hive_claim", task_id="T-0100")

    c2 = mcp("a2")
    c2.initialize()
    env = c2.fail("hive_submit", task_id="T-0100")
    assert "不归我" in env["message"]
    env = c2.fail("hive_abort", task_id="T-0100")
    assert "不能替别人终止" in env["message"]
    env = c2.fail("hive_extend_lease", task_id="T-0100")
    assert "不能替别人续期" in env["message"]


def test_mcp_submit_without_claim_is_rejected(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    world.add_task("T-0100", title="没领就想交", needs=("python",))
    c = mcp("a1")
    c.initialize()
    env = c.fail("hive_submit", task_id="T-0100")
    assert "不归我" in env["message"]


# --------------------------------------------------------------------------- #
# 终止 / 续租 / 进度
# --------------------------------------------------------------------------- #

def test_mcp_abort_releases_lease_and_requeues(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    world.add_task("T-0100", title="半途而废", needs=("python",))

    c = mcp("a1")
    c.initialize()
    c.ok("hive_claim", task_id="T-0100")
    assert "refs/heads/claim/T-0100" in world.refs("refs/heads/claim/*")

    aborted = c.ok("hive_abort", task_id="T-0100", reason="环境起不来")["details"]
    assert aborted["requeued"] is True
    assert "refs/heads/claim/T-0100" not in world.refs("refs/heads/claim/*")

    # 卡退回 ready，别人可以接手（先同步，卡片存在工作区文件里）
    world.sync()
    card = world.card("T-0100")
    assert card["state"] == "ready"
    assert not card["owner"]


def test_mcp_abort_keep_ownership(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    world.add_task("T-0100", title="只是想续一下", needs=("python",))
    c = mcp("a1")
    c.initialize()
    c.ok("hive_claim", task_id="T-0100")
    env = c.ok("hive_abort", task_id="T-0100", requeue=False)["details"]
    assert env["requeued"] is False
    world.sync()
    card = world.card("T-0100")
    assert card["owner"] == "a1", "requeue=False 时卡仍归我"
    assert "refs/heads/claim/T-0100" not in world.refs("refs/heads/claim/*")


def test_mcp_extend_lease(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    world.add_task("T-0100", title="慢工出细活", needs=("python",))
    c = mcp("a1")
    c.initialize()
    c.ok("hive_claim", task_id="T-0100")
    before = c.ok("hive_get_task", task_id="T-0100")["details"]["claim"]["lease_until"]
    after = c.ok("hive_extend_lease", task_id="T-0100", minutes=120)["details"]["lease_until"]
    assert after > before, "续租后到期时间必须往后挪"


def test_mcp_progress_is_recorded(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    world.add_task("T-0100", title="长任务", needs=("python",))
    c = mcp("a1")
    c.initialize()
    claimed = c.ok("hive_claim", task_id="T-0100")["details"]
    p = c.ok("hive_progress", task_id="T-0100", text="跑到一半了")["details"]["path"]
    text = Path(p).read_text(encoding="utf-8")
    assert "跑到一半了" in text
    assert str(Path(p).parent.parent) == claimed["worktree"]


# --------------------------------------------------------------------------- #
# 体检与统计
# --------------------------------------------------------------------------- #

def test_mcp_doctor_and_stats(world, mcp):
    world.add_agent("a1", caps=("python",), command="true")
    world.add_task("T-0100", title="没写验收", needs=("python",))
    c = mcp("a1")
    c.initialize()

    doc = c.ok("hive_doctor")["details"]
    assert doc["healthy"] is False
    assert any("verify" in i for i in doc["issues"]), "缺 verify 的卡必须被点名"

    stats = c.ok("hive_stats")["details"]
    assert stats["tasks"] == 1
    assert stats["ready"] == 1
    assert stats["mine"] == []


# --------------------------------------------------------------------------- #
# 总控角色（controller）
# --------------------------------------------------------------------------- #

CONTROLLER_TOOL_NAMES = {
    "hive_land", "hive_close", "hive_retry", "hive_assign",
    "hive_dispatch", "hive_gc",
}


def test_worker_role_cannot_see_controller_tools(world, mcp):
    """权限隔离要有明确证据：worker 视角看不到任何总控工具。"""
    world.add_agent("a1", "exec", caps=["python"], command="true")
    c = mcp("a1")
    c.initialize()
    names = {t["name"] for t in c.tools()}
    assert not (names & CONTROLLER_TOOL_NAMES), (
        f"worker 视角泄漏了总控工具：{sorted(names & CONTROLLER_TOOL_NAMES)}")
    # instructions 也不该提总控
    init = c.initialize()
    assert "改动 main" not in init["instructions"]


def test_controller_role_has_extra_tools(world, mcp):
    world.add_agent("a1", "exec", caps=["python"], command="true")
    c = mcp("a1", role="controller")
    init = c.initialize()
    names = {t["name"] for t in c.tools()}
    assert CONTROLLER_TOOL_NAMES <= names, sorted(names)
    assert "改动 main" in init["instructions"], init["instructions"]
    # whoami 报角色和待办
    who = c.ok("hive_whoami")["details"]
    assert who["role"] == "controller", who
    assert "todo" in who and "to_land" in who["todo"], who


def test_controller_land_is_dry_run_by_default(world, mcp, fake_cmd):
    """hive_land 默认只预演；apply=true 才真的合入并 close。"""
    world.add_agent("a1", "exec", caps=["python"], command=fake_cmd)
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "a1")
    world.run_worker("a1")
    assert "T-0001" in world.code_refs("refs/heads/task/*")

    c = mcp("a1", role="controller")
    c.initialize()

    # 不传 apply：预演，什么都不动
    env, is_err = c.call("hive_land", task_id="T-0001")
    assert env["status"] == "success", env
    assert env["details"]["applied"] is False, env
    assert world.card("T-0001")["state"] != "done", "dry-run 不该 close"

    # apply=true：真的合入
    env2, _ = c.call("hive_land", task_id="T-0001", apply=True)
    assert env2["status"] == "success", env2
    assert env2["details"]["applied"] is True, env2
    world.sync()   # 卡片是工作区文件，land 推完 main 后要同步才读得到
    assert world.card("T-0001")["state"] == "done"
    assert "refs/heads/res/T-0001" not in world.refs("refs/heads/res/*")


def test_controller_land_reports_conflict_without_force(world, mcp, fake_cmd):
    """冲突时必须返回 error 和文件清单，绝不 force push。"""
    world.add_agent("a1", "exec", caps=["python"], command=fake_cmd)
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "a1")
    world.run_worker("a1")
    code = world.code_clone("a1")

    sh(["git", "fetch", "-q", "origin"], cwd=code, env=world.git_env)
    sh(["git", "checkout", "-q", "main"], cwd=code, env=world.git_env)
    (code / "feature.txt").write_text("总控侧的内容\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=code, env=world.git_env)
    sh(["git", "commit", "-q", "-m", "conflict maker"], cwd=code, env=world.git_env)
    sh(["git", "push", "-q", "origin", "main"], cwd=code, env=world.git_env)

    c = mcp("a1", role="controller")
    c.initialize()
    env, is_err = c.call("hive_land", task_id="T-0001", apply=True)
    assert env["status"] == "error", env
    assert is_err is True
    assert "conflicts" in env["details"], env
    head = sh(["git", "--git-dir", world.code_origin, "rev-parse",
               "main"]).stdout.strip()
    msg = sh(["git", "--git-dir", world.code_origin, "log", "-1",
              "--format=%s", head]).stdout.strip()
    assert msg == "conflict maker", msg
    world.sync()
    assert world.card("T-0001")["state"] != "done"


def test_worker_role_cannot_call_land(world, mcp, fake_cmd):
    """即使知道工具名，worker 视角也调不动总控工具。"""
    world.add_agent("a1", "exec", caps=["python"], command=fake_cmd)
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "a1")
    world.run_worker("a1")

    c = mcp("a1")  # 默认 worker
    c.initialize()
    r = c.request("tools/call", {"name": "hive_land",
                                 "arguments": {"task_id": "T-0001",
                                               "apply": True}})
    # tools/call 会走到 _call_tool，_need_controller 拒绝
    assert "result" in r, r
    env = json.loads(r["result"]["content"][0]["text"])
    assert env["status"] == "error", env
    assert "总控" in env["message"], env


def test_controller_retry_clears_result(world, mcp, fake_cmd):
    """hive_retry 之后结果被清掉，卡回到可重跑状态。"""
    world.add_agent("a1", "exec", caps=["python"], command=fake_cmd)
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "a1")
    world.run_worker("a1")
    assert world.result_refs("T-0001")

    c = mcp("a1", role="controller")
    c.initialize()
    env, _ = c.call("hive_retry", task_id="T-0001")
    assert env["status"] == "success", env
    assert not world.result_refs("T-0001"), "retry 后结果分支应该没了"
    world.sync()
    assert world.card("T-0001")["state"] == "assigned", world.card("T-0001")
