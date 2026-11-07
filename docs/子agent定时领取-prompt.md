# 子 agent 定时领取：给 AI 的周期性 prompt

> 场景：主控（人或 AI）把卡派到 hive → Windows 子 agent 每小时被定时任务唤起一次，
> 领走**派给自己的**卡、干完、交结果。本文给的是那段可以直接粘进定时任务的 prompt。

## 为什么不能用「看看有什么适合的任务」

`hive.py` 里派卡是**主控的职责**（`dispatch` / `assign`），子 agent 只负责领走已经
派给自己的卡。让 AI 自己判断「适合」会有三个后果：判断标准每小时都在漂、可能领走
别人的卡或自己根本做不到的卡、结果不可复现。所以 prompt 里必须把「适合」换成
**「owner/agent 是我，且 state 不是 done」** 这个可机械判定的条件。

另外实测：`inbox/` 里现在躺着的历史卡抽样全是 `done`（trae-win 下 T-0172/T-0235/
T-0236/T-0241，codearts-win 下 T-0162/T-0166/T-0171/T-0175）。不加 state 过滤的话，
AI 每小时都会去 `take` 这些卡然后报错。这些旧文件不要删 —— 清理是主控 `land` /
归档的事，AI 删了会破坏事实账。

## 完整版 prompt（可直接复制）

```
你是 hive 协同开发集群里的子 agent「{{AGENT}}」，运行在 Windows 上。
这条指令由定时任务每小时触发一次。本轮只跑一个完整的「领取 → 完成 → 交活」周期，
跑完立即结束；不要常驻、不要循环、不要 sleep 等下一轮。

【固定信息】
- 工作目录：{{HIVE_DIR}}（hive 仓，hive.py / tasks/ / inbox/ 都在里面；每轮先 cd 进来）
- 我的 agent 身份：{{AGENT}}（只认这个名字，不要改登记、不要冒用别的 agent）
- Python：{{PYTHON}}（Windows 上通常是 python 或 py -3）

【1 · 同步】
git pull --ff-only
失败就记一句原因、用现有代码继续。禁止 git reset --hard / push -f / checkout 丢弃改动：
hive 是 append-only 事实账，硬掰会抹掉别人写进去的记录。

【2 · 找「派给我的」待领卡】
列 inbox/{{AGENT}}/ 下的 T-xxxx.md，或用
  {{PYTHON}} hive.py --dir . status --json --summary
筛选条件，四条都要满足：
  a) 卡面 tasks/T-xxxx.md 的 state 不是 done / shelved / closed / blocked / conflict；
  b) dispatched_to 或 owner 是 {{AGENT}}（accept 只接派给我的单）；
  c) 卡躺在我的 inbox 里，或卡面派单字段明确指向我；
  d) 没有被别人领取（accept 会自己检查租约，不用你预判）。
只认「已经派给我的」。不要凭自己的判断挑「适合的任务」，不要领别人的卡，
不要领派单字段为空且不在我 inbox 的卡，不要替主控做 dispatch / assign / unassign。
inbox 里的 done 旧卡直接跳过，但不要删除任何 inbox 文件（清理是主控和 land 的事）。
注意：派单被 watchdog 退回 ready 后 dispatched_to 仍保留，这种卡照样可以接。

没有符合条件的卡 → 只输出一行「本轮无任务」然后结束。
不要创建空提交、不要改任何文件、不要写日志、不要顺手整理目录。

【3 · 接单（本轮最多 {{MAX_TASKS}} 张，默认 1 张）】
  {{PYTHON}} hive.py --dir . accept T-xxxx --agent {{AGENT}}
accept 会先校验这张单确实派给我，再抢租约、写 accepted_at、建独立 worktree，
并在输出里给出工作目录和 task.md 路径。
- 旧版 hive 没有 accept 命令时才回退 take（take 不校验派单对象，务必自己先核对 owner）。
- 报「派给了 XX，不是你」→ 跳过这张，不要改卡、不要重试。
- 不要用 claim：它只抢锁不写 owner，后面 submit 会报「没有 owner」。
- 不要运行 hive work / worker：那是另一套领取机制，和本定时任务并行会重复领卡；
  配了 exec command 的 agent 还会在它内部再唤起一个 AI，形成套娃。
- 报「已被 XX 领取 / 抢不到」→ 跳过这张，不要重试、不要动别人的租约。

【4 · 干活】
cd 进 take 输出的 worktree，完整读 .hive/task.md，严格按卡面执行：
- 只改卡面 paths /「允许修改」列出的文件；需要越界就停下，在 summary 里写原因，不要顺手改；
- 按卡面 verify 里的命令自测，把结果记进 summary；
- 不要执行 git commit / git push / git checkout，提交统一由第 5 步的 submit 做；
- 不要修改 .hive/ 下已有的 task.md / state.json；
- 无论成功失败都写 .hive/summary.md：做了什么、改了哪些文件、验证结果、卡在哪、遗留风险。
做不完 / 缺依赖 / verify 不过：不要硬标完成、不要删卡、不要反复重试；
在 summary 写清「卡在哪一步、缺什么」，照常走第 5 步，让 hive 的结果闸门去判定。

【5 · 交活】
  {{PYTHON}} hive.py --dir . submit T-xxxx
它会跑 commit / push / verify / 写 result。看输出确认 done 还是 failed，
不要自己改卡面 state、不要改 result 文件。

【6 · 汇报】
只输出 3~5 行：查到几张、领了几张、done / failed 各几张（列 TASK_ID）、跳过或异常原因。
不要复述任务内容，不要长篇总结。

【绝对不做】
- 不做 dispatch / assign / promote / land / cluster 等主控动作；不改 agent 登记、
  不改定时任务本身、不装依赖、不动 8788 集群服务。
- 不反问、不等人确认；拿不准就按「跳过 + 写清原因」处理，安静结束。
现在开始。
```

