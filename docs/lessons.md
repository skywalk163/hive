# lessons —— 以后建卡 / 配环境时该注意什么

不是流水账，是「同类坑别再踩第二次」。格式：**现象 → 原因 → 以后怎么做**。

新的教训从 `hive digest` 的失败归因、`hive audit` 的风险标记、以及 agent 自己写的
summary 里来。总控每天 `hive digest --days 1` 看一眼；也可以建一张卡让 agent 读
digest + `hive doctor` 产出增补草稿（草稿走 `hive draft` 那套流程）。

## 一、写任务卡

- **verify 必须真跑过，光读一遍不算**。T-0114 的第三条验收我自己写错了
  （`sys.exit` 但没 `import sys`），submit 判 failed，代码其实全对。
  → 写完卡面先手动跑一遍所有 verify 命令再入库。
- **卡面的 `paths` 要把真正会改的文件写全**。写 T-0112 时我顺手把用法写进了 README，
  但 README 不在声明的 paths 里，被自己的越界检测抓了，只能回退重写。
  → 文档改动通常动 README + 手册 + CHANGELOG，三个都列上。
- **`needs` 里的能力要确认有 agent 能满足**。T-0102 写着 `needs: windows`，
  但 4 个 agent 一个都没声明，它就在 ready 列表里躺成僵尸卡。
  → 现在 `draft lint` 会拦这种情况；写正式卡时也该先 `hive agent list` 看一眼。
- **卡要拆小**。digest 里耗时最长的是 T-0004（83 分钟）—— 一张文档卡干了一个多小时。
  → 目标能在一屏内说清的卡才适合派出去；说不清就先写草稿。

## 二、环境与运维

- **worker 必须用 `setsid` 启动**。不用会随父 shell 回收，日志静默停在半路才发现
  （fb88-dsh 死过一次，6 小时后才注意到）。
- **`dsh-exec` 会自动判断运行环境**：看得到 `/compat/ubuntu` 是 FreeBSD 宿主，
  走 `sudo -A` + askpass；看不到就是已经在 linuxulator 里，直接跑不需要 sudo。
  WorkBuddy 的工具环境属于后者。
- **`.env` 的 `OPENAI_API_KEY` 被注释时 exec agent 起不来**：dsh 报
  `MISSING_CREDENTIAL: no API key for provider route "deepseek-official"`。
  `.dsh/.credentials.yaml` 里另有一个 key 但 dsh 不认那个路由名。
- **孤儿租约**：worker 被 kill 前抢到的 claim 分支不会自己消失，要用
  `git push origin :refs/heads/claim/<tid>` 回收（等价于 `gc`）。
- **残留 worktree 会挡住新建**：报「已经检出到」时先
  `git worktree remove --force` + `git worktree prune`。
- **`hive take` 的 `--fresh` 偶尔不生效**（基线还是旧 task 分支），兜底是
  `git -C <wt> fetch && git -C <wt> reset --hard origin/main`。

## 三、并发与提交

- **并发锁的粒度是 paths 不是 repo**。同一个仓里改互不相干目录的卡可以并行；
  paths 重叠（含父子目录）或没声明 paths 的才互相排队。
- **`commit_and_push` 已收口**（T-0114）：每个写命令只提交自己该动的文件。
  但**跑写命令前还是先把自己的手头活提交掉**更省心 —— 收口之前被咬过两次。
- **`git merge` 要显式带 git 身份**。测试环境/容器里常没有 `user.name`，
  merge 会以「请告诉我你是谁」失败，而且**不留任何冲突标记**，
  `--diff-filter=U` 查出来是空的，看起来像"合并成功"，极难查。
- **改动清单要在提交前抓**。提交后 `git status` 是空的，再算就恒为 `[]`。
- **动别人未提交的工作区前，先抓指纹再同步、同步后复核**。协调仓落后且脏时
  （多 agent 共用一台机器的日常），正解是确认传入改动与脏文件无重叠后
  `git merge --ff-only origin/main`——git 保证 ff 只碰两个提交间真正变化的文件，
  脏文件不在其中就原样保留，真在其中 git 自己会拒绝。**不是** stash / checkout
  别人的改动。实操：`md5sum` 四个脏文件 → ff → 再 md5sum 核对（T-0133/T-0135）。
