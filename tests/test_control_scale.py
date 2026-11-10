"""T-0315：控制面规模基准（1 万条目 / 1400B 分包 / 上报 <1ms）。

对应母文档 §1.1（20 节点 / 100 Agent / 1 万任务，控制面流量正比于变化率）
与 ``docs/控制面v0.5-实现约定.md`` §5（``Limits``）、§7（测试与验收约定）。

原则：

- 只做基准守卫，不改实现；阈值全部留足余量，取「最好一轮」降低负载噪声；
- 不依赖真实组播/端口/时钟，不 ``sleep``；
- Windows 上按任务卡把上报预算放宽到 2ms（定时器粒度与杀软扫描差异）。
"""

from __future__ import annotations

import sys
import time
import tracemalloc

from hive_control.config import Limits, dynamic_multicast_interval
from hive_control.downlink import DownlinkSender, pack
from hive_control.model import StateEntry
from hive_control.uplink_queue import Aggregator, UplinkQueue
from hive_control.view import DownlinkQueue, StateView

# 母文档 §1.1 的规模：20 节点 / 100 Agent / 1 万任务。
ENTRIES = 10_000
PROJECTS = 20
PER_PROJECT = ENTRIES // PROJECTS          # 500
CHANGED = 2_000                             # 每 project 前 100 条改版本
CHANGED_PER_PROJECT = CHANGED // PROJECTS   # 100
ROUNDS = 3                                  # 取最好一轮，压掉调度抖动

IS_WINDOWS = sys.platform.startswith("win")

# 任务卡给定预算；Windows 只放宽上报延迟一项。
BUDGET_INSTALL = 5.0
BUDGET_CHANGED = 1.0
BUDGET_PACK = 2.0
BUDGET_SUBMIT = 0.002 if IS_WINDOWS else 0.001
BUDGET_MEMORY = 100 * 1024 * 1024


def entry(project: str, tid: str, *, version: int = 1,
          state: str = "steady") -> StateEntry:
    return StateEntry(
        project=project,
        tid=tid,
        version=version,
        incarnation=1,
        owner_agent="agent-01",
        state=state,
        dna=False,
        updated_at=1_700_000_000_000,
    )


def project_name(index: int) -> str:
    return f"proj{index:02d}"


def tid_name(index: int) -> str:
    return f"T-{index:05d}"


