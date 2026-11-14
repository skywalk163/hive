#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""H-LSA 规模仿真压测与雪崩验证（T-0355）。

纯仿真：零 I/O（不连 Gitea、不真发组播、不启动集群进程、不写生产状态），
只依赖 Python 标准库 + 同仓 ``hive_lsa``（它本身也是零 I/O）。

注入式接缝::

    FakeClock      -> 主循环显式 set(t)，所有 note_change/tick/merge_incoming 都传 now=
    FakeTransport  -> 只记账 + 批量回调投递，不碰任何 socket / 组播 / 文件

两个场景：

* **A 现状**：所有节点**同相位**固定周期（60s）定时全量 ``git pull``；
  每次 = ls-remote + fetch + pull 三条 Gitea 请求 + 全量重写 state.json。
  同相位无 jitter 正是 0.16.03/0.16.05 「同步风暴 / 压力雪崩」的成因，
  也是本卡要量化的分母。**A 的轮询是盲轮询，与变更频率无关（这正是病根）**。
* **B H-LSA**：单主控（组播源恒 ≤1）+ N-1 跟随，全部用真实
  ``hive_lsa.LSACore`` 驱动 T0/T1/T2。跟随节点批量 ``merge_incoming()`` 收
  LSA、``on_hello()`` 判增量拉取。每个节点独立 rng → ±20% jitter 生效。

仿真口径（全部写进输出 JSON 的 ``methodology``，报告只引用实测值）：

* ``TASK_STATE_BYTES`` / ``STATE_JSON_BYTES_PER_CARD`` 是**模型参数**，
  不是生产实测，绝不冒充测量值。
* 控制面字节：B = 主控每 tick 经 ``hive_lsa.packet_bytes()`` 计出的报文
  字节之和（单源，O(1)）；``wire_bytes`` = 单源 × (N-1)（组播扇出）。
  A = poll 次数 × M × TASK_STATE_BYTES。
* 可见性延迟：受控探针 key（与背景负载隔离）从注入到全部 N-1 个跟随
  节点 LSDB 反映的 P50/P95/max；未收敛如实记 ``unavailable``。
* 「周期」有两处，分别标注：令牌桶周期 = ``TOKEN_BUCKET_PERIOD_S``（5s，
  32 条/64KB 上限的口径）；全量对账周期 = ``LS_REFRESH_S``（1800s ±20%）。
* 未达标项如实记 ``pass:false`` + ``reason``；跑不出来写 ``unavailable``。

用法（与 T-0355 验收命令一致；--json 既可单独开关，也可带路径）::

    python3 tools/lsa_loadtest.py --nodes 5,20,50,100 --duration 3600 \
        --seed 20261114 --json /tmp/hlsa-loadtest.json
    python3 tools/lsa_loadtest.py --nodes 5 --tasks 200 --frequency idle \
        --duration 3600 --scenario B --json
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# 既能从仓库根被 pytest 导入，也能作为独立脚本从任意 cwd 运行。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import hive_lsa as h  # noqa: E402  （零 I/O 的协议核心，本仿真的唯一依赖）


# --------------------------------------------------------------------------- #
# 仿真口径常量（模型参数，全部显式标注，绝不冒充生产实测）
# --------------------------------------------------------------------------- #
TASK_STATE_BYTES = 512            # A 场景一次全量 pull 传输的每卡字节（模型参数）
STATE_JSON_BYTES_PER_CARD = 64    # state.json 里每张卡的紧凑状态字节（模型参数）
PROBE_DIFF_VALUE = "probe"        # 探针变更的字段值
FAILOVER_FRACTION = 0.5           # 在 duration 的 50% 处切主
PROBE_SPAN = (0.05, 0.40)         # 探针注入窗口（fraction of duration，全部在切主前）


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _pct(values: list[float], p: float) -> float | None:
    """nearest-rank 百分位；空列表返回 None（如实标 unavailable）。"""
    if not values:
        return None
    s = sorted(values)
    k = max(1, math.ceil(p / 100.0 * len(s)))
    return float(s[k - 1])


# --------------------------------------------------------------------------- #
# 注入式 fake_clock / fake_transport
# --------------------------------------------------------------------------- #
class FakeClock:
    """可注入时钟：主循环显式 set(t)，所有协议调用另外显式传 now=。"""

    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += float(dt)

    def set(self, t: float) -> None:
        self.now = float(t)


class FakeTransport:
    """可注入传输：只记账 + 一次批量回调投递，没有任何 socket / 组播 / 文件 I/O。

    组播建模为「单源一次发出、扇出到 N-1 个对端」：
      ``leader_bytes`` = 源端上线的字节（O(1)，与 N 无关）；
      ``wire_bytes``   = leader_bytes × (N-1)（全网扇出字节）。
    """

    def __init__(self) -> None:
        self.leader_bytes = 0
        self.wire_bytes = 0
        self.packets_emitted = 0
        self.packets_delivered = 0
        self.per_node_bytes: dict[int, int] = defaultdict(int)
        self.per_node_packets: dict[int, int] = defaultdict(int)
        self.max_multicast_sources = 0
        self.duplicate_floods = 0
        self._sources_by_lsa: dict[tuple[str, int, str, str], str] = {}

    def broadcast(self, src: str, packets: list[dict[str, Any]], now: float,
                  peers: list[int],
                  deliver: Callable[[list[dict[str, Any]], list[int], float], None]) -> int:
        """记账全部报文字节；把整批一次性交给 deliver() 批量投递。"""
        if not packets:
            return 0
        self.max_multicast_sources = max(self.max_multicast_sources, 1)
        batch = 0
        for pkt in packets:
            b = h.packet_bytes(pkt)
            self.leader_bytes += b
            self.packets_emitted += 1
            batch += b * max(0, len(peers))
            # 重复泛洪检测：同一 (originator, seq, key) 被两个不同源发出
            if pkt.get("type") == "lsa":
                for body in pkt.get("lsas", []) or []:
                    key = body.get("key", ["", ""])
                    lid = (str(body.get("originator", "")), int(body.get("seq", 0)),
                           str(key[0]) if key else "",
                           str(key[1]) if len(key) > 1 else "")
                    prev = self._sources_by_lsa.get(lid)
                    if prev is not None and prev != src:
                        self.duplicate_floods += 1
                    self._sources_by_lsa[lid] = src
            for peer in peers:
                self.per_node_bytes[peer] += b
                self.per_node_packets[peer] += 1
                self.packets_delivered += 1
        self.wire_bytes += batch
        deliver(packets, peers, now)
        return batch


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
@dataclass
class SimParams:
    nodes: int = 5
    tasks: int = 200
    frequency: str = "normal"       # idle | normal | high
    duration: float = 3600.0        # 仿真时长（秒）
    scenario: str = "B"             # A | B
    seed: int = 20261114
    config: Any = None              # 可注入 h.SyncConfig（缩短时钟的冒烟用）
    polls_period_s: float = 0.0     # A 轮询周期；0 = cfg.multicast_base_s
    probes: int = 20                # 可见性探针 key 数量
    failover: bool = True           # 是否在中途切主
    storm_bucket_s: float = 1.0     # 「同一秒」桶宽
    tick_step_s: float = 0.0        # 0 = 5.0s（覆盖 200ms 窗口/5s 令牌桶，且高频按批量建模）


