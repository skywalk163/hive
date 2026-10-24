"""LLM 总控（T-0127）测试：in-process 后端、决策循环、apply 安全闸、真实 MCP stdio 客户端。"""
import json
from pathlib import Path
from unittest import mock

from hive import Hive

from llm_controller import (ALLOWED, InProcessBackend, McpStdioBackend, OpenAiClient,
                            HeuristicClient, MockClient, ControllerLoop, _parse_action)
from mcp_server import HiveMcp


def _backend(world, fake_cmd, agent="controller"):
    world.add_agent(agent, mode="exec", command=fake_cmd)
    impl = HiveMcp(Path(world.hive1), agent, role="controller")
    return InProcessBackend(impl)


def _res_refs(world, tid):
    return world.refs(f"refs/heads/res/{tid}")


def _to_land(backend):
    snap = backend.call_tool("hive_whoami", {}) or {}
    return snap.get("details", {}).get("todo", {}).get("to_land", [])


def _to_review(backend):
    snap = backend.call_tool("hive_whoami", {}) or {}
    return snap.get("details", {}).get("todo", {}).get("needs_review", [])


# -- 后端 & 工具面 --------------------------------------------------------- #
def test_inprocess_list_tools(world, fake_cmd):
    backend = _backend(world, fake_cmd)
    names = {t["name"] for t in backend.list_tools()}
    assert "hive_land" in names and "hive_whoami" in names and "done" in names
    # 12 个 controller MCP 工具 + 合成 done
    assert len([n for n in names if n != "done"]) == len(ALLOWED)
    assert "hive_claim" not in names  # 非 controller 工具不暴露给总控


def test_inprocess_call_tool_whoami(world, fake_cmd):
    backend = _backend(world, fake_cmd)
    res = backend.call_tool("hive_whoami", {})
    assert isinstance(res, dict)
    assert res.get("status") == "success"
    assert "todo" in res["details"]


def test_inprocess_call_tool_unknown(world, fake_cmd):
    backend = _backend(world, fake_cmd)
    res = backend.call_tool("no_such_tool", {})
    assert res.get("status") == "error"


# -- 决策循环：MockClient 驱动 --------------------------------------------- #
def test_mock_lands_done_card(world, fake_cmd):
    world.add_agent("fb", mode="exec", command=fake_cmd)
    world.add_task("T-0001", verify=["true"], repo="light")
    world.hive("assign", "T-0001", "--agent", "fb")  # worker 只接 inbox 里分派给它的卡
    world.run_worker("fb")  # → 跑出结果 done、state 仍是 assigned/running 的「可 land」卡
    assert _res_refs(world, "T-0001"), "run_worker 没产出结果分支"
    backend = _backend(world, fake_cmd)
    assert "T-0001" in _to_land(backend), "提交后应该进入 to_land"
    loop = ControllerLoop(
        backend,
        MockClient([{"tool": "hive_land", "arguments": {"all": True, "apply": True}}]),
        apply=True)
    loop.run()
    # land + close 会删掉结果分支，卡退出了 to_land
    assert _res_refs(world, "T-0001") == []
    assert "T-0001" not in _to_land(backend)


def test_preview_does_not_mutate(world, fake_cmd):
    world.add_agent("fb", mode="exec", command=fake_cmd)
    world.add_task("T-0001", verify=["true"], repo="light")
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")
    backend = _backend(world, fake_cmd)
    loop = ControllerLoop(
        backend,
        MockClient([{"tool": "hive_land", "arguments": {"all": True, "apply": True}}]),
        apply=False)  # preview
    loop.run()
    # 没 --apply：land 被强制 dry-run，结果分支仍在，卡仍在 to_land
    assert _res_refs(world, "T-0001"), "preview 不该 land（结果分支应保留）"
    assert "T-0001" in _to_land(backend)


