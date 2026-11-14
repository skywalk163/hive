"""T-0350：H-LSA 协议核心回归。

全部用 fake clock / fake random，零网络、零文件写入、不启动任何服务。
覆盖：常量表、合并窗口、MinLSInterval、MinLSArrival、令牌桶削峰、
Flap Damping 抑制与恢复、2^n 退避与变更回弹、jitter ±20%、
摘要一致 no-op、seq 缺口增量补拉、重复/旧 LSA 丢弃、1400B 分片、
1800s 只发 digest、30 分钟空闲 0 全量 0 Git 拉取 1 次 T2。
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

import hive_lsa as h


AREA = h.make_area("hive", "hive-dev", "hive-dev")
KEY = ("hive", "T-0001")
IDENT = (AREA, KEY)


@pytest.fixture()
def make_core():
    """每个用例一份独立状态机；clock/rng 可注入，默认完全确定性。"""
    made = []

    def _make(*, rng=lambda: 0.5, clock=lambda: 0.0, config=None, **kwargs):
        c = h.LSACore(
            originator="win11", area=AREA, project_tag="hive-dev",
            leader_epoch=3, config=config, clock=clock, rng=rng,
            start_at=0.0, **kwargs,
        )
        made.append(c)
        return c

    yield _make
    h.reset_default_core()


def make_lsa(seq: int, *, area: str = AREA, key: tuple[str, str] = KEY,
             epoch: int = 3, diff: dict | None = None, age: float = 0.0,
             kind: str = h.CHANGE, head_sha: str = "") -> h.LSA:
    return h.LSA(
        originator="peer", area=area, leader_epoch=epoch, seq=seq,
        kind=kind, key=key, diff=dict(diff or {}), ts=0.0, age=age,
        head_sha=head_sha,
    )


def all_packets(core: h.LSACore, times) -> list[dict]:
    """按 fake clock 推进若干时刻，收集所有待发包。"""
    out = []
    for t in times:
        out.extend(core.tick(float(t))["packets"])
    return out


# --------------------------------------------------------------------------- #
# 1. 纯标准库 / 零 I/O / 常量表
# --------------------------------------------------------------------------- #

def test_module_imports_only_stdlib_and_no_io():
    """完成标准 2：import hive_lsa 不启动任何服务；AST 里没有网络/文件调用。"""
    src = Path(h.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {"__future__", "dataclasses", "hashlib", "json", "math",
               "random", "time", "collections", "typing"}
    assert imported <= allowed, f"引入了非标准库/生产模块: {imported - allowed}"
    assert not any(m.startswith("hive") or m.startswith("dashboard")
                   or m.startswith("mcp_") for m in imported)

    forbidden = {"open", "socket", "urllib", "subprocess", "requests",
                 "Popen", "system", "urlopen", "mkdir", "write_text"}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not (used & forbidden), f"模块内出现 I/O 调用: {used & forbidden}"

    # 单独起一个干净进程验证「不启动任何服务就能 import」。
    import subprocess
    import sys
    p = subprocess.run([sys.executable, "-c", "import hive_lsa; print(hive_lsa.SyncConfig())"],
                       cwd=str(Path(h.__file__).parent), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0, p.stderr
    assert "SyncConfig(" in p.stdout


def test_sync_config_constants_match_spec():
    """完成标准 4：§2 / §5 常量逐项对齐，T-0355 可直接引用大写常量表。"""
    expected = {
        "AGGREGATION_WINDOW_S": 0.200,
        "MIN_LS_INTERVAL_S": 5.0,
        "MIN_LS_ARRIVAL_S": 1.0,
        "MULTICAST_BASE_S": 60.0,
        "MULTICAST_MAX_S": 1800.0,
        "LS_REFRESH_S": 1800.0,
        "MAX_AGE_S": 3600.0,
        "JITTER": 0.20,
        "TOKEN_BUCKET_LSA": 32,
        "TOKEN_BUCKET_BYTES": 65536,
        "MAX_PACKET_BYTES": 1400,
        "SLA_VISIBILITY_S": 300.0,
        "IDLE_BANDWIDTH_BASELINE_KB_S": 9.0,
        "SYNC_STORM_MAX_FRACTION": 0.20,
    }
    for name, value in expected.items():
        assert getattr(h, name) == value, f"{name} 漂移"
        assert h.CONSTANTS[name] == value, f"CONSTANTS.{name} 漂移"

    cfg = h.SyncConfig()
    assert cfg.min_ls_interval_s == 5.0
    assert cfg.MIN_LS_INTERVAL_S == 5.0        # 大写别名，供压测直接引用
    assert cfg.constants() == h.CONSTANTS
    assert "AGGREGATION_WINDOW_S" in h.CONSTANTS
    assert len(h.CONSTANTS) >= 20


def test_sync_config_is_frozen_and_overridable():
    cfg = h.SyncConfig()
    with pytest.raises(Exception):
        cfg.min_ls_interval_s = 1.0            # 不许为让测试通过偷偷放宽阈值
    small = cfg.overridden(min_ls_interval_s=2.0)
    assert small.min_ls_interval_s == 2.0
    assert cfg.min_ls_interval_s == 5.0        # 原常量表不受污染
    assert h.SyncConfig().MIN_LS_INTERVAL_S == 5.0


# --------------------------------------------------------------------------- #
# 2. 第 1 道闸：合并窗口
# --------------------------------------------------------------------------- #

def test_aggregation_window_merges_n_changes_into_one_lsa(make_core):
    c = make_core()
    for i in range(5):
        c.note_change(KEY, {"f%d" % i: i}, now=0.01 * i)
    assert c.tick(0.1)["emitted_change_lsa"] == 0          # 窗口未关，不发
    out = c.tick(0.2)
    assert out["emitted_change_lsa"] == 1                   # N 次变更 -> 1 条
    packets = [p for p in out["packets"] if p["type"] == "lsa"]
    assert len(packets) == 1
    assert packets[0]["lsas"][0]["diff"] == {f"f{i}": i for i in range(5)}
    assert c.status()["merged_changes"] == 4


def test_aggregation_window_diff_is_field_level_not_full_card(make_core):
    c = make_core()
    c.note_change(KEY, {"state": "running"}, now=0.0)
    c.note_change(KEY, {"progress": 42}, now=0.05)   # 字段名恰好叫 progress，
    diff = c.tick(0.2)["packets"][0]["lsas"][0]["diff"]
    assert diff == {"state": "running", "progress": 42}
    # 关键：只有这两个字段，没有整卡快照 / leases / nodes 大块状态体。
    assert set(diff) == {"state", "progress"}
    assert "leases" not in diff and "nodes" not in diff and "agents" not in diff


# --------------------------------------------------------------------------- #
# 3. 第 2 道闸（节奏）：MinLSInterval / MinLSArrival
# --------------------------------------------------------------------------- #

def test_min_ls_interval_throttles_same_key(make_core):
    c = make_core()
    c.note_change(KEY, {"v": 1}, now=0.0)
    assert c.tick(0.2)["emitted_change_lsa"] == 1
    c.note_change(KEY, {"v": 2}, now=1.0)
    assert c.tick(1.2)["emitted_change_lsa"] == 0        # 距上次仅 1s < 5s
    assert c.tick(4.9)["emitted_change_lsa"] == 0
    out = c.tick(5.2)                                     # 0.2 + 5.0 = 5.2 才放行
    assert out["emitted_change_lsa"] == 1
    assert c.status()["emitted_change_lsa"] == 2
    assert h.min_ls_interval_ok(5.0, 0.0) is True          # 边界：恰好 5s 放行
    assert h.min_ls_interval_ok(4.999, 0.0) is False
    assert h.min_ls_interval_ok(0.0, None) is True


def test_min_ls_arrival_defers_without_losing_state(make_core):
    c = make_core()
    r1 = c.merge_incoming([make_lsa(1, diff={"v": 1})], now=0.0)
    assert r1["accepted"] == 1
    r2 = c.merge_incoming([make_lsa(2, diff={"v": 2})], now=0.5)
    assert r2["deferred_min_arrival"] == 1                 # 1s 内第二次到达被节流
    assert c.lsdb.get(IDENT).seq == 1                      # 暂未安装
    c.tick(1.1)                                            # MinLSArrival 到期
    assert c.lsdb.get(IDENT).seq == 2                      # 新状态没丢，只是延后
    assert c.status()["accepted_incoming"] == 2
    assert h.min_ls_arrival_ok(1.0, 0.0) is True
    assert h.min_ls_arrival_ok(0.999, 0.0) is False


# --------------------------------------------------------------------------- #
# 4. 第 2 道闸：令牌桶削峰
# --------------------------------------------------------------------------- #

def test_token_bucket_caps_32_lsa_and_keeps_all_state(make_core):
    c = make_core()
    for k in range(60):
        c.note_change(("hive", "K%d" % k), {"v": k}, now=0.0)
    first = c.tick(0.2)
    assert first["emitted_change_lsa"] == h.TOKEN_BUCKET_LSA == 32
    assert first["deferred"] == 28
    assert c.status()["outbox"] == 28                      # 排队，不丢
    second = c.tick(5.2)                                   # 令牌补充周期到
    assert second["emitted_change_lsa"] == 28
    assert c.status()["emitted_change_lsa"] == 60          # 60 个 key 一个不少
    assert c.status()["token_deferred"] == 28


def test_token_bucket_byte_cap_is_enforced(make_core):
    cfg = h.SyncConfig().overridden(token_bucket_bytes=1500)
    c = make_core(config=cfg)
    for k in range(40):
        c.note_change(("hive", "B%d" % k), {"payload": "x" * 180}, now=0.0)
    out = c.tick(0.2)
    assert 0 < out["emitted_change_lsa"] < h.TOKEN_BUCKET_LSA
    assert out["deferred"] > 0
    emitted_bytes = sum(len(p["lsas"][i]["diff"]["payload"])
                        for p in out["packets"] if p["type"] == "lsa"
                        for i in range(p["n"]))
    assert emitted_bytes <= 1500 + 180                     # 未超字节令牌
    assert h.SyncConfig().token_bucket_bytes == 65536      # 默认口径未被改掉


# --------------------------------------------------------------------------- #
# 5. 第 3 道闸：Flap Damping
# --------------------------------------------------------------------------- #

def _oscillate(c: h.LSACore, n: int = 40, step: float = 5.01) -> float:
    for i in range(n):
        t = i * step
        c.note_change(KEY, {"v": i}, now=t)
        c.tick(t + 0.2)
    return (n - 1) * step + 0.2


def test_flap_damping_suppresses_oscillation_and_only_sends_summaries(make_core):
    c = make_core()
    _oscillate(c, 40)
    s = c.status()
    assert s["flap_suppressed"] > 20                       # 反复震荡被抑制
    assert s["emitted_change_lsa"] < 5                     # 绝不是 40 条 LSA
    assert s["flap_summaries"] >= 1                        # 抑制期「只发汇总」
    assert c.flap.snapshot(IDENT, 200.0)["interval_multiplier"] > 1  # 拉长间隔


def test_flap_damping_recovers_and_never_loses_final_state(make_core):
    c = make_core()
    end = _oscillate(c, 40)
    assert c.flap.is_suppressed(IDENT, end) is True
    restored = None
    for t in range(int(end) + 1, int(end) + 350):
        out = c.tick(float(t))
        if out["emitted_change_lsa"] and restored is None:
            restored = (t, [p["lsas"][0]["diff"] for p in out["packets"]
                           if p["type"] == "lsa"])
    assert restored is not None, "半衰衰减后必须解除抑制"
    assert restored[0] - end <= h.SLA_VISIBILITY_S         # 300s 可见性 SLA
    assert {"v": 39} in restored[1]                        # 最终状态没丢
    assert c.status()["flap_recovered"] >= 1
    assert c.status()["suppressed_keys"] == []


def test_flap_penalty_is_exponential_and_decays_by_half_life():
    assert h.flap_penalty_increment(1) == 1000.0
    assert h.flap_penalty_increment(2) == 2000.0
    assert h.flap_penalty_increment(3) == 4000.0
    assert h.flap_penalty_increment(4) == 8000.0
    assert h.flap_decay(2000.0, 60.0, 60.0) == 1000.0
    assert math.isclose(h.flap_decay(2000.0, 120.0, 60.0), 500.0)
    assert h.flap_decay(2000.0, 0.0, 60.0) == 2000.0


# --------------------------------------------------------------------------- #
# 6. 三档节奏：2^n 退避 / 变更回弹 / jitter
# --------------------------------------------------------------------------- #

def test_backoff_2n_sequence_and_cap():
    expected = [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0]
    assert [h.backoff_interval(n) for n in range(7)] == expected
    assert h.backoff_interval(100) == 1800.0
    assert h.backoff_interval(0, base=10.0, cap=80.0) == 10.0


def test_change_rebounds_backoff_to_60s(make_core):
    c = make_core()
    # 连续空转若干轮，T1 退避逐步拉长
    for t in (66, 198, 462):
        assert c.tick(float(t))["tier"] == h.TIER_T1
    assert c.status()["idle_rounds"] == 3
    assert c.status()["backoff_interval_s"] == 480.0
    c.note_change(KEY, {"v": 1}, now=500.0)                # 一有变更立刻回弹
    assert c.status()["idle_rounds"] == 0
    assert c.status()["backoff_interval_s"] == 60.0
    assert c.tick(500.2)["emitted_change_lsa"] == 1        # 且 T0 立刻发


@pytest.mark.parametrize("u", [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 0.999])
def test_jitter_always_present_and_within_20pct(u):
    for base in (60.0, 1800.0):
        value = h.jittered(base, rng=lambda: u)
        assert base * 0.8 <= value <= base * 1.2, (u, base, value)
        assert value != base, "jitter 不允许退化为整周期"


def test_all_periodic_timers_are_jittered_and_nodes_do_not_align(make_core):
    a = make_core(rng=lambda: 0.1)
    b = make_core(rng=lambda: 0.9)
    for c in (a, b):
        s = c.status()
        assert 48.0 <= s["next_t1_at"] <= 72.0
        assert 1440.0 <= s["next_t2_at"] <= 2160.0
        assert s["next_t2_at"] != h.LS_REFRESH_S == 1800.0
        assert all(d != h.LS_REFRESH_S for d in s["jittered_deadlines"])
    # 两个节点不会被排到同一秒点火（同步风暴的根源）
    assert a.status()["next_t2_at"] != b.status()["next_t2_at"]
    assert a.status()["next_t1_at"] != b.status()["next_t1_at"]


def test_classify_tier_pure_function():
    assert h.classify_tier(has_pending=True, now=0.0,
                           next_t1_at=10.0, next_t2_at=20.0) == h.TIER_T0
    assert h.classify_tier(has_pending=False, now=25.0,
                           next_t1_at=10.0, next_t2_at=20.0) == h.TIER_T2
    assert h.classify_tier(has_pending=False, now=11.0,
                           next_t1_at=10.0, next_t2_at=20.0) == h.TIER_T1
    assert h.classify_tier(has_pending=False, now=5.0,
                           next_t1_at=10.0, next_t2_at=20.0) is None


# --------------------------------------------------------------------------- #
# 7. 摘要 / 按需补拉
# --------------------------------------------------------------------------- #

def test_digest_equal_returns_noop_with_zero_git_traffic(make_core):
    c = make_core()
    c.note_change(KEY, {"state": "done"}, now=0.0)
    c.tick(0.2)
    digest = c.build_digest(now=1.0)
    decision = c.on_hello(digest, now=1.0)
    assert decision["action"] == h.NO_OP
    assert decision["full_state_transfers"] == 0
    assert c.status()["git_pull_instructions"] == 0        # 零 Git 流量
    assert c.should_pull(digest, now=1.0) is False


def test_seq_gap_triggers_incremental_pull(make_core):
    a = make_core()
    b = make_core()
    for seq in (1, 2, 3):
        a.merge_incoming([make_lsa(seq, diff={"v": seq})], now=float(seq) * 2)
    b.merge_incoming([make_lsa(1, diff={"v": 1})], now=2.0)
    peer = b.build_digest(now=10.0)
    peer["entries"] = b.build_csnp(now=10.0)[0]["entries"]
    decision = a.on_hello(peer, now=10.0)
    assert decision["action"] == h.PULL_INCREMENTAL
    assert decision["missing"] == [2, 3]
    assert decision["seq_gap"] == 2
    assert decision["full_state_transfers"] == 0


def test_legacy_peer_without_digest_triggers_full_pull(make_core):
    c = make_core()
    decision = c.on_hello({"sd": "old-0.16.05"}, now=1.0)
    assert decision["action"] == h.PULL_FULL
    assert c.status()["git_pull_instructions"] == 1


def test_huge_seq_gap_degrades_to_full_pull(make_core):
    a = make_core()
    for seq in range(1, h.PULL_FULL_GAP_LIMIT + 3):
        a.merge_incoming([make_lsa(seq, diff={"v": seq})], now=float(seq) * 2)
    peer = {"digest": "different", "seq": 1, "head_sha": ""}
    assert a.on_hello(peer, now=1000.0)["action"] == h.PULL_FULL


def test_should_pull_uses_head_sha_and_is_false_when_idle(make_core):
    c = make_core()
    c.set_head_sha("sha-local")
    assert c.should_pull() is False                        # 空闲：不问 Gitea
    assert c.status()["git_pull_instructions"] == 0
    assert c.should_pull({"digest": c.lsdb.snapshot_digest(),
                          "seq": c.lsdb.seq, "head_sha": "sha-remote"}) is True
    c.set_head_sha("sha-remote")
    assert c.should_pull({"digest": c.lsdb.snapshot_digest(),
                          "seq": c.lsdb.seq, "head_sha": "sha-remote"}) is False


def test_digest_and_csnp_carry_no_state_body(make_core):
    c = make_core()
    c.note_change(KEY, {"state": "running", "progress": 12}, now=0.0)
    c.tick(0.2)
    packets = [c.build_digest(now=1.0), c.build_digest(kind=h.REFRESH, now=1.0)]
    packets += c.build_csnp(now=1.0)
    for p in packets:
        assert not (h.FORBIDDEN_IN_DIGEST & set(p.keys())), p
        assert h.packet_bytes(p) <= h.MAX_PACKET_BYTES
        for entry in p.get("entries", []):
            assert not (h.FORBIDDEN_IN_DIGEST & set(entry.keys()))
            assert set(entry) <= {"key", "area", "seq", "epoch", "digest", "head_sha"}
    d = c.build_digest(now=1.0)
    for field in ("leader_epoch", "seq", "digest", "head_sha", "n_changes",
                  "area", "project_tag", "sent_at"):
        assert field in d, f"T-0351 摘要报文缺字段 {field}"
    assert d["type"] == "lsa_digest"


def test_build_digest_can_carry_csnp_entries_without_state_body(make_core):
    c = make_core()
    for k in range(3):
        c.note_change(("hive", "C%d" % k), {"v": k}, now=0.0)
    c.tick(0.2)
    short = c.build_digest(now=1.0)
    assert "entries" not in short                          # 默认保持 T-0351 线格式
    full = c.build_digest(now=1.0, with_entries=True)
    assert full["entries_truncated"] is False
    assert len(full["entries"]) == 3
    assert h.packet_bytes(full) <= h.MAX_PACKET_BYTES
    for entry in full["entries"]:
        assert not (h.FORBIDDEN_IN_DIGEST & set(entry.keys()))
    d = dict(full)
    d["digest"] = "different"                              # 制造 digest 不一致
    assert c.on_hello(d, now=1.0)["action"] == h.PULL_INCREMENTAL
    # 完整清单按条目边界分片，每包仍 ≤1400B
    assert all(h.packet_bytes(p) <= h.MAX_PACKET_BYTES
               for p in c.build_csnp(now=1.0))


def test_head_sha_is_separate_field_for_t0353(make_core):
    c = make_core()
    c.set_head_sha("local-head")
    c.set_remote_head_sha("remote-head")
    assert c.build_digest(now=0.0)["head_sha"] == "local-head"
    assert c.sync_health()["remote_sha"] == "remote-head"
    assert c.sync_health()["behind"] is True


def test_sync_health_shape_for_status_json(make_core):
    c = make_core()
    health = c.sync_health()
    for field in ("lsdb_seq", "last_sync_at", "head_sha", "remote_sha",
                  "behind", "tier", "reason", "seq"):
        assert field in health
    assert health["tier"] in (h.TIER_T0, h.TIER_T1, h.TIER_T2)
    assert health["seq"] == health["lsdb_seq"]             # T-0354 读 seq


# --------------------------------------------------------------------------- #
# 8. 第 4 道闸：去重 / MaxAge / 分片
# --------------------------------------------------------------------------- #

def test_duplicate_and_old_lsa_are_dropped(make_core):
    c = make_core()
    c.merge_incoming([make_lsa(5, diff={"v": 5})], now=0.0)
    r = c.merge_incoming([make_lsa(5, diff={"v": 5}),
                          make_lsa(4, diff={"v": 4}),
                          make_lsa(3, diff={"v": 3})], now=2.0)
    assert r["accepted"] == 0
    assert r["dropped_duplicate"] == 1
    assert r["dropped_old"] == 2
    assert c.lsdb.seq == 1
    assert c.lsdb.get(IDENT).seq == 5


def test_lsa_newer_semantics_epoch_then_seq(make_core):
    c = make_core()
    c.merge_incoming([make_lsa(9, epoch=1, diff={"v": 9})], now=0.0)
    assert c.lsdb.is_newer(make_lsa(100, epoch=0, diff={"v": 100})) is False
    assert c.lsdb.is_newer(make_lsa(1, epoch=2, diff={"v": 1})) is True
    assert c.lsdb.is_newer(make_lsa(10, epoch=1, diff={"v": 10})) is True
    assert c.lsdb.is_newer(make_lsa(9, epoch=1, diff={"v": 9})) is False


def test_maxage_aging_and_maxage_lsa_rejected(make_core):
    c = make_core()
    c.merge_incoming([make_lsa(1, diff={"v": 1})], now=0.0)
    assert len(c.lsdb) == 1
    assert c.lsdb.purge_expired(3599.0) == 0
    assert c.lsdb.purge_expired(3600.0) == 1               # MaxAge 3600s
    assert len(c.lsdb) == 0
    too_old = make_lsa(9, diff={"v": 9}, age=4000.0)
    r = c.merge_incoming([too_old], now=3601.0)
    assert r["dropped_max_age"] == 1
    assert len(c.lsdb) == 0


def test_fragment_1400_never_splits_a_lsa():
    lsas = [h.LSA(originator="n", area=AREA, leader_epoch=0, seq=i + 1,
                  kind=h.CHANGE, key=("hive", "K%d" % i),
                  diff={"payload": "x" * 180, "i": i}) for i in range(20)]
    packets, oversize = h.pack_lsas(lsas)
    assert not oversize
    assert all(h.packet_bytes(p) <= h.MAX_PACKET_BYTES for p in packets)
    assert sum(p["n"] for p in packets) == 20               # 一条不丢
    seen = []
    for p in packets:
        for body in p["lsas"]:
            assert isinstance(body["key"], list) and len(body["key"]) == 2
            assert body["diff"]["payload"] == "x" * 180     # 整条、没被切半
            seen.append(tuple(body["key"]))
    assert len(seen) == len(set(seen)) == 20


def test_oversize_single_lsa_goes_to_oversize_not_split():
    big = h.LSA(originator="n", area=AREA, leader_epoch=0, seq=99,
                kind=h.CHANGE, key=("hive", "BIG"),
                diff={"p": "y" * 3000})
    packets, oversize = h.pack_lsas([big])
    assert packets == []
    assert len(oversize) == 1
    assert oversize[0]["diff"]["p"] == "y" * 3000          # 完整保留，不切半条
    assert h.fragment_lsas is h.pack_lsas


def test_lsa_pacing_group_step(make_core):
    c = make_core()
    for k in range(20):
        c.note_change(("hive", "P%d" % k), {"payload": "x" * 180}, now=0.0)
    out = c.tick(0.2)
    lsa_packets = [p for p in out["packets"] if p["type"] == "lsa"]
    assert len(lsa_packets) > 1
    sent = [p["sent_at"] for p in lsa_packets]
    for a, b in zip(sent, sent[1:]):
        assert math.isclose(b - a, h.LSA_PACING_S, rel_tol=1e-9)


# --------------------------------------------------------------------------- #
# 9. T2：1800s 只发 digest；30 分钟空闲仿真（完成标准 3）
# --------------------------------------------------------------------------- #

def test_refresh_at_1800_emits_digest_only(make_core):
    c = make_core(rng=lambda: 0.1)                          # T2 -> 1512.0s
    c.note_change(KEY, {"state": "done"}, now=0.0)
    c.tick(0.2)
    out = c.tick(1512.0)
    assert out["t2_refresh"] is True
    assert out["full_state_transfers"] == 0
    assert out["git_pull_instructions"] == 0
    t2_packets = [p for p in out["packets"] if p.get("kind") == h.REFRESH]
    assert t2_packets
    assert {p["type"] for p in t2_packets} <= {"lsa_refresh", "lsa_csnp"}
    for p in t2_packets:
        assert not (h.FORBIDDEN_IN_DIGEST & set(p.keys()))
        assert h.packet_bytes(p) <= h.MAX_PACKET_BYTES
    assert c.status()["t2_events"] == 1


def test_idle_30min_zero_full_state_zero_git_one_jittered_t2(make_core):
    """完成标准 3：fake clock 跑 30 分钟空闲。"""
    c = make_core(rng=lambda: 0.1)
    packets = []
    t2_events = []
    for t in range(0, 1801):
        out = c.tick(float(t))
        assert out["full_state_transfers"] == 0
        assert out["git_pull_instructions"] == 0
        if out["t2_refresh"]:
            t2_events.append(t)
        for p in out["packets"]:
            assert h.packet_bytes(p) <= h.MAX_PACKET_BYTES
            if p["type"] in ("lsa_digest", "lsa_refresh", "lsa_csnp"):
                assert not (h.FORBIDDEN_IN_DIGEST & set(p.keys()))
            packets.append(p)

    s = c.status()
    assert len(t2_events) == 1, "30 分钟内只允许 1 次 T2 对账"
    assert 1440.0 <= t2_events[0] <= 2160.0
    assert t2_events[0] != 1800.0, "T2 触发时刻必须带 jitter"
    assert s["full_state_transfers"] == 0
    assert s["git_pull_instructions"] == 0
    assert s["emitted_change_lsa"] == 0, "空闲不得产生任何 change LSA"
    assert s["t2_events"] == 1
    assert {p["type"] for p in packets} <= {"lsa_digest", "lsa_refresh", "lsa_csnp"}
    assert c.should_pull() is False
    assert c.status()["git_pull_instructions"] == 0


def test_status_metrics_and_constant_table(make_core):
    c = make_core()
    s = c.status()
    for field in ("lsdb_seq", "seq", "emitted_packets", "emitted_bytes",
                  "full_state_transfers", "git_pull_instructions",
                  "dropped_duplicate", "flap_suppressed", "token_deferred",
                  "jittered_deadlines", "constants", "backoff_interval_s"):
        assert field in s
    assert s["full_state_transfers"] == 0
    assert s["constants"] == h.CONSTANTS
    assert s["jitter"] == 0.20


# --------------------------------------------------------------------------- #
# 10. 模块级便捷 API（供 T-0351/0352/0353 直接调用）
# --------------------------------------------------------------------------- #

def test_module_level_api_smoke(make_core):
    c = make_core()
    h.set_default_core(c)
    try:
        r = h.note_change(KEY, {"state": "running"}, now=0.0)
        assert r["queued"] is True
        assert h.tick(0.2)["emitted_change_lsa"] == 1
        d = h.build_digest(now=1.0)
        assert h.on_hello(d, now=1.0)["action"] == h.NO_OP
        assert isinstance(h.build_csnp(now=1.0), list)
        assert h.merge_incoming([make_lsa(7, diff={"v": 7})], now=2.0)["accepted"] == 1
        assert h.should_pull() is False
        assert "lsdb_seq" in h.sync_health()
        assert h.status()["emitted_change_lsa"] == 1
    finally:
        h.reset_default_core()


def test_note_change_and_tick_never_emit_full_state_body(make_core):
    """任何 tick 产物都不许出现 progress/leases/nodes 全量状态体。"""
    c = make_core()
    c.note_change(KEY, {"state": "running", "progress": 1}, now=0.0)
    for t in (0.2, 5.2, 66.0, 1512.0):
        out = c.tick(t)
        assert out["full_state_transfers"] == 0
        for p in out["packets"]:
            if p["type"] in ("lsa_digest", "lsa_refresh", "lsa_csnp", "lsa_summary"):
                assert "diff" not in p
                assert "progress" not in p and "leases" not in p