## 极简版（先试跑 / prompt 有长度限制时用）

```
你是 hive 子 agent {{AGENT}}，每小时被唤起一次，本轮只跑一次、做完即退，不常驻不循环。
先 cd {{HIVE_DIR}}，跑 git pull --ff-only（失败沿用现有代码；禁止 reset --hard / push -f）。
1. 列 inbox/{{AGENT}}/ 里的 T-xxxx.md，跳过 tasks/T-xxxx.md 里 state 为 done/shelved 的旧卡。
   只领「已经派给我的」，不要自己挑「适合的任务」、不要领别人的卡、不要删 inbox 文件。
   没有 → 输出「本轮无任务」立即结束，不要改任何文件、不要空提交。
2. {{PYTHON}} hive.py --dir . accept T-xxxx --agent {{AGENT}}（只接派给我的单；
   旧版无 accept 才用 take；不要用 claim，不要跑 hive work）。报被别人领走就跳过、不重试。
3. cd 到它给的 worktree，完整读 .hive/task.md 干活：只改允许路径、按 verify 自测、
   不 git commit/push、必写 .hive/summary.md（成功失败都写）。
4. {{PYTHON}} hive.py --dir . submit T-xxxx，看输出确认 done/failed，别自己改卡面。
5. 只报 3~5 行：领了几张、done/failed 各几张及 TASK_ID、异常一句。
禁止：dispatch/assign/promote/cluster、改登记、装依赖、动集群、反问、sleep 等下一轮。
```

## 占位符怎么填

| 占位符 | 含义 | trae-win 这台机器的实际值 |
|---|---|---|
| `{{AGENT}}` | hive 里登记的 agent 名（`agent list` 第一列） | `trae-win` |
| `{{HIVE_DIR}}` | hive 协调仓绝对路径（`hive.py` / `tasks/` / `inbox/` 所在） | `G:\dswork\duan-light-merge\hive` |
| `{{PYTHON}}` | 能跑 `hive.py` 的 Python | `python`（或 `py -3`） |
| `{{MAX_TASKS}}` | 单轮最多领几张（默认 1，别贪多，超时会被判失败） | `1` |

每台机器只改 `{{AGENT}}`，`{{HIVE_DIR}}` 取该机 `agents/<name>.json` 里 `repos.hive` 的值。
**定时任务里务必用绝对路径**，计划任务的工作目录不保证是你以为的那个。

