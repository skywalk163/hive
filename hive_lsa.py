"""H-LSA 协议核心：独立于传输层的「状态传播纪律」。

本模块只回答四个问题，且**全部在内存里回答**：

* 什么时候必须立刻发（T0 / 事件触发）；
* 什么时候只发摘要（T1 / 60s 起步的 2^n 退避、T2 / 1800s 对账）；
* 什么时候该退避、什么时候该把一堆变更合成一条；
* 周期定时器怎么避免全网同一秒点火（强制 ±20% jitter）。

设计纪律（对应卡面「不要做什么」）：

* **纯标准库**：只 import ``dataclasses / hashlib / json / math / random / time /
  collections / typing``。不 import ``hive.py`` / ``hive_cluster.py`` /
  ``hive_control`` / ``dashboard``；不开 socket、不发组播、不连 Gitea、
  不读写 ``~/.hive``、不读写任何文件、不启动线程。
* **零 I/O**：所有公开方法只改内存并返回数据。要发什么由 ``tick()`` 的返回值
  交给调用方（T-0351 组播下行 / T-0352 心跳上行 / T-0353 落盘同步），
  模块自己绝不发包。
* **摘要不夹带状态体**：``build_digest()`` / ``build_csnp()`` 产出的报文
  永远不含 ``diff`` / ``progress`` / ``leases`` / ``nodes`` / ``agents`` /
  ``tasks`` / ``full_state``。这是 0.16.03 / 0.16.05 修掉的病根。
* **没有「每 N 秒全量同步」**：T2 到点只发 digest 清单（IS-IS CSNP 式），
  永不全量重传状态；``full_state_transfers`` 恒为 0。
* **所有周期定时器强制 jitter**：T1、T2、Flap 抑制后的拉长间隔，
  一律经过 :func:`jittered`，排定时刻不会是整 60.0 / 1800.0。
* **阈值不可放宽**：常量集中在 :class:`SyncConfig`，测试逐项等值断言。

三档节奏（H-LSA-协议设计-借鉴OSPF.md §2）::

    T0  事件触发    note_change() -> 200ms 合并窗口 -> 立刻产出 change LSA
    T1  组播摘要    60s 起步，无变更按 2^n 退避到 1800s；一有变更立刻回 60s
    T2  全量对账    1800s ±20% jitter，只发 digest/CSNP，不发状态体

七道闸里落在本模块的 4 道：合并窗口、令牌桶、Flap Damping、去重 / 分片。
其余 3 道（Leader 单源、落盘三态、看板增量）由 T-0351 / T-0353 / T-0354 负责。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field, fields, replace
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    # 常量表
    "SyncConfig", "CONSTANTS",
    "AGGREGATION_WINDOW_S", "MIN_LS_INTERVAL_S", "MIN_LS_ARRIVAL_S",
    "LSA_PACING_S", "MULTICAST_BASE_S", "MULTICAST_MAX_S", "LS_REFRESH_S",
    "MAX_AGE_S", "JITTER", "TOKEN_BUCKET_LSA", "TOKEN_BUCKET_BYTES",
    "TOKEN_BUCKET_PERIOD_S", "MAX_PACKET_BYTES", "FLAP_PENALTY_BASE",
    "FLAP_SUPPRESS_THRESHOLD", "FLAP_REUSE_THRESHOLD", "FLAP_HALF_LIFE_S",
    "FLAP_MAX_SUPPRESS_S", "FLAP_WINDOW_S", "PULL_FULL_GAP_LIMIT",
    "SLA_VISIBILITY_S", "IDLE_BANDWIDTH_BASELINE_KB_S", "SYNC_STORM_MAX_FRACTION",
    # 数据结构
    "LSA", "LSDB", "LSDBEntry", "FlapDamping", "TokenBucket",
    "CHANGE", "DIGEST", "REFRESH", "LSA_KINDS",
    "NO_OP", "PULL_INCREMENTAL", "PULL_FULL",
    "TIER_T0", "TIER_T1", "TIER_T2",
    # 纯函数
    "compute_digest", "jittered", "next_deadline", "backoff_interval",
    "classify_tier", "min_ls_interval_ok", "min_ls_arrival_ok",
    "flap_penalty_increment", "flap_decay", "make_area", "parse_area",
    "pack_lsas", "fragment_lsas", "packet_bytes",
    # 核心
    "LSACore", "get_default_core", "set_default_core", "reset_default_core",
    # 模块级便捷 API（委托给默认 core）
    "note_change", "tick", "on_hello", "build_digest", "build_csnp",
    "merge_incoming", "should_pull", "sync_health", "status",
]


# --------------------------------------------------------------------------- #
# §2 / §5 常量表：唯一的真源。所有值逐项对齐设计文档，不允许在测试里放宽。
# --------------------------------------------------------------------------- #

AGGREGATION_WINDOW_S = 0.200        # 合并窗口（沿用 hive_control.Limits.aggregation_window）
MIN_LS_INTERVAL_S = 5.0             # OSPF MinLSInterval：同一 (area,key) 两条 LSA 最小间隔
MIN_LS_ARRIVAL_S = 1.0              # OSPF MinLSArrival：接收同一 LSA 的最小到达间隔
LSA_PACING_S = 0.033                # LSA Pacing 组步调（避免一个 tick 内瞬间突发）

MULTICAST_BASE_S = 60.0             # T1 摘要基线周期
MULTICAST_MAX_S = 1800.0            # 2^n 退避上限（NTP 式）
LS_REFRESH_S = 1800.0               # OSPF LSRefreshTime：T2 只发 digest 清单
MAX_AGE_S = 3600.0                  # OSPF MaxAge：LSDB 条目老化时间
JITTER = 0.20                       # 所有周期定时器强制 ±20%

TOKEN_BUCKET_LSA = 32               # 每周期最多 32 条 LSA
TOKEN_BUCKET_BYTES = 65536          # 每周期最多 64KB
TOKEN_BUCKET_PERIOD_S = 5.0         # 令牌补充周期（对齐 MinLSInterval）
MAX_PACKET_BYTES = 1400             # 单包上限（对齐 Limits.max_packet_bytes）

FLAP_PENALTY_BASE = 1000            # BGP route flap damping：单次翻转基础惩罚
FLAP_SUPPRESS_THRESHOLD = 2000      # 惩罚 >= 此值 -> 抑制，只发汇总 + 拉长间隔
FLAP_REUSE_THRESHOLD = 750          # 半衰衰减到 < 此值 -> 解除抑制
FLAP_HALF_LIFE_S = 60.0             # 惩罚半衰期：BGP 经典 15min，这里按 §5 的
                                    # 300s 可见性 SLA 压缩，保证抑制能被半衰衰减真正解除
FLAP_MAX_SUPPRESS_S = 300.0         # 最长抑制时间：不得超过 §5 的 300s 可见性 SLA
                                    # （BGP 经典值 60min 在这里会压住状态太久）
FLAP_WINDOW_S = 60.0                # 该窗口内的连续变更算「连续震荡」
PULL_FULL_GAP_LIMIT = 64            # seq 缺口超过此值 -> 退化为 pull_full

SLA_VISIBILITY_S = 300.0            # §5：状态变更 -> 全集群可见硬上限（交办方 5 分钟）
IDLE_BANDWIDTH_BASELINE_KB_S = 9.0  # §5：0.16.05 三节点空闲基线 ~9KB/s
SYNC_STORM_MAX_FRACTION = 0.20      # §5：同一秒同步节点占比上限（jitter 生效证据）

# 报文种类 / 指令 / 档位（用字符串常量，避免枚举带来的序列化成本）
CHANGE = "change"
DIGEST = "digest"
REFRESH = "refresh"
LSA_KINDS = frozenset({CHANGE, DIGEST, REFRESH})

NO_OP = "no-op"
PULL_INCREMENTAL = "pull_incremental"
PULL_FULL = "pull_full"

TIER_T0 = "T0"
TIER_T1 = "T1"
TIER_T2 = "T2"

# 摘要 / Hello 报文里绝不允许出现的键（0.16.03 / 0.16.05 的病根）
FORBIDDEN_IN_DIGEST = frozenset({
    "diff", "progress", "leases", "nodes", "agents", "tasks",
    "full_state", "state", "body", "card", "state_json",
})


@dataclass(frozen=True)
class SyncConfig:
    """所有常量集中、可注入。

    字段是小写 snake_case；为方便 T-0355 压测按「设计文档里的大写常量名」直接
    引用，``cfg.AGGREGATION_WINDOW_S`` 这类大写别名也可用（``__getattr__`` 转发）。
    模块级大写常量是唯一真源，字段默认值直接取它们，杜绝两份数字漂移。

    >>> cfg = SyncConfig()
    >>> cfg.min_ls_interval_s, cfg.MIN_LS_INTERVAL_S
    (5.0, 5.0)
    >>> cfg.overridden(min_ls_interval_s=2.0).min_ls_interval_s
    2.0
    """

    aggregation_window_s: float = AGGREGATION_WINDOW_S
    min_ls_interval_s: float = MIN_LS_INTERVAL_S
    min_ls_arrival_s: float = MIN_LS_ARRIVAL_S
    lsa_pacing_s: float = LSA_PACING_S
    multicast_base_s: float = MULTICAST_BASE_S
    multicast_max_s: float = MULTICAST_MAX_S
    ls_refresh_s: float = LS_REFRESH_S
    max_age_s: float = MAX_AGE_S
    jitter: float = JITTER
    token_bucket_lsa: int = TOKEN_BUCKET_LSA
    token_bucket_bytes: int = TOKEN_BUCKET_BYTES
    token_bucket_period_s: float = TOKEN_BUCKET_PERIOD_S
    max_packet_bytes: int = MAX_PACKET_BYTES
    flap_penalty_base: int = FLAP_PENALTY_BASE
    flap_suppress_threshold: int = FLAP_SUPPRESS_THRESHOLD
    flap_reuse_threshold: int = FLAP_REUSE_THRESHOLD
    flap_half_life_s: float = FLAP_HALF_LIFE_S
    flap_max_suppress_s: float = FLAP_MAX_SUPPRESS_S
    flap_window_s: float = FLAP_WINDOW_S
    pull_full_gap_limit: int = PULL_FULL_GAP_LIMIT
    sla_visibility_s: float = SLA_VISIBILITY_S
    idle_bandwidth_baseline_kb_s: float = IDLE_BANDWIDTH_BASELINE_KB_S
    sync_storm_max_fraction: float = SYNC_STORM_MAX_FRACTION

    def overridden(self, **changes: Any) -> "SyncConfig":
        """返回一个改了若干字段的副本（测试注入 fake 参数用，不污染全局）。"""
        return replace(self, **changes)

    def as_dict(self) -> dict[str, Any]:
        """小写字段字典。"""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def constants(cls) -> dict[str, Any]:
        """大写常量表：``{"AGGREGATION_WINDOW_S": 0.2, ...}``，供 T-0355 直接引用。"""
        return {f.name.upper(): getattr(cls(), f.name) for f in fields(cls())}

    def __getattr__(self, name: str) -> Any:
        # 只处理大写别名；其它一律正常抛 AttributeError（不干扰 deepcopy/replace）。
        if name.isupper():
            low = name.lower()
            if low in self.__dataclass_fields__:
                return getattr(self, low)
        raise AttributeError(f"{type(self).__name__!s} 没有属性 {name!r}")


#: 模块级常量表（大写 key）。与 ``SyncConfig.constants()`` 完全一致。
CONSTANTS: dict[str, Any] = SyncConfig.constants()


# --------------------------------------------------------------------------- #
# 纯函数：可注入时钟 / 可注入随机，便于单测与 T-0355 压测直接引用
# --------------------------------------------------------------------------- #

def _canonical(payload: Any) -> str:
    """稳定序列化：sort_keys + 紧凑分隔符，保证同一份内容 sha256 恒定。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def compute_digest(payload: Any) -> str:
    """任意可 JSON 序列化对象的 sha256（hex）。字段级 diff / CSNP 清单都用它。"""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def packet_bytes(packet: Any) -> int:
    """报文按 UTF-8 JSON 编码后的字节数（与 MAX_PACKET_BYTES 同一口径）。"""
    return len(_canonical(packet).encode("utf-8"))


