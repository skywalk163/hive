# 设计：worker 内并行执行多张 exec 卡

> 状态：**设计稿**（T-0161）。本文件不改动任何代码，实现留给 T-0162。
> 目标读者：写 T-0162 的人。读到最后一节应该能直接开工。
> 代码行号取自写作时的 `hive.py`（Worker 约 2328 行），行号会漂，**以函数名为准**。

---

## 0. 一句话结论

> **推荐方案：`concurrent.futures.ThreadPoolExecutor` + 每任务一条心跳线程 + 一把 worker 级 git 串行锁，并发上限直接取 agent 配置里的 `max_concurrency`（默认仍为 1）。**

配套三条硬约束：

1. **有槽位才 claim**：worker 每轮最多 claim 到「自己能同时跑」的数量，绝不先把 inbox 全占了再排队。
2. **git 操作串行**：并行的是「agent 子进程」，不是「git」。所有对共享协调仓/共享代码仓元数据的写，
   统一走一把 worker 内全局锁。
3. **默认 1**：默认行为和今天完全一致，`max_concurrency` 是显式打开的阀门。

为什么不需要进程池、为什么 concurrency 不能无限开，见 `## 模型`；心跳和锁怎么改，见 `## 租约与心跳`。

---

## 模型

### 1.1 现状：worker 为什么是串行的

`Worker.loop`（现 2804 行）一轮里做的事：

```python
self.h.sync_main()
pending = self.pending()
for tid, _card in pending:
    if self.claim(tid):
        self.run_task(tid)      # ← 阻塞到这张卡跑完才进下一张
```

`run_task`（2567 行）一次服务一张卡：建 worktree → 起心跳 → 调 agent（`_exec`，2749 行）→
跑 verify → 写结果 → 删 worktree。`_exec` 是 `subprocess.Popen` + `communicate(timeout=...)`，
一次几十秒到几十分钟。单台机器上 exec agent 的吞吐被钉死在 1。

### 1.2 三个候选

| 方案 | 做法 | 评价 |
|---|---|---|
| A. 多 worker 进程 | 同一 agent 起 N 个 `hive.py work` 进程，各自独立 clone + `HIVE_STATE` | **能用但要绕**：多个进程抢同一个 `inbox/<name>/` 里的同一批卡，`claim` 的原子抢锁能兜住不重复，但每个进程都要独立 clone、独立 state、人得维护 N 份启动脚本，且互相不知道对方在跑什么。运维成本高，不是 workers 内部并行。 |
| B. **线程池**（推荐） | 一个 worker 进程内 `ThreadPoolExecutor(max_workers=N)`，一个 future 一张卡 | **选它**。详见 1.3。 |
| C. 进程池 | `ProcessPoolExecutor`，任务函数 fork 出去 | **弃**：`Worker` 持有 `Hive`（含 git repo 路径、缓存）、日志句柄、`threading.Event`，跨进程序列化代价大；Windows 上 spawn 语义又是一套；真出了 core dump，日志还清不干净。收益在当前负载下为零。 |

### 1.3 推荐：线程池

**理由是 workload 的形状，不是信仰。**

- exec 模式下 worker 自己几乎不消耗 CPU：它把活丢给 `subprocess`（LLM agent CLI），然后阻塞在
  `communicate()` 上等 pipe。**执行是操作系统级并行的，GIL 不在关键路径上。**
- manual 模式下 worker 更闲：`while time.time() < deadline: sleep(5)` 等人写 `summary.md`（2646 行）。
- 线程池让 N 张卡共享同一个 `Worker` 实例：日志、`Hive` 对象、`state/` 路径、心跳注册表都是一处，
  不需要跨进程同步。
- `_exec` 已经 `start_new_session=True`（2760 行），杀的时候用 `os.killpg` 整组杀（2776 行）——
  **超时杀进程这件事在多线程里照样成立**，不需要进程池来提供隔离。

**什么时候该换进程池**（留给以后，T-0162 不做）：

- agent 从「外部子进程」变成「worker 内嵌 Python 调用」——那时才是真 GIL 竞争。
- worker 自己开始做 CPU 密集的事（大仓全量 diff、结构化索引）。
- 需要 OS 级别的崩溃隔离（某个插件 segfault 会带走整个进程）。
  届时形态是：每个 worker 进程一张卡，外加一个进程外的 supervisor。