## 挂到每小时

**方式 A：AI 工具自带的定时任务 / automation**（最贴合「给子 agent 一段 prompt」）
在建自动化时把上面完整版 prompt 粘进去，周期设「每 1 小时」，其余保持默认。
先手动触发一次看输出是不是「本轮无任务」，确认没误改文件。

**方式 B：Windows 计划任务直接唤起 headless CLI**

`codebuddy -p` 的 **`-y` 不能省**（不给会卡在权限确认上静默不动）。完整 prompt 不要拼进
命令行 —— Windows 有 8191 字符上限，中文走 cp936 还会乱码。做法是：把上面完整版 prompt
（占位符已替换）存成 UTF-8 文件，命令行只传一条**纯 ASCII 短指令**让 AI 自己去读：

```powershell
$p = 'Read docs/hourly-prompt.md and execute it strictly. Agent=trae-win; HIVE_DIR=G:\dswork\duan-light-merge\hive; PYTHON=python.'
$tr = 'cmd /c cd /d G:\dswork\duan-light-merge\hive && codebuddy -p "' + $p + '" -y'
schtasks /Create /TN "hive-trae-win" /SC HOURLY /MO 1 /F /TR $tr
```

`schtasks /TR` 对引号、空格、`&&` 很挑剔，生产上更稳的是把这条命令包进一个 `.cmd`
（UTF-8 无 BOM / 纯 ASCII）再注册 `/TR "<脚本绝对路径>"`，这也是
`scripts/win-scheduled-work.ps1` 的做法。注册完先 `schtasks /Run /TN "hive-trae-win"`
手动触发一次，看是不是输出「本轮无任务」且没误改文件。

## 主控这一侧怎么发卡（闭环的另一半）

```bash
python3 hive.py --dir . assign T-xxxx --agent trae-win      # 卡进 inbox/trae-win/
```

或在建卡时直接指定 agent。子 agent 下一轮（最坏 1 小时）就会领走。
注意 `dispatch` 自动派卡要求该 agent 标了 `dispatchable`；**手动 `assign` 不受这个限制**。

**派单—接单（`accept`）模式**（推荐）：把定时接单的机器登记成 `--mode accept`，
`dispatch --apply` 就能直接派给它（不需要 `dispatchable`），接单窗口默认 180 分钟，
卡不会在定时任务醒来前被 watchdog 回收。详见 `docs/主控与分发.md` §4.6。

⚠️ **双写坑**：本机 `claim_backend=store` 时 `assign` **只写 store**，
不写 Git 卡面 / `inbox/`；而定时任务读的是 Git 卡面，会看不到派单。两条都要跑：

```bash
HIVE_CLAIM_BACKEND=git python3 hive.py --dir . assign T-xxxx --agent X   # 卡面 + inbox + push
python3 hive.py --dir . assign T-xxxx --agent X                          # store（复制到其它节点）
```

## 三个必须知道的坑

1. **别和 `win-scheduled-work.ps1` 同时开。** 那是 OS 计划任务跑
   `hive work --once --no-cluster --agent X`，和这里的 AI 定时任务是两套领取者，
   同时跑会重复领卡 / 抢租约。二选一：
   - 已经配好 `exec` + `command` 的机器 → 用脚本，AI 收到的 prompt 由 hive 生成，
     不需要本文这段；
    - 还没配 `command` 的机器（GUI / 只有定时任务的机器）→ 用本文的 AI 定时任务。
      `hive work --once` 在 manual 配置下只会建 worktree、把 prompt 放进剪贴板等人点，
      **不会唤起 AI**；而 AI 自己 `accept` → 干活 → `submit` 正好绕开这个死结。
      建议把这类机器登记成 `--mode accept`，让「派单—接单」成为正式语义。
2. **`--no-cluster` 别省。** 定时领取走纯 git 通道就够了，拉起 8788 集群服务只会把
   选主、端口、冷启动自检这些事牵连进来。
3. **改完 prompt 要能同步到子 agent。** 建议把本文放进 hive 仓并推送，子 agent 每轮
   `git pull` 就自动拿到最新版；否则每台机器各改各的，很快会漂。