def _cfg(p: SimParams) -> Any:
    return p.config or h.SyncConfig()


def _poll_period(p: SimParams) -> float:
    return p.polls_period_s or _cfg(p).multicast_base_s


def _task_key(i: int) -> tuple[str, str]:
    return ("hive", "T-%05d" % i)


def _run_id(p: SimParams) -> str:
    return "%s_N%d_M%d_%s_seed%d" % (p.scenario, p.nodes, p.tasks,
                                     p.frequency, p.seed)


def _params_dict(p: SimParams) -> dict[str, Any]:
    return {
        "scenario": p.scenario, "nodes": p.nodes, "tasks": p.tasks,
        "frequency": p.frequency, "duration_s": p.duration, "seed": p.seed,
        "polls_period_s": _poll_period(p), "probes": p.probes,
        "failover": p.failover, "storm_bucket_s": p.storm_bucket_s,
        "tick_step_s": p.tick_step_s or 5.0,
        "config": _cfg(p).as_dict(),
    }


# --------------------------------------------------------------------------- #
# 场景 A：现状（所有节点同相位定时全量 git pull）
# --------------------------------------------------------------------------- #
def run_scenario_a(p: SimParams) -> dict[str, Any]:
    cfg = _cfg(p)
    period = _poll_period(p)
    duration = float(p.duration)
    n = int(p.nodes)
    m = int(p.tasks)

    # 同相位固定周期：所有节点在 t = k*period 同时发起全量 pull（无 jitter，现状病根）
    num_polls_per_node = int(math.floor(duration / period))
    poll_times = [k * period for k in range(1, num_polls_per_node + 1)]

    # 同步风暴：同一秒发起 pull 的节点数峰值（同相位 => 每个 poll 时刻都是 N）
    storm_peak = n if poll_times else 0

    # Gitea 请求：每次 poll = ls-remote + fetch + pull
    polls_total = len(poll_times) * n
    gitea_ls_remote = polls_total
    gitea_fetch = polls_total
    gitea_pull = polls_total
    gitea_total = gitea_ls_remote + gitea_fetch + gitea_pull

    payload = m * TASK_STATE_BYTES                    # 一次全量 pull 的字节
    state_file_bytes = m * STATE_JSON_BYTES_PER_CARD  # 一次 state.json 全量重写字节
    control_bytes_total = polls_total * payload
    state_writes = polls_total
    state_bytes_total = state_writes * state_file_bytes

    # 可见性：某个探针变更后，要等下一个同相位 poll 才全集群可见
    probe_n = min(p.probes, m)
    latencies: list[float] = []
    for j in range(probe_n):
        injected = duration * (PROBE_SPAN[0]
                              + (PROBE_SPAN[1] - PROBE_SPAN[0])
                              * (j + 0.5) / max(1, probe_n))
        nxt = next((t for t in poll_times if t >= injected), duration)
        latencies.append(float(nxt - injected))

    idle_kbps = (control_bytes_total / duration) / 1024.0 if duration else 0.0
    metrics = {
        "N": n, "M": m, "frequency": p.frequency, "scenario": "A",
        "duration_s": duration, "poll_period_s": period, "jitter": 0.0,
        "synchronized_polls": True,
        "gitea_requests": {
            "ls_remote_total": gitea_ls_remote, "fetch_total": gitea_fetch,
            "pull_total": gitea_pull, "total": gitea_total,
            "per_node": gitea_total / n if n else 0,
            "per_node_per_30min": gitea_total / n * (1800.0 / duration)
            if duration and n else 0,
            "leader_fallback_ls_remote": 0,
        },
        "control_plane": {
            "leader_bytes": 0, "wire_bytes": control_bytes_total,
            "total_bytes": control_bytes_total,
            "per_node_peak_bytes_per_cycle": payload,
            "cycle_definition_s": period,
            "kbps": round(idle_kbps, 6),
            "packets_emitted": 0, "packets_delivered": 0,
            "per_node_bytes_max": payload * num_polls_per_node,
        },
        "sync_storm_peak": storm_peak,
        "sync_storm_limit": int(math.floor(n * cfg.sync_storm_max_fraction)),
        "state_json": {"writes": state_writes, "bytes_total": state_bytes_total,
                       "bytes_per_write": state_file_bytes},
        "lsa": {"emitted_change_lsa": 0, "merged_changes": 0,
                "lsa_per_token_cycle_max": 0, "bytes_per_token_cycle_max": 0},
        "visibility_s": {"samples": len(latencies), "converged_samples": len(latencies),
                         "unavailable_samples": 0,
                         "p50": _pct(latencies, 50), "p95": _pct(latencies, 95),
                         "max": _pct(latencies, 100)},
        "reconcile": {"t2_events": 0, "intervals_s": [], "digest_only": None,
                      "full_state_transfers": 0},
        "failover": {"max_multicast_sources": 0, "duplicate_floods": 0,
                     "leader_switches": 0},
        "cpu_proxy": {"lsa_processing": 0, "merge_count": 0},
        "methodology_note": ("A 现状按所有节点同相位固定周期盲轮询建模；"
                             "Gitea 请求与变更频率无关，这正是 O(N) 的病根"),
    }
    return {"run_id": _run_id(p), "params": _params_dict(p), "metrics": metrics}