_DEFAULT_RNG: Callable[[], float] = random.random


def jittered(interval: float, rng: Callable[[], float] | None = None,
             jitter: float = JITTER) -> float:
    """给周期加 ±``jitter`` 抖动，返回抖动后的实际间隔。

    这是**所有周期定时器的唯一入口**。返回值保证落在
    ``[interval*(1-jitter), interval*(1+jitter)]``，并且**一定不等于**
    ``interval`` 本身——「所有节点同一秒刷新」是本卡明令禁止的。
    ``rng`` 返回 ``[0,1)``，测试可注入固定值验证边界。
    """
    if interval <= 0:
        raise ValueError("interval 必须为正数")
    u = float((rng or _DEFAULT_RNG)())
    if not 0.0 <= u < 1.0:
        raise ValueError("rng 必须返回 [0,1) 内的数")
    factor = 1.0 + (2.0 * u - 1.0) * jitter
    if factor == 1.0:
        # u 恰好 0.5（或注入值）时不能退回「无抖动」；给一个仍在区间内的非零偏移。
        factor = 1.0 + jitter * 0.5
    return interval * factor


def next_deadline(now: float, interval: float,
                  rng: Callable[[], float] | None = None,
                  jitter: float = JITTER) -> float:
    """下一次周期触发的绝对时刻（带 jitter）。T-0353 / T-0354 的节拍也该调它。"""
    return now + jittered(interval, rng=rng, jitter=jitter)


def backoff_interval(idle_rounds: int, base: float = MULTICAST_BASE_S,
                     cap: float = MULTICAST_MAX_S) -> float:
    """NTP 式 2^n 退避：60 -> 120 -> 240 -> 480 -> 960 -> 1800（封顶）。

    ``idle_rounds`` = 连续「无变更」的 T1 周期数。一有变更，调用方把它归零，
    间隔立刻乘性减回 60s 基线（AIMD 的回弹侧）；无变更则乘性增。
    """
    if idle_rounds < 0:
        raise ValueError("idle_rounds 不能为负")
    if base <= 0 or cap < base:
        raise ValueError("需要 0 < base <= cap")
    # 用位移避免大 n 时浮点溢出；n 上限由 cap 提前截断。
    n = min(idle_rounds, int(math.ceil(math.log2(cap / base))) + 1)
    return min(base * float(2 ** n), cap)


def classify_tier(*, has_pending: bool, now: float, next_t1_at: float,
                  next_t2_at: float) -> str | None:
    """三档节奏判定（纯函数、无副作用、不读时钟）。

    T0 优先：合并窗口到期且有变更 -> 立刻发；其次 T2 对账；最后 T1 摘要。
    都没有到点则 ``None``（本 tick 什么都不发，这正是空闲低占用的来源）。
    """
    if has_pending:
        return TIER_T0
    if now >= next_t2_at:
        return TIER_T2
    if now >= next_t1_at:
        return TIER_T1
    return None


def min_ls_interval_ok(now: float, last_emit_at: float | None,
                       cfg: SyncConfig | None = None) -> bool:
    """OSPF MinLSInterval：同一 (area,key) 是否允许再发一条 change LSA。

    比较的是**绝对截止时刻** ``last_at + interval`` 而不是 ``now - last_at``：
    在 fake clock 的大时间戳上，后者在「恰好到期」时会因 IEEE-754 减法
    丢精度（``500.2 - 500.0 == 0.19999999999998863``）而误判为未到期。
    两者语义等价，阈值一点没放宽。
    """
    interval = (cfg or SyncConfig()).min_ls_interval_s
    if last_emit_at is None:
        return True
    return now >= (last_emit_at + interval)


def min_ls_arrival_ok(now: float, last_arrival_at: float | None,
                      cfg: SyncConfig | None = None) -> bool:
    """OSPF MinLSArrival：同一 (area,key) 是否允许再接收一条 LSA。"""
    interval = (cfg or SyncConfig()).min_ls_arrival_s
    if last_arrival_at is None:
        return True
    return now >= (last_arrival_at + interval)


def flap_penalty_increment(consecutive: int, base: int = FLAP_PENALTY_BASE) -> float:
    """连续震荡第 n 次的惩罚增量：指数上升（2^(n-1)），上限 2^8 防溢出。"""
    n = max(1, min(consecutive, 9))
    return float(base) * float(2 ** (n - 1))


def flap_decay(penalty: float, elapsed_s: float,
               half_life_s: float = FLAP_HALF_LIFE_S) -> float:
    """BGP 半衰衰减：每过一个 half_life 惩罚减半。"""
    if elapsed_s <= 0 or penalty <= 0:
        return penalty
    return penalty * (0.5 ** (elapsed_s / half_life_s))


def make_area(repo: str, cluster_id: str = "", project_tag: str = "") -> str:
    """把 ``(repo, cluster_id, project_tag)`` 规范化成可哈希的 area 字符串。"""
    return "/".join((str(repo or ""), str(cluster_id or ""), str(project_tag or "")))


def parse_area(area: str) -> tuple[str, str, str]:
    """``make_area`` 的逆运算；缺段补空串。"""
    parts = str(area).split("/")
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def _normalize_key(key: Any, repo: str | None = None,
                   tid: str | None = None, area: str = "") -> tuple[str, str]:
    """``(repo, tid)`` 二元组；允许传字符串 tid 或 ``repo``/``tid`` 关键字。"""
    if key is None:
        key = (repo if repo is not None else parse_area(area)[0], tid)
    if isinstance(key, (str, int)):
        key = (repo if repo is not None else parse_area(area)[0], str(key))
    if isinstance(key, (list, tuple)) and len(key) == 2:
        return (str(key[0]), str(key[1]))
    raise TypeError("key 必须是 (repo, tid) 二元组，或配合 repo/tid 关键字")