- **测试里模拟「别人推过提交」时，scratch clone 要先 `reset --hard origin/main`**。
  只 fetch 不 reset，本地 main 还停在旧基线上，commit 完推送必然非快进被拒
  （T-0136 测试里 `hive2` 是初始化时 clone 的，之后 hive1 推了不少提交）。

## 四、改 hive 自己的代码时

- **批量替换脚本：锚点必须唯一**。`for name, a in agents.items():` 在
  `cmd_dispatch` 里先出现，把本该插进 `cmd_doctor` 的块插错了地方，直接 IndentationError。
  → 插入前先 `assert s.count(anchor) == 1`。
- **写文件的脚本断言失败 = 什么都没写**。有一次性替换因为第三个断言失败整体没落盘，
  但随后的 `py_compile` 照样显示"编译 OK"，差点以为成功。
  → 落盘后单独 grep 确认，别拿后续命令的输出当证据。
- **自然语言的口供别用固定词表匹配**。`hive audit` 上线后第一次实跑就漏了它自己的
  动机案例：T-0101 写的是「未能启动…未得到实际测试结果」，词表里一条都没命中。
  → 改成正则 `CONFESS_RE`。
- **「只构造对象」也可能有破坏性副作用**。`Worker.__init__` / `Worker.loop()` 曾无条件
  `reset --hard origin/main`，于是 `hive take` / `work` / `submit` 会把协调仓里未提交的
  已跟踪改动（正写到一半的 `drafts/` 草稿）**静默抹掉**——一次「草稿内容被还原」的排查就是它。
  → 排查静默数据丢失用最小实锤法：**把一个已跟踪文件改脏 → 只触发可疑动作 → 看文件还在不在**；
  写会动工作区的代码前先想清楚它跑在主工作区还是独占 clone（MCP 用独占 clone 就是为此）。
  现改为复用 `sync_main()`：对齐/领先就不动，落后且脏直接报错。
- **错误信息必须和实际入参一致**。`hive_land` 传了 `apply=true` 撞冲突时，仍回
  「默认只预演，什么都没动」——这句话只在不传 apply 时成立。LLM 总控靠读 message 判断自己在
  什么模式，会被误导成"我还在 dry-run"然后反复重试同一个 land。
  → 断言错误信息时要覆盖「不同入参下措辞是否还成立」；单测只断言 `status` / `conflicts` 不够，
  **实跑一轮 decision-point** 才暴露得了（这条就是实跑时发现的，已在 0.9.8 修掉）。
- **`0` 是假值，别用 `or` 判断"有没有设置"**。
  `int(card.get("max_retries") or DEFAULT_MAX_RETRIES)` 会把显式写的 `max_retries: 0`
  误判成"没设置"而退回默认 3，于是「一次都不许自动重派」永远不生效。
  → 判断有没有设置用 `is None`（或 `== ""`），别用 `or`。
- **列表字面量插 dict：「打印 len 确认」的口头纪律不够，要配不变式测试**。
  T-0123 踩过「往 TOOLS 插 dict 漏 `{`」，当时只立了"改完打印 len(TOOLS) + 工具名"
  的口头纪律；T-0135 在 block/unblock/abort 上第二次踩（三个 dict 被并成一个、
  同名 key 互相覆盖，两个工具从 tools/list 消失）。现已加
  `test_every_handler_is_advertised_in_tools_list`：handler 与广告位一一对应、
  不许重名、条目结构合法——**能被机器检查的纪律就别靠嘴**。
- **测试里的「期望值」本身也可能是错的**。`tests/test_mcp.py` 的 `EXPECTED_TOOLS`
  （12 个 worker 工具）把上面的 bug 表面固化成了"预期"，`assert names == EXPECTED_TOOLS`
  让测试站在了 bug 那一边。→ 修这类 bug 时必须连测试里的期望值一起改，并想清楚
  「正确的期望应该是什么」。
- **`git status --porcelain` 的前导空格是语义位**（未暂存标志）。`.strip()` 会把它
  吃掉，害我误判「改动被暂存/丢失」白查半天（T-0136 测试）。→ 只能 `rstrip("\n")`。