### 1.4 并发数从哪来

单一来源，覆盖顺序：

```
CLI --max-concurrency  >  环境变量 HIVE_MAX_CONCURRENCY  >  agents/<name>.json 的 max_concurrency  >  1
```

- `agents/<name>.json` 的字段已存在（`agent add --max-concurrency`，CLI 3280 行），
  `whoami` 也已经把它吐出来了（`mcp_server.py:435`）。
- **默认必须是 1**：保证不配置就和今天完全一样（见 `## 隔离保证`「worktree 不隔离的东西」一节，
  并行会放大仓库外副作用）。

### 1.5 目标形态

```text
 Worker（单进程，单轮询线程）
 ├── sync_main() + pending()        ← 主线程，每轮一次
 ├── _git_lock（全局 RLock）        ← 所有 git 读写都从这里过
 ├── ThreadPoolExecutor(max_workers=N)
 │    ├── future(T-0201) ─┬─ claim → worktree add → _exec(agent) → verify → commit/push → worktree remove
 │    │                   └─ hb-T-0201 心跳线程（daemon）
 │    ├── future(T-0202) ─┬─ ...同构...
 │    │                   └─ hb-T-0202
 │    └── future(T-0203) ─┴─ ...
 └── _writers_lock        ← claim/commit/push 与别人的错开
```

---

## 租约与心跳

### 2.1 现状：`lease_heartbeat` 是按「一 worker 一卡」写的

`lease_heartbeat`（2485 行）本身已经是「每次调用起一条新线程」的形状：

```python
stop = threading.Event()
interval = max(5, lease // 3) * 60
t = threading.Thread(target=self._heartbeat_loop, args=(tid, interval, stop),
                     name=f"hb-{tid}", daemon=True)
t.start()
try:
    yield
finally:
    stop.set()
    t.join(timeout=5)
```

这在串行时是对的：同一时刻只有一条 `hb-*` 在跑。并行后会同时有 N 条，**暴露三个问题**。

### 2.2 并行后必须解决的三件事

**(1) N 条心跳打同一个 git 仓库。**
`_heartbeat_loop` → `_refresh_lease`（2510 行）每次干两件重活：`self.h.fetch()`（网络往返 + 远端引用更新 + index 更新）
和 `commit_file_to_ref(..., "origin/claim/<tid>")`。N 条线程各自 `fetch` 同一个远端，既是 N 倍无谓网络开销，
又会碰 `packed-refs` / index 锁。这是**最容易先炸的一处**。

> 修法：**worker 级一把 `_git_lock`**，所有 git 读写（含心跳续租、claim push、结果 push、
> worktree add/remove/prune）都从这把锁过。锁内只做几十毫秒的 git 操作，不影响并发度。
> 同时按 tid 加随机抖动（`interval * (0.8~1.2)`），让 N 条心跳不要整点齐射。

**(2) 主进程退出时心跳必须全部停止。**
现在靠 `daemon=True` 托底（进程死线程就死），但「托底」不等于「正确」：

- `finally` 里 `t.join(timeout=5)` 最多等 5 秒，超时就放它自己挂着；
- Ctrl-C / SIGTERM 打在轮询主线程上时，**正在跑的 future 会被 `KeyboardInterrupt` 打断**，
  `finally` 能不能执行取决于中断落在哪条语句上；
- 更要命的是：租约的 `lease_until` 已经被推到未来 `lease_minutes`，worker 真被 `kill -9` 之后，
  这张卡要等到租约过期才会被 watchdog 回收。

> 修法：
> - 维护 Worker 级心跳注册表：`_hb: dict[str, tuple[Thread, Event]]`，加解锁保护，线程安全。
> - 提供**显式** `start_heartbeat(tid) -> None` / `stop_heartbeat(tid) -> None`，
>   不再用 `with` 包住整个任务体——future 里的控制流跨过 `as_completed` 点，contextmanager 可读性很差。
>   `lease_heartbeat` 保留作薄封装（现在 `take` / 串行路径还在用），内部改为调用这一对 API。
> - worker 注册 SIGINT/SIGTERM handler + `finally` 全局清理：广播 `stop.set()` → `join(timeout=5)`
>   → 还没退出的记 `warning`（daemon 兜底，进程不会挂住）。
> - v1 **不引入主动释放语义**。「claim 分支存在 = 有人持有」是整个总线的原子锁基础，
>   「释放」意味着删除一条别人可能正在看的 ref，跨机器竞态复杂。回收交给现有的 lease 过期 + watchdog。
>   真要做（v2），形态是把 `lease_until` 改写成现在 ISO 时间，语义上等价于「立刻可被回收」。
> - 反过来说：**正因为 `lease_until` 是被往前推的，`lease_minutes` 不要设得过长**，
>   默认 180 分钟在并行场景下建议放到 60–90。

