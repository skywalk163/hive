#!/usr/bin/env python3
"""hive LLM 总控 —— 用 controller MCP 工具面让 LLM 逐轮决策。

和 T-0126 的确定性 `hive orchestrate`（watchdog→dispatch→land→doctor 死循环）互补：
orchestrate 只走稳定路径（verify 全绿就 land），本模块把「需要判断」的卡点——
needs_review / conflict / failed / blocked，以及「哪张 ready 卡派给哪个 agent」这类带偏好的抉择——
交给 LLM。`hive llm` 每轮把 hive 当前状态喂给 LLM，让它从 controller MCP 工具里挑一个动作执行，
直到它认为没事可做。

设计要点：
- 工具后端 `ToolBackend` 把 controller MCP 工具面暴露成 {list_tools, call_tool}：
  - `InProcessBackend`：直接复用 `mcp_server.HiveMcp` 的 handler（== controller MCP 工具面），
    无需再起一个进程；
  - `McpStdioBackend`：真的 spawn `mcp_server.py --role controller`，走 JSON-RPC 握手，
    是货真价实的 MCP 客户端。
- LLM 客户端可插拔：`OpenAiClient`（标准库 urllib，OpenAI 兼容）、`HeuristicClient`
  （无 key 时的可用回退，等价于「不做危险决策的 orchestrate」）、`MockClient`（测试）。
- 决策循环带 **apply 安全闸**：没传 `--apply` 时，dry-run 类工具（land/dispatch/gc/watchdog）
  强制 apply=false；`hive_close/hive_retry/hive_assign` 这种无 dry-run 的破坏性操作直接跳过不执行。

零第三方依赖（连 LLM 调用都只用标准库 urllib）。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_server  # noqa: E402
from mcp_server import HiveMcp, TOOLS  # noqa: E402

MCP_SERVER = Path(__file__).resolve().parent / "mcp_server.py"

# controller MCP 工具面（只读 introspection + 会改 main 的 7 个 controller 工具）
ALLOWED = {
    "hive_whoami", "hive_list_tasks", "hive_get_task", "hive_stats", "hive_doctor",
    "hive_land", "hive_close", "hive_retry", "hive_assign", "hive_dispatch",
    "hive_gc", "hive_watchdog",
}
# 带 apply 的「可预演」工具：preview 模式下强制 apply=false 即无害 dry-run
DRYRUN_SAFE = {"hive_land", "hive_dispatch", "hive_gc", "hive_watchdog"}
# 无 dry-run 的破坏性工具：preview 模式下绝不执行
FORCE_MUTATING = {"hive_close", "hive_retry", "hive_assign"}

DONE_TOOL = {
    "name": "done",
    "description": "本轮没有可执行、且有益的下一步动作，结束总控循环。",
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}


# --------------------------------------------------------------------------- #
# 工具后端
# --------------------------------------------------------------------------- #
class ToolBackend:
    def list_tools(self) -> list[dict]:
        raise NotImplementedError

    def call_tool(self, name: str, args: dict) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass


class InProcessBackend(ToolBackend):
    """复用 mcp_server.HiveMcp 的 handler——也就是 controller MCP 工具面本身。"""

    def __init__(self, impl: HiveMcp):
        self.impl = impl
        self._tools = [t for t in TOOLS if t["name"] in ALLOWED] + [DONE_TOOL]

    def list_tools(self) -> list[dict]:
        return self._tools

    def call_tool(self, name: str, args: dict) -> dict:
        if name == "done":
            return {"status": "ok", "message": "done"}
        handler = HiveMcp.HANDLERS.get(name)
        if handler is None:
            return {"status": "error", "message": f"没有工具 {name}"}
        buf = io.StringIO()
        try:
            # stdout 被协议/Hive 内部 print 占用，统一赶到 stderr
            with contextlib.redirect_stdout(buf):
                env = handler(self.impl, args or {})
        except Exception as e:  # 兜底：不要让总控循环崩
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}
        return env if isinstance(env, dict) else {"status": "ok", "message": str(env)}


class McpStdioBackend(ToolBackend):
    """真·MCP 客户端：spawn mcp_server.py，走 JSON-RPC（initialize→tools/list→tools/call）。"""

    def __init__(self, agent: str, hive_dir: Path, role: str = "controller"):
        self._id = 0
        self.proc = subprocess.Popen(
            [sys.executable, str(MCP_SERVER), "--agent", agent,
             "--role", role, "--dir", str(hive_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._tools = None
        self._init()

    def _send(self, msg: dict):
        # 通知（notification）不需要回应，发了就走，绝不等待
        if str(msg.get("method", "")).startswith("notifications/"):
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
            return None
        self._id += 1
        self.proc.stdin.write(json.dumps({**msg, "id": self._id}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == self._id:
                return obj

    def _init(self) -> None:
        import threading

        result: dict = {}

        def _handshake():
            self._send({"jsonrpc": "2.0", "method": "initialize", "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "llm-controller", "version": "0.9.7"}}})
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            result["resp"] = self._send({"jsonrpc": "2.0", "method": "tools/list",
                                        "params": {}})

        t = threading.Thread(target=_handshake, daemon=True)
        t.start()
        t.join(timeout=20)
        if t.is_alive():
            self.close()
            raise RuntimeError("MCP server 握手超时（20s）")
        resp = result.get("resp")
        all_tools = (resp.get("result", {}) or {}).get("tools", []) if resp else []
        self._tools = [t for t in all_tools if t["name"] in ALLOWED] + [DONE_TOOL]

    def list_tools(self) -> list[dict]:
        return self._tools or []

    def call_tool(self, name: str, args: dict) -> dict:
        if name == "done":
            return {"status": "ok", "message": "done"}
        resp = self._send({"jsonrpc": "2.0", "method": "tools/call",
                           "params": {"name": name, "arguments": args or {}}})
        if not resp:
            return {"status": "error", "message": "MCP server 无响应"}
        result = resp.get("result", {}) or {}
        content = result.get("content") or []
        text = (content[0].get("text", "{}") if content else "{}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"status": "error", "message": text}

    def close(self) -> None:
        try:
            self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "shutdown"}) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# LLM 客户端
# --------------------------------------------------------------------------- #
class LlmClient:
    def decide(self, state_text: str, history: list, snapshot: dict | None = None) -> dict:
        raise NotImplementedError


def _render_tools(tools: list[dict]) -> str:
    parts = []
    for t in tools:
        sch = t.get("inputSchema", {})
        props = sch.get("properties", {})
        req = set(sch.get("required", []))
        plines = []
        for pname, pinfo in props.items():
            flag = "（必填）" if pname in req else ""
            plines.append(f"    - {pname} ({pinfo.get('type', 'any')}){flag}：{pinfo.get('description', '')}")
        parts.append(f"- {t['name']}：{t.get('description', '')}\n" + "\n".join(plines))
    return "\n".join(parts)


def _parse_action(text: str) -> dict:
    """从模型输出里抠出 {"tool":..., "arguments":...}。"""
    text = (text or "").strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # 兜底：抠出第一个 { 到最后一个 } 之间的内容
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e == -1 or e <= s:
            raise ValueError(f"无法解析模型输出为 JSON 动作：{text[:200]!r}")
        obj = json.loads(text[s:e + 1])
    if not isinstance(obj, dict) or not obj.get("tool"):
        raise ValueError(f"动作缺少 tool 字段：{text[:200]!r}")
    return {"tool": obj["tool"], "arguments": obj.get("arguments") or {}}


SYSTEM_PROMPT = """你是 hive 多 agent 任务总线的「总控 LLM」。你逐轮决定下一步操作，每轮只能选一个工具执行。