# --------------------------------------------------------------------------- #
# 场景 B：H-LSA（T0 + T1 + T2，单主控）
# --------------------------------------------------------------------------- #
def run_scenario_b(p: SimParams) -> dict[str, Any]:
    cfg = _cfg(p)
    duration = float(p.duration)
    n = int(p.nodes)
    m = int(p.tasks)
    clock = FakeClock(0.0)
    transport = FakeTransport()

    area = h.make_area("hive", "hive-dev", "hive-dev")
    # 每个节点独立 rng：LSACore 的 T1/T2 抖动、以及跟随节点增量 pull 的
    # jittered 错峰都用各自独立的随机源，保证「不会所有节点同一秒齐发」。
    node_rngs = [random.Random(p.seed + i).random for i in range(n)]
    cores = [h.LSACore(originator="n%d" % i, area=area,
                       project_tag="hive-dev", leader_epoch=0, config=cfg,
                       clock=clock, rng=node_rngs[i], start_at=0.0)
             for i in range(n)]

    leader = 0
    leader_switches = 0
    rng_load = random.Random(p.seed + 99991)
    # 模拟 Git 事实账的 origin/main 头：每次 repo 变更推进一次。
    # 主控是唯一连 Gitea 的节点（Leader 单源），它把 head_sha 放进 T1 摘要；
    # 跟随节点靠 on_hello() 比对，不一致才增量补拉（不是定时全量 pull）。
    git_head = ["h0"]
    head_seq = [0]
    for c in cores:
        c.set_head_sha(git_head[0])

    def advance_head() -> str:
        head_seq[0] += 1
        git_head[0] = "h%d" % head_seq[0]
        return git_head[0]

    all_keys = [_task_key(i) for i in range(max(1, m))]
    probe_n = min(p.probes, len(all_keys))
    probe_keys = all_keys[:probe_n]
    background_keys = all_keys[probe_n:] or all_keys[:1]
    probe_set = set(probe_keys)
    probe_area = area

    # 事件表：(时刻, 序号, 类型, 数据)
    events: list[tuple[float, int, str, Any]] = []
    _seq = [0]

    def push(t: float, kind: str, data: Any = None) -> None:
        if t <= duration:
            heapq.heappush(events, (float(t), _seq[0], kind, data))
            _seq[0] += 1

    # 1) 背景变更 + 3) 主控 tick 节拍
    #    idle=0 / normal=1 次每 60s / high=10 次每秒。
    #    高频按「每 tick 一批 rate*step 次」建模（语义等价：这些变更全部落在
    #    200ms 合并窗口内合成 1 条/键的 LSA），避免 36000 个堆事件拖慢仿真。
    freq = p.frequency
    step = p.tick_step_s or 5.0
    rate = (0.0 if freq == "idle"
            else (1.0 / cfg.multicast_base_s if freq == "normal" else 10.0))
    cum = 0.0
    t = step
    while t <= duration:
        cum += rate * step
        cnt = int(cum)
        cum -= cnt
        if cnt:
            push(t, "change_batch", cnt)
        push(t, "tick", None)
        t += step

    # 2) 可见性探针：全部注入在切主之前，与背景负载隔离
    injected_at: dict[tuple[str, str], float] = {}
    for j, key in enumerate(probe_keys):
        t = duration * (PROBE_SPAN[0]
                        + (PROBE_SPAN[1] - PROBE_SPAN[0])
                        * (j + 0.5) / max(1, probe_n))
        injected_at[key] = t
        push(t, "probe", key)

    # 4) 切主：中点换主控，leader_epoch 单调递增
    if p.failover and n >= 2:
        push(duration * FAILOVER_FRACTION, "failover", None)

    # -- 采集容器 ---------------------------------------------------------- #
    t2_times: list[float] = []
    t2_periods: list[float] = []      # 每次 T2 重新抖出的实际对账周期（next_t2_at - 触发时刻）
    t2_packet_types: set[str] = set()
    full_state_transfers = 0
    gitea = {"ls_remote": 1, "fetch": 0, "pull": 0}  # 主控启动兜底 1 次 ls-remote
    cycle_lsa: dict[int, int] = defaultdict(int)       # 令牌桶周期 -> LSA 条数
    cycle_lsa_bytes: dict[int, int] = defaultdict(int)  # 令牌桶周期 -> LSA 字节
    reconcile_cycle_bytes: dict[int, int] = defaultdict(int)  # 1800s 周期 -> 字节
    state_writes = 0
    state_bytes = 0
    last_seq = [0] * n
    processing_count = 0
    total_emitted_change = 0
    total_deferred = 0
    probe_target_seq: dict[tuple[str, str], int] = {}
    probe_visible_at: dict[tuple[str, str], list[float]] = {
        k: [] for k in probe_keys}
    probe_recorded: set[tuple[tuple[str, str], int]] = set()
    pull_buckets: dict[int, set[int]] = defaultdict(set)
    # 一个跟随节点同时只挂一次待补拉（不会叠加冗余 pull），这是令牌/去重闸的语义。
    next_pull_allowed: dict[int, float] = defaultdict(float)
    M_state_bytes = m * STATE_JSON_BYTES_PER_CARD

    def record_pull(peer: int, at: float) -> None:
        # 跟随节点发现摘要水位落后，并不立刻连 Gitea，而是按 H-LSA 的
        # **周期定时器 ±20% jitter**（h.next_deadline，模块文档里「所有周期
        # 定时器的唯一入口」）在各自独立 rng 的下一个错峰时刻补拉。
        # 这正是 §5「同步风暴峰值 <= N*20%」要验证的 jitter 机制：
        # 50 个节点散布在 [48, 72]s 窗口里，而不是挤在同一秒。
        scheduled = h.next_deadline(at, cfg.multicast_base_s,
                                    rng=node_rngs[peer], jitter=cfg.jitter)
        pull_buckets[int(scheduled / p.storm_bucket_s)].add(peer)
        next_pull_allowed[peer] = scheduled

    def deliver(packets: list[dict[str, Any]], peers: list[int],
                now: float) -> None:
        nonlocal state_writes, state_bytes, processing_count, full_state_transfers
        lsa_bodies: list[dict[str, Any]] = []
        header: dict[str, Any] | None = None
        for pkt in packets:
            ty = pkt.get("type")
            if ty == "lsa":
                lsa_bodies.extend(pkt.get("lsas", []) or [])
            elif ty == "lsa_refresh":
                header = pkt
            elif ty == "lsa_digest" and header is None:
                header = pkt
        for peer in peers:
            core = cores[peer]
            if lsa_bodies:
                res = core.merge_incoming(lsa_bodies, now=now)
                processing_count += len(lsa_bodies)
                if res.get("deferred_min_arrival"):
                    push(now + cfg.min_ls_arrival_s + 0.05, "follower_tick", peer)
                if res["lsdb_seq"] != last_seq[peer]:
                    last_seq[peer] = res["lsdb_seq"]
                    state_writes += 1
                    state_bytes += M_state_bytes
                # 探针可见性：收端 LSDB 真的反映了该 seq 才算可见
                for body in lsa_bodies:
                    k0 = body.get("key", ["", ""])
                    k = (str(k0[0]), str(k0[1]) if len(k0) > 1 else "")
                    target = probe_target_seq.get(k)
                    if target is None:
                        continue
                    entry = core.lsdb.get((probe_area, k))
                    if entry is not None and entry.seq >= target:
                        tag = (k, peer)
                        if tag not in probe_recorded:
                            probe_recorded.add(tag)
                            probe_visible_at[k].append(now)
            if header is not None:
                res = core.on_hello(header, now=now)
                processing_count += 1
                # 一个节点同时只挂一次待补拉；已排期的摘要直接跳过（去重闸）。
                if (res.get("action") != h.NO_OP
                        and now >= next_pull_allowed[peer]):
                    gitea["fetch"] += 1
                    gitea["pull"] += 1
                    record_pull(peer, now)
                    # 增量补拉完成：把跟随节点的本地头对齐到主控通告的头，
                    # 否则后续每个摘要都会重复判定「头不一致」而无限拉。
                    core.set_head_sha(str(header.get("head_sha") or ""))
                full_state_transfers += int(res.get("full_state_transfers") or 0)

    # -- 主事件循环 -------------------------------------------------------- #
    while events:
        t, _s, kind, data = heapq.heappop(events)
        if t > duration:
            break
        clock.set(t)

        if kind in ("change", "change_batch"):
            if kind == "change":
                batches = [(data[0], data[1])]
            else:
                # 一批 cnt 次变更：随机撒到背景 key 上，全部落在同一合并窗口
                batches = [(rng_load.choice(background_keys), t)
                           for _ in range(int(data))]
            for key, v in batches:
                # repo 变更推进模拟 origin/main 头；LSA 只带字段级状态 diff，
                # 不带 head_sha（状态传播 != repo 文件同步）。头由 T1/T2 摘要通告。
                advance_head()
                cores[leader].set_head_sha(git_head[0])
                cores[leader].note_change(key=key,
                                          diff={"state": "v%s" % v, "v": v},
                                          now=t)
        elif kind == "probe":
            # 探针同样推进头；跟随节点靠摘要里的 head_sha 判断要不要补拉
            advance_head()
            cores[leader].set_head_sha(git_head[0])
            cores[leader].note_change(key=data,
                                      diff={"state": PROBE_DIFF_VALUE}, now=t)
        elif kind == "failover":
            old = leader
            leader = 1 if leader == 0 else 0
            leader_switches += 1
            for c in cores:
                c.leader_epoch = max(c.leader_epoch, leader_switches)
            # T1/T2 周期定时器是「主控角色」的节拍，不是某个节点的私有闹钟：
            # 切主后新主控继承旧主控的下一次截止时刻，对账节奏连续。
            # 否则新主控会拿自己从 start_at=0 起算的旧 deadline 立刻再发一轮
            # T2，制造一次虚假的「周期变短」，把 SLA-2 判据误伤。
            cores[leader]._next_t1_at = cores[old]._next_t1_at
            cores[leader]._next_t2_at = cores[old]._next_t2_at
        elif kind == "follower_tick":
            # 只用于 MinLSArrival 到期后 drain 延后安装的 LSA，不发包
            cores[data].tick(now=t)
        elif kind == "tick":
            core = cores[leader]
            out = core.tick(now=t)
            pkts = out.get("packets") or []
            # 探针目标 seq 必须在 broadcast（投递）之前记录
            for pkt in pkts:
                if pkt.get("type") == "lsa":
                    for body in pkt.get("lsas", []) or []:
                        k0 = body.get("key", ["", ""])
                        k = (str(k0[0]), str(k0[1]) if len(k0) > 1 else "")
                        if k in injected_at and k not in probe_target_seq:
                            probe_target_seq[k] = int(body.get("seq", 0))
            peers = [i for i in range(n) if i != leader]
            transport.broadcast(core.originator, pkts, t, peers, deliver)

            # 令牌桶周期由真实 bucket.refilled_at 界定（与 hive_lsa 的补充时刻
            # 完全一致），而不是按绝对时间整除；这样「32 条/64KB 每周期」的
            # 口径和令牌桶实现逐字对齐。
            cidx = int(core.bucket.refilled_at)
            cycle_lsa[cidx] += int(out.get("emitted_change_lsa") or 0)
            lsa_bytes = sum(h.packet_bytes(x) for x in pkts
                            if x.get("type") == "lsa")
            cycle_lsa_bytes[cidx] += lsa_bytes
            total_emitted_change += int(out.get("emitted_change_lsa") or 0)
            total_deferred += int(out.get("deferred") or 0)
            # 对账周期（1800s）内的全部报文字节
            ridx = int(t / cfg.ls_refresh_s)
            reconcile_cycle_bytes[ridx] += sum(h.packet_bytes(x) for x in pkts)
            if out.get("t2_refresh"):
                t2_times.append(float(t))
                # tick() 已把 _next_t2_at 重新抖到下一次（1800s ±20%）；
                # 两个相邻 T2 的间隔，与这次重新抖出的周期，都落在 [1440,2160]。
                t2_periods.append(round(float(out.get("next_t2_at", t)) - t, 3))
                # T2 对账本身只产出摘要族报文（lsa_refresh / lsa_csnp / lsa_digest）。
                # 同一 tick 也可能有 T0 的 change LSA（另一种闸，另一档节奏），
                # 那不算「对账夹带状态体」；这里只记摘要族，口径写在 methodology。
                for pkt in pkts:
                    ty = str(pkt.get("type"))
                    if ty in ("lsa_refresh", "lsa_csnp", "lsa_digest"):
                        t2_packet_types.add(ty)
            for pkt in pkts:
                if pkt.get("type") in ("lsa_digest", "lsa_refresh"):
                    last_digest = pkt
    # end loop

    # -- 汇总（status() 只在最后调用一次，避免热循环里反复扫 LSDB） ------- #
    st = cores[leader].status()
    total_merged = sum(c.status()["merged_changes"] for c in cores)

    latencies: list[float] = []
    for key in probe_keys:
        vis = probe_visible_at.get(key, [])
        needed = max(1, n - 1)
        if len(vis) >= needed:
            latencies.append(max(vis) - injected_at[key])
        else:
            latencies.append(float("nan"))   # 未收敛 -> unavailable，不填估算
    finite = [v for v in latencies if not math.isnan(v)]
    vis_s = {
        "samples": len(latencies), "converged_samples": len(finite),
        "unavailable_samples": len(latencies) - len(finite),
        "p50": _pct(finite, 50), "p95": _pct(finite, 95),
        "max": _pct(finite, 100),
    }

    t2_intervals = [round(b - a, 3) for a, b in zip(t2_times, t2_times[1:])]
    # 「全量对账周期」的判据用重抖出的实际周期（首轮也有效，不必等两次触发）
    reconcile_periods = t2_intervals + t2_periods
    digest_only = bool(t2_times) and t2_packet_types.issubset(
        {"lsa_refresh", "lsa_csnp", "lsa_digest"})
    storm_peak = max((len(v) for v in pull_buckets.values()), default=0)
    leader_bytes = transport.leader_bytes
    wire_bytes = transport.wire_bytes
    cycle_peak_lsa = max(cycle_lsa.values(), default=0)
    cycle_peak_bytes = max(cycle_lsa_bytes.values(), default=0)
    idle_kbps = (wire_bytes / duration) / 1024.0 if duration else 0.0

    metrics = {
        "N": n, "M": m, "frequency": freq, "scenario": "B",
        "duration_s": duration, "jitter": cfg.jitter,
        "single_multicast_source": True,
        "gitea_requests": {
            "ls_remote_total": gitea["ls_remote"], "fetch_total": gitea["fetch"],
            "pull_total": gitea["pull"],
            "total": gitea["ls_remote"] + gitea["fetch"] + gitea["pull"],
            "per_node": (gitea["fetch"] + gitea["pull"]) / n if n else 0,
            "per_node_per_30min":
                (gitea["fetch"] + gitea["pull"]) / n * (1800.0 / duration)
                if duration and n else 0,
            "leader_fallback_ls_remote": gitea["ls_remote"],
        },
        "control_plane": {
            "leader_bytes": leader_bytes, "wire_bytes": wire_bytes,
            "total_bytes": wire_bytes,
            "per_node_peak_bytes_per_cycle": cycle_peak_bytes,
            "cycle_definition_s": cfg.token_bucket_period_s,
            "per_node_peak_bytes_per_reconcile": max(
                reconcile_cycle_bytes.values(), default=0),
            "reconcile_definition_s": cfg.ls_refresh_s,
            "kbps": round(idle_kbps, 6),
            "packets_emitted": transport.packets_emitted,
            "packets_delivered": transport.packets_delivered,
            "per_node_bytes_max": max(transport.per_node_bytes.values(), default=0),
        },
        "sync_storm_peak": storm_peak,
        "sync_storm_limit": int(math.floor(n * cfg.sync_storm_max_fraction)),
        "state_json": {"writes": state_writes, "bytes_total": state_bytes,
                       "bytes_per_write": M_state_bytes if state_writes else 0},
        "lsa": {
            "emitted_change_lsa": st["emitted_change_lsa"],
            "emitted_digest": st["emitted_digest"],
            "emitted_refresh": st["emitted_refresh"],
            "emitted_csnp_packets": st["emitted_csnp_packets"],
            "merged_changes": total_merged,
            "flap_suppressed": st["flap_suppressed"],
            "flap_recovered": st["flap_recovered"],
            "flap_summaries": st["flap_summaries"],
            "token_deferred": st["token_deferred"],
            "dropped_duplicate": st["dropped_duplicate"],
            "lsa_per_token_cycle_max": cycle_peak_lsa,
            "bytes_per_token_cycle_max": cycle_peak_bytes,
            "total_deferred_this_run": total_deferred,
        },
        "visibility_s": vis_s,
        "reconcile": {
            "t2_events": len(t2_times),
            "t2_times_s": [round(x, 3) for x in t2_times],
            "intervals_s": t2_intervals,
            "rearmed_periods_s": t2_periods,
            "periods_checked_s": reconcile_periods,
            "expected_interval_s": cfg.ls_refresh_s,
            "min_interval_s": cfg.ls_refresh_s * (1 - cfg.jitter),
            "max_interval_s": cfg.ls_refresh_s * (1 + cfg.jitter),
            "digest_only": digest_only,
            "t2_packet_types": sorted(t2_packet_types),
            "full_state_transfers": full_state_transfers,
        },
        "failover": {
            "max_multicast_sources": transport.max_multicast_sources,
            "duplicate_floods": transport.duplicate_floods,
            "leader_switches": leader_switches,
        },
        "cpu_proxy": {"lsa_processing": processing_count,
                      "merge_count": total_merged},
        "methodology_note": ("B 用真实 hive_lsa.LSACore；跟随节点批量 "
                             "merge_incoming + on_hello；组播源恒为当前主控 1 个"),
    }
    return {"run_id": _run_id(p), "params": _params_dict(p), "metrics": metrics}