**(3) 续租失败要能反馈给任务自己。**
现在 `_refresh_lease` 和 `_heartbeat_loop` 都是 best-effort：失败只 log，不打断任务（2507 行）。
串行时这是对的。并行时多了一张牌：**如果某张卡的租约连续 K 次刷新失败（很可能是远端没了、
或 claim 已被别人抢占），这张卡其实已经处于「大概率会被 gc」的状态**，继续跑几十分钟是浪费 LLM 配额。

> 修法：心跳线程连续失败超过阈值时，置该任务的 `AbortSignal`；任务体在轮询点检查一下，
> best-effort 提前收尾（写完 summary + 写一个 failed 结果）。**不要直接杀别的心跳、更不要杀 worker。**

### 2.3 为什么不用「一条 refresher 线程 multiplex 所有 tid」

备选设计：一条心跳线程，每 tick `fetch` 一次，然后串行给所有活跃 tid 续租。优点是真的只 fetch 一次、
天然无竞争。不采用的理由：

- 卡的 `lease_minutes` 可能不同，节奏却被最慢那张卡绑住；
- 单线程里一处异常很容易把整条线带崩，除非把 try/except 精细收口到「每卡」粒度；
- 排查时看不到「谁没续上」，得靠日志回溯；
- 本卡明确要求每任务一条心跳线程，各自成败互不干扰。

**但把它的 fetch 合并技巧记下来当优化项**：如果 profiling 显示 N 次 fetch 是瓶颈，
可以在 `_refresh_lease` 里加「同一个 tick 内已 fetch 过就跳过」的短期标志位是可行折中。

### 2.4 生命周期：claim 与心跳严格对齐

```text
claim(tid) 成功
   └─> start_heartbeat(tid)                  # 抢到锁立刻续租，别等任务启动
         └─> submit(run_task, tid)
                └─> finally: stop_heartbeat(tid)
```

要点：**先 claim，后心跳**。没抢到锁就不要注册心跳（现在的代码已经是这样，
`run_task` 的 `with self.lease_heartbeat(tid)` 在 worktree 之后，语义等价）。
反过来，任务无论正常/异常/超时结束，都必须走到 `stop_heartbeat` —— 这也是把它从
contextmanager 改成显式 pair 的原因：future 的 `finally` 里一眼能看见配对。

---

## 隔离保证

### 3.1 worktree 已经提供了物理隔离

每张卡都有自己的 worktree（2527/2600 行 `git worktree add -B task/<tid>`），
路径 `<worktrees>/<tid>`，分支 `task/<tid>`，**每个 worktree 有自己的 index**（落在
`.git/worktrees/<name>/index`）。所以：

- 并行任务之间**看不到彼此的未提交改动**——这一点与跨机器并行完全同构，
  见 [`多机并行开发.md`](多机并行开发.md) §5「为什么文件不会打架（三层保障）」：

| 层 | 机制 | 防的是什么 | 并行后是否变化 |
|---|---|---|---|
| 分支隔离 | 每卡一个 `task/T-xxxx` worktree | 看不到对方未提交改动 | ❌ 不变，仍成立 |
| 租约锁 | `claim/T-xxxx` git ref，push 非快进即失败 | 同一张卡不会被两个 agent 同时干 | ⚠️ 心跳要改成每任务一条（见 §2） |
| 路径互斥 | dispatch 检查 `paths` 重叠 + `HIVE_MAX_PER_REPO` | 两张不同的卡改同一片文件 | ⚠️ 见 3.4「手工 assign 的漏网之鱼」（跳过 + log，不静默） |
| 合流串行 | `land` 一次合一张，冲突显式暴露 | 最后合并时互相覆盖 | ❌ 不变 |