只读工具（hive_whoami / hive_list_tasks / hive_get_task / hive_stats / hive_doctor）用来看清现状；
会改 main 的工具（hive_dispatch / hive_land / hive_watchdog / hive_retry / hive_close /
hive_assign / hive_gc）用来推进任务。

决策原则：
1. 先用只读工具确认状态，尤其 needs_review / conflict / blocked 的卡，先看 hive_get_task 的内容与结果。
2. 遇到合入冲突（hive_land 返回 conflicts）：不要尝试自己解冲突，返回 done 留给人工。
3. needs_review 的卡：若 verify 全绿、diff 无明显风险，可调 hive_land 合入；否则返回 done 留给人工。
4. failed / conflict 的卡：先看结果，再决定 hive_retry（重跑）、hive_assign（换 agent）或 hive_close。
5. 没事可做了就返回 {"tool": "done", "arguments": {}}。

只输出一个 JSON 对象（不要解释、不要 markdown 代码块）：
{"tool": <工具名>, "arguments": {<参数>}}"""


class OpenAiClient(LlmClient):
    """OpenAI 兼容端点（标准库 urllib，无第三方依赖）。"""

    def __init__(self, base_url: str, model: str, api_key: str, tools: list[dict]):
        self.base = base_url.rstrip("/")
        self.model = model
        self.key = api_key
        self.tools = tools

    def decide(self, state_text: str, history: list, snapshot: dict | None = None) -> dict:
        messages = [
            {"role": "system",
             "content": SYSTEM_PROMPT + "\n\n可用工具：\n" + _render_tools(self.tools)},
            {"role": "user",
             "content": _history_block(history)
                        + "\n\n当前 hive 状态：\n" + state_text
                        + "\n\n只输出一个 JSON 动作。"},
        ]
        payload = json.dumps({"model": self.model, "messages": messages,
                             "temperature": 0}).encode()
        req = urllib.request.Request(
            self.base + "/chat/completions", data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}" if self.key else ""})
        with urllib.request.urlopen(req, timeout=180) as resp:
            obj = json.loads(resp.read().decode())
        content = (obj.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return _parse_action(content)


class HeuristicClient(LlmClient):
    """无 key 时的可用回退：确定性策略，等价于「不做危险决策的 orchestrate」。"""

    def __init__(self):
        self._tried: set[str] = set()

    def decide(self, state_text: str, history: list, snapshot: dict | None = None) -> dict:
        todo = (snapshot or {}).get("todo") or {}
        if todo.get("to_land") and "land" not in self._tried:
            self._tried.add("land")
            return {"tool": "hive_land", "arguments": {"all": True, "apply": True}}
        if todo.get("ready") and "dispatch" not in self._tried:
            self._tried.add("dispatch")
            return {"tool": "hive_dispatch", "arguments": {"apply": True}}
        # 需要判断的卡点：默认留给人工，不自动动
        if todo.get("conflict") or todo.get("needs_review") or todo.get("blocked"):
            return {"tool": "done", "arguments": {}}
        if "watchdog" not in self._tried:
            self._tried.add("watchdog")
            return {"tool": "hive_watchdog", "arguments": {"apply": True}}
        return {"tool": "done", "arguments": {}}


class MockClient(LlmClient):
    """测试用：依次吐出 script 里的动作，用完返回 done。"""

    def __init__(self, script: list[dict]):
        self.script = list(script)

    def decide(self, state_text: str, history: list, snapshot: dict | None = None) -> dict:
        if self.script:
            return self.script.pop(0)
        return {"tool": "done", "arguments": {}}


def _history_block(history: list) -> str:
    if not history:
        return "（还没有执行过任何动作）"
    lines = ["已执行的动作："]
    for h in history[-6:]:
        r = h.get("result") or {}
        status = r.get("status", "?")
        lines.append(f"  - {h['tool']}({h.get('args', {})}) → {status}: "
                     f"{(r.get('message') or '')[:80]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 决策循环
# --------------------------------------------------------------------------- #
class ControllerLoop:
    def __init__(self, backend: ToolBackend, llm: LlmClient,
                 apply: bool = False, max_iter: int = 12, stop_file: str | None = None):
        self.backend = backend
        self.llm = llm
        self.apply = apply
        self.max_iter = max_iter
        self.stop_file = Path(stop_file) if stop_file else None
        self.history: list[dict] = []

    def _snapshot(self) -> dict:
        return self.backend.call_tool("hive_whoami", {})

    def _render(self, snap: dict) -> str:
        todo = (snap or {}).get("details", {}).get("todo") or {}
        lines = ["总控待办："]
        for k in ("to_land", "conflict", "blocked", "needs_review", "ready"):
            v = todo.get(k) or []
            lines.append(f"  - {k}: {len(v)} 张 {v}")
        mine = (snap or {}).get("mine") or []
        if mine:
            lines.append(f"  - 我名下（in-flight）: {len(mine)} 张")
        return "\n".join(lines)

    def _execute(self, tool: str, args: dict) -> dict:
        if tool in FORCE_MUTATING and not self.apply:
            return {"status": "skipped",
                    "message": f"PREVIEW 模式不执行破坏性操作 {tool}（需 --apply）"}
        if tool in DRYRUN_SAFE and not self.apply:
            args = dict(args)
            args["apply"] = False
        return self.backend.call_tool(tool, args)

    def run(self) -> list[dict]:
        for _ in range(self.max_iter):
            if self.stop_file and self.stop_file.exists():
                break
            snap = self._snapshot()
            state_text = self._render(snap)
            try:
                action = self.llm.decide(state_text, self.history, snapshot=snap)
            except Exception as e:  # LLM 解析失败：保守收尾
                action = {"tool": "done", "arguments": {}, "_error": str(e)}
            if not isinstance(action, dict) or not action.get("tool"):
                break
            if action["tool"] == "done":
                break
            tool = action["tool"]
            if tool not in ALLOWED and tool != "done":
                # 不在工具面内：跳过这一轮，避免把未知动作当命令执行
                self.history.append({"tool": tool, "args": action.get("arguments", {}),
                                     "result": {"status": "error",
                                                "message": f"未知工具 {tool}"}})
                if len(self.history) > 8:
                    self.history.pop(0)
                continue
            res = self._execute(tool, action.get("arguments") or {})
            self.history.append({"tool": tool, "args": action.get("arguments", {}),
                                 "result": res})
            if len(self.history) > 8:
                self.history.pop(0)
        return self.history


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_backend(args) -> ToolBackend:
    hive_dir = Path(args.dir)
    if getattr(args, "mcp", False):
        return McpStdioBackend(args.agent, hive_dir, role="controller")
    impl = HiveMcp(hive_dir, args.agent, role="controller")
    return InProcessBackend(impl)


def run_llm(args) -> int:
    backend = _build_backend(args)
    tools = backend.list_tools()
    if args.llm == "openai":
        base = args.base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        model = args.model or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        key = args.api_key or os.environ.get("OPENAI_API_KEY") or ""
        if not key:
            print("[llm] 未设置 OPENAI_API_KEY，回退到 heuristic 模式", file=sys.stderr)
            llm: LlmClient = HeuristicClient()
        else:
            llm = OpenAiClient(base, model, key, tools)
    else:
        llm = HeuristicClient()
    loop = ControllerLoop(backend, llm, apply=args.apply,
                         max_iter=1 if args.once else args.max_iter,
                         stop_file=args.stop_file)
    print(f"[llm] 启动：agent={args.agent} llm={args.llm} "
          f"apply={args.apply} max_iter={loop.max_iter}", file=sys.stderr)
    history = loop.run()
    for h in history:
        r = h.get("result") or {}
        print(f"  · {h['tool']}({h.get('args', {})}) → {r.get('status', '?')}: "
              f"{(r.get('message') or '')[:120]}")
    print(f"[llm] 结束，共执行 {len(history)} 个动作", file=sys.stderr)
    backend.close()
    return 0


def add_subparser(sub):
    s = sub.add_parser("llm", help="LLM 总控：用 controller MCP 工具面让 LLM 逐轮决策")
    s.add_argument("--agent", default="controller", help="总控 agent 名（默认 controller）")
    s.add_argument("--mcp", action="store_true",
                   help="走真正的 MCP stdio 客户端（spawn mcp_server.py），默认 in-process 复用")
    s.add_argument("--llm", choices=["openai", "heuristic"], default="heuristic",
                   help="决策后端：openai=真 LLM；heuristic=无 key 回退（默认）")
    s.add_argument("--model", default=None, help="覆盖 ${OPENAI_MODEL}")
    s.add_argument("--base-url", default=None, help="覆盖 ${OPENAI_BASE_URL}")
    s.add_argument("--api-key", default=None, help="覆盖 ${OPENAI_API_KEY}")
    s.add_argument("--apply", action="store_true", help="真改 main（默认只预演）")
    s.add_argument("--max-iter", type=int, default=12, help="最多决策几轮")
    s.add_argument("--stop-file", default=None, help="出现该文件就停止循环")
    s.add_argument("--once", action="store_true", help="只跑一轮决策就退")
    s.set_defaults(func=cmd_llm)
    return s


def cmd_llm(args) -> None:
    run_llm(args)


# 允许独立运行：python3 llm_controller.py --dir <hive> [--llm openai] [--apply]
def main(argv=None):
    p = argparse.ArgumentParser(prog="llm_controller", description="hive LLM 总控")
    p.add_argument("--dir", required=True, help="hive 协调仓目录")
    p.add_argument("--agent", default="controller")
    p.add_argument("--mcp", action="store_true")
    p.add_argument("--llm", choices=["openai", "heuristic"], default="heuristic")
    p.add_argument("--model", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--api-key", default=None)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--max-iter", type=int, default=12)
    p.add_argument("--stop-file", default=None)
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv)
    run_llm(args)


if __name__ == "__main__":
    main()
