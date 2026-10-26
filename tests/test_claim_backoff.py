"""claim 抢锁失败做带抖动的指数退避（T-0146）。

关键约定：
* push 的原子性不动 —— 非快进仍然是最高优先级的「抢锁失败」信号，绝不 force；
* 第一次 non-fast-forward 后如果确认没人持有 claim，就 fetch + 退避重试；
* 如果 fetch 后能确认 claim 已属于别人，立即失败、不进退避；
* 重试次数有上限，超了抛错，并把当前持有者带进错误信息。
"""
from __future__ import annotations

import pytest

import hive
from hive import HiveError, Worker


def _worker(world, name: str = "w1") -> Worker:
    """一个不依赖真实 ~/.hive 的 manual worker（claim 不需要跑 agent）。"""
    world.add_agent(name, "manual")
    world.add_task("T-0001", verify=["true"])
    return Worker(str(world.hive1), name,
                  hive_state=str(world.root / f"state-{name}"))


def _patch_push(monkeypatch, outcomes):
    """拦截 git push：按顺序消费 outcomes（异常=模拟失败，None=真推）。

    只拦 push，fetch 等其它网络操作照常走真实 git，避免测试把整个网络层都假掉。
    outcomes 用完后重复最后一个。
    """
    real = hive.git_net
    seen = {"push": 0}

    def fake(args, cwd, check=True, input=None):
        if args and args[0] == "push":
            idx = min(seen["push"], len(outcomes) - 1)
            outcome = outcomes[idx]
            seen["push"] += 1
            if outcome is not None:
                raise outcome
        return real(args, cwd, check=check, input=input)

    monkeypatch.setattr(hive, "git_net", fake)
    return seen


# -- 退避后抢到：第一次 non-fast-forward，第二次成功 ------------------------- #
def test_non_fast_forward_once_then_success_retries_exactly_once(
        world, monkeypatch):
    w = _worker(world)
    sleeps: list[float] = []
    monkeypatch.setattr(hive, "_claim_sleep", lambda s: sleeps.append(s))
    seen = _patch_push(monkeypatch, [
        HiveError("! [rejected] claim/T-0001 -> claim/T-0001 (non-fast-forward)"),
        None,
    ])

    assert w.claim("T-0001") is True
    assert seen["push"] == 2, "应当是「首发 1 次 + 重试 1 次」"
    assert len(sleeps) == 1, "两次 push 之间必须且只能退避一次"


# -- 确认有主：不重试，立刻把主人带出来 -------------------------------------- #
def test_confirmed_owner_is_not_retried(world, monkeypatch):
    w = _worker(world)
    sleeps: list[float] = []
    monkeypatch.setattr(hive, "_claim_sleep", lambda s: sleeps.append(s))
    seen = _patch_push(monkeypatch, [
        HiveError("! [rejected] claim/T-0001 (non-fast-forward)"),
    ])

    def fake_claim_info(self, tid):
        # push 之前看不到主；push 失败、fetch 之后能看到 —— 正是「被抢」的形状
        return {"agent": "rival"} if seen["push"] else None

    monkeypatch.setattr(hive.Hive, "claim_info", fake_claim_info)

    with pytest.raises(HiveError) as e:
        w.claim("T-0001")
    assert "rival" in str(e.value)
    assert seen["push"] == 1, "确认有主后不能再打第二次 push"
    assert sleeps == [], "确认有主是正常竞争失败，不该进退避"


# -- 一直 non-fast-forward：次数用尽后抛错，并报出谁拿了卡 ------------------- #
def test_persistent_non_fast_forward_raises_with_holder_after_cap(
        world, monkeypatch):
    w = _worker(world)
    monkeypatch.setenv("HIVE_CLAIM_MAX_RETRIES", "3")
    sleeps: list[float] = []
    monkeypatch.setattr(hive, "_claim_sleep", lambda s: sleeps.append(s))
    seen = _patch_push(monkeypatch, [
        HiveError("! [rejected] claim/T-0001 (non-fast-forward)"),
    ])

    def fake_claim_info(self, tid):
        # 前几次 fetch 还看不到主人；最后一次刷新后才暴露出来
        return {"agent": "rival"} if seen["push"] >= 4 else None

    monkeypatch.setattr(hive.Hive, "claim_info", fake_claim_info)

    with pytest.raises(HiveError) as e:
        w.claim("T-0001")
    assert seen["push"] == 4, "默认 3 次重试 = 总共 4 次 push，不能多也不能少"
    assert len(sleeps) == 3
    assert "rival" in str(e.value), f"错误里必须说明谁拿了卡: {e.value}"