**关键提醒（原文照抄，并行后更重要）**：分支隔离保证「干活时不冲突」，**不保证「合流时不冲突」**。
两张卡改同一文件，各自都能提交，land 时才炸。所以 `paths` 写准 + 必要时 `depends_on` 串行，
在并行场景下从「建议」升级成「必须」。

### 3.2 并行后新引入的共享面（必须串行化）

worktree 隔离了工作目录，但**隔离不了 git 仓库的元数据**。同一个物理 repo
（`git rev-parse --git-common-dir` 指向同一个地方）的这些操作要抢锁：

| 操作 | 现位置 | 并行风险 |
|---|---|---|
| `fetch --prune origin`（代码仓） | `run_task` 2580 行，每任务一次 | N 倍网络开销 + ref 更新竞争 → **提到每轮一次，按 repo 去重** |
| `git worktree add / remove / prune` | 2600 / 2729 行 | worktree 管理层有自己的锁，但 remove 和别人的 add 撞上会报「正在使用」 |
| `commit_file_to_ref`（claim/res 分支） | `_write_result` 2795、`_refresh_lease` 2520 | 写同一个 holding repo 的 index/refs |
| `sync_main()` | 2339 / 2811 行 | 只在轮询线程调用，**不要放进 future** |

统一方案：worker 内一把 `_git_lock = threading.RLock()`，上面四类全包。
RLock 而不是 Lock，是因为 `_write_result` 内部可能再次调用被包裹的方法而不至于自锁。

### 3.3 worktree 不隔离的东西（并行会放大）

这一节是并行真正的风险源，比 git 锁重要：

- **仓库外的绝对路径**。worktree 只保证仓库内的文件系统隔离。两个任务线程如果都让 agent 往
  `/tmp/<固定名>`、`~/.cache/<固定名>`、固定端口、固定 socket 上写，照样打架。
  > 约定：exec agent 只允许写 `$HIVE_WORKTREE` 之内的相对路径（`_exec` 已经在 env 里给了
  > `HIVE_WORKTREE`、`HIVE_TASK`、`HIVE_AGENT`、prompt 绝对路径，2750-2755 行）。
  > T-0162 可选加强：给每个 future 一个私有的 `TMPDIR` / tmp path prefix。
- **外部副作用**：同一个 LLM 账号的并发配额、同一个 Gitea 的 push 速率、同一台机器的内存。
  这些不受 git 控制，只能靠 `max_concurrency` 这个阀门。
- **`.hive/` 是每个 worktree 独立的**（2614 行），并且已经在 `info/exclude` 里（2605 行），不会被提交。
  并行不会串。

### 3.4 路径互斥的漏网之鱼

`paths` 冲突检测在 **dispatch 侧**（`_paths_conflict`，1221 行附近）。
但总控可以绕过 dispatch 直接 `hive assign`（或手工塞 `inbox/<name>/T-xxxx.md`），
这时两张 paths 重叠的卡可能同时落到同一个 worker 的 inbox 里。

> 兜底：worker 在进入池子之前做一次**本地软检查**——与「本轮已 claim 的卡」做 paths 重叠比较，
> 重叠则跳过并 log 一句（不静默）。这不影响正确性，只是把 land 阶段的冲突提前到 worker 阶段暴露。
> 粒度上现在是目录前缀比较，精度问题由 **T-0163（文件级锁声明）** 解决；本设计不强依赖它，
> 但若 T-0163 先落地，这里直接换成 `_file_lock_conflict` 即可。

---

## 错误隔离

目标是：**一张卡崩了 / 超时了 / 被杀了，池子里其它卡必须照样能正常交付。**

### 4.1 每张卡一个 future，异常就地收敛

```python
def _run_one(self, tid: str) -> dict:
    try:
        if not self.claim(tid):
            return {"task": tid, "skipped": True}
        self.start_heartbeat(tid)
        try:
            return self.run_task(tid)
        finally:
            self.stop_heartbeat(tid)
    except BaseException as e:            # 含 KeyboardInterrupt：记录下来再放出去给 executor
        log(f"[{tid}] 任务线程异常: {e!r}")
        return self._fail(tid, self.h.card(tid), None, f"任务线程异常: {e!r}")
```

要点：