- **抄协议先分清它为什么存在：一致性 ≠ 可用性**。选主原本要求「凑够多数票才能
  当选」，那是 Raft 防网络分区双主的**一致性**关卡；而 hive 的 store 只是缓存、
  git 才是事实账，要的是**可用性**。结果僵尸记录把 quorum 抬高到实际凑不够，
  集群长期无主（一天 382 次「未获多数」）。→ 引入外部协议语义前先问：
  它解决的是哪个问题？我们的场景是同一个问题吗？抄 OSPF 的**择优**（priority →
  速率 → id，不凑票）才对。
- **两把锁互等，症状是「整集群周期性无响应」**。持主锁时 `_write_json`（521KB，
  40ms+，ZFS 上撞 fsync），以及持主锁时跨到 `store._lock` 取位点，配上补卡线程
  长时间占 store 锁（autocommit 逐行 fsync，194 张卡 18 秒）→ 雪崩。
  → 画一张「谁持哪把锁、里面做了什么慢操作」的表；**任何锁内的 IO/网络/sqlite
  都当作 bug**。日志里 `lock_wait=136ms` 这种数字就是体检指标。
- **修「偶发」前先量化基线，否则分不清是自己弄坏的还是本来就飘**。有个用例改成
  后台写盘线程后失败率从 20% 升到 40%，回退原版跑 10 次确认它本来就 20%，才知道
  是我的改动让 GIL 竞争变严重，于是换掉方案。
  → 动到时序敏感代码时，**改动前后各跑 10 次**记录通过率，别用单次结果下结论。
- **测试里的期望值会固化错误语义**（第二次踩）。`test_known_member_blocks_single_vote`
  断言「1 票不能当选」，这是旧 quorum 语义；改择优后它自然失败。失败**不等于**
  改坏了 —— 要看它钉的到底是「保护目的」还是「旧实现细节」。前者必须保留
  （改写成「更强的可达节点在就不得自封」），后者应该跟着改。
- **文档写着「实现基线」，但代码里根本没有**。`集群架构-v3.1.md` 的选举章节描述了
  `solo_grace` / `backup leader` / `arbiter`，grep 全都不存在。这类文档比没有更危险。
  → 标「设计意图」还是「现状」必须显式；改动机制后顺手 grep 旧名词
  （`quorum` / `多数` / `随机退避`），把过时的正文、表头、环境变量说明一起改掉，
  连代码里的注释也要同步 —— 否则下次读注释的人会照着错的实现。
- **往 JSON 配置文件追加文本前先想合法性**。测试里往 `agents/fb.json` 追加
  `# 注释`，直接把它变成非法 JSON；被测代码读 agent 配置时静默跳过，故障点
  （land 报「没有 agent 登记代码仓」）完全跑偏（T-0136 测试）。
  → 改 JSON 就 load/dump，别当纯文本追加。

## 五、hive 的用法

- 干活流程：`hive take --agent <name>` → 在 worktree 里改 → 写 `.hive/summary.md`
  → `hive submit` → `hive land --apply` 自动合入关闭。
- 合并前看一眼 `hive audit T-xxxx`：比对 agent 的 summary 和真实 diff。
- 每天 `hive digest --days 1`，`hive doctor` 有 error 就退出码非 0。
- **代码被回退后，卡片要同步「记作废」**。hive 的记录和代码仓库会脱节：把 light 仓库的
  commit 撤掉后，hive 里对应卡片仍显示 `done/closed`，会骗人以为改动还在。
  → 撤销代码后同步加备注：`hive close <tid> --note "作废（日期）：…"`
  （`cmd_close` 可重复调用，只追加备注，对已关闭的卡也不会报错）。
- **「合并推成但关卡被拦」的半途状态：同一个绕步出现两次，就该停下来开卡修机制**。
  T-0133/T-0135 连踩两次——`land --apply` 的合并在临时 worktree 里成功推入
  origin/main，收尾 `cmd_close` 的 `sync_main` 却因协调仓「落后 + 有未提交改动」抛错，
  代码合了、卡片没关，操作者只看到一句「不敢自动 reset」，完全看不出合并已成功。
  两次都靠人工 ff + close 补救。→ 0.9.12（T-0136）已修：`_sync_for_commit`
  （落后试 ff，git 保证不碰脏文件）+ land 关卡失败报 `merged-not-closed`
  （明说"已合入、没关成"并给出收尾命令）。**别扭的绕步骤别手工绕第三次。**