# -- 网络层失败：也走退避，但总等待时间受「次数 × 单次上限」约束 ------------- #
def test_retry_wait_is_bounded_by_attempts_and_max_delay(world, monkeypatch):
    w = _worker(world)
    monkeypatch.setenv("HIVE_CLAIM_BACKOFF_BASE", "0.1")
    monkeypatch.setenv("HIVE_CLAIM_BACKOFF_MAX", "0.3")
    monkeypatch.setenv("HIVE_CLAIM_BACKOFF_JITTER", "0.5")
    monkeypatch.setenv("HIVE_CLAIM_MAX_RETRIES", "2")
    sleeps: list[float] = []
    monkeypatch.setattr(hive, "_claim_sleep", lambda s: sleeps.append(s))
    seen = _patch_push(monkeypatch, [
        HiveError("fatal: unable to connect: connection reset by peer"),
    ])

    with pytest.raises(HiveError):
        w.claim("T-0001")

    assert seen["push"] == 3
    assert len(sleeps) == 2
    assert sleeps == sorted(sleeps), f"退避间隔应递增: {sleeps}"
    assert all(0 < d <= 0.3 for d in sleeps), sleeps
    assert sum(sleeps) <= 2 * 0.3, f"总等待超上限: {sum(sleeps)}"


# -- 退避函数本身：指数递增 + 正抖动 + 单次不超过上限 ------------------------ #
def test_backoff_delay_is_monotonic_and_jittered(monkeypatch):
    monkeypatch.setenv("HIVE_CLAIM_BACKOFF_BASE", "0.5")
    monkeypatch.setenv("HIVE_CLAIM_BACKOFF_MAX", "8")
    monkeypatch.setenv("HIVE_CLAIM_BACKOFF_JITTER", "0.5")
    uniforms: list[tuple[float, float]] = []

    class FakeRandom:
        def uniform(self, lo, hi):
            uniforms.append((lo, hi))
            return hi / 2

    # 只替换 hive 模块里的随机源，不动全局 random（避免影响 pytest/tempfile）
    monkeypatch.setattr(hive, "random", FakeRandom())
    delays = [hive.claim_backoff_delay(i) for i in (1, 2, 3)]

    assert delays == sorted(delays) and len(set(delays)) == 3, delays
    # 每次都吃了正抖动：比纯指数基值大
    assert delays[0] > 0.5 and delays[1] > 1.0 and delays[2] > 2.0, delays
    assert all(lo == 0.0 and hi > 0.0 for lo, hi in uniforms), uniforms
    assert all(d <= hive.CLAIM_BACKOFF_MAX_S for d in delays), delays
    assert sum(delays) <= 3 * hive.CLAIM_BACKOFF_MAX_S


def test_backoff_env_overrides(monkeypatch):
    monkeypatch.setenv("HIVE_CLAIM_BACKOFF_BASE", "0.25")
    monkeypatch.setenv("HIVE_CLAIM_BACKOFF_MAX", "2")
    monkeypatch.setenv("HIVE_CLAIM_BACKOFF_JITTER", "0.25")
    monkeypatch.setenv("HIVE_CLAIM_MAX_RETRIES", "1")

    assert hive.claim_backoff_config() == {
        "base": 0.25, "max": 2.0, "jitter": 0.25, "retries": 1,
    }


# -- 超时（T-0145 的 timed_out 标记）也属于值得重试的网络层失败 ------------- #
def test_timeout_error_is_retried(world, monkeypatch):
    w = _worker(world)
    sleeps: list[float] = []
    monkeypatch.setattr(hive, "_claim_sleep", lambda s: sleeps.append(s))
    timeout = HiveError("命令超时（1 秒内未返回，已终止）: git push")
    timeout.timed_out = True
    seen = _patch_push(monkeypatch, [timeout, None])

    assert w.claim("T-0001") is True
    assert seen["push"] == 2
    assert len(sleeps) == 1


# -- 抢锁失败不能把卡留成「owner 是我、租约在别人手上」 ---------------------- #
def test_failed_take_does_not_leave_half_assigned_card(world):
    world.add_agent("rival", "manual")
    world.add_agent("w1", "manual")
    world.add_task("T-0001", verify=["true"])
    world.hive("claim", "T-0001", "--agent", "rival")

    p = world.hive("take", "T-0001", "--agent", "w1", check=False)
    assert p.returncode != 0

    world.sync()
    card = world.card("T-0001")
    assert card["state"] == "ready", f"失败的 take 不该改卡状态: {card}"
    assert card.get("owner") is None, f"失败的 take 不该写 owner: {card}"


# -- 退避中被打断：异常原样冒泡，本地 claim 半成品要清掉 -------------------- #
def test_interrupt_during_backoff_cleans_claim_artifacts(world, monkeypatch):
    w = _worker(world)
    artifact = world.hive1 / ".hive" / "claim" / "T-0001.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("{}", encoding="utf-8")

    _patch_push(monkeypatch, [HiveError("connection reset by peer")])

    def interrupted(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(hive, "_claim_sleep", interrupted)
    with pytest.raises(KeyboardInterrupt):
        w.claim("T-0001")
    assert not artifact.exists(), "中断后不该留下 .hive/claim 半成品"