- **`ThreadPoolExecutor` 不会因为一个 future 抛异常而终止池**，异常被塞进 future 对象里，
  在 `as_completed` 时重新抛出。所以关键是**每个 future 自己 try/except**，别让它冒到聚合处。
- 现在的 `loop` 把整轮 `for pending:` 包在一个 `try/except HiveError` 里（2823 行），
  一张卡炸了会连带跳过后面所有卡。并行版必须把这个保护下沉到每任务。
- `_fail`（2783 行）已经能写出合法的 failed 结果 + `state/done/<tid>` marker，直接复用。

### 4.2 超时与强杀

- exec 模式：`_exec` 的 `communicate(timeout=timeout_s)` + `os.killpg` 整组杀（2769-2779 行）已具备，
  **并行下语义不变**，只是有多个进程组并存，杀错的风险来自 `start_new_session` 是否正确——它是对的。
- manual 模式：`deadline` 轮询（2645-2651 行）**会长期占用一个槽位**（等人类几十分钟）。
  > 设计决定：**manual / verify-only 的卡不进线程池**，走原来的串行通道。
  > 理由：给它们占槽位毫无收益（它们不消耗 worker CPU），反而把 `max_concurrency` 的语义污染了。
  > T-0162 实现时在 `pending()` 之后按 mode 分流，而不是在池子里塞 3 小时的 sleep。

### 4.3 日志

- **per-task 日志文件已经存在**：`self.state / "logs" / f"{tid}.log"`（2619 行），子进程 stdout+stderr
  重定向合并写进去（2763 行）。这一条天然满足「互不串」，不用改。
- **要改的是进程级 stdout**：`log()` 现在是所有线程往同一个控制台打，N 张卡交替输出会完全没法读。
  > 落地时对并行路径的日志统一加 `[<tid>]` 前缀（或在线程名上标 tid，从
  > `threading.current_thread().name` 取）。最少改动方案：给 `run_task` 里所有 `log(...)`
  > 换成 `self._tlog(tid, ...)` 薄封装。

### 4.4 git / 结果写入失败

- 结果走 `commit_file_to_ref(..., "res/<tid>", force=True)`（2795 行），**每张卡一个分支**，
  彼此不会非快进冲突；但要过 `_git_lock`，并在 `_write_result` 外层 try 住：
  写不出去的结果等于「没结果」，卡会被 watchdog 判定为超时回收、**白白重跑一次**。
- 同理 claim push 失败（`claim()` 已有带抖动的指数退避 + 区分「被抢」和「暂时失败」，2387 行）
  在并行下要**保持原样**，不要为了「更快抢到」缩短退避——N 条线程同时退避会产生 push 风暴
  （这个坑 `lessons.md` 里 T-0146 记过一次，别在同一个地方再踩）。

### 4.5 优雅退出的优先级

```text
SIGINT/SIGTERM
  1. 停止轮询新任务
  2. 广播所有心跳 stop.set()            ← 先停续租，让 watchdog 能尽快回收
  3. 给正在跑的 future 一个宽限期（如 30s），期间让它们自然收尾
  4. 超时未退出的：不再等（daemon 线程 + executor.shutdown(wait=False, cancel_futures=True)）
  5. 进程退出
```

注意第 2 步：**停心跳要早于杀任务**。反过来（先杀任务后停心跳）会出现「任务已经死了、
租约还在被续」的僵尸状态，watchdog 永远判不出它死了。

---

## 与 dispatch 的关系

### 5.1 现在 `max_concurrency` 是什么语义

它在 **dispatch 侧**被读三次（1216 / 1240 / 1263 行），用来做「同一轮里给这个 agent 派多少张」的预算：

```python
load = self._load_counts(cards)          # 已经在飞的卡
planned = dict(load)
...
if planned.get(name, 0) >= max_agent:
    blocked.append(f"负载已满（{name} {planned.get(name,0)}/{max_agent}）")
```

也就是说，**它是「分派侧上限」，不是 worker 执行能力的声明**。`dispatch` 从没保证过 worker 真的会并行跑，
worker 内部也确实没跑——这就是两者脱节的地方。

### 5.2 并行后的语义收敛

> **`max_concurrency` = 同一个 agent「同一时刻最多同时持有 / 执行」多少张卡。dispatch 与 worker 共用这一个数，不重复限流。**