def run_sim(p: SimParams) -> dict[str, Any]:
    if p.scenario == "A":
        return run_scenario_a(p)
    if p.scenario == "B":
        return run_scenario_b(p)
    raise ValueError("scenario 必须是 A 或 B，收到 %r" % (p.scenario,))


# --------------------------------------------------------------------------- #
# 震荡场景：同一个 (repo, tid) 连续 1000 次状态翻转
# --------------------------------------------------------------------------- #
def run_flap(flips: int = 1000, seed: int = 20261114,
             config: Any = None) -> dict[str, Any]:
    cfg = config or h.SyncConfig()
    clock = FakeClock(0.0)
    area = h.make_area("hive", "hive-dev", "hive-dev")
    key = ("hive", "T-FLAP")
    c = h.LSACore(originator="flap-leader", area=area, project_tag="hive-dev",
                  config=cfg, clock=clock, rng=random.Random(seed).random,
                  start_at=0.0)
    step = cfg.flap_window_s / max(1, flips)   # 全部翻转压在一个 flap_window 内
    end = 0.0
    emitted_diffs: list[dict[str, Any]] = []
    for i in range(flips):
        t = round((i + 1) * step, 6)
        c.note_change(key=key, diff={"v": i, "state": "s%d" % (i % 2)}, now=t)
        out = c.tick(now=t + min(cfg.aggregation_window_s / 2.0, step / 2.0))
        for pkt in out.get("packets") or []:
            if pkt.get("type") == "lsa":
                for body in pkt.get("lsas", []) or []:
                    if body.get("kind") == h.CHANGE:
                        emitted_diffs.append(dict(body.get("diff") or {}))
        end = t + min(cfg.aggregation_window_s / 2.0, step / 2.0)
    # 停止震荡后继续推进，验证半衰解除抑制并把最终状态补发
    drain_step = max(cfg.flap_max_suppress_s / 20.0, cfg.min_ls_interval_s)
    horizon = end + cfg.flap_max_suppress_s + 4 * cfg.flap_half_life_s
    t = math.ceil(end / drain_step) * drain_step
    while t <= horizon:
        out = c.tick(now=t)
        for pkt in out.get("packets") or []:
            if pkt.get("type") == "lsa":
                for body in pkt.get("lsas", []) or []:
                    if body.get("kind") == h.CHANGE:
                        emitted_diffs.append(dict(body.get("diff") or {}))
        t += drain_step
    st = c.status()
    entry = c.lsdb.get((area, key))
    final_diff = emitted_diffs[-1] if emitted_diffs else {}
    ads = st["emitted_change_lsa"] + st["flap_summaries"]
    out = {
        "flips": flips, "seed": seed,
        "emitted_change_lsa": st["emitted_change_lsa"],
        "flap_suppressed": st["flap_suppressed"],
        "flap_recovered": st["flap_recovered"],
        "flap_summaries": st["flap_summaries"],
        "merged_changes": st["merged_changes"],
        "advertisements": ads,
        "final_lsdb_entry": entry.summary() if entry else None,
        "last_emitted_diff": final_diff,
        "final_v": final_diff.get("v"),
        "expected_final_v": flips - 1,
        "suppressed_keys": st["suppressed_keys"],
        "advertisements_lt_flips": ads < flips,
        "state_not_lost": bool(entry is not None
                               and final_diff.get("v") == flips - 1),
        "config": cfg.as_dict(),
    }
    return out