def build_entries(count: int = ENTRIES) -> list[StateEntry]:
    """生成 ``count`` 条互不相同的 ``(project, tid)``，均匀分到 20 个 project。"""
    return [
        entry(project_name(i // PER_PROJECT), tid_name(i % PER_PROJECT))
        for i in range(count)
    ]


def build_view() -> StateView:
    """装满 1 万条，然后把 2,000 条各推进一个版本（变化率 ≠ 0）。"""
    view = StateView(Limits())
    for item in build_entries():
        view.install(item)
    for index in range(PROJECTS):
        project = project_name(index)
        for serial in range(CHANGED_PER_PROJECT):
            view.install(entry(project, tid_name(serial), version=2))
    return view


def best_of(rounds: int, work) -> float:
    """跑 ``rounds`` 轮取最短耗时（秒），降低机器负载带来的假失败。"""
    best = float("inf")
    for _ in range(rounds):
        started = time.perf_counter()
        work()
        best = min(best, time.perf_counter() - started)
    return best


class FakeTransport:
    """记录发送，不碰真实组播。"""

    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def send_multicast(self, data: bytes) -> None:
        self.packets.append(data)

    def send_unicast(self, addr, data: bytes) -> None:
        self.packets.append(data)

    def start_receiver(self, callback) -> None:
        raise AssertionError("规模基准不启动接收线程")

    def stop(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# StateView：安装与增量查询
# --------------------------------------------------------------------------- #

def test_view_install_10000_under_5s():
    """1 万条不同 (project, tid) 安装 < 5s，且全部计入视图。"""
    items = build_entries()

    def run() -> None:
        view = StateView(Limits())
        for item in items:
            view.install(item)

    elapsed = best_of(ROUNDS, run)
    assert elapsed < BUDGET_INSTALL, f"安装 1 万条耗时 {elapsed:.2f}s ≥ {BUDGET_INSTALL}s"

    view = StateView(Limits())
    for item in items:
        assert view.install(item).installed
    assert view.stats()["entries"] == ENTRIES
    assert view.stats()["projects"] == PROJECTS


def test_view_changed_after_2000_changes_under_1s():
    """1 万条里 2,000 条有变化，按 project 各查一次（floor 水位）< 1s。"""
    view = build_view()

    def run() -> None:
        total = 0
        for index in range(PROJECTS):
            total += len(view.changed_after(project_name(index), None, (1, 1)))
        assert total == CHANGED

    elapsed = best_of(ROUNDS, run)
    assert elapsed < BUDGET_CHANGED, f"2,000 条增量查询耗时 {elapsed:.3f}s ≥ {BUDGET_CHANGED}s"

    # 逐条水位（D3 优先路径）同样只回这 2,000 条，且同样在预算内。
    watermarks = {tid_name(serial): (1, 1) for serial in range(PER_PROJECT)}
    started = time.perf_counter()
    total = sum(
        len(view.changed_after(project_name(index), watermarks, (1, 0)))
        for index in range(PROJECTS)
    )
    elapsed = time.perf_counter() - started
    assert total == CHANGED
    assert elapsed < BUDGET_CHANGED, f"逐条水位查询耗时 {elapsed:.3f}s ≥ {BUDGET_CHANGED}s"


def test_view_changed_after_single_call_latency():
    """单次全量查询（1 万条同一 project）也要在几十毫秒量级。

    ``changed_after`` 每次调用都会对全部条目排序再按 project 过滤，
    单次成本随视图总量线性增长；这里把它钉住，防止退化成更差的实现。
    """
    view = StateView(Limits())
    for serial in range(ENTRIES):
        view.install(entry("proj00", tid_name(serial)))
    for serial in range(CHANGED):
        view.install(entry("proj00", tid_name(serial), version=2))

    started = time.perf_counter()
    rows = view.changed_after("proj00", None, (1, 1))
    elapsed = time.perf_counter() - started

    assert len(rows) == CHANGED
    assert elapsed < 0.2, f"单次 1 万条视图增量查询 {elapsed:.3f}s ≥ 0.2s"


# --------------------------------------------------------------------------- #
# 下行分包
# --------------------------------------------------------------------------- #

def test_pack_10000_entries_within_1400_bytes_and_2s():
    """每包 ≤1400B、10,000 条拆包 < 2s，且不切断任何 JSON 行（D8）。"""
    items = build_entries()
    lines = [item.to_line().encode("utf-8") + b"\n" for item in items]

    def run() -> None:
        packets, oversize = pack(items, 1400)
        assert oversize == 0
        assert packets

    elapsed = best_of(ROUNDS, run)
    assert elapsed < BUDGET_PACK, f"1 万条拆包耗时 {elapsed:.2f}s ≥ {BUDGET_PACK}s"

    packets, oversize = pack(items, 1400)
    assert oversize == 0
    assert all(len(packet) <= 1400 for packet in packets)
    # 拼回去必须与原行逐字节一致：既没截断也没重复。
    assert b"".join(packets) == b"".join(lines)
    # 1400B 的量级：每包至少几百字，不能一条一包。
    assert len(packets) < len(items) // 4


def test_pack_uses_limits_max_packet_bytes():
    """``DownlinkSender`` 走 ``Limits.max_packet_bytes``（默认 1400）。"""
    limits = Limits()
    assert limits.max_packet_bytes == 1400

    transport = FakeTransport()
    sender = DownlinkSender(DownlinkQueue(), limits=limits, transport=transport)
    for item in build_entries():
        sender._queue.put(item)
    sender.send_periodic()
    assert transport.packets
    assert max(len(packet) for packet in transport.packets) <= limits.max_packet_bytes


# --------------------------------------------------------------------------- #
# 上行：队列与聚合器的上报路径（I6 目标 <1ms）
# --------------------------------------------------------------------------- #

def test_uplink_queue_submit_average_under_1ms():
    """10,000 次 ``put`` 平均 <1ms：默认容量（走淘汰分支）与大容量两条路径都测。"""
    items = build_entries()

    def overflow_run() -> None:
        queue = UplinkQueue(capacity=4096)
        for item in items:
            queue.put(item)

    elapsed = best_of(ROUNDS, overflow_run)
    assert elapsed / ENTRIES < BUDGET_SUBMIT, (
        f"队列满时单次 put 平均 {elapsed / ENTRIES * 1000:.3f}ms ≥ "
        f"{BUDGET_SUBMIT * 1000:.1f}ms"
    )

    def roomy_run() -> None:
        queue = UplinkQueue(capacity=ENTRIES * 2)
        for item in items:
            queue.put(item)

    elapsed = best_of(ROUNDS, roomy_run)
    assert elapsed / ENTRIES < BUDGET_SUBMIT, (
        f"队列未满时单次 put 平均 {elapsed / ENTRIES * 1000:.3f}ms ≥ "
        f"{BUDGET_SUBMIT * 1000:.1f}ms"
    )


def test_aggregator_submit_average_under_1ms():
    """10,000 次 ``Aggregator.submit`` 平均 <1ms，且不丢 key。"""
    items = build_entries()

    def run() -> None:
        aggregator = Aggregator(lambda batch: None, window=0.2, clock=lambda: 0.0)
        for item in items:
            aggregator.submit(item)
        assert len(aggregator._pending) == ENTRIES

    elapsed = best_of(ROUNDS, run)
    assert elapsed / ENTRIES < BUDGET_SUBMIT, (
        f"单次 submit 平均 {elapsed / ENTRIES * 1000:.3f}ms ≥ "
        f"{BUDGET_SUBMIT * 1000:.1f}ms"
    )


def test_aggregator_flush_10000_keys_under_2s():
    """1 万 key 一次 flush（排序 + 交给 sink）< 2s。"""
    items = build_entries()
    batches: list[int] = []
    aggregator = Aggregator(lambda batch: batches.append(len(batch)),
                            window=0.2, clock=lambda: 0.0)
    for item in items:
        aggregator.submit(item)

    started = time.perf_counter()
    count = aggregator.flush(force=True)
    elapsed = time.perf_counter() - started

    assert count == ENTRIES
    assert batches == [ENTRIES]
    assert elapsed < BUDGET_PACK, f"1 万 key flush 耗时 {elapsed:.2f}s ≥ {BUDGET_PACK}s"


# --------------------------------------------------------------------------- #
# 下行周期（D7）
# --------------------------------------------------------------------------- #

def test_downlink_interval_boundaries():
    """500→2.0s、2000→1.0s、2001→0.5s；与 ``config`` 同源。"""
    limits = Limits()
    sender = DownlinkSender(DownlinkQueue(), limits=limits, transport=FakeTransport())

    assert sender.interval(500) == 2.0
    assert sender.interval(2000) == 1.0
    assert sender.interval(2001) == 0.5
    # 边界另一侧与两端
    assert sender.interval(0) == 2.0
    assert sender.interval(501) == 1.0
    assert sender.interval(ENTRIES) == 0.5

    for active in (0, 499, 500, 501, 1999, 2000, 2001, ENTRIES):
        assert sender.interval(active) == dynamic_multicast_interval(active)


# --------------------------------------------------------------------------- #
# 内存粗检
# --------------------------------------------------------------------------- #

def test_view_memory_10000_entries_under_100mb():
    """1 万条视图的 Python 堆占用 < 100MB（tracemalloc，宽松上限）。"""
    items = build_entries()
    tracemalloc.start()
    try:
        view = StateView(Limits())
        for item in items:
            view.install(item)
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert view.stats()["entries"] == ENTRIES
    assert peak < BUDGET_MEMORY, f"视图峰值内存 {peak / 2**20:.1f}MB ≥ 100MB"
    assert current < BUDGET_MEMORY, f"视图常驻内存 {current / 2**20:.1f}MB ≥ 100MB"