- worker：`ThreadPoolExecutor(max_workers=max_concurrency)`。
- dispatch：继续用 `_load_counts`（真实在飞数）+ 本轮 `planned` 判「负载已满」，**口径不用改**。
- 二者天然对齐的前提是：**「在飞」≈「已 claim 且未 done」**，而 worker 只会 claim 到自己实际能跑的数量。
  所以不需要给 dispatch 加任何新判断。

### 5.3 worker 绝不能「先把 inbox 全 claim 了再排队」

这是本设计里最容易写错的一处，单独强调。

错误写法：

```python
for tid, _ in pending():
    if self.claim(tid):           # ❌ 一口气占住所有卡
        futures.append(ex.submit(self._run_one, tid))
```

后果：worker 的 `claim/<tid>` 锁把**别的机器、别的 agent** 也能干的卡全占了。
从总控视角看是「负载已满」，实际上它们排队在同一个 worker 的线程池里没人跑，租约却在烧。

正确写法（`pending()` 之后、提交之前先用槽位卡一道）：

```python
n = self.max_concurrency
inflight = 0
for tid, _card in self.pending():
    if inflight >= n:
        log(f"已达并发上限 {n}，本轮剩余 {tid} 等下一轮")
        break
    res = ex.submit(self._run_one, tid)      # claim 发生在 future 里
    ...
```

claim 动作本身仍放在 future 里保持它的天然并发抢锁能力；槽位节流放在**提交侧**用 `Semaphore(n)` 或直接
先 `submit` 到 `n` 个再逐个回收更稳。**推荐 `threading.Semaphore(n)`**：先对 `pending()` 全部 submit，
任务体开头 `sem.acquire()` 后再 claim，`finally` 里 `release()`。这样既不让空 future 白白占位，
也保证「同时 claim 成功的卡 ≤ n」。

### 5.4 职责边界

| | 负责 | 不负责 |
|---|---|---|
| `dispatch` | 选谁干：能力匹配、repo、依赖、`depends_on`、**paths 不冲突**、`max_per_repo` | 怎么干、干多快 |
| `worker` | 领了多少能跑多少：抢锁、心跳、worktree、执行、verify、结果 | 判断两张卡会不会打架（相信 dispatch 已过滤 + §3.4 的软检查兜底） |

**结论：并行之后 `dispatch` 不需要改任何代码。** 它继续按「负载已满」限流，只是这个「负载」
现在真的会被 worker 并行消费掉，而不是排串行队列。这也是为什么本卡不动 `.py`——
只要 worker 侧的量不超过 dispatch 派给它的量，整条链路的一致性是天然保持的。

> 唯一需要同步给总控的运营约定：**想让某个 agent 并行，改 `agents/<name>.json` 的 `max_concurrency`
> 即可，dispatch 会自动多派、worker 会自动多跑。** 一处配置，两端生效。

---

## 落地建议

T-0162 按这个顺序做，每步都能独立验证 / 独立回退。

| # | 步骤 | 改什么 | 验证方式 |
|---|---|---|---|
| 1 | 配置读取 | `Worker.__init__` 读 `cfg["max_concurrency"]`，覆盖顺序 CLI > `HIVE_MAX_CONCURRENCY` > 配置 > 1；`work` 子命令加 `--max-concurrency`（现 3280 行只有 `agent add` 有） | `whoami` 输出一致；默认行为零变化 |
| 2 | git 串行锁 | 新增 `self._git_lock = threading.RLock()`，包住 `sync_main` / `fetch` / `commit_file_to_ref` / worktree add·remove·prune | 现有全量测试通过 |
| 3 | 心跳 API 化 | 新增 `start_heartbeat(tid)` / `stop_heartbeat(tid)` + 线程安全注册表；`lease_heartbeat` 变成薄封装（保留给串行路径） | 单任务行为与旧版逐字节一致 |
| 4 | 并行入口 | 新增 `Worker.run_tasks(tids)`：`Semaphore(n)` + `ThreadPoolExecutor` + `as_completed`，每任务 `_run_one`（含 try/except + 心跳配对） | `--max-concurrency 1` 时输出与旧版一致 |
| 5 | 主循环 | `loop()` 改为：`sync_main` → 按 repo 去重 `fetch` → 分流 manual/verify-only（串行） → `run_tasks(rest)` → `sleep(interval)`；`once=True` 要等 futures 全部结束再返回 | `work --once` 要等 futures 全部结束再返回，不丢任务 |
| 6 | fetch 去重 | 把 `run_task` 里的 `git_net(["fetch","--prune","origin"], repo)`（2580 行）提到循环外，按 repo 做一次 | 日志里 N 张卡只有 1 次 fetch |
| 7 | 日志 tid 化 | 并行路径的 `log()` 加 `[<tid>]` 前缀；`logs/<tid>.log` 不变 | 人工看一轮输出能分清卡 |
| 8 | 脚本透传 | `scripts/start-worker.sh` 支持 `--max-concurrency N`（它现在 `"$@"` 已经全透传给 `hive.py work`，只需确认 argparse 接得住） | `start-worker.sh x --max-concurrency 3` 生效 |
| 9 | 回归测试 | 新建 `tests/test_worker_parallel.py`，用 `fake_agent.py` 造 3 张 paths 互不重叠的卡 | 见下方断言清单 |
| 10 | 文档同步 | `多机并行开发.md` 加「单机 worker 内并行」小节；`AGENT快速上手.md` 的 `max_concurrency` 字段补一句说明 | CI 里 docs 测试通过 |

