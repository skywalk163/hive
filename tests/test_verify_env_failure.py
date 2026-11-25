"""verify 环境性假红：识别、留痕、派单规避（T-0342 / T-0344 回归）。

近几天两张卡都不是产物不合格，而是判据在交付机上根本跑不起来：

* T-0344：本机 fb98 缺 torch，测试模块级 importorskip 让 pytest 收集为空
  （rc=4 / `found no collectors`），两条用例一条都没执行；
* T-0342：Windows worktree 里 `bench.py:29 import resource`（POSIX-only）
  直接 ModuleNotFoundError，吞吐判定一次都没跑到。

过去两者和真失败长得一模一样：结果 JSON 只有 `status=failed, error=verify 未通过`，
人只能靠卡面附注解释，看板上留下 `done + failed` 的矛盾态。

这套改动只做**标注与规避**，三条红线钉在这里：
  1. 环境性失败照样 failed、照样挡 land，绝不自动判绿；
  2. 真断言失败优先，不许被误标成环境假红而放松排查；
  3. `verify_requires` 是 opt-in：没声明的机器照旧派单，旧卡行为一字不变。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import hive_compat
from hive import (  # noqa: WPS433
    _verify_verdict,
    annotate_result_failure,
    classify_verify_failure,
    parse_card,
    result_failure_kind,
    verify_entry,
    verify_failure_message,
    verify_requires_lack,
)


# T-0344 结果 JSON 里 verify[0] 的 tail（原样复刻，不依赖工作区真实数据）
T0344_TAIL = [
    "",
    "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html",
    'No entry for terminal type "unknown";',
    "using dumb terminal settings.",
    "ERROR: found no collectors for /home/skywalk/.hive/agents/raccoon-fb98/"
    "wt/T-0344/tests/test_acceptance.py::test_acceptance_main_exit_code_reflects_failures",
]
T0344_CMD = "python3 -m pytest -q tests/test_acceptance.py::test_x tests/test_openi_v3_judge.py::test_y"

# T-0342 结果 JSON 里 verify[0] 的 tail
T0342_TAIL = [
    "Traceback (most recent call last):",
    '  File "C:\\dswork\\hive-wt\\T-0342\\scripts\\bench.py", line 29, in <module>',
    "    import resource",
    "ModuleNotFoundError: No module named 'resource'",
]
T0342_CMD = "python3 scripts/bench.py --config configs/demo-3m.json --tokens 200"


def test_t0344_pytest_collect_empty_is_environment():
    env = classify_verify_failure(T0344_CMD, 4, T0344_TAIL)
    assert env is not None
    assert env["kind"] == "no_tests_collected"
    assert "绝不自动判绿" in env["hint"]


def test_t0342_platform_module_is_environment():
    env = classify_verify_failure(T0342_CMD, 1, T0342_TAIL)
    assert env is not None
    assert env["kind"] == "platform_module"
    assert "resource" in env["reason"]


def test_missing_dependency_is_only_suspected():
    env = classify_verify_failure(
        "python3 -m pytest -q tests/test_model.py", 2,
        ["ImportError while importing test module",
         "ModuleNotFoundError: No module named 'torch'"])
    assert env["kind"] == "missing_dependency"
    assert "需人工确认" in env["reason"], "不能把缺依赖直接写成定论"


@pytest.mark.parametrize("tail", [
    ["E       AssertionError: assert 1 == 2", "1 failed, 2 passed in 0.12s"],
    ["ModuleNotFoundError: No module named 'torch'",
     "FAILED tests/test_x.py::test_y", "1 failed in 0.3s"],
])
def test_real_assertion_failure_is_never_labeled_environment(tail):
    """真跑出了用例失败时优先按产物失败排查，不许被环境签名掩盖。"""
    assert classify_verify_failure("python3 -m pytest -q tests/", 1, tail) is None


def test_command_not_found():
    env = classify_verify_failure(
        "bash scripts/check.sh", 127,
        ["'bash' 不是内部或外部命令，也不是可运行的程序"])
    assert env["kind"] == "command_missing"


def test_green_verify_is_never_classified():
    assert classify_verify_failure(T0344_CMD, 0, T0344_TAIL) is None


def test_verify_entry_uses_full_output_but_keeps_short_tail():
    """关键报错可能在 traceback 中间，只留尾部 5 行会漏判。"""
    out = "ModuleNotFoundError: No module named 'resource'\n" + "\n".join(
        f"noise line {i}" for i in range(10)) + "\n"
    p = SimpleNamespace(stdout=out, stderr="", returncode=1)
    entry = verify_entry(T0342_CMD, p)
    assert entry["code"] == 1
    assert len(entry["tail"]) == 5
    assert "resource" not in "\n".join(entry["tail"])
    assert entry["env_error"]["kind"] == "platform_module"


def test_verify_entry_env_error_does_not_break_result_failure_kind():
    """verify_entry 的精简 env_error 只有 {kind, reason}，判读时须回填 cmd/code。

    T-0473 现场：硬取 env["cmd"] 抛 KeyError，cmd_submit 在写 res/<tid> 之前
    中断——分支推上去了、结果文件却写不出来。
    """
    p = SimpleNamespace(stdout="ModuleNotFoundError: No module named 'resource'",
                        stderr="", returncode=1)
    entry = verify_entry(T0342_CMD, p)
    assert set(entry["env_error"]) == {"kind", "reason"}

    res = {"task": "T-0000", "status": "failed", "verify": [entry]}
    fk = result_failure_kind(res)
    assert fk is not None and fk["kind"] == "environment"
    assert fk["items"][0]["cmd"] == T0342_CMD
    assert fk["items"][0]["code"] == 1

    annotated = annotate_result_failure(dict(res))
    assert annotated["verify_env_errors"][0]["kind"] == "platform_module"
    assert "非产物判据失败" in verify_failure_message([entry])


def test_legacy_results_are_recognized_without_rewriting_history():
    """历史卡没有 failure_kind 字段，靠 verify[].tail 回溯即可显形。"""
    legacy = {"task": "T-0344", "status": "failed", "error": "verify 未通过",
              "verify": [{"cmd": T0344_CMD, "code": 4, "tail": T0344_TAIL},
                         {"cmd": "make -n demo", "code": 0, "tail": []}]}
    fk = result_failure_kind(legacy)
    assert fk is not None and fk["kind"] == "environment"
    assert fk["all_environment"] is True
    assert legacy.get("failure_kind") is None, "回溯判读不许改写历史对象"

    legacy2 = {"task": "T-0342", "status": "failed", "error": "verify 未通过",
               "verify": [{"cmd": T0342_CMD, "code": 1, "tail": T0342_TAIL}]}
    assert result_failure_kind(legacy2)["all_environment"] is True


def test_mixed_failure_is_not_claimed_as_pure_environment():
    res = {"status": "failed", "verify": [
        {"cmd": T0342_CMD, "code": 1, "tail": T0342_TAIL},
        {"cmd": "python3 -m pytest -q tests/test_real.py", "code": 1,
         "tail": ["E AssertionError: assert 1 == 2", "1 failed in 0.1s"]},
    ]}
    fk = result_failure_kind(res)
    assert fk["kind"] == "mixed"
    assert fk["all_environment"] is False


def test_done_result_is_never_labeled():
    assert result_failure_kind({"status": "done", "verify": [
        {"cmd": T0344_CMD, "code": 4, "tail": T0344_TAIL}]}) is None


def test_annotate_only_adds_fields_never_changes_verdict():
    result = {"task": "T-0344", "status": "failed", "error": "verify 未通过",
              "verify": [{"cmd": T0344_CMD, "code": 4, "tail": T0344_TAIL}]}
    annotate_result_failure(result)
    assert result["status"] == "failed", "标注绝不判绿"
    assert result["failure_kind"] == "environment"
    assert result["verify_env_errors"][0]["kind"] == "no_tests_collected"


def test_failure_message_names_environment_but_still_says_failed():
    verify = [{"cmd": T0344_CMD, "code": 4, "tail": T0344_TAIL}]
    msg = verify_failure_message(verify)
    assert "verify 未通过" in msg
    assert "非产物判据失败" in msg
    assert "绝不自动判绿" in msg
    # 普通失败文案一字不改
    plain = [{"cmd": "python3 -m pytest -q tests/", "code": 1,
              "tail": ["1 failed in 0.2s"]}]
    assert verify_failure_message(plain) == "verify 未通过"


def test_verify_verdict_distinguishes_env_blocked_from_fail():
    env = _verify_verdict([{"cmd": T0344_CMD, "code": 4, "tail": T0344_TAIL}])
    assert env["verdict"] == "env-blocked"
    real = _verify_verdict([{"cmd": "python3 -m pytest -q tests/", "code": 1,
                             "tail": ["1 failed in 0.2s"]}])
    assert real["verdict"] == "fail"


# --------------------------------------------------------------------------- #
# 派单规避：卡面 verify_requires + 机器显式声明缺能力
# --------------------------------------------------------------------------- #

def test_verify_requires_merges_into_required_capabilities():
    caps = hive_compat.required_capabilities(
        {"needs": ["python"], "verify": ["python3 -m pytest -q"],
         "verify_requires": ["torch", "avx2"]})
    assert {"python", "torch", "avx2"} <= caps
    # 旧卡没有该字段，行为不变
    assert hive_compat.required_capabilities(
        {"needs": ["python"], "verify": ["python3 -V"]}) == {"python"}


@pytest.mark.parametrize("agent", [
    {"capabilities": ["python"], "no_capabilities": ["torch"]},
    {"capabilities": ["python", "no-torch"]},
    {"capabilities": ["python"], "no_torch": True},
    {"capabilities": ["python"], "has_torch": False},
])
def test_soft_filter_only_blocks_explicit_denial(agent):
    card = {"verify_requires": ["torch"]}
    assert hive_compat.missing_declared_capabilities(agent, card) == {"torch"}
    assert verify_requires_lack(agent, card) == {"torch"}
    # 没声明过的机器一律放行 —— 新字段不能把现有队列全卡死
    assert hive_compat.missing_declared_capabilities(
        {"capabilities": ["python"]}, card) == set()
    assert verify_requires_lack({}, card) == set()


def test_dispatch_skips_machine_declaring_missing_verify_env(world, fake_cmd):
    """端到端：判据需要 torch 的卡，只派给没声明缺 torch 的机器。"""
    world.add_agent("nolib", "exec", caps=["python"], command=fake_cmd,
                    repos={"hive": world.code_clone("nolib")})
    world.add_agent("hase", "exec", caps=["python"], command=fake_cmd,
                    repos={"hive": world.code_clone("hase")})
    cfg = world.hive1 / "agents" / "nolib.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["no_capabilities"] = ["torch"]
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    world.hive("task", "new", "--id", "T-9001", "--title", "需要 torch 的判据",
               "--repo", "hive", "--verify-requires", "torch")
    assert parse_card(world.hive1 / "tasks" / "T-9001.md")[0]["verify_requires"] == ["torch"]

    out = world.hive_json("dispatch")
    plan = {p["task"]: p["agent"] for p in out["plan"]}
    assert plan.get("T-9001") == "hase", out
    reasons = " ".join(s["reason"] for s in out["skipped"] if s["task"] == "T-9001")
    assert "判据环境不匹配" not in reasons, "派成功的卡不该出现在 skipped"

    # 两台机器都声明缺 torch：需要 torch 的卡应被明确挡下，而没有
    # verify_requires 声明的普通卡照旧派 —— opt-in 字段不影响旧卡。
    cfg2 = world.hive1 / "agents" / "hase.json"
    data2 = json.loads(cfg2.read_text(encoding="utf-8"))
    data2["no_capabilities"] = ["torch"]
    cfg2.write_text(json.dumps(data2, ensure_ascii=False, indent=2), encoding="utf-8")
    world.hive("task", "new", "--id", "T-9002", "--title", "普通卡",
               "--repo", "hive", "--verify", "true")
    out2 = world.hive_json("dispatch")
    tasks = {p["task"] for p in out2["plan"]}
    assert "T-9002" in tasks, out2
    assert "T-9001" not in tasks, out2
    reasons = " ".join(s["reason"] for s in out2["skipped"] if s["task"] == "T-9001")
    assert "判据环境不匹配" in reasons, out2