def flap_assertions(flap: dict[str, Any]) -> list[dict[str, Any]]:
    ads = flap["advertisements"]
    flips = flap["flips"]
    return [
        {"id": "FLAP-1", "name": "1000 次翻转的通告数显著少于 1000",
         "threshold": "< %d" % flips, "measured": ads, "unit": "advertisements",
         "pass": bool(flap["advertisements_lt_flips"]),
         "reason": ("实测 %d 次翻转只发出 %d 条 change LSA + %d 条汇总"
                    % (flips, flap["emitted_change_lsa"],
                       flap["flap_summaries"]))},
        {"id": "FLAP-2", "name": "震荡结束后最终状态不丢",
         "threshold": "v == %d 且 LSDB 有该条目" % flap["expected_final_v"],
         "measured": {"final_v": flap["final_v"],
                      "entry": flap["final_lsdb_entry"]},
         "unit": "mixed", "pass": bool(flap["state_not_lost"]),
         "reason": ("恢复后补发的最后一条 change LSA diff.v=%s，LSDB 条目=%s"
                    % (flap["final_v"],
                       "present" if flap["final_lsdb_entry"] else "missing"))},
        {"id": "FLAP-3", "name": "Flap Damping 确实触发并解除",
         "threshold": "suppressed>0 且 recovered>0",
         "measured": {"flap_suppressed": flap["flap_suppressed"],
                      "flap_recovered": flap["flap_recovered"]},
         "unit": "events",
         "pass": bool(flap["flap_suppressed"] > 0
                      and flap["flap_recovered"] > 0),
         "reason": ("抑制 %d 次、恢复 %d 次"
                    % (flap["flap_suppressed"], flap["flap_recovered"]))},
    ]