**测试必须覆盖的断言：**

1. 3 张卡全部 `done`，每条 verify 退出码 0；
2. **claim 的张数 ≤ max_concurrency**（关键：不能把 inbox 占光——见 5.3）；
3. 每张卡的租约在跑的过程中各自被刷新过（`refreshed_at` 有值且属于不同的 tid）；
4. `--max-concurrency 1` 的输出与改造前一致（回归防线）；
5. 故意让 1 张卡的执行体抛异常 → 另外 2 张仍 `done`，异常那张拿到合法 failed 结果；
6. `state/logs/` 下每个 tid 一个日志文件，内容不串；
7. 超时路径：一张卡 `timeout_minutes` 到点被 `killpg`，其它卡不受影响；
8. SIGTERM 后心跳线程全部停止（检查 `threading.enumerate()` 里没有存活的 `hb-*`）。

### 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| git index/lock 竞争（同一协调仓） | 续租或结果写入偶发失败，最差是坏死锁 | 全局 `_git_lock` + 每轮只 fetch 一次（步骤 2、6） |
| worker 抢太多卡，别家 agent 无卡可干 | 全局吞吐反而下降，租约空烧 | Semaphore 严格按槽位 claim（5.3，测试断言 2） |
| LLM 并发配额 / 内存不够 | 一批卡一起失败 | 默认 1；放量先 2–3；已有 retry 熔断兜底（`max_retries`） |
| worker 被 `kill -9`，租约已推远 | 卡要等 lease 到期才被回收 | 别把 `lease_minutes` 设太长（并行下建议 60–90）；§2.2(2) 的优雅退出 |
| 手工 `assign` 绕过 dispatch 的 paths 检查 | 两张重叠的卡并行干，land 阶段冲突 | worker 端软检查跳过（§3.4）+ 长远靠 T-0163 文件级锁 |
| 仓库外副作用（`TMPDIR` / 端口 / `~/.cache`）互相踩 | 诡异的偶发失败，最难查 | 约定只用 `$HIVE_WORKTREE` 相对路径；可选每任务私有 `TMPDIR` |
| 日志混淆 | 并行出问题时无法排障 | tid 前缀 + per-task log file（4.3） |
| `once` 语义变化 | 脚本以为跑完了，其实 future 还在飞 | 主循环要 join 全部 future（步骤 5） |

---

## 附录：verify

本卡的验收命令检查以下小节存在——写完后人工再跑一遍：

```sh
python3 -c "import pathlib; t=pathlib.Path('docs/design-worker-parallel.md').read_text(encoding='utf-8'); need=['## 模型','## 租约与心跳','## 隔离保证','## 错误隔离','## 与 dispatch 的关系','## 落地建议']; missing=[s for s in need if s not in t]; assert not missing, f'缺少必备小节: {missing}'"
```

> 提醒（`lessons.md` 第一条）：**先手动跑一遍再看**。别忘了这条路上的前车之鉴——
> T-0114 的验收命令自己写错了（`sys.exit` 却没 `import sys`），代码全对却被判 failed。