def test_llm_closes_needs_review(world, fake_cmd):
    """LLM 能在人工闸（needs_review）上做决策：判可接受后 hive_close。"""
    world.add_agent("fb", mode="exec", command=fake_cmd)
    world.add_task("T-0001", verify=["true"], repo="light")
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")
    # 把卡挪到 needs_review 并推到 origin
    h = Hive(world.hive1)
    h._set_state("T-0001", "needs_review", "需人工确认", who="controller")
    world.sync()
    backend = _backend(world, fake_cmd)
    assert "T-0001" in _to_review(backend)
    loop = ControllerLoop(
        backend,
        MockClient([{"tool": "hive_close", "arguments": {"task_id": "T-0001", "apply": True}}]),
        apply=True)
    loop.run()
    # close 删结果分支、退出 needs_review
    assert _res_refs(world, "T-0001") == []
    assert "T-0001" not in _to_review(backend)


def test_heuristic_preview_no_mutate(world, fake_cmd):
    world.add_agent("fb", mode="exec", command=fake_cmd)
    world.add_task("T-0001", verify=["true"], repo="light")
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")  # landable
    backend = _backend(world, fake_cmd)
    loop = ControllerLoop(backend, HeuristicClient(), apply=False)
    loop.run()
    # heuristic 在 preview 下只会 dry-run，不 close
    assert _res_refs(world, "T-0001"), "preview 不该 close/land"
    assert "T-0001" in _to_land(backend)


def test_heuristic_leaves_decision_cards_for_human(world, fake_cmd):
    world.add_agent("fb", mode="exec", command=fake_cmd)
    world.add_task("T-0001", verify=["true"], repo="light")
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")
    h = Hive(world.hive1)
    h._set_state("T-0001", "needs_review", "需人工", who="controller")
    world.sync()
    backend = _backend(world, fake_cmd)
    # heuristic 对 needs_review 直接 done，不会去 close
    loop = ControllerLoop(backend, HeuristicClient(), apply=True)
    loop.run()
    assert _res_refs(world, "T-0001"), "heuristic 不该动 needs_review 卡"


# -- OpenAI 客户端解析 & 安全闸 -------------------------------------------- #
def test_parse_action_fallback():
    text = '好的，我来选：\n{"tool": "hive_dispatch", "arguments": {"apply": true}}\n以上'
    act = _parse_action(text)
    assert act["tool"] == "hive_dispatch"
    assert act["arguments"] == {"apply": True}


def test_openai_client_parse(world, fake_cmd):
    payload = {"choices": [{"message": {"content":
        '{"tool": "hive_land", "arguments": {"all": true, "apply": true}}'}}]}
    backend = _backend(world, fake_cmd)

    class FakeResp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
        client = OpenAiClient("http://example/v1", "m", "k", backend.list_tools())
        act = client.decide("state", [], snapshot=None)
    assert act["tool"] == "hive_land"
    assert act["arguments"]["all"] is True


def test_execute_safety_gate():
    backend = mock.MagicMock()
    backend.call_tool.return_value = {"status": "success"}
    # preview 下：DRYRUN_SAFE 工具被强制 apply=false
    loop = ControllerLoop(backend, MockClient([]), apply=False)
    loop._execute("hive_land", {"all": True, "apply": True})
    assert backend.call_tool.call_args[0][1]["apply"] is False
    # preview 下：FORCE_MUTATING 工具直接跳过，不调用
    backend.reset_mock()
    res = loop._execute("hive_close", {"task_id": "T-1"})
    assert res.get("status") == "skipped"
    backend.call_tool.assert_not_called()


# -- 真实 MCP stdio 客户端 ------------------------------------------------- #
def test_mcp_stdio_backend(world, fake_cmd):
    world.add_agent("controller", mode="exec", command=fake_cmd)
    world.sync()
    backend = McpStdioBackend("controller", Path(world.hive1), role="controller")
    try:
        names = {t["name"] for t in backend.list_tools()}
        assert "hive_land" in names and "done" in names
        res = backend.call_tool("hive_whoami", {})
        assert isinstance(res, dict)
    finally:
        backend.close()
