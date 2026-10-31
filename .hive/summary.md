# T-0220 交付：派发中间态（TTL 对 dispatch 生效 + 派发未领取快速回收）

## 结论

T-0196 现场（unassign 回 ready 后 3 分钟被 orchestrate 连派 3 次、每次都没人领）
有两条根因，都在代码里，本卡各给了一条修法：

1. `_assign_local()` 只写 `state/owner` + inbox `assigned_at`，从不写
   `dispatched_to` / `dispatched_at` ⇒ `is_dispatch_candidate()` 认为「没推过」，
   下一轮 180s 立刻重派。
2. 没有「派发未领取」的快速回收，只有 `orphan_grace` 45 分钟的 orphan 兜底，
   既不记「这次没人领」，也不阻止下一轮再派给同一个 agent。

## 改了什么

| 文件 | 改动 |
| --- | --- |
| `hive.py` | `_assign_local()` 同时写 `dispatched_to` / `dispatched_at`（与 inbox `assigned_at` 共用同一个 stamp）；新增 `_dispatch_unclaimed_minutes()`（`HIVE_DISPATCH_UNCLAIMED_MINUTES`，默认 10）与 watchdog `dispatch_unclaimed` / `reclaim_miss` 分支；新增 `_reclaim_dispatch_miss()`（`dispatch_miss += 1`、删 inbox 残留、`>=2` 转 blocked 并写明「转人工」）；`DISPATCH_MISS_BLOCK_AT = 2`；`TASK_FIELDS` 追加 `dispatch_miss`；dispatch 侧跳过 `dispatch_miss >= 2` 的卡；新增 `_store_state_conflict()` 做 store 模式派发前校验；`_assigned_at()` 增加 `dispatched_at` 兜底；`cmd_unblock` 清零 `dispatch_miss`（人工解挂 = 重新给自动派发机会）；watchdog `--json` 新增 `would_reclaim_miss` |
| `tests/test_dispatch_miss.py`（新） | 7 条判据：派发写标记、unassign 后 TTL 生效、11 分钟未领取回收并清 inbox、阈值可配、连续 2 次转 blocked、ready 卡 miss>=2 不进计划、store 状态不一致不派 |

`dispatch_tracking.py` **未改**：`is_dispatch_candidate()` 的语义（ready + 无 owner +
TTL）本来就是对的，缺的只是写入端。

## 判据与反证（每条都钉住旧实现的错误）

1. 派发后卡面有 `dispatched_to` + `dispatched_at` —— 删掉写入即红；
2. 派发后 `unassign` → `is_dispatch_candidate() == False`，且 `dispatch --json`
   的 skipped 给出「刚推送过（TTL 60 分钟内不重复派）」—— 退回旧实现这里会重新派；
3. `assigned` + 无 claim + `assigned_at` 11 分钟前 → 回收后 `state=ready`、
   `dispatch_miss=1`、`inbox/<agent>/<tid>.md` 被删；
4. `dispatch_miss=2` → 转 `blocked` 且 `dispatch --json` 的 plan 里不再出现
   （skipped 理由「连续 2 次派发无人领取，转人工」）。

## 验证

```
python3 -m pytest -q tests/test_dispatch_tracking.py tests/test_orchestrate.py tests/test_dispatch_miss.py
46 passed in 58.53s
```

回归（相关面）：`test_watchdog / test_retry_cap / test_store / test_store_overlay /
test_mcp / test_new_surfaces` → **83 passed in 185.97s**。

## 现场回归的局限

0.88 的 orchestrate 与 store 多数派集群不在本机，无法直接跑「两轮真集群」。已在真实
协调仓跑两轮 `dispatch --dry-run` 取证：T-0220 自身因「能力缺失 python/pytest +
fb88-dsh 是 manual」被跳过，T-0213/T-0215 因仓白名单被跳过，没有出现新的
assigned-无-claim 卡。真集群两轮复核需在 0.88 上换新代码后跑
`orchestrate --interval 180`。

## 边界与已知限制

- **没动** `cmd_unassign` 的语义（人工退回仍无条件执行）；只动了 `cmd_unblock`
  的计数清零。
- **没改** `orphan_grace` 默认 45 分钟：`dispatch_unclaimed` 是更快的另一条通道，
  阈值调大时仍会落回原来的 orphan 语义。
- **没给** dispatch 加新的必填参数（阈值走环境变量，默认 10 分钟向后兼容）。
- **没碰** `run()`（T-0206）与 `fetch()`（T-0216）。
- store 后端仍不写 Git 卡面（原设计），`dispatch_miss` 在 store 模式下不落盘；
  TTL 是「按卡」而非「按 agent」，unassign 后 60 分钟内其它 agent 也接不走 ——
  若要按 agent 粒度需要另开卡。