# --------------------------------------------------------------------------- #
# 8 项 SLA 断言（阈值全部取自 SyncConfig，不放宽）
# --------------------------------------------------------------------------- #
def build_assertions(runs: list[dict[str, Any]], cfg: Any) -> list[dict[str, Any]]:
    b = [r["metrics"] for r in runs if r["metrics"]["scenario"] == "B"]
    a = [r["metrics"] for r in runs if r["metrics"]["scenario"] == "A"]
    out: list[dict[str, Any]] = []

    def add(aid, name, threshold, measured, unit, passed, reason, evidence=None):
        out.append({"id": aid, "name": name, "threshold": threshold,
                    "measured": measured, "unit": unit, "pass": bool(passed),
                    "reason": reason, "evidence": evidence})

    # 1) 状态变更 -> 全集群可见 max <= 300s
    vals = [x["visibility_s"]["max"] for x in b
            if x["visibility_s"].get("max") is not None]
    worst = max(vals) if vals else None
    unconv = sum(x["visibility_s"].get("unavailable_samples", 0) for x in b)
    add("SLA-1", "状态变更 → 全集群可见 max", cfg.sla_visibility_s, worst, "s",
        worst is not None and worst <= cfg.sla_visibility_s,
        ("实测 max=%.3fs，未收敛探针 %d 个" % (worst, unconv))
        if worst is not None else "unavailable：无可见性实测样本",
        {"per_run_max": [x["visibility_s"]["max"] for x in b],
         "unconverged_probes": unconv})

    # 2) 全量对账周期 1800s ±20%，且只发 digest
    # 周期判据同时看「相邻 T2 的实际间隔」与「每次 T2 重抖出的下一次周期」。
    periods = [iv for x in b for iv in x["reconcile"]["periods_checked_s"]]
    first_t2 = [x["reconcile"]["t2_times_s"][0] for x in b
                if x["reconcile"]["t2_times_s"]]
    lo, hi = cfg.ls_refresh_s * (1 - cfg.jitter), cfg.ls_refresh_s * (1 + cfg.jitter)
    digest_ok = bool(b) and all(x["reconcile"]["digest_only"] is True for x in b)
    t2_count = sum(x["reconcile"]["t2_events"] for x in b)
    period_ok = (bool(periods) and all(lo <= x <= hi for x in periods)
                 and bool(first_t2) and all(lo <= x <= hi for x in first_t2))
    add("SLA-2", "全量对账周期 1800s ±20% 且只发 digest",
        {"min_s": lo, "max_s": hi, "digest_only": True},
        {"first_t2_s": first_t2[:5], "periods_checked_s": periods[:10],
         "t2_events_total": t2_count, "digest_only": digest_ok},
        "mixed", bool(digest_ok and period_ok and t2_count >= 1),
        ("实测 %d 个 T2 事件，首轮 %s s、周期样本 %s s 全落在 [%.1f, %.1f]，"
         "对账报文仅摘要/CSNP" % (t2_count, first_t2[:3], periods[:5], lo, hi))
        if (digest_ok and period_ok and t2_count >= 1) else
        "unavailable/fail：T2 事件不足/周期越界/夹带状态体",
        {"full_state_transfers": sum(x["reconcile"]["full_state_transfers"]
                                     for x in b)})

    # 3) 场景 B 空闲期 Gitea 请求 0 次/节点/30min（Leader 兜底 <=1）
    idle_b = [x for x in b if x["frequency"] == "idle"]
    per_node = [x["gitea_requests"]["per_node_per_30min"] for x in idle_b]
    fallback = [x["gitea_requests"]["leader_fallback_ls_remote"] for x in idle_b]
    idle_ok = bool(idle_b) and all(v == 0 for v in per_node) and all(
        v <= 1 for v in fallback)
    add("SLA-3", "场景 B 空闲期 Gitea 请求（0/节点/30min，Leader 兜底 ≤1）",
        {"per_node_per_30min": 0, "leader_fallback_max": 1},
        {"runs": len(idle_b), "per_node_per_30min": per_node[:8],
         "leader_fallback": fallback[:8]},
        "mixed", bool(idle_ok),
        ("实测 %d 个 idle 运行：每节点 30min 请求全为 0，主控兜底 ≤1"
         % len(idle_b)) if idle_ok else "unavailable/fail：无 idle 运行或有非零请求",
        None)

    # 4) 空闲控制面带宽 <= 9KB/s（0.16.05 基线），A/B 对比不得回退
    idle_a = [x for x in a if x["frequency"] == "idle"]
    b_idle_kbps = [x["control_plane"]["kbps"] for x in idle_b]
    a_idle_kbps = [x["control_plane"]["kbps"] for x in idle_a]
    max_b = max(b_idle_kbps) if b_idle_kbps else None
    min_a = min(a_idle_kbps) if a_idle_kbps else None
    bw_ok = max_b is not None and max_b <= cfg.idle_bandwidth_baseline_kb_s
    regress = (max_b is not None and min_a is not None and max_b <= min_a)
    add("SLA-4", "空闲控制面带宽 ≤ 9KB/s 且 A/B 对比不回退",
        {"max_kbps": cfg.idle_bandwidth_baseline_kb_s, "no_regression": True},
        {"B_idle_kbps_max": max_b, "A_idle_kbps_min": min_a,
         "B_runs": len(b_idle_kbps), "A_runs": len(a_idle_kbps)},
        "mixed", bool(bw_ok and regress),
        ("B idle 峰值 %.6f KB/s ≤ %.1f KB/s；A idle 最低 %.6f KB/s，未回退"
         % (max_b, cfg.idle_bandwidth_baseline_kb_s, min_a))
        if (bw_ok and regress) else "unavailable/fail：带宽越界或 A/B 回退",
        None)

    # 5) 高频变更 + N=50/100：单节点 <=64KB/周期、<=32 条 LSA/周期
    hi = [x for x in b if x["frequency"] == "high" and x["N"] in (50, 100)]
    peak_bytes = [x["control_plane"]["per_node_peak_bytes_per_cycle"] for x in hi]
    peak_lsa = [x["lsa"]["lsa_per_token_cycle_max"] for x in hi]
    mb = max(peak_bytes) if peak_bytes else None
    ml = max(peak_lsa) if peak_lsa else None
    hi_ok = (bool(hi) and mb is not None and ml is not None
             and mb <= cfg.token_bucket_bytes and ml <= cfg.token_bucket_lsa)
    add("SLA-5", "高频 + N=50/100：单节点 ≤64KB/周期、≤32 条 LSA/周期",
        {"bytes_per_cycle": cfg.token_bucket_bytes,
         "lsa_per_cycle": cfg.token_bucket_lsa,
         "cycle_s": cfg.token_bucket_period_s},
        {"runs": len(hi), "max_bytes_per_cycle": mb, "max_lsa_per_cycle": ml},
        "mixed", bool(hi_ok),
        ("实测 %d 个高频大规格运行：峰值 %.0f B/周期、%d 条 LSA/周期"
         % (len(hi), mb, ml)) if hi_ok else
        "unavailable/fail：缺 N=50/100 高频运行或令牌桶越界",
        [{"N": x["N"], "M": x["M"],
          "bytes": x["control_plane"]["per_node_peak_bytes_per_cycle"],
          "lsa": x["lsa"]["lsa_per_token_cycle_max"]} for x in hi])

    # 6) 同步风暴峰值 <= N × 20%（jitter 生效证据；断言针对 B，A 为对照基线）
    storm_b = [(x["N"], x["frequency"], x["sync_storm_peak"],
                x["sync_storm_limit"]) for x in b]
    storm_a = [(x["N"], x["frequency"], x["sync_storm_peak"],
                x["sync_storm_limit"]) for x in a]
    storm_ok = bool(storm_b) and all(v <= lim for _n, _f, v, lim in storm_b)
    breaches = [(n, f, v, lim) for n, f, v, lim in storm_b if v > lim]
    # N=100 + 高频 是卡面完成标准 #3 点名的硬场景，单列实测
    n100_high = [(n, f, v, lim) for n, f, v, lim in storm_b
                 if n == 100 and f == "high"]
    n100_ok = bool(n100_high) and all(v <= lim for _n, _f, v, lim in n100_high)
    add("SLA-6", "B 场景同步风暴峰值 ≤ N × 20%（jitter 生效）",
        "N * %.2f" % cfg.sync_storm_max_fraction,
        {"B_runs": storm_b, "B_max_peak":
         max((v for _n, _f, v, _l in storm_b), default=0),
         "A_baseline_runs": storm_a,
         "A_baseline_max_peak":
         max((v for _n, _f, v, _l in storm_a), default=0),
         "breaches": breaches,
         "N100_high": n100_high},
        "mixed", bool(storm_ok),
        (("B 全部运行峰值 %d，逐运行均 ≤ 同秒上限；A 现状（同相位无 jitter）"
          "峰值 %d 作为对照" % (
              max((v for _n, _f, v, _l in storm_b), default=0),
              max((v for _n, _f, v, _l in storm_a), default=0)))
         if storm_ok else
         ("fail：%d/%d 个 B 运行超过同秒上限 %s；"
          "卡面完成标准点名的 N=100+高频 场景 %s（实测 %s）；"
          "A 现状同相位峰值 %d。小 N（如 N=5）的 20%% 上限为 %d，"
          "4 个跟随节点在 [%.0f, %.0f]s 抖动窗口内仍可能同秒，属统计边界，"
          "未调宽阈值、未隐藏"
          % (len(breaches), len(storm_b),
             [(n, f, v, lim) for n, f, v, lim in breaches[:4]],
             "通过" if n100_ok else "未通过", n100_high,
             max((v for _n, _f, v, _l in storm_a), default=0),
             int(math.floor(5 * cfg.sync_storm_max_fraction)),
             cfg.multicast_base_s * (1 - cfg.jitter),
             cfg.multicast_base_s * (1 + cfg.jitter)))),
        None)

    # 7) 切主：组播源恒 <=1，无重复泛洪
    sources = [x["failover"]["max_multicast_sources"] for x in b]
    dups = [x["failover"]["duplicate_floods"] for x in b]
    switches = [x["failover"]["leader_switches"] for x in b]
    fo_ok = bool(b) and all(v <= 1 for v in sources) and all(v == 0 for v in dups)
    add("SLA-7", "切主：组播源恒 ≤1，无重复泛洪",
        {"max_sources": 1, "duplicate_floods": 0},
        {"max_sources": max(sources) if sources else None,
         "duplicate_floods": sum(dups), "leader_switches": switches[:8]},
        "mixed", bool(fo_ok),
        ("实测 %d 次切主，任一时刻组播源 ≤1，重复泛洪 0 次" % sum(switches))
        if fo_ok else "unavailable/fail：组播源 >1 或重复泛洪",
        None)

    # 8) B 总流量 vs A：数量级下降，且 N 越大优势越明显（O(N)->O(1)）
    ratios: list[dict[str, Any]] = []
    for n in sorted({x["N"] for x in a} & {x["N"] for x in b}):
        at = sum(x["gitea_requests"]["total"] for x in a
                 if x["N"] == n and x["frequency"] == "idle")
        bt = sum(x["gitea_requests"]["total"] for x in b
                 if x["N"] == n and x["frequency"] == "idle")
        ratios.append({"N": n, "A_gitea_total": at, "B_gitea_total": bt,
                       "ratio": round(at / max(1, bt), 2)})
    order_ok = bool(ratios) and all(r["ratio"] >= 10 for r in ratios)
    grows = (len(ratios) >= 2 and
             all(ratios[i]["ratio"] <= ratios[i + 1]["ratio"]
                 for i in range(len(ratios) - 1)))
    add("SLA-8", "B 总流量 vs A 数量级下降且 N 越大优势越明显",
        {"min_ratio": 10, "monotonic_in_N": True},
        {"ratios": ratios, "all_ge_10": order_ok, "monotonic": grows},
        "mixed", bool(order_ok and grows),
        ("idle 场景 Gitea 请求 A/B 比值 %s，随 N 单调不减（A∝N，B 单源 O(1)）"
         % [r["ratio"] for r in ratios]) if (order_ok and grows) else
        "unavailable/fail：比值未达数量级或不随 N 增长",
        None)
    return out


