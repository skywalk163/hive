"""T-0206：Windows 子进程全局无窗（CREATE_NO_WINDOW 统一出口）的判据。

背景：Windows 上 console 子进程（git / tasklist / python …）在**父进程没有控制台**
时会自建一个可见控制台。看板 `refresh_loop` 每 30 秒跑一次 `hive status --json`
（内部 `fetch()` → git），于是每 30 秒闪一次黑窗。`DETACHED_PROCESS` 恰恰是帮凶：
它让子进程脱离父控制台，于是子进程只好自己开一个。

判据分三层，都是「有牙」的：

1. **AST 全量扫描**（:func:`test_every_subprocess_call_is_guarded`）—— 扫描全部
   生产模块，枚举 `subprocess.run/Popen/check_output/call/check_call`，要求每个
   调用点要么显式带 `creationflags=`，要么展开 `**no_window_kwargs()`。既没带也
   不在白名单里 → 红。白名单是**精确集合**：多一条（没人用的僵尸豁免）也红。
2. **单点钉死**（:func:`test_run_is_the_single_guarded_exit` 等）—— `hive.run()`
   是全仓命令出口，它掉链子等于全仓掉链子，单独钉住；看板 `refresh_loop` 同理。
3. **行为**（:func:`test_run_passes_creationflags_on_windows` 等）—— 真调一次
   `hive.run()`，看 kwargs 里到底有没有 `creationflags`；`HIVE_NO_WINDOW=0`
   是逃生阀，必须能一键退回原行为。

白名单里**不许**出现 `DETACHED_PROCESS` 与 `CREATE_NO_WINDOW` 混用：两者都管
控制台，混用会起不来（daemon 拉不起来）。:func:`test_detached_spawn_never_mixes`
用 AST 守住这条。
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hive_compat  # noqa: E402
import hive  # noqa: E402

SUBPROCESS_FUNCS = ("run", "Popen", "check_output", "call", "check_call")
GUARD = "no_window_kwargs"

# 生产模块 = 仓库根的 *.py + dashboard/*.py。
# 不扫 tests/：测试进程由 pytest 前台驱动，不是常驻周期路径，闪窗也不影响用户；
# 它们自己也是「被验收对象」，把夹具算进判据只会让白名单越长越假。
def production_modules() -> list[Path]:
    mods = sorted(p for p in ROOT.glob("*.py") if p.is_file())
    mods += sorted((ROOT / "dashboard").glob("*.py"))
    return mods


# --------------------------------------------------------------------------- #
# 白名单：**精确集合**，每条都要写清为什么可以没有 no_window kwargs
# --------------------------------------------------------------------------- #
WHITELIST: dict[tuple[str, str, str], str] = {
    ("hive.py", "Worker.run_task", "Popen"):
        "交互式 GUI agent 拉起（manual 模式的 --launch）：人要在那个窗口里干活，"
        "隐藏它等于把 agent 藏起来",
    ("hive.py", "Worker._exec", "Popen"):
        "worker 执行 agent 命令（交互式 agent 拉起）：实时输出要给人看，"
        "CREATE_NO_WINDOW 会把输出吞掉",
    ("hive.py", "_dashboard_spawn", "Popen"):
        "看板 daemon 拉起：creationflags 拼在 **kwargs 里（AST 看不到），实际是 "
        "CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP —— 见 2026-11-01 那条注释："
        "DETACHED_PROCESS 会让 .cmd 里的 Start-Process 卡死在控制台分配上",
    ("hive_cluster.py", "_spawn_background", "Popen"):
        "集群 daemon 拉起：DETACHED_PROCESS，与 CREATE_NO_WINDOW 互斥",
    ("hive_pr.py", "git_remote", "run"):
        "`hive pr` 交互命令里的 git 调用：不在 30 秒刷新路径上；且本卡允许修改"
        "的路径不含 hive_pr.py —— 留待后续卡统一收口",
    ("llm_controller.py", "McpStdioBackend.__init__", "Popen"):
        "MCP stdio 子进程：全管道通信、控制器启动时拉一次，不在周期刷新路径上；"
        "本卡允许修改的路径不含 llm_controller.py",
}

# 整文件豁免：端到端自测脚本（人手动跑，不是产品常驻路径；也不在本卡允许路径内）
FILE_WHITELIST = {
    "selftest.py": "端到端自测脚本，人工/CI 前台执行，不是常驻周期路径",
}


# --------------------------------------------------------------------------- #
# AST 工具
# --------------------------------------------------------------------------- #

def _read(path: Path) -> str:
    """按 utf-8-sig 读源码：hive_compat.py 等文件带 BOM，utf-8 会留下 U+FEFF 炸 ast。"""
    return path.read_text(encoding="utf-8-sig")


def _iter_functions(body, prefix):
    """按定义顺序产出 (qualname, node)，类名用 `.` 连接。"""
    for node in body:
        if isinstance(node, ast.ClassDef):
            yield from _iter_functions(node.body, prefix + [node.name])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qual = ".".join(prefix + [node.name])
            yield qual, node
            yield from _iter_functions(node.body, prefix + [node.name])


def _calls_in(node):
    """node 体内（**不含**嵌套函数/类，它们有自己的 qualname）的 Call 节点。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from _calls_in(child)