# --------------------------------------------------------------------------- #
# 数据结构：LSA / LSDBEntry / LSDB
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LSA:
    """一条链路状态通告。

    ``diff`` 是**字段级**变更（例如 ``{"state": "running"}``），不是整卡快照；
    ``digest`` 为该 diff 的 sha256；``head_sha`` 单列，供 T-0353 判断
    「要不要连 Gitea」——它绝不混进摘要报文的状态体里。
    """

    originator: str
    area: str
    leader_epoch: int
    seq: int
    kind: str = CHANGE
    key: tuple[str, str] = ("", "")
    diff: dict[str, Any] = field(default_factory=dict)
    digest: str = ""
    ts: float = 0.0
    age: float = 0.0
    ttl: float = MAX_AGE_S
    head_sha: str = ""

    def __post_init__(self) -> None:
        if self.kind not in LSA_KINDS:
            raise ValueError(f"未知 LSA kind: {self.kind!r}")
        if not self.digest:
            # change LSA 的摘要 = 字段级 diff 的内容摘要；digest/refresh 用空串
            # （它们的摘要是 LSDB 水位，由 build_digest() 单独给出）。
            object.__setattr__(self, "digest",
                               compute_digest(self.diff) if self.kind == CHANGE else "")

    def identity(self) -> tuple[str, tuple[str, str]]:
        """LSDB 的键 ``(area, key)``。"""
        return (self.area, self.key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "originator": self.originator, "area": self.area,
            "leader_epoch": self.leader_epoch, "seq": self.seq,
            "kind": self.kind, "key": list(self.key),
            "diff": dict(self.diff), "digest": self.digest,
            "ts": self.ts, "age": self.age, "ttl": self.ttl,
            "head_sha": self.head_sha,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LSA":
        key = data.get("key", ("", ""))
        return cls(
            originator=str(data.get("originator", "")),
            area=str(data.get("area", "")),
            leader_epoch=int(data.get("leader_epoch", 0)),
            seq=int(data.get("seq", 0)),
            kind=str(data.get("kind", CHANGE)),
            key=(str(key[0]), str(key[1])),
            diff=dict(data.get("diff") or {}),
            digest=str(data.get("digest", "")),
            ts=float(data.get("ts", 0.0)),
            age=float(data.get("age", 0.0)),
            ttl=float(data.get("ttl", MAX_AGE_S)),
            head_sha=str(data.get("head_sha", "")),
        )

    def size_bytes(self) -> int:
        return packet_bytes(self.to_dict())

    def overridden(self, **changes: Any) -> "LSA":
        return replace(self, **changes)


@dataclass(frozen=True)
class LSDBEntry:
    """LSDB 里的**摘要**条目：``(area,key) -> (seq, epoch, digest, head_sha)``。

    刻意不保存 diff：LSDB 只是「我见过什么水位」的索引，不是状态仓库。
    Git 卡面才是事实账（T-0353 纪律）。
    """

    area: str
    key: tuple[str, str]
    seq: int
    epoch: int
    digest: str
    head_sha: str
    ts: float
    age: float = 0.0

    def identity(self) -> tuple[str, tuple[str, str]]:
        return (self.area, self.key)

    def summary(self) -> dict[str, Any]:
        """CSNP 条目：**只有** key/seq/epoch/digest/head_sha，无状态体。"""
        return {
            "key": list(self.key), "area": self.area, "seq": self.seq,
            "epoch": self.epoch, "digest": self.digest, "head_sha": self.head_sha,
        }


class LSDB:
    """内存摘要库 + 幂等去重 + MaxAge 老化。

    ``seq`` 是全局水位（每次 install +1，供看板 ``X-Hive-LSDB-Seq`` 使用）；
    每个 ``(area,key)`` 另有独立的 OSPF 式序列号（``LSDBEntry.seq``），
    用于 seq 缺口检测——全局水位不能直接当 per-key 缺口，否则多 key 交错会误报。
    """

    def __init__(self, config: SyncConfig | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.cfg = config or SyncConfig()
        self._clock = clock or time.monotonic
        self._entries: dict[tuple[str, tuple[str, str]], LSDBEntry] = {}
        self._per_key_seq: dict[tuple[str, tuple[str, str]], int] = {}
        self.seq: int = 0                # 全局水位
        self.epoch: int = 0
        self.head_sha: str = ""          # 本地已知 origin/main 头
        self.remote_sha: str = ""        # 最近一次 T1 组播通告的头

    # -- 判新旧（幂等去重） ------------------------------------------------- #
    def is_newer(self, lsa: LSA) -> bool:
        """旧 / 重复 LSA 返回 ``False``（调用方直接丢）。

        优先级：``leader_epoch`` > 同 key 的 ``seq`` > 内容摘要 > ``age``。
        同 (epoch, seq) 但 digest 相同 = 重复；digest 不同 = 冲突，
        用 age 裁决（age 小 = 更新鲜），仍相同则用 digest 字典序保证确定性。
        """
        cur = self._entries.get(lsa.identity())
        if cur is None:
            return True
        if lsa.leader_epoch != cur.epoch:
            return lsa.leader_epoch > cur.epoch
        if lsa.seq != cur.seq:
            return lsa.seq > cur.seq
        if lsa.digest == cur.digest:
            return False
        if lsa.age != cur.age:
            return lsa.age < cur.age
        return lsa.digest > cur.digest

    def install(self, lsa: LSA, now: float | None = None) -> bool:
        """安装一条较新的 LSA；返回是否真的更新了 LSDB。"""
        if not self.is_newer(lsa):
            return False
        at = self._clock() if now is None else now
        ident = lsa.identity()
        self._entries[ident] = LSDBEntry(
            area=lsa.area, key=lsa.key, seq=lsa.seq,
            epoch=lsa.leader_epoch, digest=lsa.digest,
            head_sha=lsa.head_sha, ts=at, age=lsa.age,
        )
        self._per_key_seq[ident] = lsa.seq
        self.epoch = max(self.epoch, lsa.leader_epoch)
        if lsa.head_sha:
            self.head_sha = lsa.head_sha
        self.seq += 1
        return True

    def next_seq(self, ident: tuple[str, tuple[str, str]]) -> int:
        """某个 (area,key) 的下一条 OSPF 序列号（从 1 开始）。"""
        return self._per_key_seq.get(ident, 0) + 1

    # -- 老化 --------------------------------------------------------------- #
    def purge_expired(self, now: float | None = None,
                      max_age_s: float | None = None) -> int:
        """MaxAge 老化：清掉超过 ``max_age_s`` 没被刷新的条目，返回清理数。"""
        at = self._clock() if now is None else now
        limit = self.cfg.max_age_s if max_age_s is None else max_age_s
        dead = [ident for ident, e in self._entries.items()
                if (at - e.ts) >= limit or e.age >= limit]
        for ident in dead:
            self._entries.pop(ident, None)
            self._per_key_seq.pop(ident, None)
        return len(dead)

    # -- 读取 --------------------------------------------------------------- #
    def get(self, ident: tuple[str, tuple[str, str]]) -> LSDBEntry | None:
        return self._entries.get(ident)

    def entries(self) -> list[LSDBEntry]:
        """按 (area, key, seq) 稳定排序，保证 digest 可复现。"""
        return sorted(self._entries.values(),
                      key=lambda e: (e.area, e.key[0], e.key[1], e.seq))

    def summaries(self) -> list[dict[str, Any]]:
        return [e.summary() for e in self.entries()]

    def snapshot_digest(self) -> str:
        """整库摘要：条目摘要 + 全局水位 + head_sha 的 sha256。"""
        return compute_digest({
            "entries": self.summaries(), "seq": self.seq,
            "epoch": self.epoch, "head_sha": self.head_sha,
        })

    def seq_gaps(self, peer_entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """比对本库与对端 CSNP 条目，返回缺口清单（增量补拉的依据）。

        每条：``{"key", "area", "local_seq", "peer_seq", "missing_seq"}``。
        """
        peer: dict[tuple[str, tuple[str, str]], int] = {}
        for item in peer_entries:
            k = item.get("key", ("", ""))
            ident = (str(item.get("area", "")), (str(k[0]), str(k[1])))
            peer[ident] = max(peer.get(ident, 0), int(item.get("seq", 0)))
        gaps: list[dict[str, Any]] = []
        for ident, local_seq in sorted(self._per_key_seq.items()):
            peer_seq = peer.get(ident, 0)
            if local_seq > peer_seq:
                missing = list(range(peer_seq + 1, local_seq + 1))
                gaps.append({
                    "key": list(ident[1]), "area": ident[0],
                    "local_seq": local_seq, "peer_seq": peer_seq,
                    "missing_seq": missing,
                })
        return gaps

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, ident: tuple[str, tuple[str, str]]) -> bool:
        return ident in self._entries


# --------------------------------------------------------------------------- #
# 第 2 / 3 道闸：令牌桶 与 Flap Damping
# --------------------------------------------------------------------------- #

class TokenBucket:
    """每周期 ≤32 条 / ≤65536 字节；超限**排队合并、不丢状态、只削峰**。"""

    def __init__(self, config: SyncConfig | None = None) -> None:
        self.cfg = config or SyncConfig()
        self.lsa_left = self.cfg.token_bucket_lsa
        self.bytes_left = self.cfg.token_bucket_bytes
        self.refilled_at: float = 0.0
        self.deferred_total = 0

    def refill_if_due(self, now: float) -> bool:
        """到周期就补满令牌；返回本 tick 是否补充过。"""
        if self.refilled_at == 0.0 or (now - self.refilled_at) >= self.cfg.token_bucket_period_s:
            self.lsa_left = self.cfg.token_bucket_lsa
            self.bytes_left = self.cfg.token_bucket_bytes
            self.refilled_at = now
            return True
        return False

    def can_take(self, size: int) -> bool:
        return self.lsa_left > 0 and self.bytes_left >= size

    def take(self, size: int) -> None:
        self.lsa_left -= 1
        self.bytes_left -= size

    def defer(self, n: int = 1) -> None:
        self.deferred_total += n


class FlapDamping:
    """BGP 路由抑制：同一 (area,key) 反复震荡时惩罚值指数上升。

    * 超 ``FLAP_SUPPRESS_THRESHOLD`` -> 只发汇总（digest）、拉长发送间隔；
    * 期间的字段级变更累积进 ``pending_diff``，**状态不丢**；
    * 停止震荡后惩罚按半衰衰减，降到 ``FLAP_REUSE_THRESHOLD`` 以下解除抑制，
      解除时把累积的最终 diff 合成一条 change LSA 补发。
    """

    def __init__(self, config: SyncConfig | None = None) -> None:
        self.cfg = config or SyncConfig()
        self._states: dict[tuple[str, tuple[str, str]], dict[str, Any]] = {}
        self.suppressed_total = 0
        self.recovered_total = 0

    @staticmethod
    def _new_state(now: float) -> dict[str, Any]:
        return {
            "penalty": 0.0, "consecutive": 0, "last_flap_at": now,
            "suppressed": False, "suppressed_since": 0.0,
            "pending_diff": {}, "flaps": 0,
        }

    def _decay(self, st: dict[str, Any], now: float) -> None:
        elapsed = now - st["last_flap_at"]
        if elapsed > 0:
            # 只在时间真的前进时刷新基准：fake clock 常返回常量，
            # 否则只读查询会把 last_flap_at 拉回过去，破坏后续惩罚计算。
            st["penalty"] = flap_decay(st["penalty"], elapsed, self.cfg.flap_half_life_s)
            st["last_flap_at"] = now

    def record(self, ident: tuple[str, tuple[str, str]], now: float,
               diff: dict[str, Any] | None = None) -> dict[str, Any]:
        """记一次状态翻转；返回该 key 的当前抑制状态快照。

        只有**连续**震荡才指数加罚：孤立的一次变更（间隔 > flap_window_s）
        把 consecutive 归 1，且不加惩罚——正常的 1 次/分钟状态推进不应该被
        路由抑制误伤。第 2 次连续翻转 +1000、第 3 次 +2000、第 4 次 +4000…
        """
        st = self._states.setdefault(ident, self._new_state(now))
        gap = now - st["last_flap_at"]          # 必须在 _decay 刷新基准前取
        self._decay(st, now)
        if 0 <= gap <= self.cfg.flap_window_s:
            st["consecutive"] += 1
        else:
            st["consecutive"] = 1
        if st["consecutive"] >= 2:
            # 指数上升，但封顶（BGP 也有 max penalty），避免惩罚值失控后
            # 半衰永远衰减不回 reuse 阈值、把状态永久压住。
            inc = flap_penalty_increment(st["consecutive"] - 1,
                                         self.cfg.flap_penalty_base)
            ceiling = self.cfg.flap_suppress_threshold * 10
            st["penalty"] = min(st["penalty"] + inc, ceiling)
        st["last_flap_at"] = now
        st["flaps"] += 1
        if diff:
            st["pending_diff"].update(diff)
        if not st["suppressed"] and st["penalty"] >= self.cfg.flap_suppress_threshold:
            st["suppressed"] = True
            st["suppressed_since"] = now
            self.suppressed_total += 1
        return self.snapshot(ident, now)

    def accumulate(self, ident: tuple[str, tuple[str, str]],
                   diff: dict[str, Any]) -> None:
        """抑制期间累积字段级 diff（**只记状态，不额外加惩罚**）。"""
        st = self._states.setdefault(ident, self._new_state(0.0))
        st["pending_diff"].update(diff)

    def snapshot(self, ident: tuple[str, tuple[str, str]], now: float) -> dict[str, Any]:
        """只读快照（会做一次半衰刷新，但不改变抑制历史）。"""
        st = self._states.get(ident)
        if st is None:
            return {"penalty": 0.0, "consecutive": 0, "suppressed": False,
                    "pending_diff": {}, "flaps": 0, "interval_multiplier": 1.0}
        self._decay(st, now)
        # 半衰衰减到 reuse 阈值以下、或抑制超过兜底上限 -> 解除
        too_long = (st["suppressed_since"] and
                    (now - st["suppressed_since"]) >= self.cfg.flap_max_suppress_s)
        if st["suppressed"] and (st["penalty"] < self.cfg.flap_reuse_threshold or too_long):
            st["suppressed"] = False
            st["consecutive"] = 0
            self.recovered_total += 1
        mult = 1.0
        if st["suppressed"]:
            mult = float(2 ** min(max(st["consecutive"], 1), 5))
        return {
            "penalty": st["penalty"], "consecutive": st["consecutive"],
            "suppressed": st["suppressed"], "pending_diff": dict(st["pending_diff"]),
            "flaps": st["flaps"], "interval_multiplier": mult,
        }

    def is_suppressed(self, ident: tuple[str, tuple[str, str]], now: float) -> bool:
        return bool(self.snapshot(ident, now)["suppressed"])

    def peek(self, ident: tuple[str, tuple[str, str]]) -> dict[str, Any]:
        """**非变异**只读：不做半衰刷新，专供 note_change() 回显状态，
        避免只读查询把 decay 基准时间冲掉。"""
        st = self._states.get(ident)
        if st is None:
            return {"penalty": 0.0, "suppressed": False, "flaps": 0}
        return {"penalty": st["penalty"], "suppressed": st["suppressed"],
                "flaps": st["flaps"]}

    def drain_pending(self, ident: tuple[str, tuple[str, str]]) -> dict[str, Any]:
        """取出并清空抑制期间累积的字段级 diff（恢复时合成一条 LSA）。"""
        st = self._states.get(ident)
        if not st:
            return {}
        diff = dict(st["pending_diff"])
        st["pending_diff"] = {}
        return diff

    def penalty(self, ident: tuple[str, tuple[str, str]], now: float) -> float:
        return float(self.snapshot(ident, now)["penalty"])

    def suppressed_keys(self, now: float) -> list[list[Any]]:
        """只读：用 peek 而非 snapshot，避免 status() 反过来影响惩罚时间轴。"""
        return [list(ident) for ident in self._states
                if self.peek(ident)["suppressed"]]


# --------------------------------------------------------------------------- #
# 第 4 道闸：去重 / 分片 —— 单包 ≤1400B，按 LSA 边界分片，不切半条
# --------------------------------------------------------------------------- #

def _as_lsa(item: LSA | dict[str, Any]) -> LSA:
    return item if isinstance(item, LSA) else LSA.from_dict(item)


def pack_lsas(lsas: Iterable[LSA | dict[str, Any]],
              max_bytes: int = MAX_PACKET_BYTES) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 LSA 装进 ≤``max_bytes`` 的包，返回 ``(packets, oversize)``。

    * 只在 **LSA 边界**切包，一条 LSA 绝不被拆到两个包里；
    * 单条 LSA 本身就超限的，整条放进 ``oversize``（调用方决定后续压缩/合并），
      **不切半条**，也不悄悄发一个超 1400B 的包；
    * 包大小按 UTF-8 JSON 字节数算，与 ``MAX_PACKET_BYTES`` 同一口径。
    """
    packets: list[dict[str, Any]] = []
    oversize: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def make_packet(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "lsa", "n": len(items), "lsas": items}

    for raw in lsas:
        lsa = _as_lsa(raw)
        body = lsa.to_dict()
        if packet_bytes({"type": "lsa", "n": 1, "lsas": [body]}) > max_bytes:
            oversize.append(body)
            continue
        trial = current + [body]
        if packet_bytes(make_packet(trial)) > max_bytes and current:
            packets.append(make_packet(current))
            current = [body]
        else:
            current = [body] if not current else trial
    if current:
        packets.append(make_packet(current))
    return packets, oversize


#: 语义化别名：T-0351 下行侧按 LSA 边界分片时可直接引用
fragment_lsas = pack_lsas


def _assert_digest_packet(packet: dict[str, Any]) -> None:
    """硬不变量：摘要 / Hello / CSNP 报文里绝不能夹带全量状态体。"""
    bad = FORBIDDEN_IN_DIGEST.intersection(packet.keys())
    if bad:
        raise AssertionError(f"摘要报文夹带了状态体字段: {sorted(bad)}")
    for item in packet.get("entries", []) or []:
        bad = FORBIDDEN_IN_DIGEST.intersection(item.keys())
        if bad:
            raise AssertionError(f"CSNP 条目夹带了状态体字段: {sorted(bad)}")


# --------------------------------------------------------------------------- #
# 核心：LSACore
# --------------------------------------------------------------------------- #

@dataclass
class _Pending:
    """合并窗口内同一个 (area,key) 的 N 次变更，合成 1 条 LSA。"""

    ident: tuple[str, tuple[str, str]]
    first_at: float
    last_at: float
    diff: dict[str, Any] = field(default_factory=dict)
    count: int = 0
    head_sha: str = ""

    def absorb(self, diff: dict[str, Any], now: float, head_sha: str = "") -> None:
        self.diff.update(diff)          # 字段级合并：后到的字段覆盖先到的
        self.last_at = now
        self.count += 1
        if head_sha:
            self.head_sha = head_sha


class LSACore:
    """H-LSA 状态机。**零 I/O**：所有方法只改内存并返回数据。

    :param originator: 本节点标识
    :param area: ``make_area(repo, cluster_id, project_tag)`` 的结果
    :param project_tag: 单独给 T-0351 摘要报文字段用
    :param leader_epoch: 当前 leader 任期（切主后单调递增）
    :param config: 可注入的 :class:`SyncConfig`
    :param clock: 可注入时钟 ``() -> float``（fake clock）
    :param rng: 可注入随机 ``() -> float``（fake jitter，返回 [0,1)）
    :param start_at: 仿真起始时刻；``None`` 时取 ``clock()``
    """

    def __init__(
        self,
        originator: str = "",
        area: str = "",
        project_tag: str = "",
        leader_epoch: int = 0,
        config: SyncConfig | None = None,
        clock: Callable[[], float] | None = None,
        rng: Callable[[], float] | None = None,
        start_at: float | None = None,
    ) -> None:
        self.originator = str(originator or "")
        self.area = str(area or "")
        self.project_tag = str(project_tag or parse_area(self.area)[2])
        self.leader_epoch = int(leader_epoch)
        self.cfg = config or SyncConfig()
        self._clock = clock or time.monotonic
        self._rng = rng or _DEFAULT_RNG
        self._start_at = self._clock() if start_at is None else float(start_at)

        self.lsdb = LSDB(self.cfg, clock=self._clock)
        self.bucket = TokenBucket(self.cfg)
        self.flap = FlapDamping(self.cfg)

        self._pending: dict[tuple[str, tuple[str, str]], _Pending] = {}
        self._outbox: deque[LSA] = deque()
        self._deferred_incoming: deque[LSA] = deque()
        self._last_emit_at: dict[tuple[str, tuple[str, str]], float] = {}
        self._arrival_at: dict[tuple[str, tuple[str, str]], float] = {}
        self._suppressed_identities: set[tuple[str, tuple[str, str]]] = set()

        # 周期定时器：一律经 jittered()，绝不使用整 60.0 / 1800.0
        self._idle_rounds = 0
        self._jittered_deadlines: list[float] = []
        self._next_t1_at = self._jitter_deadline(self._start_at, self.cfg.multicast_base_s)
        self._next_t2_at = self._jitter_deadline(self._start_at, self.cfg.ls_refresh_s)
        self._next_flap_summary_at: float | None = None

        # 指标（供 T-0355 / T-0356 采集，口径不随测试变化）
        self._m: dict[str, Any] = {
            "emitted_change_lsa": 0, "emitted_digest": 0, "emitted_refresh": 0,
            "emitted_csnp_packets": 0, "emitted_packets": 0, "emitted_bytes": 0,
            "full_state_transfers": 0, "git_pull_instructions": 0,
            "dropped_duplicate": 0, "dropped_old": 0,
            "dropped_min_arrival": 0, "deferred_min_arrival": 0,
            "dropped_max_age": 0,
            "flap_suppressed": 0, "flap_recovered": 0,
            "flap_summaries": 0, "token_deferred": 0, "oversize_deferred": 0,
            "merged_changes": 0, "t0_events": 0, "t1_events": 0, "t2_events": 0,
            "accepted_incoming": 0, "digest_incoming": 0,
        }
        self._last_t1_at: float | None = None
        self._last_t2_at: float | None = None
        self._last_sync_at: float | None = None
        self._last_tier: str | None = None
        self._last_reason: str = "idle"
        self._n_changes_since_digest = 0

    # -- 内部工具 ----------------------------------------------------------- #
    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else float(now)

    def _jitter_deadline(self, now: float, interval: float) -> float:
        deadline = next_deadline(now, interval, rng=self._rng, jitter=self.cfg.jitter)
        self._jittered_deadlines.append(deadline)
        return deadline

    def _new_lsa(self, ident: tuple[str, tuple[str, str]], diff: dict[str, Any],
                 now: float, kind: str = CHANGE, head_sha: str = "") -> LSA:
        area, key = ident
        return LSA(
            originator=self.originator, area=area,
            leader_epoch=self.leader_epoch, seq=self.lsdb.next_seq(ident),
            kind=kind, key=key, diff=dict(diff),
            ts=now, age=0.0, ttl=self.cfg.max_age_s,
            head_sha=head_sha or self.lsdb.head_sha,
        )

    def _enqueue(self, lsa: LSA) -> None:
        self._outbox.append(lsa)

    @staticmethod
    def _compact_outbox(items: Sequence[LSA]) -> list[LSA]:
        """排队合并：同一 (area,key) 的多条 LSA 合成 1 条，字段级 diff 按序覆盖。

        削峰的同时保证「不丢状态」——最后一条的 diff 含有期间所有字段的最终值。
        """
        merged: dict[tuple[str, tuple[str, str]], LSA] = {}
        order: list[tuple[str, tuple[str, str]]] = []
        for lsa in items:
            ident = lsa.identity()
            if ident not in merged:
                order.append(ident)
                merged[ident] = lsa
            else:
                prev = merged[ident]
                combined = dict(prev.diff)
                combined.update(lsa.diff)
                merged[ident] = prev.overridden(
                    seq=max(prev.seq, lsa.seq), diff=combined,
                    ts=max(prev.ts, lsa.ts), head_sha=lsa.head_sha or prev.head_sha,
                )
        return [merged[i] for i in order]

    # -- 公开 API 1：note_change -------------------------------------------- #
    def note_change(self, key: Any = None, diff: dict[str, Any] | None = None,
                    *, repo: str | None = None, tid: str | None = None,
                    area: str | None = None, now: float | None = None,
                    head_sha: str | None = None) -> dict[str, Any]:
        """登记一次**实质字段变更**（T0 触发）。

        同一 ``(area,key)`` 在 200ms 合并窗口内的 N 次变更合成 1 条 LSA；
        纯心跳 ``last_seen`` / 租约续签**不该调用本方法**（T-0352 纪律）。

        同时把 T1 退避**立刻乘性减回 60s 基线**（AIMD 的变更回弹侧）。
        无任何 I/O；返回本次登记的状态。
        """
        at = self._now(now)
        norm_area = str(area if area is not None else self.area)
        norm_key = _normalize_key(key, repo=repo, tid=tid, area=norm_area)
        ident = (norm_area, norm_key)
        payload = dict(diff or {})

        # 合并窗口只做字段级合并。Flap Damping 的记账点故意放在
        # 「窗口到期、准备发出 1 条 LSA」的 _collect_pending()：抑制的是
        # **反复泛洪**，不是窗口内的原始变更次数——否则 N 次合并会被误判成
        # N 次震荡，第一道闸和第三道闸会互相打架。
        self._absorb(ident, payload, at, head_sha=head_sha)

        # 一有变更立刻回 60s；idle_rounds 归零。
        self._idle_rounds = 0
        self._next_t1_at = self._jitter_deadline(at, self.cfg.multicast_base_s)
        self._last_tier = TIER_T0
        self._last_reason = "change noted (T0)"
        self._n_changes_since_digest += 1
        if head_sha:
            self.lsdb.head_sha = head_sha
        p = self._pending[ident]
        peek = self.flap.peek(ident)
        return {
            "queued": True, "area": norm_area, "key": list(norm_key),
            "merged_changes": p.count,
            "window_closes_at": p.first_at + self.cfg.aggregation_window_s,
            "flap_suppressed": bool(peek["suppressed"]),
            "penalty": peek["penalty"], "backoff_reset_to_s": self.cfg.multicast_base_s,
        }

    def _absorb(self, ident: tuple[str, tuple[str, str]], diff: dict[str, Any],
                now: float, head_sha: str | None = None) -> None:
        p = self._pending.get(ident)
        if p is None:
            p = _Pending(ident=ident, first_at=now, last_at=now)
            self._pending[ident] = p
        if p.count:
            self._m["merged_changes"] += 1
        p.absorb(diff, now, head_sha=head_sha or "")

    # -- 公开 API 2：tick ---------------------------------------------------- #
    def tick(self, now: float | None = None) -> dict[str, Any]:
        """推进状态机一个节拍，返回**本 tick 待发送的所有报文**（模块自己不发）。

        返回 dict 的关键字段：

        * ``tier``：``"T0" | "T1" | "T2" | None``
        * ``packets``：待发包列表，每个 ≤1400B；T1/T2 包只有摘要，无状态体
        * ``full_state_transfers`` / ``git_pull_instructions``：本 tick 产生数，
          协议保证恒为 0
        * ``t2_refresh``：本 tick 是否发生了 1800s 对账
        * ``deferred``：令牌桶排队（削峰）条数
        """
        at = self._now(now)
        self.lsdb.purge_expired(at)
        self.bucket.refill_if_due(at)
        self._collect_pending(at)
        self._maybe_recover_flap(at)
        self._drain_deferred_incoming(at)

        has_pending = bool(self._outbox)
        tier = classify_tier(has_pending=has_pending, now=at,
                             next_t1_at=self._next_t1_at,
                             next_t2_at=self._next_t2_at)

        packets: list[dict[str, Any]] = []
        emitted_change = 0
        emitted_digest = 0
        emitted_refresh = 0
        emitted_csnp = 0
        deferred = 0
        t2_refresh = False

        # ---- T0：把令牌桶允许的 change LSA 装包发出 ------------------------ #
        if tier == TIER_T0:
            ready: list[LSA] = []
            waiting: list[LSA] = []
            for lsa in list(self._outbox):
                size = lsa.size_bytes()
                if self.bucket.can_take(size):
                    self.bucket.take(size)
                    ready.append(lsa)
                else:
                    waiting.append(lsa)
            if waiting:
                # 超限排队合并，不丢状态，下个周期再试。
                waiting = self._compact_outbox(waiting)
                deferred = len(waiting)
                self.bucket.defer(deferred)
                self._m["token_deferred"] += deferred
            self._outbox = deque(waiting)
            # 预留 64B 给 tick 追加的 tier / sent_at 元数据，保证成品仍 ≤1400B。
            built, oversize = pack_lsas(ready, self.cfg.max_packet_bytes - 64)
            if oversize:
                self._m["oversize_deferred"] += len(oversize)
                # 单条超限：保留完整 LSA 排队（不切半条），下周期合并后再试。
                for body in oversize:
                    self._outbox.append(LSA.from_dict(body))
            for i, pkt in enumerate(built):
                pkt["tier"] = TIER_T0
                pkt["sent_at"] = at + i * self.cfg.lsa_pacing_s  # LSA Pacing 组步调
                packets.append(pkt)
            emitted_change = len(ready)
            self._m["emitted_change_lsa"] += emitted_change
            self._m["t0_events"] += 1
            for lsa in ready:
                self._last_emit_at[lsa.identity()] = at
                if self.lsdb.install(lsa, at):
                    self._last_sync_at = at

        # ---- T2：1800s ±20% 全量对账，只发 digest/CSNP，不发状态体 --------- #
        if at >= self._next_t2_at:
            t2_refresh = True
            tier = TIER_T2
            header = self.build_digest(kind=REFRESH, now=at)
            packets.append(header)
            emitted_refresh += 1
            for csnp in self.build_csnp(now=at, kind=REFRESH):
                packets.append(csnp)
                emitted_csnp += 1
            self._m["emitted_refresh"] += emitted_refresh
            self._m["emitted_csnp_packets"] += emitted_csnp
            self._m["t2_events"] += 1
            self._last_t2_at = at
            self._last_sync_at = at
            self._last_tier = TIER_T2
            self._last_reason = "T2 periodic reconciliation (digest only)"
            self._next_t2_at = self._jitter_deadline(at, self.cfg.ls_refresh_s)
            self._n_changes_since_digest = 0

        # ---- T1：60s 起步的 2^n 退避摘要；空闲无变更则逐步拉长 ------------- #
        if at >= self._next_t1_at:
            if not t2_refresh:
                tier = TIER_T1
            packets.append(self.build_digest(kind=DIGEST, now=at))
            emitted_digest += 1
            self._m["emitted_digest"] += emitted_digest
            self._m["t1_events"] += 1
            self._last_t1_at = at
            self._idle_rounds += 1
            interval = backoff_interval(self._idle_rounds,
                                        self.cfg.multicast_base_s,
                                        self.cfg.multicast_max_s)
            self._next_t1_at = self._jitter_deadline(at, interval)
            if not t2_refresh:
                self._last_tier = TIER_T1
                self._last_reason = "T1 digest advertisement (no state body)"
            self._n_changes_since_digest = 0

        # ---- Flap 抑制期的汇总（只发 digest，拉长间隔） --------------------- #
        if self._next_flap_summary_at is not None and at >= self._next_flap_summary_at:
            if self._suppressed_identities:
                packets.append(self.build_digest(kind=DIGEST, now=at, flap_summary=True))
                self._m["flap_summaries"] += 1
                self._m["emitted_digest"] += 1
                self._next_flap_summary_at = self._jitter_deadline(
                    at, self.cfg.multicast_base_s * 2)
            else:
                self._next_flap_summary_at = None

        # 兜底硬不变量：本模块永远不发全量状态体、也永不直接下 Git 拉取指令。
        for pkt in packets:
            if pkt.get("type") in ("lsa_digest", "lsa_refresh", "lsa_csnp"):
                _assert_digest_packet(pkt)
            size = packet_bytes(pkt)
            if size > self.cfg.max_packet_bytes:
                raise AssertionError(f"报文 {pkt.get('type')} 超过 "
                                     f"{self.cfg.max_packet_bytes}B: {size}B")
        self._m["emitted_packets"] += len(packets)
        self._m["emitted_bytes"] += sum(packet_bytes(p) for p in packets)

        return {
            "now": at, "tier": tier, "packets": packets,
            "emitted_change_lsa": emitted_change,
            "emitted_digest": emitted_digest, "emitted_refresh": emitted_refresh,
            "emitted_csnp_packets": emitted_csnp,
            "full_state_transfers": 0, "git_pull_instructions": 0,
            "deferred": deferred, "oversize": self._m["oversize_deferred"],
            "t2_refresh": t2_refresh,
            "next_t1_at": self._next_t1_at, "next_t2_at": self._next_t2_at,
            "backoff_interval_s": backoff_interval(
                self._idle_rounds, self.cfg.multicast_base_s, self.cfg.multicast_max_s),
        }

    def _collect_pending(self, now: float) -> None:
        """合并窗口到期 -> 合成 1 条 change LSA。

        三道闸按顺序协作，互不打架：
        合并窗口（N 次变更 -> 1 条） -> MinLSInterval（同 key 5s 内不重发）
        -> Flap Damping（反复泛洪则只发汇总 + 拉长间隔，字段级 diff 累积不丢）。
        """
        for ident, p in list(self._pending.items()):
            if now < (p.first_at + self.cfg.aggregation_window_s):
                continue
            if not min_ls_interval_ok(now, self._last_emit_at.get(ident), self.cfg):
                continue
            snap = self.flap.record(ident, now, p.diff)
            if snap["suppressed"]:
                self._suppressed_identities.add(ident)
                self._m["flap_suppressed"] += 1
                if self._next_flap_summary_at is None:
                    # 「只发汇总并拉长间隔」：汇总间隔拉长为基线 2 倍（120s ±20%），
                    # 逐条 change LSA 的间隔另按 2^n 拉长（见
                    # FlapDamping.snapshot()["interval_multiplier"]）。
                    # 若直接用 2^n 乘子做汇总周期，惩罚一高就永远等不到汇总，
                    # 反而把「只发汇总」这条闸变成「什么都不发」。
                    base = self.cfg.multicast_base_s * 2
                    self._next_flap_summary_at = self._jitter_deadline(now, base)
                # 状态不丢：diff 已由 record 累积进 pending_diff，等恢复后补发。
                del self._pending[ident]
                continue
            # 未抑制：把抑制期间累积的字段级 diff 一并合并，保证最终状态完整。
            merged = self.flap.drain_pending(ident)
            merged.update(p.diff)
            lsa = self._new_lsa(ident, merged, now, head_sha=p.head_sha)
            self._enqueue(lsa)
            del self._pending[ident]

            if ident in self._suppressed_identities:
                self._suppressed_identities.discard(ident)
                self._m["flap_recovered"] += 1

    def _maybe_recover_flap(self, now: float) -> None:
        """抑制解除后，把期间累积的最终 diff 合成 1 条 LSA 补发（状态不丢）。"""
        for ident in list(self._suppressed_identities):
            if self.flap.is_suppressed(ident, now):
                continue
            diff = self.flap.drain_pending(ident)
            self._suppressed_identities.discard(ident)
            self._m["flap_recovered"] += 1
            if diff:
                self._absorb(ident, diff, now)

    def _drain_deferred_incoming(self, now: float) -> None:
        """MinLSArrival 到期后，把被节流的新状态装进 LSDB（**不丢状态**）。"""
        if not self._deferred_incoming:
            return
        rest: deque[LSA] = deque()
        for lsa in self._deferred_incoming:
            ident = lsa.identity()
            if (min_ls_arrival_ok(now, self._arrival_at.get(ident), self.cfg)
                    and self.lsdb.is_newer(lsa)):
                if self.lsdb.install(lsa, now):
                    self._m["accepted_incoming"] += 1
                    self._arrival_at[ident] = now
                    self._last_sync_at = now
            elif self.lsdb.is_newer(lsa):
                rest.append(lsa)
        self._deferred_incoming = rest

    # -- 公开 API 3/4：摘要 -------------------------------------------------- #
    def build_digest(self, kind: str = DIGEST, now: float | None = None,
                     flap_summary: bool = False,
                     with_entries: bool = False) -> dict[str, Any]:
        """产出 **T1 组播摘要报文**（T-0351 下行直接用）。

        字段与设计文档 §2 / T-0351 一致：
        ``(leader_epoch, seq, digest, head_sha, n_changes, area, project_tag, sent_at)``。
        **不带状态体**、天然 ≤1400B。

        ``with_entries=True`` 时附带 **CSNP 式摘要清单**（每条只有
        ``key/area/seq/epoch/digest/head_sha``，仍无状态体），供 Hello 比对
        seq 缺口；清单放不进单包时标记 ``entries_truncated``，完整清单走
        :meth:`build_csnp` 分片。这样 ``build_digest()`` 既能当线格式报文，
        也能当 CSNP 清单用，且不破坏 T-0351 的 8 字段契约。
        """
        if kind not in (DIGEST, REFRESH):
            raise ValueError("build_digest 的 kind 只能是 digest / refresh")
        at = self._now(now)
        packet = {
            "type": "lsa_refresh" if kind == REFRESH else "lsa_digest",
            "kind": kind,
            "leader_epoch": self.leader_epoch,
            "seq": self.lsdb.seq,
            "digest": self.lsdb.snapshot_digest(),
            "entries_digest": compute_digest(self.lsdb.summaries()),
            "head_sha": self.lsdb.head_sha,
            "n_changes": self._n_changes_since_digest,
            "area": self.area,
            "project_tag": self.project_tag,
            "sent_at": at,
            "flap_summary": bool(flap_summary),
        }
        if with_entries:
            entries = self.lsdb.summaries()
            candidate = dict(packet)
            candidate["entries"] = entries
            if packet_bytes(candidate) <= self.cfg.max_packet_bytes - 32:
                packet["entries"] = entries
                packet["entries_truncated"] = False
            else:
                packet["entries"] = []
                packet["entries_truncated"] = True
                packet["n_entries"] = len(entries)
        _assert_digest_packet(packet)
        if packet_bytes(packet) > self.cfg.max_packet_bytes:
            raise AssertionError("T1/T2 摘要头部超过单包上限")
        return packet

    def build_csnp(self, now: float | None = None,
                   kind: str = REFRESH) -> list[dict[str, Any]]:
        """产出 **IS-IS CSNP 式摘要清单**，按条目边界分片，每包 ≤1400B。

        条目只含 ``(key, area, seq, epoch, digest, head_sha)`` —— 没有 diff、
        没有 progress/leases/nodes。T2 全量对账发的就是它，**不是全量状态**。
        """
        at = self._now(now)
        ptype = "lsa_csnp"
        base = {
            "type": ptype, "kind": kind, "leader_epoch": self.leader_epoch,
            "seq": self.lsdb.seq, "digest": self.lsdb.snapshot_digest(),
            "head_sha": self.lsdb.head_sha, "area": self.area,
            "project_tag": self.project_tag, "sent_at": at, "index": 0, "total": 1,
            "entries": [],
        }
        packets: list[dict[str, Any]] = []
        # 预留 32B：最后回填 index/total 可能变长，不能让成品越过 1400B。
        limit = self.cfg.max_packet_bytes - 32
        current = dict(base)
        current["entries"] = []
        for summary in self.lsdb.summaries():
            trial = current["entries"] + [summary]
            candidate = dict(current)
            candidate["entries"] = trial
            if packet_bytes(candidate) > limit:
                if not current["entries"]:
                    raise AssertionError("单条 CSNP 摘要超过单包上限，且不允许切半条")
                packets.append(current)
                current = dict(base)
                current["entries"] = [summary]
                if packet_bytes(current) > self.cfg.max_packet_bytes:
                    raise AssertionError("单条 CSNP 摘要超过单包上限，且不允许切半条")
            else:
                current["entries"] = trial
        packets.append(current)
        for i, pkt in enumerate(packets):
            pkt["index"] = i
            pkt["total"] = len(packets)
            _assert_digest_packet(pkt)
        return packets

    # -- 公开 API 5：merge_incoming ------------------------------------------ #
    def merge_incoming(self, lsas: Iterable[LSA | dict[str, Any]],
                       now: float | None = None) -> dict[str, Any]:
        """接收一批 LSA：**去重 / 判新旧 / MinLSArrival / MaxAge**，只更新内存 LSDB。

        ``digest`` / ``refresh`` 报文只用来刷新对端水位与 ``remote_sha``，
        不安装 LSA、不产生任何 Git 拉取指令（那是 :meth:`on_hello` 的职责）。
        返回各类计数，方便 T-0352 / T-0351 做回归断言。
        """
        at = self._now(now)
        accepted = dup = old = arrival = max_age = digest_only = deferred = 0
        for raw in lsas:
            lsa = _as_lsa(raw)
            ident = lsa.identity()
            if lsa.kind in (DIGEST, REFRESH):
                self.lsdb.remote_sha = lsa.head_sha or self.lsdb.remote_sha
                self.lsdb.epoch = max(self.lsdb.epoch, lsa.leader_epoch)
                digest_only += 1
                self._last_tier = TIER_T1 if lsa.kind == DIGEST else TIER_T2
                continue
            if lsa.age >= self.cfg.max_age_s:
                max_age += 1
                continue
            newer = self.lsdb.is_newer(lsa)
            cur = self.lsdb.get(ident)
            if not min_ls_arrival_ok(at, self._arrival_at.get(ident), self.cfg):
                # MinLSArrival：1s 内同一 (area,key) 的第二次到达要被节流。
                # 真实新状态（seq 推进）不丢弃，而是延后到 tick 到期再装；
                # 重复 / 旧 LSA 直接丢。既抑制重复泛洪，又满足「不丢状态」。
                if newer and (cur is None or lsa.seq > cur.seq):
                    self._deferred_incoming.append(lsa)
                    deferred += 1
                else:
                    arrival += 1
                continue
            if not newer:
                if cur is None:
                    old += 1
                elif (lsa.leader_epoch, lsa.seq) < (cur.epoch, cur.seq):
                    old += 1
                else:
                    dup += 1
                continue
            if self.lsdb.install(lsa, at):
                accepted += 1
                self._arrival_at[ident] = at
                self._last_sync_at = at
        self._m["accepted_incoming"] += accepted
        self._m["dropped_duplicate"] += dup
        self._m["dropped_old"] += old
        self._m["dropped_min_arrival"] += arrival
        self._m["deferred_min_arrival"] += deferred
        self._m["dropped_max_age"] += max_age
        self._m["digest_incoming"] += digest_only
        return {
            "accepted": accepted, "dropped_duplicate": dup, "dropped_old": old,
            "dropped_min_arrival": arrival, "deferred_min_arrival": deferred,
            "dropped_max_age": max_age,
            "digest_only": digest_only, "lsdb_seq": self.lsdb.seq,
            "full_state_transfers": 0, "git_pull_instructions": 0,
        }

    # -- 公开 API 6/7：on_hello / should_pull -------------------------------- #
    def _decide(self, peer_digest: dict[str, Any] | None) -> dict[str, Any]:
        """Hello 比对的纯判定（不计数、不改状态）。"""
        if peer_digest is None:
            # 根本没有 Hello 到来（空闲）：零 Git 流量、零 HTTP 请求。
            return {"action": NO_OP, "reason": "no hello to compare (idle)",
                    "missing": [], "head_sha_changed": False, "seq_gap": 0}
        if not isinstance(peer_digest, dict) or not peer_digest.get("digest"):
            # 旧节点不带 sd/digest：走全量兼容路径（0.16.05 已验证的滚动升级纪律）
            return {"action": PULL_FULL, "reason": "legacy peer without digest",
                    "missing": [], "head_sha_changed": False, "seq_gap": 0}

        local_digest = self.lsdb.snapshot_digest()
        peer_top = str(peer_digest.get("digest", ""))
        remote_sha = str(peer_digest.get("head_sha", ""))
        head_changed = bool(remote_sha and remote_sha != self.lsdb.head_sha)
        if peer_top == local_digest and not head_changed:
            return {"action": NO_OP, "reason": "digest identical, zero traffic",
                    "missing": [], "head_sha_changed": False, "seq_gap": 0}

        peer_seq = int(peer_digest.get("seq", 0))
        seq_gap = abs(self.lsdb.seq - peer_seq)
        entries = peer_digest.get("entries") or []
        if entries:
            gaps = self.lsdb.seq_gaps(entries)
            missing = [s for g in gaps for s in g["missing_seq"]]
        else:
            missing = list(range(1, min(seq_gap, self.cfg.pull_full_gap_limit) + 1))
        if seq_gap > self.cfg.pull_full_gap_limit:
            return {"action": PULL_FULL, "reason": "seq gap exceeds incremental limit",
                    "missing": [], "head_sha_changed": head_changed, "seq_gap": seq_gap}
        return {
            "action": PULL_INCREMENTAL,
            "reason": "digest differs / seq gap -> incremental pull",
            "missing": missing, "head_sha_changed": head_changed,
            "seq_gap": seq_gap,
        }

    def on_hello(self, peer_digest: dict[str, Any] | None,
                 now: float | None = None) -> dict[str, Any]:
        """比对本地 LSDB 与对端摘要，给出补拉指令。

        * digest 一致 -> ``no-op``（**零 Git 流量、零 HTTP 请求**）；
        * 不一致 / seq 有缺口 -> ``pull_incremental``（带 ``missing`` 清单）；
        * 旧节点无摘要 / 缺口过大 -> ``pull_full``。

        返回体里 **不含任何状态体**，只含指令与水位。
        """
        at = self._now(now)
        decision = self._decide(peer_digest)
        if isinstance(peer_digest, dict) and peer_digest.get("head_sha"):
            self.lsdb.remote_sha = str(peer_digest["head_sha"])
        if decision["action"] != NO_OP:
            self._m["git_pull_instructions"] += 1
            self._last_sync_at = at
        self._last_reason = decision["reason"]
        result = dict(decision)
        result.update({
            "now": at, "lsdb_seq": self.lsdb.seq,
            "head_sha": self.lsdb.head_sha, "remote_sha": self.lsdb.remote_sha,
            "full_state_transfers": 0,
        })
        _assert_digest_packet(result)
        return result

    def should_pull(self, peer_digest: dict[str, Any] | None = None,
                    now: float | None = None) -> bool:
        """T-0353 用：现在**要不要问 Gitea**？

        空闲（``peer_digest=None``）恒 ``False`` —— 30 分钟空闲仿真因此产生
        0 次 Git 拉取指令。只有收到对端摘要且 ``head_sha`` / digest 不一致才为真。
        """
        at = self._now(now)
        decision = self._decide(peer_digest)
        answer = decision["action"] != NO_OP or decision["head_sha_changed"]
        if answer:
            self._m["git_pull_instructions"] += 1
            self._last_sync_at = at
            if isinstance(peer_digest, dict) and peer_digest.get("head_sha"):
                self.lsdb.remote_sha = str(peer_digest["head_sha"])
        return bool(answer)

    def set_head_sha(self, sha: str) -> None:
        """T-0353 / T-0351 喂入本地 origin/main 头（纯内存）。"""
        self.lsdb.head_sha = str(sha or "")

    def set_remote_head_sha(self, sha: str) -> None:
        """T-0351 组播通告的头（只更新水位，不触发任何拉取）。"""
        self.lsdb.remote_sha = str(sha or "")

    # -- 公开 API 8：sync_health / status ------------------------------------ #
    def sync_health(self) -> dict[str, Any]:
        """T-0353 ``status --json`` 的 ``sync_health`` 对象。

        含设计文档要求的 ``lsdb_seq / last_sync_at / head_sha / remote_sha /
        behind / tier / reason``；额外给 ``seq`` 别名，供 T-0354 看板
        「仅当 seq 变化才重写 status.json」直接读。
        """
        return {
            "seq": self.lsdb.seq,
            "lsdb_seq": self.lsdb.seq,
            "last_sync_at": self._last_sync_at,
            "head_sha": self.lsdb.head_sha,
            "remote_sha": self.lsdb.remote_sha,
            "behind": bool(self.lsdb.remote_sha and
                           self.lsdb.remote_sha != self.lsdb.head_sha),
            "tier": self._last_tier or TIER_T2,
            "reason": self._last_reason,
            "digest": self.lsdb.snapshot_digest(),
            "n_entries": len(self.lsdb),
        }

    def status(self) -> dict[str, Any]:
        """全量指标（T-0355 / T-0356 采集用；口径固定，不随测试放宽）。"""
        out = {
            "originator": self.originator, "area": self.area,
            "project_tag": self.project_tag, "leader_epoch": self.leader_epoch,
            "lsdb_seq": self.lsdb.seq, "seq": self.lsdb.seq,
            "lsdb_entries": len(self.lsdb), "head_sha": self.lsdb.head_sha,
            "remote_sha": self.lsdb.remote_sha,
            "pending": len(self._pending), "outbox": len(self._outbox),
            "idle_rounds": self._idle_rounds,
            "backoff_interval_s": backoff_interval(
                self._idle_rounds, self.cfg.multicast_base_s, self.cfg.multicast_max_s),
            "next_t1_at": self._next_t1_at, "next_t2_at": self._next_t2_at,
            "last_t1_at": self._last_t1_at, "last_t2_at": self._last_t2_at,
            "last_sync_at": self._last_sync_at,
            "jittered_deadlines": list(self._jittered_deadlines),
            "jitter": self.cfg.jitter,
            "last_tier": self._last_tier, "last_reason": self._last_reason,
            "suppressed_keys": self.flap.suppressed_keys(self._clock()),
            "constants": self.cfg.constants(),
        }
        out.update(self._m)
        return out


# --------------------------------------------------------------------------- #
# 模块级便捷 API：委托给一个懒创建的默认 core。
# 兄弟卡既可以 ``hive_lsa.note_change(...)`` 直接用，也可以自己 ``LSACore``
# 多实例（推荐，避免共享全局状态）。默认 core 在 import 时**不会**被创建。
# --------------------------------------------------------------------------- #

_default_core: LSACore | None = None


def get_default_core() -> LSACore:
    global _default_core
    if _default_core is None:
        _default_core = LSACore()
    return _default_core


def set_default_core(core: LSACore) -> None:
    """注入一个自带 fake clock / fake rng 的 core（测试与集成装配用）。"""
    global _default_core
    _default_core = core


def reset_default_core() -> None:
    global _default_core
    _default_core = None


def note_change(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return get_default_core().note_change(*args, **kwargs)


def tick(now: float | None = None) -> dict[str, Any]:
    return get_default_core().tick(now)


def on_hello(peer_digest: dict[str, Any] | None,
             now: float | None = None) -> dict[str, Any]:
    return get_default_core().on_hello(peer_digest, now)


def build_digest(kind: str = DIGEST, now: float | None = None,
                 flap_summary: bool = False,
                 with_entries: bool = False) -> dict[str, Any]:
    return get_default_core().build_digest(kind, now, flap_summary, with_entries)


def build_csnp(now: float | None = None,
               kind: str = REFRESH) -> list[dict[str, Any]]:
    """完整 CSNP 摘要清单（分片、每包 ≤1400B）；T2 对账用它。"""
    return get_default_core().build_csnp(now, kind)


def merge_incoming(lsas: Iterable[LSA | dict[str, Any]],
                   now: float | None = None) -> dict[str, Any]:
    return get_default_core().merge_incoming(lsas, now)


def should_pull(peer_digest: dict[str, Any] | None = None,
                now: float | None = None) -> bool:
    return get_default_core().should_pull(peer_digest, now)


def sync_health() -> dict[str, Any]:
    return get_default_core().sync_health()


def status() -> dict[str, Any]:
    return get_default_core().status()