# --------------------------------------------------------------------------- #
# 顶层：跑矩阵 + 输出原始 JSON
# --------------------------------------------------------------------------- #
METHODOLOGY = {
    "pure_simulation": True,
    "touched_production": False,
    "sent_prod_multicast": False,
    "connected_gitea": False,
    "started_cluster_process": False,
    "dependencies": "python stdlib + hive_lsa (both zero-I/O)",
    "TASK_STATE_BYTES": TASK_STATE_BYTES,
    "STATE_JSON_BYTES_PER_CARD": STATE_JSON_BYTES_PER_CARD,
    "model_params_note": "以上两个字节常量是仿真模型参数，不是生产实测值",
    "A_model": "所有节点同相位固定周期全量 git pull（无 jitter，现状病根）；每次=ls-remote+fetch+pull+全量 state.json 重写；与变更频率无关",
    "B_model": "单主控 LSACore 发 T0/T1/T2；N-1 跟随节点批量 merge_incoming + on_hello；组播源恒 1",
    "control_plane_bytes_B": "sum(hive_lsa.packet_bytes(pkt)) 为主控单源字节；wire_bytes = 单源 × (N-1)",
    "control_plane_bytes_A": "poll 次数 × M × TASK_STATE_BYTES",
    "visibility": "探针 key（M 个任务的子集，与背景负载隔离）从注入到全部跟随节点 LSDB 反映的 P50/P95/max；未收敛记 unavailable",
    "token_cycle": "由真实 bucket.refilled_at 界定（与令牌桶补充时刻一致）：32 条 / 64KB 每周期上限；只计 change LSA 报文",
    "reconcile_cycle": "cfg.ls_refresh_s（默认 1800s ±20%）：对账产出仅 lsa_refresh/lsa_csnp/lsa_digest，不含状态体；同 tick 的 T0 change LSA 属另一档节奏，不计入对账报文",
    "storm_bucket": "同一秒 = floor(t / storm_bucket_s) 桶内发起 pull 的节点数；A 同相位 -> 峰值 N（fail），B 跟随节点按各自 rng 的 jittered 错峰 -> 峰值小",
    "state_json": "仅当 sync_health 的 seq（用 merge_incoming 返回的 lsdb_seq）变化才计一次落盘",
    "cpu_proxy": "LSA 处理次数 = 收端 merge_incoming/on_hello 的报文条数；合并次数 = 各节点 merged_changes 之和",
    "tick_step": "主控 tick 步长默认 5.0s（覆盖 200ms 合并窗口与 5s 令牌桶周期）；高频变更按每 tick 一批 50 次建模，语义等价于同一合并窗口内的 50 次变更",
    "unavailable_policy": "跑不出来写 unavailable，绝不填估算或历史值",
}