def _callee(call: ast.Call) -> str | None:
    """`subprocess.run(...)` → "run"；别的形状返回 None。"""
    func = call.func
    if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess" and func.attr in SUBPROCESS_FUNCS):
        return func.attr
    return None


def _is_guarded(call: ast.Call) -> bool:
    """显式 creationflags=… 或 **no_window_kwargs() 都算「经过统一出口」。"""
    for kw in call.keywords:
        if kw.arg == "creationflags":
            return True
        if kw.arg is None and isinstance(kw.value, ast.Call):
            inner = kw.value.func
            if isinstance(inner, ast.Name) and inner.id == GUARD:
                return True
            if isinstance(inner, ast.Attribute) and inner.attr == GUARD:
                return True
    return False


def subprocess_sites(path: Path):
    """产出 (qualname, callee, guarded, call_node)。"""
    tree = ast.parse(_read(path))
    for qual, node in _iter_functions(tree.body, []):
        for call in _calls_in(node):
            callee = _callee(call)
            if callee:
                yield qual, callee, _is_guarded(call), call
    for call in _calls_in(tree):
        callee = _callee(call)
        if callee:
            yield "<module>", callee, _is_guarded(call), call


def unguarded_sites() -> list[tuple[str, str, str]]:
    """全部「没带 no_window kwargs 且不在白名单」的调用点：(文件, qualname, callee)。"""
    out = []
    for path in production_modules():
        rel = path.relative_to(ROOT).as_posix()
        if rel in FILE_WHITELIST:
            continue
        for qual, callee, guarded, _call in subprocess_sites(path):
            if guarded:
                continue
            out.append((rel, qual, callee))
    return out


# --------------------------------------------------------------------------- #
# 1. AST 全量扫描
# --------------------------------------------------------------------------- #

def test_every_subprocess_call_is_guarded():
    """除白名单外，每个 subprocess 调用点都必须经过统一出口。"""
    bad = [s for s in unguarded_sites() if s not in WHITELIST]
    assert not bad, (
        "这些子进程调用点没带 no_window kwargs，Windows 上会闪黑窗：\n"
        + "\n".join("  %s::%s -> subprocess.%s" % s for s in bad)
    )


def test_whitelist_has_no_zombie_entries():
    """白名单必须是「用得上」的精确集合：多一条（已收口/写错名字）就红。

    僵尸豁免比漏判更危险 —— 它会让后来新加的同类调用点无声通过。
    """
    actual = set(unguarded_sites())
    zombies = sorted(set(WHITELIST) - actual)
    assert not zombies, "白名单里有没人用的条目（已收口或 qualname 写错）：\n" + \
        "\n".join("  %s::%s -> subprocess.%s" % s for s in zombies)


def test_scan_covers_the_production_modules():
    """扫描面本身要有牙：至少覆盖这些模块，且不含 tests/。"""
    rels = {p.relative_to(ROOT).as_posix() for p in production_modules()}
    for must in ("hive.py", "hive_compat.py", "hive_cluster.py",
                 "dashboard/serve.py", "hive_pr.py", "llm_controller.py"):
        assert must in rels, f"扫描面漏了 {must}"
    assert not any(r.startswith("tests/") for r in rels), "tests/ 不该进生产扫描面"


def test_no_bare_from_subprocess_import():
    """不许 `from subprocess import run` 绕开扫描（扫描只认 `subprocess.xxx`）。"""
    offenders = []
    for path in production_modules():
        if path.name in FILE_WHITELIST:
            continue
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                names = {a.name for a in node.names} & set(SUBPROCESS_FUNCS)
                if names:
                    offenders.append(f"{path.name}: {sorted(names)}")
    assert not offenders, "用 from subprocess import 绕开了 AST 扫描：" + str(offenders)


# --------------------------------------------------------------------------- #
# 2. 单点钉死：全仓唯一出口 + 看板刷新循环
# --------------------------------------------------------------------------- #

def _function_guarded(module: str, qualname: str) -> bool:
    path = ROOT / module
    tree = ast.parse(_read(path))
    for qual, node in _iter_functions(tree.body, []):
        if qual != qualname:
            continue
        calls = [c for c in _calls_in(node) if _callee(c)]
        return bool(calls) and all(_is_guarded(c) for c in calls)
    raise AssertionError(f"{module} 里找不到 {qualname}")


def test_run_is_the_single_guarded_exit():
    """`hive.run()` 是全仓命令出口（git()/git_net() 都走它）：两处调用都必须带。

    这条是反证靶子：把 run() 里的 `**no_window_kwargs()` 删掉，它立刻变红。
    """
    assert _function_guarded("hive.py", "run"), \
        "hive.run() 必须带 **no_window_kwargs()，否则全仓 git 调用都会闪窗"


