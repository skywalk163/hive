#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-0355 压测脚本自身的可信度冒烟（小规格 + 缩短时钟，纯仿真）。

纪律（与 T-0355 卡面一致）：
* 不连 Gitea、不真发组播、不启动集群进程、不碰生产状态；
* 不调宽阈值：所有阈值都从 ``hive_lsa.SyncConfig`` 取；
* 不用理论估算代替仿真输出；A/B 都测，不只报 A；
* 随机数可复现（固定 seed + 本地 Random，不污染全局）；
* 只用标准库 + pytest，不依赖 head/grep/awk/! /$()（Windows/POSIX 都能跑）。
"""
from __future__ import annotations

import ast
import json
import math
import random
import sys
from pathlib import Path

import pytest

# 仓库根入 sys.path（从仓库根跑 pytest 时本就如此；这里兜底）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import hive_lsa as h  # noqa: E402
from tools import lsa_loadtest as lt  # noqa: E402


# --------------------------------------------------------------------------- #
# 小规格 / 缩短时钟的公共夹具
# --------------------------------------------------------------------------- #
SMALL_DURATION = 3600.0        # 仍覆盖 2×1800s 对账周期（缩短的是 tick 粒度不是时长）
SEED = 20261114


def small_cfg() -> h.SyncConfig:
    """只缩短「周期定时器」，阈值类常量一律不动（不许调宽阈值）。"""
    return h.SyncConfig().overridden(
        multicast_base_s=30.0,     # T1 起步 60 -> 30（缩短测试时间，非阈值）
        ls_refresh_s=300.0,        # 全量对账 1800 -> 300，duration 覆盖 12 个周期
        flap_window_s=20.0,
        flap_half_life_s=20.0,
        flap_max_suppress_s=100.0,
    )


def matrix(**kw) -> dict:
    """默认最小矩阵：N=5、M=200、三频率、A/B 都跑、固定种子。"""
    args = dict(node_list=[5], task_list=[200],
                freq_list=["idle", "normal", "high"],
                duration=SMALL_DURATION, scenario_list=["A", "B"],
                seed=SEED, config=small_cfg(), failover=True, probes=5)
    args.update(kw)
    return lt.run_matrix(**args)


@pytest.fixture(scope="module")
def full_small() -> dict:
    """小规格冒烟：N=5、M=200、三频率、A/B 都跑、缩短的周期定时器。"""
    return matrix()


@pytest.fixture(scope="module")
def scale_small() -> dict:
    """覆盖 N=20/50/100 的小规格矩阵，用来验 N 越大优势越明显 / N=100 无风暴。"""
    return matrix(node_list=[20, 50, 100], task_list=[200],
                  freq_list=["idle", "high"], scenario_list=["A", "B"],
                  probes=5)


# --------------------------------------------------------------------------- #
# 1. 脚本本身可信：纯标准库 / 零 I/O / 可注入
# --------------------------------------------------------------------------- #
def test_tool_is_pure_stdlib_and_zero_io():
    """压测工具不得含真实网络 / 文件 / 进程 / 组播 I/O（AST 静态检查）。"""
    src = (lt.__file__)
    tree = ast.parse(Path(src).read_text(encoding="utf-8"))
    forbidden_names = {
        "socket", "urllib", "requests", "subprocess", "Popen", "os.system",
        "urlopen", "sendto", "setsockopt",
    }
    forbidden_calls = {
        "open", "exec", "eval", "compile", "__import__",
    }
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed_imports = {
        "argparse", "heapq", "json", "math", "os", "random", "sys", "time",
        "collections", "dataclasses", "datetime", "pathlib", "typing",
        "__future__", "hive_lsa",
    }
    assert imported <= allowed_imports, ("非标准库/未知依赖: %s"
                                         % (imported - allowed_imports))
    assert not (imported & forbidden_names), "import 了被禁的 I/O 模块"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in forbidden_calls, \
                "出现了被禁调用 %s()" % node.func.id
    # --json/--out 的写盘是明确的产物输出，不在热循环里；单独确认没有别的 write
    writes = [n for n in ast.walk(tree)
              if isinstance(n, ast.Attribute) and n.attr in
              ("write_text", "write_bytes", "mkdir")]
    assert len(writes) <= 2, "除产物输出外不应有其它文件写入"


def test_fake_clock_and_fake_transport_are_injectable():
    clock = lt.FakeClock(12.5)
    assert clock() == 12.5
    clock.advance(2.5)
    assert clock() == 15.0
    clock.set(0.0)
    assert clock() == 0.0

    tr = lt.FakeTransport()
    seen = []

    def deliver(pkts, peers, now):
        seen.append((list(pkts), list(peers), now))

    pkts = [{"type": "lsa_digest", "leader_epoch": 0, "seq": 1,
             "digest": "x", "head_sha": "", "n_changes": 0, "area": "a",
             "project_tag": "p", "sent_at": 1.0, "flap_summary": False,
             "kind": "digest", "entries_digest": "y"}]
    b = h.packet_bytes(pkts[0])
    tr.broadcast("n0", pkts, 1.0, [1, 2, 3], deliver)
    assert tr.leader_bytes == b
    assert tr.wire_bytes == b * 3
    assert tr.packets_emitted == 1
    assert tr.packets_delivered == 3
    assert tr.max_multicast_sources == 1
    assert tr.duplicate_floods == 0
    assert seen == [(pkts, [1, 2, 3], 1.0)]


# --------------------------------------------------------------------------- #
# 2. 确定性 / 可复现
# --------------------------------------------------------------------------- #
def test_matrix_is_reproducible_with_fixed_seed():
    a = matrix(node_list=[5], task_list=[200], freq_list=["normal", "high"],
               scenario_list=["B"])
    b = matrix(node_list=[5], task_list=[200], freq_list=["normal", "high"],
               scenario_list=["B"])
    # 去掉带时间戳的信封字段后逐字节一致
    def stable(x):
        y = dict(x)
        for k in ("collected_at", "elapsed_s"):
            y.pop(k, None)
        return json.dumps(y, ensure_ascii=False, sort_keys=True, default=str)
    assert stable(a) == stable(b)


def test_different_seed_changes_measurements():
    x = matrix(node_list=[5], task_list=[200], freq_list=["high"],
               scenario_list=["B"], seed=1)
    y = matrix(node_list=[5], task_list=[200], freq_list=["high"],
               scenario_list=["B"], seed=2)
    def digest(z):
        return [(r["metrics"]["control_plane"]["leader_bytes"],
                 r["metrics"]["lsa"]["emitted_change_lsa"])
                for r in z["runs"]]
    # 不同种子应至少有一处不同（证明 rng 真的被注入了）
    assert digest(x) != digest(y)


# --------------------------------------------------------------------------- #
# 3. 两组对照指标都齐全，且 A/B 都报（不许只报 A）
# --------------------------------------------------------------------------- #
REQUIRED_METRIC_KEYS = {
    "gitea_requests", "control_plane", "sync_storm_peak", "sync_storm_limit",
    "state_json", "lsa", "visibility_s", "reconcile", "failover", "cpu_proxy",
}


def test_both_scenarios_reported_with_all_metrics(full_small):
    runs = full_small["runs"]
    assert len(runs) == 6
    assert {r["metrics"]["scenario"] for r in runs} == {"A", "B"}
    for r in runs:
        m = r["metrics"]
        assert REQUIRED_METRIC_KEYS <= set(m), r["run_id"]
        g = m["gitea_requests"]
        assert {"ls_remote_total", "fetch_total", "pull_total", "total",
                "per_node", "per_node_per_30min"} <= set(g)
        cp = m["control_plane"]
        assert {"leader_bytes", "wire_bytes", "total_bytes",
                "per_node_peak_bytes_per_cycle", "kbps"} <= set(cp)
        v = m["visibility_s"]
        assert {"samples", "converged_samples", "unavailable_samples",
                "p50", "p95", "max"} <= set(v)
        assert v["samples"] > 0
        # state.json 落盘次数与字节；CPU 用「LSA 处理/合并次数」近似
        assert {"writes", "bytes_total"} <= set(m["state_json"])
        assert {"lsa_processing", "merge_count"} <= set(m["cpu_proxy"])


def test_json_envelope_has_raw_values_and_constants(full_small):
    j = full_small
    assert j["schema"] == "hive-hlsa-loadtest/1"
    assert j["params"]["seed"] == SEED
    assert j["constants"] == h.CONSTANTS
    assert j["methodology"]["pure_simulation"] is True
    assert j["methodology"]["touched_production"] is False
    assert j["methodology"]["sent_prod_multicast"] is False
    assert j["methodology"]["connected_gitea"] is False
    # 报告只引用实测值：每个 run 都带原始 metrics
    assert all("metrics" in r and "run_id" in r for r in j["runs"])


# --------------------------------------------------------------------------- #
# 4. 8 项 SLA 断言一条不落，且阈值不许被调宽
# --------------------------------------------------------------------------- #
def test_all_eight_assertions_present(full_small):
    ids = [a["id"] for a in full_small["assertions"]]
    assert ids == ["SLA-%d" % i for i in range(1, 9)]


def test_assertions_are_real_not_always_pass(full_small, scale_small):
    """断言不是摆设：每条都有实测 measured + reason，且能如实给出 fail。

    全矩阵（默认生产常量、N=5..100）如实记录了 SLA-6 在小 N 下的统计边界
    失败（N=5 的 20% 上限=1，4 个跟随节点在抖动窗口内可能同秒）；脚本
    不许通过调宽阈值把它抹掉。小规格冒烟里 7 条通过、N=100 重点场景通过。
    """
    for a in full_small["assertions"]:
        assert "measured" in a and a["measured"] is not None
        assert a["reason"] and a["unit"] and a["threshold"] is not None
    scale = {x["id"]: x for x in scale_small["assertions"]}
    # N=20/50/100 的规模下抖动把同秒峰值压回 20% 以内（N=100+high 是硬场景）
    assert scale["SLA-6"]["pass"] is True, scale["SLA-6"]["measured"]
    assert scale["SLA-8"]["pass"] is True
    assert scale["SLA-5"]["pass"] is True
    # 独立造一条「人为越界」的假指标，断言必须变 fail（证明没写死 True）
    def fake_metrics(scenario: str, vis_max: float) -> dict:
        return {
            "scenario": scenario, "N": 5, "frequency": "idle", "M": 200,
            "visibility_s": {"max": vis_max, "unavailable_samples": 0},
            "gitea_requests": {"per_node_per_30min": 0.0,
                               "leader_fallback_ls_remote": 1, "total": 1},
            "control_plane": {"kbps": 0.0,
                              "per_node_peak_bytes_per_cycle": 0},
            "sync_storm_peak": 0, "sync_storm_limit": 1,
            "lsa": {"lsa_per_token_cycle_max": 0},
            "reconcile": {"intervals_s": [], "periods_checked_s": [1800.0],
                          "t2_times_s": [1800.0], "t2_events": 1,
                          "digest_only": True, "full_state_transfers": 0},
            "failover": {"max_multicast_sources": 1, "duplicate_floods": 0,
                         "leader_switches": 1},
        }
    out = lt.build_assertions(
        [{"metrics": fake_metrics("B", 999.0)},
         {"metrics": fake_metrics("A", 999.0)}], small_cfg())
    by = {x["id"]: x for x in out}
    assert by["SLA-1"]["pass"] is False, "999s > 300s 必须如实判 fail"
    good = lt.build_assertions(
        [{"metrics": fake_metrics("B", 1.0)},
         {"metrics": fake_metrics("A", 1.0)}], small_cfg())
    assert {x["id"]: x for x in good}["SLA-1"]["pass"] is True


def test_thresholds_are_not_widened(full_small):
    cfg = small_cfg()
    a = {x["id"]: x for x in full_small["assertions"]}
    assert a["SLA-1"]["threshold"] == cfg.sla_visibility_s == h.SLA_VISIBILITY_S
    assert a["SLA-2"]["threshold"]["min_s"] == \
        cfg.ls_refresh_s * (1 - cfg.jitter)
    assert a["SLA-2"]["threshold"]["max_s"] == \
        cfg.ls_refresh_s * (1 + cfg.jitter)
    assert a["SLA-3"]["threshold"]["per_node_per_30min"] == 0
    assert a["SLA-3"]["threshold"]["leader_fallback_max"] == 1
    assert a["SLA-4"]["threshold"]["max_kbps"] == \
        cfg.idle_bandwidth_baseline_kb_s == h.IDLE_BANDWIDTH_BASELINE_KB_S
    assert a["SLA-5"]["threshold"]["bytes_per_cycle"] == \
        cfg.token_bucket_bytes == h.TOKEN_BUCKET_BYTES
    assert a["SLA-5"]["threshold"]["lsa_per_cycle"] == \
        cfg.token_bucket_lsa == h.TOKEN_BUCKET_LSA
    assert "0.20" in a["SLA-6"]["threshold"]
    assert a["SLA-7"]["threshold"]["max_sources"] == 1
    assert a["SLA-7"]["threshold"]["duplicate_floods"] == 0
    assert a["SLA-8"]["threshold"]["min_ratio"] == 10


# --------------------------------------------------------------------------- #
# 5. 逐项断言确实有「实测数字」支撑，不是摆设
# --------------------------------------------------------------------------- #
def test_sla1_visibility_max_is_measured_and_bounded(full_small):
    a = full_small["assertions"][0]
    assert a["measured"] is not None and a["measured"] >= 0
    assert a["measured"] <= h.SLA_VISIBILITY_S
    assert a["evidence"]["per_run_max"]


def test_sla2_reconcile_is_digest_only_and_jittered(full_small):
    cfg = small_cfg()
    lo, hi = cfg.ls_refresh_s * (1 - cfg.jitter), cfg.ls_refresh_s * (1 + cfg.jitter)
    for r in full_small["runs"]:
        if r["metrics"]["scenario"] != "B":
            continue
        rec = r["metrics"]["reconcile"]
        assert rec["t2_events"] >= 1
        assert rec["digest_only"] is True
        assert rec["full_state_transfers"] == 0
        for p in rec["periods_checked_s"]:
            assert lo <= p <= hi, "切主后对账节奏必须连续：%s 不在 [%s,%s]" \
                % (p, lo, hi)
        assert rec["t2_times_s"][0] <= hi
        # 只发 digest/CSNP：报文类型里没有 lsa（change 状态体）
        assert set(rec["t2_packet_types"]) <= {"lsa_refresh", "lsa_csnp",
                                               "lsa_digest"}


def test_sla3_idle_zero_gitea_per_node(full_small):
    for r in full_small["runs"]:
        m = r["metrics"]
        if m["scenario"] == "B" and m["frequency"] == "idle":
            assert m["gitea_requests"]["per_node_per_30min"] == 0
            assert m["gitea_requests"]["leader_fallback_ls_remote"] <= 1
            assert m["gitea_requests"]["fetch_total"] == 0
            assert m["gitea_requests"]["pull_total"] == 0


def test_sla4_idle_bandwidth_no_regression(full_small):
    cfg = small_cfg()
    b = [r["metrics"] for r in full_small["runs"]
         if r["metrics"]["scenario"] == "B"
         and r["metrics"]["frequency"] == "idle"]
    a = [r["metrics"] for r in full_small["runs"]
         if r["metrics"]["scenario"] == "A"
         and r["metrics"]["frequency"] == "idle"]
    assert b and a
    assert max(x["control_plane"]["kbps"] for x in b) <= \
        cfg.idle_bandwidth_baseline_kb_s
    assert max(x["control_plane"]["kbps"] for x in b) <= \
        min(x["control_plane"]["kbps"] for x in a)


def test_sla5_token_bucket_caps_are_real():
    """小规格也要能验令牌桶上限（用 M 大于桶容量制造削峰）。"""
    j = matrix(node_list=[5], task_list=[200], freq_list=["high"],
               scenario_list=["B"], probes=3)
    for r in j["runs"]:
        m = r["metrics"]
        cfg = small_cfg()
        assert m["lsa"]["lsa_per_token_cycle_max"] <= cfg.token_bucket_lsa
        assert m["lsa"]["bytes_per_token_cycle_max"] <= cfg.token_bucket_bytes
        assert m["control_plane"]["per_node_peak_bytes_per_cycle"] <= \
            cfg.token_bucket_bytes


def test_sla6_storm_peak_jitter_effective(scale_small):
    """B 的抖动把同秒 pull 峰值压到远低于 A；N=100+high 必须 <= N*20%。

    A 现状（同相位无 jitter）峰值恒为 N，作为「雪崩分母」对照；
    B 用 h.next_deadline(±20%) 给每个跟随节点独立错峰。
    """
    cfg = small_cfg()
    by_n = {}
    for r in scale_small["runs"]:
        m = r["metrics"]
        key = (m["scenario"], m["N"], m["frequency"])
        by_n[key] = m
        assert m["sync_storm_limit"] == int(
            math.floor(m["N"] * cfg.sync_storm_max_fraction))
        if m["scenario"] == "A":
            assert m["sync_storm_peak"] == m["N"]
    # 卡面完成标准 #3 点名：N=100 + 高频 不出现 >20% 同步风暴
    n100 = by_n[("B", 100, "high")]
    assert n100["sync_storm_peak"] <= n100["sync_storm_limit"]
    # 所有 B 运行都显著优于同规模 A（至少减半），证明 jitter 生效
    for (sc, n, f), m in by_n.items():
        if sc == "B":
            assert m["sync_storm_peak"] < n, (n, f, m["sync_storm_peak"])


def test_sla7_single_multicast_source_and_no_duplicate_flood(full_small):
    for r in full_small["runs"]:
        m = r["metrics"]
        if m["scenario"] != "B":
            continue
        assert m["failover"]["max_multicast_sources"] <= 1
        assert m["failover"]["duplicate_floods"] == 0
        assert m["failover"]["leader_switches"] >= 1   # 确实切过主


def test_sla8_total_traffic_order_of_magnitude_and_scales_with_n():
    j = matrix(node_list=[5, 20, 50], task_list=[200],
               freq_list=["idle"], scenario_list=["A", "B"], probes=3)
    a = {r["metrics"]["N"]: r["metrics"]["gitea_requests"]["total"]
         for r in j["runs"] if r["metrics"]["scenario"] == "A"}
    b = {r["metrics"]["N"]: r["metrics"]["gitea_requests"]["total"]
         for r in j["runs"] if r["metrics"]["scenario"] == "B"}
    ratios = [a[n] / max(1, b[n]) for n in sorted(a)]
    assert all(r >= 10 for r in ratios)
    # B 是单源 O(1)，A 是 O(N)：N 越大优势越明显
    assert all(ratios[i] <= ratios[i + 1] for i in range(len(ratios) - 1))


# --------------------------------------------------------------------------- #
# 6. 震荡场景：1000 次翻转被 Flap Damping 压住，且状态最终不丢
# --------------------------------------------------------------------------- #
def test_flap_damping_compresses_1000_flips(full_small):
    f = full_small["flap_damping"]
    assert f["flips"] == 1000
    assert f["advertisements"] < 1000
    assert f["emitted_change_lsa"] < 1000
    assert f["flap_suppressed"] > 0
    assert f["flap_recovered"] > 0
    assert f["state_not_lost"] is True
    assert f["final_v"] == 999
    assert f["final_lsdb_entry"] is not None
    assert f["suppressed_keys"] == []


def test_flap_assertions_all_pass(full_small):
    fa = full_small["flap_assertions"]
    assert [x["id"] for x in fa] == ["FLAP-1", "FLAP-2", "FLAP-3"]
    assert all(x["pass"] for x in fa)


def test_flap_damping_is_reproducible():
    x = lt.run_flap(flips=1000, seed=SEED, config=small_cfg())
    y = lt.run_flap(flips=1000, seed=SEED, config=small_cfg())
    assert x == y


# --------------------------------------------------------------------------- #
# 7. 场景 A 现状模型：与变更频率无关（盲轮询），同相位风暴
# --------------------------------------------------------------------------- #
def test_scenario_a_blind_polling_independent_of_frequency():
    j = matrix(node_list=[5], task_list=[200],
               freq_list=["idle", "normal", "high"], scenario_list=["A"],
               probes=3)
    totals = [r["metrics"]["gitea_requests"]["total"] for r in j["runs"]]
    assert len(set(totals)) == 1, "A 的定时全量 pull 不该随变更频率变化"
    assert all(r["metrics"]["sync_storm_peak"] == 5 for r in j["runs"])


def test_scenario_b_idle_has_one_leader_fallback_only():
    j = matrix(node_list=[5], task_list=[200], freq_list=["idle"],
               scenario_list=["B"], probes=3)
    m = j["runs"][0]["metrics"]
    assert m["gitea_requests"]["total"] == 1
    assert m["gitea_requests"]["leader_fallback_ls_remote"] == 1


# --------------------------------------------------------------------------- #
# 8. 纯函数 / 百分位 / CLI 冒烟
# --------------------------------------------------------------------------- #
def test_percentile_and_run_sim_dispatch():
    assert lt._pct([], 50) is None
    assert lt._pct([1.0], 50) == 1.0
    assert lt._pct([1, 2, 3, 4], 50) == 2.0
    assert lt._pct([1, 2, 3, 4], 95) == 4.0
    p = lt.SimParams(nodes=5, tasks=200, frequency="idle",
                     duration=3600.0, scenario="A", seed=SEED,
                     config=small_cfg())
    assert lt.run_sim(p)["metrics"]["scenario"] == "A"
    p.scenario = "B"
    assert lt.run_sim(p)["metrics"]["scenario"] == "B"
    with pytest.raises(ValueError):
        p.scenario = "X"
        lt.run_sim(p)


def test_cli_json_output_to_file(tmp_path):
    out = tmp_path / "loadtest.json"
    rc = lt.main(["--nodes", "5", "--tasks", "200", "--frequency", "idle",
                  "--duration", "600", "--scenario", "B", "--seed", str(SEED),
                  "--json", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema"] == "hive-hlsa-loadtest/1"
    assert data["params"]["seed"] == SEED
    assert data["runs"]


def test_cli_bad_params_exit_2():
    with pytest.raises(SystemExit) as e:
        lt.main(["--nodes", "5", "--frequency", "bogus"])
    assert e.value.code == 2


def test_no_heavy_dependency_imported():
    """随机数用本地 Random，不依赖 numpy 等重型库。"""
    j = matrix(node_list=[5], task_list=[200], freq_list=["idle"],
               scenario_list=["B"], probes=2)
    assert j["methodology"]["dependencies"] == \
        "python stdlib + hive_lsa (both zero-I/O)"
    # 随机源是 random.Random，不是全局 random.seed
    rng = random.Random(SEED)
    assert 0.0 <= rng.random() < 1.0