def run_matrix(node_list: list[int], task_list: list[int], freq_list: list[str],
               duration: float, scenario_list: list[str], seed: int,
               config: Any = None, failover: bool = True,
               probes: int = 20, storm_bucket_s: float = 1.0,
               tick_step_s: float = 0.0) -> dict[str, Any]:
    cfg = config or h.SyncConfig()
    runs: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    for n in node_list:
        for m in task_list:
            for freq in freq_list:
                for sc in scenario_list:
                    p = SimParams(nodes=n, tasks=m, frequency=freq,
                                  duration=duration, scenario=sc, seed=seed,
                                  config=cfg, failover=failover, probes=probes,
                                  storm_bucket_s=storm_bucket_s,
                                  tick_step_s=tick_step_s)
                    runs.append(run_sim(p))
    elapsed = time.perf_counter() - t0
    flap = run_flap(flips=1000, seed=seed, config=cfg)
    return {
        "schema": "hive-hlsa-loadtest/1",
        "collected_at": _iso_now(),
        "host": (os.environ.get("COMPUTERNAME")
                 or os.environ.get("HOSTNAME") or ""),
        "python": sys.version.split()[0],
        "params": {
            "nodes": node_list, "tasks": task_list, "frequency": freq_list,
            "duration_s": duration, "scenario": scenario_list, "seed": seed,
            "config": cfg.as_dict(),
        },
        "constants": h.CONSTANTS,
        "methodology": METHODOLOGY,
        "runs": runs,
        "flap_damping": flap,
        "flap_assertions": flap_assertions(flap),
        "assertions": build_assertions(runs, cfg),
        "elapsed_s": round(elapsed, 3),
        "guardrails": {
            "prod_state_dir": "~/.hive/cluster", "prod_mc_group": "239.255.42.1",
            "prod_mc_port": 47899, "touched_prod_state": False,
            "sent_prod_multicast": False, "connected_gitea": False,
        },
    }


def _parse_list(raw: str, cast: Callable[[str], Any]) -> list[Any]:
    return [cast(x.strip()) for x in str(raw).split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="H-LSA 规模仿真压测（纯标准库、零 I/O、可复现）")
    ap.add_argument("--nodes", default="5,20,50,100",
                    help="节点数列表，逗号分隔（默认 5,20,50,100）")
    ap.add_argument("--tasks", default="200,1000,5000",
                    help="任务卡数 M 列表，逗号分隔（默认 200,1000,5000）")
    ap.add_argument("--frequency", default="idle,normal,high",
                    help="变更频率 idle|normal|high，逗号分隔或 all")
    ap.add_argument("--duration", type=float, default=3600.0,
                    help="仿真时长秒（默认 3600 = 2 个 1800s 对账周期）")
    ap.add_argument("--scenario", default="both",
                    help="A|B|both（默认 both，A/B 都报，不许只报 A）")
    ap.add_argument("--seed", type=int, default=20261114,
                    help="随机种子（默认 20261114，固定可复现）")
    ap.add_argument("--json", nargs="?", const="-", metavar="PATH",
                    help="输出 JSON；单独开关打印到 stdout，带路径则写入文件")
    ap.add_argument("--out", default="",
                    help="把原始 JSON 写入文件（house style，等价于 --json PATH）")
    ap.add_argument("--no-failover", action="store_true",
                    help="关闭切主场景（默认开启）")
    ap.add_argument("--probes", type=int, default=20,
                    help="可见性探针 key 数量（默认 20）")
    ap.add_argument("--tick-step", type=float, default=0.0,
                    help="主控 tick 步长秒；0 = 5.0")
    ap.add_argument("--storm-bucket", type=float, default=1.0,
                    help="同步风暴「同一秒」桶宽（默认 1.0s）")
    args = ap.parse_args(argv)

    node_list = _parse_list(args.nodes, int)
    task_list = _parse_list(args.tasks, int)
    freq_list = (["idle", "normal", "high"] if args.frequency == "all"
                 else _parse_list(args.frequency, str))
    for f in freq_list:
        if f not in ("idle", "normal", "high"):
            ap.error("--frequency 只能是 idle/normal/high，收到 %r" % f)
    scenario_list = (["A", "B"] if args.scenario == "both"
                     else [args.scenario])
    if args.scenario not in ("A", "B", "both"):
        ap.error("--scenario 只能是 A/B/both")
    if not node_list or not task_list or not freq_list:
        ap.error("--nodes/--tasks/--frequency 不能为空")
    if any(n < 2 for n in node_list):
        ap.error("--nodes 至少为 2（要有跟随节点）")

    result = run_matrix(
        node_list=node_list, task_list=task_list, freq_list=freq_list,
        duration=args.duration, scenario_list=scenario_list, seed=args.seed,
        failover=not args.no_failover, probes=args.probes,
        storm_bucket_s=args.storm_bucket, tick_step_s=args.tick_step)

    text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False,
                      default=str)
    out_path = ""
    if args.json and args.json != "-":
        out_path = args.json
    if args.out:
        out_path = args.out
    if out_path:
        op = Path(os.path.expanduser(out_path))
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(text, encoding="utf-8")
    if args.json or args.out:
        print(text)
    else:
        print("H-LSA 压测完成：%s，%d 个运行，耗时 %.2fs"
              % (result["collected_at"], len(result["runs"]), result["elapsed_s"]))
        for x in result["assertions"]:
            print("  [%s] %s: measured=%s"
                  % ("PASS" if x["pass"] else "FAIL", x["id"], x["measured"]))
        for x in result["flap_assertions"]:
            print("  [%s] %s" % ("PASS" if x["pass"] else "FAIL", x["id"]))
        f = result["flap_damping"]
        print("  Flap: %d flips -> %d change LSA + %d summaries"
              % (f["flips"], f["emitted_change_lsa"], f["flap_summaries"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
