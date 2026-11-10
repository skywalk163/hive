"""控制面租约：90s TTL / 30s 续约 / 30s 宽限 / expiring 提示（D4）。

对应实现约定 §3.7；全部用注入时钟，不 sleep、不碰网络、不碰 Git。
"""

from __future__ import annotations

from hive_control.config import Limits
from hive_control.lease import LeaseBook
from hive_control.model import StateEntry

UPDATED_AT = 1_700_000_000_000


class Clock:
    """可手动推进的注入时钟（秒）。"""

    def __init__(self, value: float = 0.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> float:
        self.value += float(delta)
        return self.value


def entry(**kw) -> StateEntry:
    base = dict(
        project="proj",
        tid="T-0308",
        version=1,
        incarnation=7,
        owner_agent="agent-a",
        state="claimed",
        dna=False,
        updated_at=UPDATED_AT,
    )
    base.update(kw)
    return StateEntry(**base)


def book(clock: Clock, **kw) -> LeaseBook:
    return LeaseBook(limits=Limits(), clock=clock, **kw)


# --------------------------------------------------------------------------
# 90s TTL：超时才提示，提示是副本而不是状态安装
# --------------------------------------------------------------------------

def test_claimed_lease_expires_after_ttl_without_install():
    clock = Clock()
    lb = book(clock)
    original = entry()
    lb.note(original, local=True)

    clock.advance(89.9)
    assert lb.expired() == []

    clock.advance(0.2)  # t=90.1
    hints = lb.expired()
    assert len(hints) == 1
    hint = hints[0]
    # D4：只换 state/urgent，版本与归属原样保留（不安装、不写回 Git）
    assert (hint.incarnation, hint.version) == (
        original.incarnation, original.version)
    assert hint.state == "expiring"
    assert hint.urgent is True
    assert hint.owner_agent == original.owner_agent
    assert hint.updated_at > original.updated_at


def test_local_lease_before_ttl_is_not_expired():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=True)
    clock.advance(45)
    assert lb.expired() == []
    assert lb.status()["leases"] == 1


# --------------------------------------------------------------------------
# 30s 续约：只续本地租约，版本不变、时间刷新
# --------------------------------------------------------------------------

def test_renewals_due_every_30s_for_local_lease():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=True)

    clock.advance(29.9)
    assert lb.renewals_due() == []

    clock.advance(0.1)  # t=30
    due = lb.renewals_due()
    assert len(due) == 1
    assert due[0].version == 1
    assert due[0].state == "claimed"
    assert due[0].updated_at > UPDATED_AT

    lb.mark_renewed(due[0])
    clock.advance(29.9)
    assert lb.renewals_due() == []
    clock.advance(0.1)  # t=60
    assert len(lb.renewals_due()) == 1


def test_remote_lease_is_never_renewed_locally():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=False)
    clock.advance(30)
    assert lb.renewals_due() == []


def test_renewal_keeps_version_and_only_refreshes_time():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=True)

    clock.advance(20)
    renewal_copy = entry(updated_at=UPDATED_AT + 20_000)
    lb.note(renewal_copy, local=True, renewal=True)

    # 续约把计时推到 t=20：下个 30s 点在 t=50。
    clock.advance(29.9)
    assert lb.renewals_due() == []
    clock.advance(0.1)  # t=50
    due = lb.renewals_due()
    assert len(due) == 1
    assert due[0].version == 1
    assert due[0].updated_at > UPDATED_AT + 20_000


def test_renewal_resets_expiry_timer():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=False)

    clock.advance(80)
    lb.note(entry(), local=False)  # 同版本上行续约
    clock.advance(9.9)  # t=89.9，距续约 9.9s
    assert lb.expired() == []

    clock.advance(80.2)  # t=170.1，距续约 90.1s
    assert len(lb.expired()) == 1


# --------------------------------------------------------------------------
# 提示节流：同一租约每 ttl 最多一次
# --------------------------------------------------------------------------

def test_expiring_hint_at_most_once_per_ttl():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=True)

    clock.advance(90.1)
    assert len(lb.expired()) == 1
    assert lb.expired() == []          # 同一时刻不重复
    clock.advance(89.9)                # t=180.0，距上次提示 89.9s
    assert lb.expired() == []
    clock.advance(0.2)                 # t=180.2，满一个 ttl
    assert len(lb.expired()) == 1


# --------------------------------------------------------------------------
# 切主 30s 宽限
# --------------------------------------------------------------------------

def test_leader_change_grants_30s_grace():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=False)

    clock.advance(90.1)                # 已超时，但马上切主
    lb.on_leader_change()

    clock.advance(29.9)                # t=120.0，宽限内
    assert lb.expired() == []
    clock.advance(0.2)                 # t=120.2，宽限结束
    assert len(lb.expired()) == 1
    assert lb.status()["grace_remaining"] == 0.0


def test_explicit_grace_until_is_honoured():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=False)
    clock.advance(100)
    assert lb.expired(grace_until=105) == []
    clock.advance(5.1)
    assert len(lb.expired(grace_until=105)) == 1


def test_custom_windows_for_tests():
    clock = Clock()
    lb = book(clock, lease_ttl=10, grace_period=5)
    lb.note(entry(), local=True)

    clock.advance(3.4)
    assert len(lb.renewals_due()) == 1      # ttl/3 ≈ 3.33

    lb.on_leader_change()                   # t=3.4，宽限到 t=8.4
    clock.advance(4.9)                      # t=8.3
    assert lb.expired() == []
    clock.advance(0.2)                      # t=8.5，宽限已过但未到 ttl
    assert lb.expired() == []
    clock.advance(1.7)                      # t=10.2 > ttl
    assert len(lb.expired()) == 1


# --------------------------------------------------------------------------
# 清理与版本语义
# --------------------------------------------------------------------------

def test_released_entry_clears_lease():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=True)
    lb.note(entry(version=2, state="released"), local=True)

    assert lb.status()["leases"] == 0
    assert lb.renewals_due() == []
    assert lb.expired() == []


def test_newer_version_starts_new_lease_period():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=True)

    clock.advance(90.1)
    assert len(lb.expired()) == 1           # v1 提示

    lb.note(entry(version=2, state="running", updated_at=UPDATED_AT + 100_000),
            local=True)
    clock.advance(89.9)                     # 距 v2 不到 90s
    assert lb.expired() == []

    clock.advance(0.2)
    hints = lb.expired()
    assert len(hints) == 1
    assert hints[0].version == 2
    assert hints[0].state == "expiring"


def test_stale_version_does_not_reset_or_hint():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(version=3), local=False)

    clock.advance(50)
    lb.note(entry(version=1), local=False)  # 旧版本，必须忽略

    clock.advance(40.1)                     # t=90.1，仍按 v3 计时
    hints = lb.expired()
    assert len(hints) == 1
    assert hints[0].version == 3


def test_drop_removes_lease():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=True)
    lb.drop("proj", "T-0308")
    clock.advance(100)
    assert lb.renewals_due() == []
    assert lb.expired() == []
    assert lb.status()["leases"] == 0


def test_status_reports_windows_and_counts():
    clock = Clock()
    lb = book(clock)
    lb.note(entry(), local=True)
    lb.note(entry(tid="T-9999", owner_agent="agent-b"), local=False)

    clock.advance(30.1)
    status = lb.status()
    assert status["leases"] == 2
    assert status["local"] == 1
    assert status["renewals_due"] == 1
    assert status["expired"] == 0
    assert status["hinted"] == 0
    assert status["lease_ttl"] == 90.0
    assert status["renewal_interval"] == 30.0
    assert status["grace_period"] == 30.0