def test_dashboard_refresh_loop_is_guarded():
    """看板 30 秒刷新循环（`refresh_loop`）—— 用户看到的那一闪就在这。"""
    assert _function_guarded("dashboard/serve.py", "refresh_loop"), \
        "refresh_loop 的 subprocess.run 必须带 **no_window_kwargs()"


def test_cluster_git_helpers_are_guarded():
    """集群里周期性跑的 git/tasklist 小工具。"""
    for qual in ("RepoSyncAgent._git_rev", "RepoSyncAgent._remote_head",
                 "_pid_alive"):
        assert _function_guarded("hive_cluster.py", qual), f"{qual} 漏了 no_window kwargs"


# --------------------------------------------------------------------------- #
# 3. 不许混用 DETACHED_PROCESS 与 CREATE_NO_WINDOW
# --------------------------------------------------------------------------- #

DETACHED_SITES = [
    ("hive.py", "_dashboard_spawn"),
    ("hive_cluster.py", "_spawn_background"),
]


@pytest.mark.parametrize("module,qualname", DETACHED_SITES)
def test_detached_spawn_never_mixes(module, qualname):
    """daemon 拉起用 DETACHED_PROCESS；再叠 CREATE_NO_WINDOW 会起不来。"""
    src = _read(ROOT / module)
    tree = ast.parse(src)
    for qual, node in _iter_functions(tree.body, []):
        if qual != qualname:
            continue
        segment = ast.get_source_segment(src, node) or ""
        assert "DETACHED_PROCESS" in segment, f"{qualname} 应当用 DETACHED_PROCESS"
        assert GUARD not in segment, \
            f"{qualname} 不得混用 {GUARD}（与 DETACHED_PROCESS 互斥，daemon 会起不来）"
        return
    raise AssertionError(f"{module} 里找不到 {qualname}")


# --------------------------------------------------------------------------- #
# 4. 行为：no_window_kwargs() 本身 + run() 真把 kwargs 传下去
# --------------------------------------------------------------------------- #

def test_no_window_kwargs_off_windows(monkeypatch):
    """非 Windows 一律空 dict：POSIX 上口径一个字节都不动。"""
    monkeypatch.setattr(hive_compat.os, "name", "posix")
    assert hive_compat.no_window_kwargs() == {}


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW 只在 Windows 上有意义")
def test_no_window_kwargs_on_windows(monkeypatch):
    monkeypatch.delenv(hive_compat.NO_WINDOW_ENV, raising=False)
    kw = hive_compat.no_window_kwargs()
    assert kw == {"creationflags": hive_compat.subprocess.CREATE_NO_WINDOW}


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF", " 0 "])
def test_escape_valve(monkeypatch, val):
    """逃生阀：HIVE_NO_WINDOW=0 → 全仓立刻退回「可能有窗」的原行为。"""
    monkeypatch.setenv(hive_compat.NO_WINDOW_ENV, val)
    assert hive_compat.no_window_kwargs() == {}


@pytest.mark.parametrize("val", ["1", "true", "yes", "", "anything"])
def test_valve_default_is_on(monkeypatch, val):
    """默认（以及任何非否定值）都是开着的 —— 不设也该无窗。"""
    monkeypatch.setenv(hive_compat.NO_WINDOW_ENV, val)
    kw = hive_compat.no_window_kwargs()
    if os.name == "nt":
        assert kw.get("creationflags") == hive_compat.subprocess.CREATE_NO_WINDOW
    else:
        assert kw == {}


def _fake_run(captured):
    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake(cmd, **kwargs):
        captured.update(kwargs)
        return _Proc()

    return fake


def test_run_passes_creationflags_on_windows(monkeypatch):
    """真调一次 hive.run()：Windows 上 kwargs 里必须有 creationflags。"""
    captured: dict = {}
    monkeypatch.setattr(hive.subprocess, "run", _fake_run(captured))
    monkeypatch.delenv(hive_compat.NO_WINDOW_ENV, raising=False)
    hive.run(["git", "--version"])
    if os.name == "nt":
        assert captured.get("creationflags") == hive.subprocess.CREATE_NO_WINDOW
    else:
        assert "creationflags" not in captured


def test_run_honours_escape_valve(monkeypatch):
    """逃生阀要能穿透到 run()（否则现场排障只能改代码）。"""
    captured: dict = {}
    monkeypatch.setattr(hive.subprocess, "run", _fake_run(captured))
    monkeypatch.setenv(hive_compat.NO_WINDOW_ENV, "0")
    hive.run(["git", "--version"])
    assert "creationflags" not in captured


def test_run_keeps_encoding_redlines(monkeypatch):
    """加 creationflags 不许碰 T-0143 的编码红线（encoding/errors 原样还在）。"""
    captured: dict = {}
    monkeypatch.setattr(hive.subprocess, "run", _fake_run(captured))
    hive.run(["git", "--version"])
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"
    assert captured.get("capture_output") is True
