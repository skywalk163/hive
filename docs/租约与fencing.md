# 租约、fencing token 与本地留底

> 纯 Git 模型下，`claim/T-xxxx` 分支的**原子 push** 决定「谁在跑」。这套模型很强，
> 但它有一个固有盲区：**易失状态**。一个卡死的旧 worker 可能在租约被回收、卡已被
> 别人重新领取之后才醒来写结果；仅靠 `res/<tid>` 分支的 force push 是拦不住的——
> 它会把新持有者的结果顶掉。本文讲 hive 补上的两道防线。

---

## 1. 问题：旧 worker 「复活」

场景（真实发生过，见 `docs/lessons.md`）：

1. `a1` 领取 `T-0100`，进程卡在某个慢命令里；
2. 租约过期，`hive watchdog --apply`（或人工 `unassign`）回收，卡回到 ready；
3. `a2` 重新领取，跑完，写了 `res/T-0100`；
4. `a1` 的进程终于醒来，继续跑 verify，最后 `_write_result` → **t2 的结果被 t1 覆盖**。

第 4 步是纯 Git 模型答不了的：`res/*` 分支的 push 用的是 `--force-with-lease`，
目的是「同一张卡重跑时能覆盖上一次」，而这恰好也允许旧 worker 覆盖新 worker。

---

## 2. 机制：lease_id + fence

从 0.15.0 起，每次领取都会写入两样东西：

```json
// claim/T-0100 分支上的 claims/T-0100.json
{
  "task": "T-0100",
  "agent": "a1",
  "claimed_at": "2026-10-30T08:00:00+08:00",
  "lease_until": "2026-10-30T11:00:00+08:00",
  "lease_id": "0b0f4c9e…",          // 每次领取唯一
  "fence": 3,                       // 单调递增的 fencing 序号
  "backend": "git"
}
```

| 字段 | 含义 | 谁推进它 |
|---|---|---|
| `lease_id` | 本次领取的**唯一身份**；续租保持不变 | `Worker.claim` |
| `fence` | 这张卡的 fencing 序号 | `Worker.claim`（+1）、`unassign`/`retry`/`watchdog` 回收（+1） |

任务卡上也有一个 `fence` 字段（frontmatter）。**回收动作把卡面 fence 推进一格**，
于是「旧结果」和「新结果」在同一条 git 历史上就能被事后区分。

### 写入前的 fencing 校验

`Worker._write_result` 在真正 push 之前做三步对账（`Worker._assert_lease`）：

1. 远端 `claim/<tid>` 分支还存在吗？不存在 → `LeaseFenced`；
2. 持有者还是我吗？不是 → `LeaseFenced`（消息里带对方的 agent 名）；
3. 远端 `lease_id` 和我手里的一致吗？不一致 → `LeaseFenced`。

任何一步不过，这条结果**整条丢弃**（不覆盖、不重试），只在本地 `state/done/<tid>`
留一份带 `fenced` 字段的副本，日志里写明原因。失败路径（`_fail`）也走同一道闸，
避免「旧 worker 判失败」反过来把别人的 done 结果打掉。

### 事后的 fencing 校验

即使旧 worker 恰好卡在「校验通过 → 真正 push」的窗口里写进来了，仍然可以事后识别：

- 结果里带 `fence`（写入时的值）；
- 卡面 `fence` 只会在回收时推进；
- `hive land` / `hive status` / `hive doctor` 判定 `result.fence < card.fence` 即
  **过期租约写入**，`land` 直接拒绝（`plan.status == "fenced"`），看板 `fenced: true`，
  doctor 给一条 warn。

修复办法：`hive retry <tid>`——让当前持有者重跑，新结果带新 fence。

---

## 3. 本地留底

Git 上的 claim 分支是权威，但它也可能「不见了」：`gc` 回收、`close` 删除分支、机器
离线。所以每次领取/续租还会把同一份租约写一份到**本机**：

```
~/.hive/leases/<tid>.json          # 0600，原子替换（tmp + os.replace）
```

用途：

- 结果写入前与远端 claim 对账（上面第 3 步），断网时也能给出明确原因；
- 心跳发现「远端 claim 已被别人接管」「claim 分支没了」时**立即停止续租**并留日志，
  而不是默默继续刷 `lease_until`（这本身就是在制造僵尸租约）；
- 运维排查：`hive lease list` 一眼看到本机领过哪些卡、哪个 fence。

```sh
hive lease list              # 表格：任务 / agent / fence / lease_id / lease_until
hive lease list --json       # 结构化
hive lease show T-0100       # 只看一张
hive lease clear T-0100      # 删掉本机留底（确认是人工接手时）
```

> 留底目录可用 `HIVE_LEASE_DIR` 覆盖（测试和一台机器跑多个 hive 实例时用）。
> 它**不进协调仓、不随 Git 同步**。

---

## 4. 与 store 后端的关系

`claim_backend=store`（P2）时，claim 是 leader 上的 CAS，本身带 `lease_id/lease_seq/term`。
本机留底仍然写，`fence` 仍然单调，所以两种后端下 `hive status` / `hive land` 的口径一致。

差别只在实现位置：

| 后端 | 抢锁 | fencing 校验 |
|---|---|---|
| `git`（默认） | `push` 非快进 | push 前的 `_assert_lease` + 事后的 fence 比较 |
| `store`（P2） | leader CAS | store 侧的 `lease_id` 比对 + 同样的本地留底 |

---

## 5. 排障速查

| 现象 | 含义 | 处理 |
|---|---|---|
| 日志 `领取异常 ... LeaseFenced` | 你的租约在跑任务期间被回收 | 正常：这条结果不该写。`hive retry` 重新排队 |
| `hive land` 报 `结果由过期租约写入` | 旧 worker 的结果混进了 res 分支 | `hive retry <tid>` 让当前持有者重跑 |
| `hive status --json` 行里有 `fenced: true` | 同上，看板标红 | 同上 |
| `hive lease list` 里有一张你不想看的旧卡 | 本地留底残留 | `hive lease clear <tid>` |
| 心跳日志 `租约已被 X 接管，停止续租` | 卡被重派给别人了 | 正常；本机不再持有 |

---

## 6. 相关实现位置

| 内容 | 位置 |
|---|---|
| `LeaseFenced` 异常 | `hive.py`（`HiveError` 之后） |
| 本地留底读写 | `hive.py` 的 `save_local_lease` / `load_local_lease` / `local_leases` |
| 领取时生成 lease_id/fence | `Worker.claim` |
| 续租 + 接管检测 | `Worker._refresh_lease` |
| 写入前的 fencing 校验 | `Worker._assert_lease` / `Worker._write_result` |
| 卡面 fence 推进 | `Hive._bump_fence`（watchdog 回收 / retry / unassign） |
| 事后判定 | `Hive.result_fenced`（status / land / doctor 共用） |
| CLI | `hive lease list/show/clear` |

回归测试：`tests/test_fence.py`（领取带 lease_id/fence、旧 worker 被拒、回收推进 fence 后
land 拒绝、CLI 列出/清理、本地留底 round-trip）。
