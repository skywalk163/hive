# Changelog

## Unreleased

### Added

- **主控优先级：OSPF 式 `leader_priority`（值大者优先，默认 1）**。
  `preferred_leader` 是「本机配置」，想让某台当家得在那台上单独配一条、别的节点
  还看不见。优先级改成每台自己声明「我排第几」，随节点自述广播，全集群可见。
  六条语义全部照抄 OSPF 的 DR/BDR 选举（取值范围 0-255 也是照抄 8bit 接口优先级）：
  - **值大者优先**：排序键 `(-priority, node_id)`，优先级高的选举延迟更短，先发起；
  - **同值按 node_id 定序**：相当于 OSPF 用 Router ID 打平手，顺序确定、不会同轮
    打平后反复重选；
  - **不抢占在位主控**：优先级只在「当前没有主控」时才起作用。允许抢占的话，
    设了 2 的机器每次重启都抢一次主控 —— 而重启往往正发生在排障时；
  - **0 = 放弃竞选资格**（OSPF 里 priority 0 的接口是 DRother，永远不是 DR/BDR）：
    自己不发起选举、集群侧也拒投它、`promote` 同样不越过；但仍参与 quorum，
    给只读副本/弱机器用。
  全集群都是 0 时退化成人人 1，否则没人愿意选举、集群永远没主控（这一条是
  刻意偏离 OSPF 的可用性取舍：OSPF 全 0 就是没有 DR，广播网络直接不通）。
  `hive cluster priority` 看排序、`--priority N` 改本机（改完自动重启生效），
  `hive cluster peers` 多一列 PRIO。两个 parser（`hive.py` / `hive_cluster.add_parser`）
  都已同步 —— 只改后者的那份是不生效的。

- **人工指定主控：`hive cluster promote` / `prefer` / `start --leader`（T-0260）**。
  自动选主只看「谁先发起 + term 谁高」，和「谁该当主控」无关 —— 常年开机那台被
  笔记本抢走主控之后再也回不去，是实际困扰。现在留了三条人工通道：
  - `hive cluster promote [--node X]`：立刻把 X（默认本机）推上主控。先请现任主控
    `resign` 主动下台（避免双主窗口），再把本机 term 追到集群最高，最后 `term+1`
    选举。**追 term 这步是关键**：重启节点 term 往往落后一整截，不追的话
    `term+1` 仍比在位主控小，投票以 `stale_term` 被拒，现象是「敲了没反应」。
  - `hive cluster prefer`：设本机为首选主控。被偏好的节点选举延迟为 0，其它节点
    让出一个窗口，所以主控失联时它最先发起并拿回主控权。偏好是本机配置，
    只在「当前没有主控」时生效。
  - `hive cluster start --leader`：启动时直接把自己记为首选。
  - `hive cluster leader`：一行看清本机角色、当前主控、租约剩余、在线数与多数门槛。
- **夺权可以越过「store 落后不许当选」那道闸**。`handle_vote` 原本拒绝给 store
  落后的候选投票（防止选出数据落后的主节点），但重启节点的 store 几乎必然落后，
  人工夺权会被自己人拦住。现在投票请求带 `force: true` 时放行，依据是
  **store 只是缓存、git 才是事实账**，落后会在当上主控后由 `StoreReconciler`
  从卡面补回。放行时写 `leader_forced` 事件注明落后条数，事后能分辨这次任期
  切换是「按规矩选的」还是「人硬点的」。不想放开就设
  `HIVE_CLUSTER_ALLOW_FORCE_PROMOTE=0`，语义变成「宁可没主控也不接受落后节点」。
- **`scripts/agent-exec.py`：一处收口所有 AI CLI 差异，让「主控分配 → AI 完成」
  真正闭环（T-0259）**。此前 `agents/*.json` 里 14 个 agent 的 `command` 和 `launch`
  都是空字符串，worker 收到卡之后没有任何能把任务喂进 AI 的入口，只剩「复制到剪贴板」，
  于是多机协同必然退化成人工粘贴。这个适配器按 `codebuddy` → `cbc` → `traecli` →
  `dsh-exec` → `dsh-headless` → `dsh` 的顺序探测本机可用的 headless CLI（先查 `PATH`，
  再查脚本同目录），把 prompt 交给它执行、把输出接到 stdout、把退出码原样传回。
  **不把 prompt 拼进命令行**：Windows 的 cmd 上限 8191 字符，长卡必炸，中文还要过
  cp936；改成只传一句固定短指令，让 AI 自己读 worktree 里的 `.hive/task.md`——
  既绕开长度限制，也保证 AI 拿到的是卡面原文而非转述。`--probe` 自检这台机器
  能走哪条路；`HIVE_AGENT_CLI` / `HIVE_AGENT_ARGS` / `HIVE_AGENT_MODEL` 可覆盖。
- **`join-agent.{ps1,sh}` 支持 `-Mode auto`（默认）**：机器主人不需要知道
  exec / manual 的区别。探测到 CLI 就自动切 exec（主控派卡后全程无人），探测不到
  就切 manual，并在常见安装路径里找 Trae / WorkBuddy 自动填 `launch`。
  Windows 侧额外生成本地 `agent-exec.cmd` 包装，避免 PowerShell 把带空格的路径
  传给原生 exe 时断成两截，也让「以后调 CLI 参数」不必重新登记 agent。
  两个脚本都新增 `-Probe` / `--probe`：只报告、不登记、不写文件。
- **manual agent 也能被主控自动派卡（T-0259）**。此前 `dispatch` 里
  `if mode == "manual": continue` 一行，让 15 个 `mode: manual` 的机器全部不进候选，
  `dispatch --apply` 恒输出「没有可分派的任务」，人只能 `hive prompt` 再手工复制到
  IDE —— 这是「多机协同其实全靠人肉转发」的直接原因。现在 manual 默认仍不派，
  但 agent 配置里显式写 `dispatchable: true` 就参与候选（语义：这台机器上有常驻
  worker 会在收件箱里接活）。`agent add --dispatchable` 新增该开关；重新登记时
  不传该参数视为保持原状，避免脚本里复制粘贴旧命令把它悄悄关掉；`agent list`
  用 `[可派]` 标出来。
- **manual 卡不再只有「人写 summary.md」才算完成（T-0259）**。旧逻辑死等
  `summary.md` 到 `timeout_minutes` 超时，把已经改完的卡判成失败（GUI agent 忘了
  写 summary 是常态）。现在三种信号任一成立即收工：summary.md 非空、HEAD 变了、
  工作区有改动。后两条要求改动快照连续两次相同（`HIVE_MANUAL_SETTLE_SECONDS`，
  默认 20s）才认，避免「改到一半」误判。**不放松任何质量闸门**：越界检查、verify、
  结果闸门照旧全跑。
- **`scripts/master.sh` / `scripts/worker.sh` / `scripts/join-agent.{sh,ps1}`**：
  把「主控派活 / 机器干活」拆成两个可独立启停的常驻进程，各带单实例 PID 保护；
  接入一台新机器从「按文档敲七八条命令」变成一条 `join-agent`（登记 + 起 worker +
  注册登录自启）。文档见 `docs/主控与分发.md`。

- **多机副本一致性：把「启动时一次摄入」升级为持续不变式，并让 `doctor` 自己喊出不一致**。
  承接缺卡 claim 误报的复盘，目标始终是让多机多 agent 看到同一份事实：
  - `Cluster._ingest_store_cards()` 对**缺行**改走 `Store.ingest_card` 的 op 日志
    （产生 op、推进 `commit_seq`），已有行仍只补卡面定义、不碰运行时状态；
    leader 在维护循环里每 60s 低频 reconcile 启动后新增的卡。旧实现直接
    `ingest_cards` 写库不产生 op，曾造成 leader 138 张 / follower 134 张而两边
    `commit_seq` 都是 43 —— 追平条件 `target > commit_seq` 永不成立，差异不会自愈。
  - `hive doctor` 新增 store 体检：列出「Git 有、store 没有」的卡号；对比本机与
    各 alive 节点的 tasks 数 / `commit_seq`，**水位相同但内容不同按 error 报**，
    只是水位落后按 warn 报；集群不可达时只做本机检查。真实 follower 上实跑已
    报出现场的 134/138 差异。
  - 协议适配收敛：新增 `unsupported` 类别，`_ingest_missing_card` 只看 `kind`
    决定是否回退旧 leader；中文文案兼容只留在 `_classify_store_error` 一处，
    业务代码不再解析错误字符串。
  - 文档：`docs/架构与排障全景.md` 新增「多机副本一致性（store 模式）」章节，
    写清三条不变式、按「下一步动作」划分的错误分类、体检输出与部署生效边界；
    排障表补了三条对应现象。
  - 测试：`tests/test_store_claim_errors.py` 增至 34 项。
- **看板「任务计划」：把一组任务当一个项目跟踪**。搁置旁边多了「计划」Tab（同一区域
  两个 Tab 切换，搁置原有的折叠/计数/出口全不动）：
  - 字段：ID（`P-0001`）、前缀、标题、描述、状态、是否置顶、关联任务 ID 列表、创建人、
    创建/更新时间、完成时间。状态 `active` / `completed` / `closed` / `deleted`（软删）。
  - 页面上能新建、编辑、完成、关闭、删除、置顶/取消置顶。完成即移入下端的「已完成」
    区；关闭与删除都要二次确认（删除只改状态，记录仍留在 `dashboard/plans.json`）。
  - 排序：进行中置顶在前，其余按更新时间倒序；已完成按完成时间倒序。置顶只对进行中
    生效，计划一完成/关闭就自动撤掉。
  - 关联任务显示 **总数 / 已完成数 / 完成率**，进度从任务快照现算（不另存计数，
    避免「快照更新了、计划还显示 3/8」）；全部完成时按规则自动把计划标为已完成
    （每张计划可单独关掉 `auto_complete`）。
  - 权限复用现有 token 模型：创建者本人或 controller/admin 可写，普通成员只读；
    删除还要 `confirm=true` 或 controller。所有写操作逐条进 `dashboard/audit.log`。
  - 留言通道也能操作计划（agent 没有浏览器，只有留言）：
    `/plan create <前缀> <标题>`、`@command:plan update <ID> 标题=… 关联=T-0001,T-0002
    置顶=on`、`@command:plan create 前缀=PAY 标题=… 关联=T-…`（一次带上被跟踪的任务组）、
    `complete` / `close` / `delete` / `pin` / `unpin`。格式错会回一段带示例
    的用法；权限不足直接拒；留言里删除只给 controller（没有二次确认这一步可用）。
  - 指令从两条路进来都生效：看板留言框（当场执行），以及别的机器 agent 用
    `hive chat post` / MCP 发进集群的（由拥有计划文件的看板在轮询时补执行，
    增量游标 + 消息 id 账本保证不重放历史、不重复执行）。
  - 新增 `dashboard/plan_store.py`（JSON + 原子写 + 进程内锁）、`/api/plans`、
    `dashboard/plans.json`（已进 `.gitignore`）；测试见
    `tests/test_dashboard_plans.py` 与 `tests/test_dashboard_plans_ui.py`。

### Changed

- 看板统计条多了常驻的「计划 N」入口（进行中数量），点它切到计划 Tab。

- **任务状态「搁置」（`shelved`）：不能忘，但大部分时候可以忽略**。有些卡当前确实
  做不了：打回 `ready` 只白占一个名额、还会干扰 agent 领取；一直挂在异常里也不对（情况
  已知，租约/结果/进度这些现场完全可以释放）；直接删更不行，删了就真忘了。
  - `hive shelve <tid> --reason "…"`：卡面写 `shelved` + `shelved_at` /
    `shelved_from` / `shelve_reason` 和现场备注；**释放** `claim/` / `res/` /
    `status/` 远端分支、`results/<tid>.json` 归档、`inbox/*/<tid>.md` 分派信号、
    本机与集群租约，并推进 fence 让旧 worker 复活写进来的结果一律作废。
  - 三个出口：`hive unshelve <tid>` 放回 `ready`；
    `hive unshelve <tid> --spawn-new --title "…"` 以搁置卡为模板另立一张
    `ready` 新卡（继承 repo/base/priority/verify/depends_on，原卡保持搁置）；
    `hive close <tid>` 废弃。卡面备注里也写着这三条命令。
  - 不挂账：不进任何看板列、不进「共 N 个任务」活跃统计、`doctor` 不报、
    不归档（冷热分离只收 `done`/`closed`）。`dispatch` / `hive take` /
    MCP `hive_claim` / `hive land --all` 一律跳过；`retry` / `unassign` /
    `block` 会拒绝搁置卡，唯一出口是 `unshelve`。`hive_whoami.todo` 单列
    `shelved` 清单，免得封着封着就忘了。
  - **排版用按钮，不加第 7 列**：六列已经够挤，搁置卡也不该跟活跃卡平级。
    顶栏统计条常驻一枚低调的「搁置 N」，看板上方是一条默认收起的横条，点开才
    展开卡片列表（原因 / 搁置时间 / 原状态 + 放回、另立、废弃三个按钮）；
    搜索命中搁置卡时，折叠态会显示「有匹配」提示。详情抽屉也补了搁置信息块。
  - 活跃列（待领取 / 进行中 / 待合并 / 异常）卡片加「搁置」按钮；
    `/api/task` 白名单加 `shelve` / `unshelve` / `respawn`，均限 controller 角色，
    `respawn` 校验标题并把新卡号回给前端。
  - 派发指令加 `mode=shelved`：搁置卡的「复制」拿到的是写明**不要领取**的搁置说明，
    而不是一句「请领取任务」把 agent 又勾回来（JSON 输出也不带 `claim_tool`）。
  - `hive top`（TUI）加第五组「搁置」，计数常驻但排在最后，不抢前四组注意力。
- **看板「留言」：跨机给 agent 递话的聊天框**。机器 A 对某张卡的理解/疑问、
  对机器 B 上另一个 agent 的指点，以前只能贴到某个窗口再口口相传；现在看板右下角
  点「留言」就能发。频道三种：`hall`（大厅）、`task:T-0231`（某张卡的讨论线程）、
  `agent:名字`（私信）。任务卡和详情抽屉里都有「讨论 n」入口，点了直接进那张卡的线程。
  - 存储走集群而不是 git：worker 的 worktree / MCP 的独占 clone 每轮都
    `reset --hard origin/main`，写进仓里的留言会被冲掉；`git push` 也撑不起秒级。
  - 每个节点 `~/.hive/cluster/chat.jsonl` 追加一份（一行一条 JSON），内存保留最近
    2000 条；follower 每 2 秒向主节点增量拉取，主节点不可达时本地留存（看板照样能看能发）。
  - 接口：`GET/POST /api/cluster/chat`（集群）、`GET/POST /api/chat`（看板，带写鉴权，
    集群不在时降级到 `~/.hive/chat/chat.jsonl`）。
- **`hive chat post / list / wait`**（`--task` / `--agent` / `--scope`、`--kind
  note|question|answer`、`--unread --mark-read`、`--json`）。`wait` 是长轮询，
  agent 干到一半想知道有没有人留话，用它比反复 list 省事；未读水位记在
  `~/.hive/chat-seen.json`，每台机器各记各的。
- **MCP 工具 `hive_chat_post` / `hive_chat_read`**：agent 可以直接把疑问、跨任务的
  经验发出来，也能读到「大厅 + 私信 + 我名下各卡」的留言；`hive_claim` 会把这张卡的
  留言回在 `chat` 字段里，并落一份到 worktree 的 `.hive/chat.md`。
- `hive prompt` 渲染派发指令时，自动把该卡的留言附在末尾（最多 10 条）。
- **看板「复制派发指令」状态感知**：同一个按钮按卡的运行时状态渲染不同文本
  （判定口径与看板分列对齐）。待合并卡复制出的是给主控的**合并指令**
  （`mode=merge`：结果分支 `res/<tid>`、head、执行 agent、`hive land <tid> --apply`、
  冲突处理），不再是叫人领取的旧文案，按钮文案也跟着改成「复制合并指令」；
  异常卡（failed / blocked / 租约过期）在派发指令末尾附**溯源块**（`mode=abnormal`：
  此前领取 agent、领取/异常时间、异常状态与摘要），重派时新 agent 知道前情；
  其余状态 `mode=claim` 与旧行为逐字一致。`/api/dispatch-prompt` 响应新增 `mode`
  字段；状态快照读不到时自动降级为原渲染，不影响复制。

### Fixed

- **幽灵节点复发：查清「删了又活」的根因并给出固定处置**。`_adopt_leader_locked()`
  收到「leader 是 X」的快照时会 `_update_node_locked(rec, touch=True)`，只要
  `leader_id` 还指向 X，每次心跳就给 X 续命 —— 单独 `leave` 删节点表下一拍必复活。
  正确顺序是**每个节点**先 `resign`（断 leader 指向）→ `leave`（删记录）→
  `promote` 产生更高 term 的新 leader。已按此清掉指向 104 的
  `laiweilideMacBook-Air` 僵尸记录，fb98 以 term 58 重新当选，节点表回到 2 个。
  文档 §7.1 留了可复制的处置命令。
- **新增 `scripts/win-sync-restart.ps1`**：Windows 节点只开 445/8788、无 SSH/WinRM，
  集群 HTTP 也没有 exec 接口，所以「同步 hive 代码 + 重启服务」需要一次人工
  bootstrap。脚本默认只体检，`-Apply` 执行 `git pull --ff-only` + 官方
  `cluster restart` + 探活，`-EnableRemote` 额外开只放行内网的 OpenSSH Server，
  之后主控可直接 ssh 运维。**UTF-8 BOM + CRLF**（PS 5.1 含中文注释必需）。
- **主控优先级复核 OSPF 语义后收紧三处**（用户补了「0-255、默认 1、值大者优先、
  0 放弃资格」的准确定义）：
  1. **取值范围 0-255 原先没校验**：`--priority 300` / 环境变量 / `config.json`
     里的越界值会原样进排序。现在 CLI 越界直接报错（含 `serve/start --priority`），
     其它来源统一 `normalize_leader_priority()` clamp 到 0..255，解析不了回默认 1。
  2. **0 原先只是「本机自觉不发起」，集群侧没有防线**：`handle_vote` 不看候选人的
     `priority`，`_start_election(force=True)` 也不拦 —— 跑老代码或配错的节点一抢跑
     照样能凑够多数当上主控，`promote` 也能把弃权节点推上去。现在 0 是硬资格：
     正优先级节点拒投弃权候选人、自动/人工通道都提前拦住，`promote` 返回可照做的
     提示（先 `cluster priority --priority 1`）。全员 0 的退化路径同步放开，不误伤。
  3. **两个边角**：`preferred_leader` 指向一台弃权节点时，其它节点会一起让路空等
     导致无人当选 —— 现在视同没配偏好；`election_jitter=0` 会让所有节点同一刻开跑、
     优先级失效 —— 名次间隔加了 0.05s 下限。
  文档 §4.3.2 / §6 与 `--help` 同步；`tests/test_cluster.py` 新增 7 项覆盖。

- **集群节点被反复「拉起」导致端口冲突 + `database is locked` + 主控反复易主**。
  三个缺陷串成一条雪崩链，症状是 `cluster status` 时好时坏、term 每分钟涨几,
  `hive cluster leader` 一会儿说本机、一会儿说别人：
  1. `_local_health()` 的超时写死 **0.45s**。节点一忙（store 追平上百张卡、
     git 同步）health 往返就超时，`orchestrate` 每轮 tick 都判定「集群没起来」。
     改为默认 3.0s，可用 `HIVE_CLUSTER_HEALTH_TIMEOUT` 调（连接被拒在本地是
     立即返回的，放宽超时不会拖慢「确实没起」的判定）。
  2. `ensure_background()` 只看 health 就 spawn，不看 PID。health 一时不响应
     就再拉一个进程 → 抢不到端口（`Address already in use`）却打开同一个
     sqlite（`database is locked`）→ 把正在服务的节点拖慢 → 下一轮又拉。
     现在：PID 还活就只提示「进程在跑但未响应，卡死请用 `cluster restart`」。
  3. `_spawn_background()` 在 Popen 之后**立刻**把 pid 文件写成自己的 pid，
     而失败的进程随即退出 —— pid 文件从此指向死进程，下一轮 ensure 读不到活
     PID 又去 spawn，且 `cluster stop` 按它停不掉真正的节点。改为子进程自述时
     自己写、父进程只在确认健康后兜底补写。

- **`hive unblock` 之后卡派不出去（回归里暴露的既有问题）**：解挂只清了
  `dispatch_miss`，没清 `dispatched_to` / `dispatched_at`；而 `dispatch` 的
  「刚推送过（TTL 内不重复派）」看的是后者，于是刚解挂的卡又被挡 60 分钟。
  现在解挂与 `shelve` 同一口径清掉推送痕迹，`tests/test_core.py` 加了断言钉住。
  用未改动的 HEAD 复跑同一用例同样失败，确认与本次 store 改动无关。
- **缺卡 claim 不再误报「已被别人领取，抢不到」，且会自动补摄入 leader store**。
  现场：leader 进程只在启动时 `ingest_cards` 一次，启动后新增的 T-0254~T-0258
  在 store 里缺行；`Store.claim` 抛的是「没有任务 T-0257」，但 `Worker.claim`
  把原因丢进日志只回一个 `False`，`hive_claim` / `hive take` 又把一切失败兜底成
  「已被 别人 领取，抢不到」——卡其实 `ready`、远端也没有 `claim/T-0257`，
  误导人去找一个根本不存在的持有者。
  - **补摄入**：`hive_store` 新增 `Store.ingest_card(tid, card)`，缺行才插入、
    已有行只补卡面定义并保留运行时 `state/owner/retries`；**走 op 日志**，所以
    复制到所有副本。旧 `ingest_cards` 直接写库不产生 op，正是本次现场
    leader 138 张 / follower 134 张、两边 `commit_seq` 却都是 43（心跳追平条件
    `target > commit_seq` 永不成立）的原因。`hive_cluster._store_op_for` 新增
    `store/ingest` 动作；`Worker.claim` 遇 `E_NO_TASK` 时用本机 Git 卡面补登记一次
    再重试；旧 leader 没有该动作时回退现成的 `store/assign`，follower 先升级也能自愈。
  - **错误细分**：`StoreError` 带 `code`（`E_NO_TASK` / `E_TAKEN` / `E_CLOSED` /
    `E_HAS_RESULT`），`ClusterError` 透传、`handle_store` 的 409 响应新增 `code`；
    `hive.py` 新增 `StoreCallError(kind/code/http)` 与 `_classify_store_error`，
    分成 `no_task` / `taken` / `unavailable` / `other`（旧 leader 无 code 时用
    已知中文文案兜底）。`Worker.claim` 保留 `last_claim_error`，`cmd_take` 与
    `hive_claim` 按类别出文案并给 `hint`；只有真被领走才说「已被 X 领取」，
    git 模式行为逐字不变。
  - 测试：新增 `tests/test_store_claim_errors.py`（24 项），store / cluster / mcp /
    claim_backoff / store_overlay 回归 88 passed。
- **Windows 判据「单引号 + 空格」被判 failed（T-0228）**：`git grep -q 'version = "0.4.1"' --
  pyproject.toml` 在 cmd.exe 下 exit 128（`fatal: unable to resolve revision: =`），
  产物却是好的。老垫片只拦「单引号里有 `|` / `&`」的片段，这一段两个都没有，
  于是原样放过；而 cmd 不剥单引号、空格照样切参数，argv 变成
  `['git','grep','-q',"'version",'=','0.4.1'",'--','pyproject.toml']`。
  现在 `_win_single_quote_fix` 把「含空格或 `|&<>^` 的单引号片段」一并换成双引号，
  内层双引号转义成 `\"`（同时含 `"` 与 `|&<>^` 时不转，避免 cmd 引号翻转后元字符暴露）。
  双引号区域内的 `'`（`git grep -q "don't"`）不再被误当引号配对。
- **`test -f 'v4|v7'` 被垫片自己改坏**：旧顺序是「先转单引号、再交 `hive_compat.rewrite`」，
  产出 `run 'test -f "v4|v7"'`，cmd 剥掉内层双引号后 `|` 暴露成管道。
  改为垫片认得的写法先认领（`rewrite` 自带外层双引号保护），转引号只作用于其余命令。

## 0.15.2 — 2026-11-01

> 这一版回应的是「看板打不开了」：**地址没写错，是两个进程 + 一道防火墙各自失守**，
> 而 `status` 又只报了集群地址，人无从自查。

### Added

- **`hive status` / `hive cluster status` 末尾多一行看板地址**：形如
  `看板 http://192.168.0.100:8787/  运行中` / `未运行（hive dash start 拉起）`。
  端口与绑定地址都取自 `hive_ports.py`（唯一来源），人不用再记 8787/8788；
  `--json` 不追加（`app.js` 与既有脚本对「顶层是数组」硬校验）。
  只绑回环时额外提示 `ssh -L 8787:127.0.0.1:8787`，避免把「绑错地址」误判成「服务挂了」。
- `hive dash start` / `dash restart` 成功后直接打印确切访问地址（以前只报 pid 与端口）。

### Changed

- **`hive cluster start` / `cluster restart` 顺带把看板拉起来**（`hive dash start` 等价）。
  看板与集群是两个进程，但人的心智里「重启服务」= 一切恢复；此前 `cluster` 在
  `DASH_AUTOSTART_SKIP` 里，于是「重启完还是打不开看板」。`cluster stop` 仍不动看板。

### Fixed

- **（根因）Windows 上「按需自愈」从来没真正生效**：`_dashboard_spawn` 用
  `DETACHED_PROCESS` 拉起 `cmd /c dashboard-service.cmd start`，而脚本里紧接着调
  powershell 的 `Start-Process` —— **无控制台的 powershell 卡在控制台分配上**。
  实测：cmd 进程 30 秒不退出、8787 永远不通、日志一个字都没有，只留下一个死 pid 的
  `dashboard.pid`。于是「看板没了」之后，任何 `hive` 命令的自动兜底都只是再挂一个
  孤儿 cmd 进程，看板永远起不来（这也解释了此前「重启服务还是一样」）。
  改为 `CREATE_NO_WINDOW`（不可见但真实的控制台）：实测 **1.0s 内就绪**。
  dashboard 本身由 `Start-Process -WindowStyle Hidden` 起，自带独立控制台，父进程退出照旧活着。
- **看板进程死掉后没有任何线索**：`dashboard-service.cmd` 的 `Start-Process` 没做重定向，
  崩溃时 stdout/stderr 全部丢失，只剩一个残留的 `~/.hive/run/dashboard.pid`。
  现在落到 `~/.hive/logs/dashboard.log`（启动横幅）与 `dashboard.err.log`（traceback），
  并加 `python -u` 免缓冲——进程被秒杀也不会丢最后几行。
- **`dashboard-service.cmd` 改纯 ASCII**（注释与 echo 全改）。cmd 按 OEM 代码页逐字节
  解析批处理，UTF-8 中文会让字节计数错位：轻则 `rem` 片段被当命令执行（`'杀整棵进程树…'
  不是内部或外部命令`），重则 `echo` 前缀被吃掉（`echo 端口：%PORT%` 变成执行 `端口：?PORT`）。
  顺带修掉 `if (...)` 块里 `echo not running (stale pid file)` 的 `)` 提前闭合块导致的
  静默失效。中文提示统一由 `hive.py` 打，脚本输出另外按 UTF-8→OEM 兜底解码，不再乱码。
- **`_dashboard_run_script` 补 `no_window_kwargs()`**：无控制台父进程（agent/服务里跑
  `hive`）连开 `cmd /c` 会撞 conhost 分配竞争，表现为 `hive dash start` 偶发永久挂起
  （0 CPU、端口永远不通）。`tests/test_no_window.py` 的僵尸白名单条目随之移除。
- **`dashboard-service.cmd` 里的 `tasklist | find` 会凭空造出一扇控制台窗口**（现场那两扇
  标题 `find  "11916"` 的窗口，其中一扇从 00:52 挂到 10:07）：管道右半边是 helper 进程，
  父 cmd 没有控制台时它拿到一个**可见**控制台，然后永久阻塞在读 stdin 上。判活改为一次
  `Get-Process -Id`（无管道、无额外进程）。
- **脚本输出「静默消失」与「永久挂起」是同一个改动的两副面孔**：给子进程 `CREATE_NO_WINDOW`
  之后，直接继承 stdout 的 cmd 的 `echo` 会被丢弃（`dash start/status` 一个字都不输出、
  rc 却是对的）；换成 `PIPE` 又会被 `Start-Process` 起的看板继承句柄、`run()` 永远等不到
  EOF。最终方案：`CREATE_NO_WINDOW` + `stdin=DEVNULL` + **临时文件**重定向，读完转发。

### Ops（fb98 / 0.88）

- **pf 放行 8787/TCP**（仅 192.168.0.0/24 + 192.168.1.0/24）。0.15.1 把看板默认绑到
  `0.0.0.0`，但 `/etc/pf.conf` 里仍写着「8787 只绑 127.0.0.1，由 lo0 覆盖，不在此开」——
  前提失效、规则没跟上，于是本机 `curl 127.0.0.1:8787` 200、局域网访问全被末条
  `block drop` 丢掉。备份 `/etc/pf.conf.bak.20261101-hive8787`。

## 0.15.1 — 2026-10-30

### Changed

- **看板默认绑定地址 127.0.0.1 → 0.0.0.0**：看板本来就是给人看的，绑回环让每台机器
  都要一条 SSH 隧道。默认值仍然只在 `hive_ports.py` 定义一次（`DASHBOARD_HOST`）；
  `hive dash --host` / `HIVE_DASHBOARD_HOST` 可随时收回本机。
- **写操作保护随之自动生效**：非回环绑定下 `--auth auto` 会要求 token（0.15.0 已实现），
  读接口保持开放——所以 8787 只能放在可信内网，别直接暴露公网。
- `hive dash url` / `dash status` / 两个服务脚本的提示改为给出「本机 + 局域网」两档地址，
  不再把 `0.0.0.0` 直接当访问地址印给人看。

### Fixed

- **Windows 上 `hive dash start/stop/status` 完全不可用**：`dashboard_script()` 固定先找
  `dashboard-service.sh`，然后拿 `/bin/sh` 去执行 → `WinError 2`。改为按平台挑包装脚本
  （Windows 选 `.cmd`，其它选 `.sh`）。
- `hive dash` 的 `--host` 一路传到服务脚本与 `serve.py`（以前停在 `hive.py` 这一层，
  脚本仍按旧默认起服务）。
- 探活地址跟绑定地址走：绑到具体网卡地址时，探 `127.0.0.1` 会永远「没起」，ensure 每轮
  重复拉起。绑 `0.0.0.0` 时仍探回环（一定通）。

## 0.15.0 — 2026-10-30

> 这一版回应的是「纯 Git 模型在实战里暴露的三个缺口」：易失的租约状态、Windows 的
> 平台不确定性、以及「合并」这一步的人工摩擦。全部改动都保持 **git 仍是唯一真相**。

### Added

- **租约 fencing token + 本地留底**（`docs/租约与fencing.md`）：
  - 每次领取生成唯一 `lease_id` 并把 `fence` 推进一格（写进 `claim/<tid>` 分支和任务卡）；
  - `Worker._write_result` 推送前对账（claim 存在？还是我？lease_id 一致？），
    不匹配抛 `LeaseFenced`，**整条丢弃、不覆盖**；失败路径同样受控；
  - 回收/重派（watchdog、`retry`、`unassign`）推进卡面 `fence`，`land`/`status`/`doctor`
    据此识别「过期租约写入的结果」并拒绝合入（`plan.status = "fenced"`）；
  - 本机留底 `~/.hive/leases/<tid>.json`（0600、原子写），`HIVE_LEASE_DIR` 可覆盖；
  - 新命令 `hive lease list/show/clear`；`hive status --json` 增 `lease_id` / `fence` /
    `claim_fence` / `result_fence` / `fenced` 字段。
- **PR 自动化（可选）**（`docs/PR自动化.md`）：新模块 `hive_pr.py`（仅标准库）+
  `hive work --pr`、`hive submit --pr`、`hive pr <tid> [--dry-run] [--provider]`。
  推送 `task/<tid>` 且 verify 全绿后向 Gitea/GitLab 创建（或复用）PR；
  token 走环境变量/`~/.config/hive/gitea.env`，日志与异常一律脱敏；
  失败只记 `result["pr"]["error"]`，不影响任务结论。GitHub 明确提示用 `gh pr create`。
- **看板写操作鉴权 + 审计**（`docs/看板写操作与鉴权.md`）：`serve.py --auth auto|on|off`
  （默认 `auto`：非回环绑定才要 token）；复用 `mcp_auth` 的 token 表，
  `POST /api/login` 换成 HttpOnly cookie；`close`/`reopen` 限 controller 角色；
  每次写操作追加一行到 `dashboard/audit.log`（成功/失败/未鉴权都记）。
  前端顶栏新增「登录」按钮（`#login-btn`）。
- **Windows verify 官方基准**（`docs/windows-baseline.md` + `windows/verify-smoke.ps1`）：
  真机一键 17 项判据（工具链、init/assign/work、UTF-8 往返、只读不产出 task 分支、
  红灯 verify 退出码、fencing 拒绝过期写入、close 归档）。实测 **17/17 PASS**
  （Win10 19045 / Python 3.14.7 / Git 2.55.0）。
- **`hive_compat.py`：verify 的 POSIX 垫片**：Windows 上把 `true` / `false` /
  `test -e|-f|-d|-s|-r` / 整数比较转成等价判断；未知写法原样交给 shell，绝不猜成功。
  `HIVE_COMPAT=0` 关闭。修复了「同一张卡在 FreeBSD 绿、在 Windows 必然红」的跨平台断层。
- **集群默认化**：`hive init --cluster` 顺手拉起本机集群；`hive work` 默认拉起
  （`--no-cluster` 或 `HIVE_CLUSTER_AUTOSTART=0` 关闭），多机最终一致性窗口从分钟级到秒级。

### Changed

- `hive init` 会一并复制 `hive_pr.py` / `hive_compat.py`（缺模块时相关功能给出可操作的错误）。
- `run()`：**给了 stdin 就走二进制**。以前 `text=True` 在 Windows 上会把送进
  `git hash-object -w --stdin` 的 `\n` 改写成 `\r\n`，同一份结果 JSON 在 Windows 与
  Linux 上刷出的 blob 字节不同（跨平台 diff / 校验全乱）。输出侧仍自解 UTF-8。
- `tests/conftest.py`：子进程夹具统一 UTF-8（`encoding="utf-8"` + `PYTHONIOENCODING`），
  修掉 Windows 上「中文输出 → UnicodeDecodeError → stdout=None → json 爆栈」的假失败；
  同时把 `HIVE_LEASE_DIR` 隔离开，别把测试卡的租约留底写进开发机 `~/.hive`。

### Fixed

- **fencing 不该误伤「本就没 claim」的人工链路**：claim 分支不存在时，只有**本地有我这份
  租约留底**才判 `LeaseFenced`；总控 `assign` 后由 worker/人工直接跑（从未 claim）仍允许写结果，
  结果标 `lease: "missing"` 便于事后分辨。
- **`hive pr --dry-run` 不再要求 token**：它只是「看看会发什么」，没配 token 的机器也能用；
  dry-run 结果里带 `api` 与 `request`（method/url/head/base/title）。
- **并发探针的时序缺陷**（`tests/test_worker_parallel.py`）：Windows 上 worker 的启动阶段
  （fetch / worktree add / 写 task.md）走进程内 git 锁是串行的，1 秒的探针在第一张卡退出后
  第二张才进 agent，峰值必然测成 1。探针睡 4 秒后稳定测到 2；被测逻辑未改。
- **平台相关的测试口径**：`test_same_bytes_would_have_crashed_without_errors` 不再依赖
  「子进程解码异常会不会冒泡」（Windows 上线程异常只留 warning），改为直接对同一批字节做
  严格解码；`test_repos_falls_back_when_no_path_exists` 按 `Path` 规范化后比较，
  不再把 `/also/missing` 与 `\also\missing` 当成回归。
- Windows 上 `cmd /c` 与 `cmd.exe /c` 的引号差异、PowerShell 5.1 传参吞引号
  （都固化进基准脚本，见 `docs/windows-baseline.md` §3）。

## 0.14.0 — 2026-10-29

### Added

- **P2b：hived 只读缓存接 store**：`mcp_cache` 在 `claim_backend=store` 时用 store 的
  task 运行字段/claim/result 覆盖 Git 卡面；MCP `hive_whoami`/`hive_list_tasks` 等能看到
  store 领取，不再只读 Git。
- **result accepted 镜像**：Worker/manual submit 写完 Git result 后同步写 store
  `results`（`accepted`），`Store.release` 不会再把已提交结果打回 ready。
- **daemon 配置不匹配保护**：已有集群进程不是 store/MCP 模式时，`daemon start --store`
  不再静默返回 0，而是提示用 `daemon restart`（热加载不存在）。

- **两阶段提交 + commit_index**：leader 先 prepare 到多数，再本地 commit、向 follower
  发 commit；`commit_seq` 作为选举限制和新主截断未提交尾部的依据。新增
  `/api/cluster/op/since` 追平接口，节点注册后自动从 leader 拉已提交 op。

- **P2b：dispatch/assign 进 store**：`cmd_dispatch`/`cmd_assign` 在 store 模式直接
  `store assign`，不再写 Git owner/inbox；`Worker.pending` 从 store 查 assigned/claimed，
  `Hive.cards/card` 用 store 运行时字段覆盖 Git 卡面。
- **P2b：ArchiveWorker + coordinator**：leader 的 `ArchiveAgent` 把 store accepted result
  补归档到 Git `res/<tid>` 并标 `archived_at`；`--coordinator`/`HIVE_COORDINATOR=1` 可在
  hived 内每 30s 跑一轮 orchestrate（默认关闭，避免和外部 orchestrate 重复）。
- **P2b：两阶段提交 + commit_index**：prepare 多数 → leader commit → follower commit；
  `commit_seq` 用于选主限制和新主截断；`/api/cluster/op/since` 用于重连追平。

- **P2b：op log 压缩/快照/追平**：`Store.compact(keep=1000)` 删除旧 committed op；
  `GET /api/cluster/store/snapshot` 导出全量状态；新节点先 snapshot 再 `/op/since`，
  落后超过压缩点时自动改走 snapshot。
- **P2b：archive 元数据复制**：`mark_archived` 作为 op 复制，不再只是 leader 本地状态；
  `ArchiveAgent` 通过 leader store API 提交，Git 归档 + store archived_at 一致。
- **`hive store [check]`**：本机 store 计数/commit_seq/压缩点与 SQLite integrity_check。

### Changed

- **P2b 边界**：dispatch/assign/result accepted/archive/commit_index/压缩/追平已接；
  极端分区混沌、真四客户端压测仍待补。

## 0.13.0 — 2026-10-29

### Added

- **P2a 控制面 store**：`hive_store.py` 用 SQLite WAL 保存
  `tasks/claims/results/ops`；每节点一份全量副本，leader 单写、op 复制、follower
  fsync 后 ACK。claim 是 leader 上的 CAS，带 `lease_id/lease_seq/term`。
- **集群 store API**：`/api/cluster/store/{claim,lease,release,result,assign,close}`、
  `GET /api/cluster/store/state`、内部复制 `/api/cluster/op`；follower 自动转发 leader。
- **选主限制**：投票人拒绝 `last_store_seq` 落后的候选人，避免新主缺已复制的 op。
- **`claim_backend=store`**：`hive cluster/daemon --store --claim-backend store` 或
  `HIVE_STORE=1 HIVE_CLAIM_BACKEND=store`。Worker 的 claim/续租/释放、`Hive.claim_info`、
  `take`/MCP claim 的 Git assign 都切到 store；默认仍是 `git`，可随时回滚。
- **测试**：`tests/test_store.py`（CAS/幂等/复制回放）、store 复制/选主限制/
  Worker store claim 集成；端口/文档同步。

### Changed

- **P2a 边界**：dispatch/assign 的完整迁移、result 归档、coordinator 进 hived 仍属 P2b；
  当前 store 模式是实验性的，默认不启用。

## 0.12.0 — 2026-10-29

### Added

- **P1 `hived` 本机 MCP HTTP 入口**：`hive daemon` 是 `hive cluster` 的别名，默认在
  `127.0.0.1:8790/mcp` 起 Streamable HTTP；`hive cluster start --mcp` 也可显式开启。
  一个进程服务本机多个 agent，读工具走只读缓存，写工具仍走现有 Git/claim。
- **`hive agent enroll/revoke` + 本机 token**：token 只存
  `~/.hive/cluster/mcp_tokens.json`（SHA-256，0600），不进协调仓；
  `enroll` 直接打印 `mcpServers` 配置（URL + Bearer token）。
- **只读缓存 `mcp_cache.py`**：独立 read clone 每 2s 解析任务卡/agent/in-flight
  claim/result；每 10s fetch/reset 一次。MCP 读工具不再每次起 git 子进程，也不阻塞
  写 clone。
- **stdio 桥 `hive mcp --connect`**：给只支持 stdio 的客户端转发到 8790；
  原 `hive mcp --agent` 独立 stdio server 保留。
- **端口规划**：8790 正式分配给 MCP HTTP（唯一来源 `hive_ports.py`），
  预留段改为 8791–8798。
- **测试**：`tests/test_mcp_http.py`（token/initialize/tools/list/tools/call）、
  cluster 内嵌 MCP 启动、`tests/test_ports.py` 更新。

### Changed

- **P1 边界**：claim/lease 仍以 Git 为真相；`hived` 只负责本机入口、缓存和 token。
  store 控制面仍是 P2。
- `hive cluster` 默认不启 MCP（保持旧行为）；`hive daemon` 默认启，避免已有集群
  脚本突然多监听一个端口。

## 0.11.0 — 2026-10-29

### Added

- **P0 代码同步事件 `repo.updated` + 每机 `RepoSyncAgent`**：`land` 成功后把
  `{task, repos:[{name, sha}]}` 发给本机集群，集群广播给其他节点；各机 SyncAgent
  按本机 `repos` 映射做 `ff-only` 快进，写 `~/.hive/cluster/sync_cursor.json`。
  事件漏了也没关系：每 5 分钟用 `git ls-remote` 对比游标补偿。`hive cluster sync`
  （或 `GET /api/cluster/sync`）可查看游标/最近状态。
- **`hive cluster --no-elect` / `HIVE_CLUSTER_ELECTABLE=0`**：verify-only 机器做 observer，
  不参选、不投票；协调/land 只在 electable 节点上发生。
- **orchestrate leader gate**：有集群时只有当前 leader 跑 watchdog/dispatch/land；
  follower/no-leader 跳过写动作并节流打日志；写前复核 term，旧主自动停手。
- **`HIVE_STATUS_GIT`**：`auto`（默认，仅里程碑且集群不可用时兜底）/`always`（旧行为）/
  `never`。进度默认只走集群/HTTP，不再每 60s force push `status/<tid>`。
- **测试**：`tests/test_status_git.py`、`tests/test_cluster_sync.py`，
  orchestrate leader gate 4 例、observer 不参选 1 例。

### Changed

- **land 后本机主检出立即快进**：不再等下一轮 orchestrate 的 `sync-repos`；
  事件里带协调仓 `hive` 和代码仓，其他机器按需同步。
- **CI 决策**：Gitea CI 当前关闭（Agent 产出速度超过 CI 承载）；`docs/集群架构-v3.1.md`
  的 Git/CI 预算已改为「CI 关闭时只按仓库 push 算，重新开启再评估合并队列」。
- **P0 边界**：claim/lease 仍以 Git 为真相；`_refresh_lease` 保留。P2 才迁 store。

## 0.10.1 — 2026-10-28

### Added

- **端口规划单一来源 `hive_ports.py`**：看板 8787 TCP、集群 HTTP 8788 TCP、集群发现
  8789 UDP、测试/冒烟 8799 TCP、8790–8798 预留；`hive.py` / `hive_cluster.py` /
  `dashboard/serve.py` 的默认值全部改为从这里读取，杜绝「手动 8787、服务 8791」和
  「8789 被看板/集群混用」这类错位。
- **`hive ports [--check] [--json]`**：打印默认值/当前环境变量生效值/外部依赖端口，
  `--check` 尝试绑定本机端口标出占用；`hive ports` 不触发看板自启。
- **`docs/端口与服务规划.md`**：完整分配表、防火墙矩阵、多实例改端口步骤、冲突排查、
  外部依赖（Gitea 3000 / DSH 3080 / LightMem 18443/18444）与历史 8791 备注。

### Changed

- **集群文档明确「建议每台都跑，但不是必须」**：不跑仍可纯 git 工作；分布式消除单点，
  但不宣称物理意义上的「永不宕机」（全节点断电/全网断开仍会降级）。
- `README` / `操作手册`（新增 §12 常驻集群与端口 + FAQ Q5）/ `架构与排障全景` /
  `dashboard/README` / `dashboard/VERIFICATION`（历史 8789 改 8799 并注明）/
  `scripts/serve-dashboard.sh` / `scripts/dashboard-service.sh` 的端口与集群说明同步更新。
- `init` 一并复制 `hive_ports.py`；`check.sh` 编译新增模块；新增 `tests/test_ports.py`
  9 个用例（唯一来源、预留段、环境变量、CLI、init 拷贝）。

## 0.10.0 — 2026-10-28

### Added

- **常驻分布式集群 `hive cluster`（hive_cluster.py）**：每台机器一个 `hive cluster serve`，
  局域网 UDP 广播自动发现 + 静态 seed 兜底；新节点向主节点注册，节点间心跳/状态/事件
  互通。主节点停止或崩溃后自动重选，跟随者在 1~8 秒内接管，并通过 `leader_elected`
  事件通知其他程序。
- **任期（term）fencing token + 化身号（incarnation）**：每次选举 term+1，旧 term 写入
  被 409 拒绝；节点重启 incarnation 严格递增，低化身号记录不覆盖新记录，避免僵尸主节点
  和「死亡传闻」复活。
- **持久化建议性租约 + 恢复宽限期**：`claims/` 仍是唯一真相；集群额外持久化
  lease（agent / lease_until / term / gen），新主上任给在飞租约默认 120s 宽限，
  避免「刚换主就重派」。
- **实时进度通道**：`Worker.claim/_refresh_lease` 向本机集群发租约，`push_status`
  先试 `HIVE_PUSH_URL` 再退到 `/api/cluster/push`；看板 `/api/status` 合并集群进度
  （`push_source=cluster`），并新增 `/api/cluster` 与顶部集群徽标（主节点 / term / 在线数）。
- **集群 CLI**：`serve / start / stop / restart / ensure / status / peers / events /
  join / leave / discover / watch`；`hive work` / `hive orchestrate` 启动时自动
  `cluster ensure`（`HIVE_CLUSTER_AUTOSTART=0` 关闭）。
- **长轮询 `/api/cluster/wait`** 与事件列表 `/api/cluster/events`，供脚本/看板做秒级通知。
- 文档 [docs/集群服务.md](docs/集群服务.md)：拓扑、发现/注册、选主、任期/化身号/宽限、
  API、与 RIP/OSPF 的对照、排障与环境变量。

### Changed

- 版本号 0.9.17 → 0.10.0。
- `init` 仍然只保证 `hive.py`；集群是可选件，缺 `hive_cluster.py` 时 `hive cluster`
  给出明确提示，其余命令不受影响。

### Safety

- 集群**不替代** git：claim 原子性、result 审计、land 合流全部不动；集群不可用时
  worker/dispatch/land 回退到原来的纯 git 路径。
- 跨机建议配置 `HIVE_CLUSTER_TOKEN`；不配置时仅适合可信局域网。HTTP 明文，跨公网
  请套 TLS 反向代理。
- 多数门槛按「当前可见活节点」计算（可用性优先）：2 节点主节点退出后剩余节点可接管；
  网络分区可能出现短暂双主，恢复后由 term 收敛。需要严格 CP 请用 3 节点。

## 0.9.17 — 2026-10-28

### Changed

- **看板端口统一到 8787，并且只有一个来源 `HIVE_DASHBOARD_PORT`**。
  此前 `serve.py` / `serve-dashboard.sh` 默认 8787，而 `dashboard-service.sh`、`hive.py`
  的按需自愈、各平台自启安装器默认 8791 —— 于是「手动跑在 8787、服务起在 8791」，
  文档（README / 操作手册 / dashboard/README）全篇写 8787，实际访问却要换端口，
  看起来像端口被人偷偷改了。现在所有入口默认 8787，`PORT` 保留为历史覆盖项，
  脚本侧一律 `${PORT:-${HIVE_DASHBOARD_PORT:-8787}}`，`hive.py` 拉起时两个名字都传。
- 文档补一句「端口来源」与「改看板脚本必须 stop→改→start」：shell 按字节偏移边读边执行，
  热改正在运行的脚本会让解析错位（实测出现 `esh_ci: not found` 与刷新循环空转）。

### Added

- **`/api/status` 返回 `X-Snapshot-Mtime`**（状态文件 mtime，epoch 秒），前端据此显示
  「数据源：/api/status（快照 3 分钟前）」，超过 10 分钟加 ⚠ 提示。
  刷新循环停摆时 `/api/status` 依旧 200，页面会安安静静渲染几小时前的数据——
  2026-10-28 就因为一份 2h43m 的旧快照、外加前端 `isStale()` 用过期租约判异常，
  让人以为「手工退回没生效」。

### Fixed

- **刷新循环会往日志里灌垃圾**：`refresh()` 用固定的 `status.json.tmp`，多个实例并存时
  互相踩——别人写完、这边失败分支 `rm` 掉、再 `mv` 就报 `stat 失败: 没有那个文件或目录`，
  实测灌到 **338MB**。改成 `status.json.tmp.$$` 各写各的，`.gitignore` 补 `tmp*` 通配。
- **日志不轮转**：`dashboard-service.sh start` 时若 `dashboard.log` 超过 10MB，
  先 `mv` 成 `.log.1`（只在启动时做，运行中轮转会和正在写的进程抢）。

## 0.9.16 — 2026-10-28

### Changed

- **`close` 不再无条件等于「完成」**（T-0181 的教训）。看板「终止」按钮直接调 `close`，
  一张没有任何 `result` 的卡被标成 `done`，看板上和真的做完长得一模一样，备注只有
  「看板操作」四个字 —— 事后没人说得清它是完成还是被掐掉。
  现在按有没有 `res/<tid>` 自动判定：**有结果 → `done`，没有 → `closed`（作废）**；
  需要强制时用 `--as-done` / `--as-cancelled`。作废和完成都会把**现场**写进卡面备注：
  原状态、原 owner、有无结果、失败原因。

### Added

- **`hive reopen`**：把 `done` / `closed` 的卡拉回 `ready`（清 owner、删 claim/res 分支、
  撤掉归档结果，原结果仍在 git 历史），并在卡面留一条「谁在什么时候、为什么重开」。
  以前终态不可逆，误关一张卡只能手改文件。
- **看板操作语义对齐**：异常列按钮从「终止 / 改派 / 重试 / 退回未分配」改成
  「退回待领取 / 改派 / 重试 / 关闭作废」，已关闭列补「重开」；`close`/`unassign`/`reopen`
  都会弹原因输入框（默认值带现场），不再只留一句「看板操作」。
  `classify()` 补上 `closed`：作废的卡不会掉进「待领取」列。
- `/api/task` 增加 `reopen`，`close` 支持 `done: true/false` 强制指定。

### Fixed

- **跨平台 CI 的 macOS 作业一直红**：`ci-smoke.sh: line 42: PORT: unbound variable`。
  根因不是脚本逻辑，是 **bash 3.2 + 非 UTF-8 locale 下，`$PORT）` 里的全角右括号会被并进
  变量名**（变成 `PORT）` 这个不存在的变量）。把所有「变量紧跟中文」的位置统一改成
  `${VAR}`（8 处），并在 `LC_ALL=C` 下复跑验证。
- CI 失败时的排障通道：作业日志接口要仓库 admin 权限，我们拿不到；现在失败会把一句话
  原因写进 **commit status**（公网可读），并把完整日志推到 **`ci-log` 分支**，
  本地 `git fetch github ci-log` 就能整份读。

## 0.9.15 — 2026-10-28

### Added

- **跨平台 CI（GitHub Actions）**：`skywalk163/hive` 是 public 仓，标准 runner 不计费，
  于是把「只能在本机 Linux 兼容层里测」的那几个脚本放到真机跑——
  ubuntu / macOS 原生 runner、Windows runner（`.cmd` / `.ps1` 的第一次真机 E2E）、
  FreeBSD 15.1（`vmactions/freebsd-vm` 起 QEMU）。见 `.github/workflows/cross-platform.yml`。
  - `unit-linux` 是硬门禁（全量 pytest）；`unit-other`（macOS/Windows）先
    `continue-on-error`，绿了再收紧。Windows 的 `.cmd`/`.ps1` E2E 同理。
- **`scripts/ci-smoke.sh`**：把冒烟逻辑从 YAML 里抽出来，本地能原样跑——
  `syntax`（所有 .sh 过 `sh -n`）、`service`（起 → HTTP 探活 → status → stop →
  确认端口释放，用 8799 避开真看板）、`autostart`（dry-run，并断言安装器
  "选定了方式"或"按设计拒绝"，不能既不选也不说）。
- **`.gitattributes`**：`*.sh` 强制 LF。仓里有 Windows 机器且 `core.autocrlf=true`，
  CRLF 的 shell 脚本在 CI 上会直接 `$'\r': command not found`。

### Changed

- **仓库白名单对齐现实**：`ALLOWED_REPOS` 从 `{hive}` 扩到 `{hive, lightharness, light}`，
  `FORBIDDEN` 清空。原因不是"放宽纪律"，而是两个仓早已按流程正式接入：
  lightharness 服务 0.2.1-rc.3 升级线（T-0170~T-0179），light 有在飞的 T-0181/T-0183。
  守卫的职责照旧——**没登记的仓永远推不出去**；要退役某个仓，把仓名塞进 `FORBIDDEN`
  并清掉 agents 登记，测试会逼着把在飞的卡处理掉。`docs/操作手册.md` 同步改了流程说明。

### Fixed

- **`test_mcp_next_skips_unqualified` 一直红**：不是代码坏了，是 418da7e 加同步 TTL
  （默认 10s，修"每次工具调用卡 30~60s"）后，测试里"推了新卡立刻能看见"的断言会被
  缓存吃掉。测试 fixture 现在显式 `HIVE_MCP_SYNC_TTL=0`，并新增
  `test_sync_ttl_skips_refetch` 把缓存的**两个面**都钉住：开 TTL 时看不见新卡、
  关 TTL 时立刻看得见。生产默认值没动。

## 0.9.14 — 2026-10-27

### Added

- **`hive dash`（别名 `dashboard`）：看板服务的统一入口**。`ensure`（默认，没起就起）/
  `start` / `stop` / `restart` / `status` / `url`。命令行走 `scripts/dashboard-service.sh`，
  与应用内拉起共用一套 PID 与日志，不搞第二套实现。
- **按需自愈：任何 `hive` 命令都顺手把看板兜住**。入口处做一次 TCP 探活（0.25s），
  不通就在后台脱离会话拉起服务；`HIVE_DASH_AUTOSTART=0` 关闭，`HIVE_DASH_AUTOSTART_TTL`
  控制重试节流（默认 300s，避免服务真起不来时被反复 spawn）。失败一律静默，
  绝不让兜底逻辑把正常命令带崩；测试环境（`PYTEST_CURRENT_TEST`）自动跳过。

### Fixed

- **`install-autostart.sh` 不再在"没有 init 的环境"里假装成功**。背景：本机 2026-10-27
  实测，写进 crontab 的 `@reboot` 行**一次都没触发**——探针任务等 2.5 分钟零执行、
  `~/.hive/logs/dashboard-cron.log` 从未生成。根因是 hive 跑在 `chroot /compat/ubuntu`
  的 linuxulator 兼容层里：`uname` 报 Linux，PID 1 却是宿主的 FreeBSD init，
  本层内 systemd 是 offline、也没有服务于我们 crontab 的 cron。
  - 新增 `in_freebsd_linux_compat()`：`uname=Linux` 但 `/proc/version` 含 FreeBSD 字样即判定为兼容层。
  - 新增 `cron_serves_us()`：比对 `/proc/<pid>/root` 而不是只 `pgrep cron`——FreeBSD 各 jail
    共享 PID 空间，11 个 `/usr/sbin/cron` 里 10 个属于 `/usr/jails/lims-*`，剩下 root=`/`
    的那个是宿主自己的 cron，从 chroot 里根本分不清。因此兼容层一律不走 cron 分支。
  - 检测不到服务管理器时 **exit 3** 并打印两条真正可行的路径（按需自愈 / 宿主 rc.d），
    不再静默写一张没人执行的 crontab；`--force-cron` 保留强行安装的旁路。

### Added（宿主侧）

- `scripts/freebsd-host/hive_dashboard`：FreeBSD 宿主 rc.d 服务，开机经
  `chroot /compat/ubuntu` + `su <user>` 调 `dashboard-service.sh start`。不套 `daemon(8)`
  ——服务脚本自己会 setsid/nohup 脱离会话，再套一层会在脚本退出时误判服务已死。
- `scripts/freebsd-host/install-host-autostart.sh`：在宿主机以 root 运行，装 rc 脚本、
  `sysrc` 写开关、启动并用 `fetch` 探活；`--dry-run` / `--uninstall` 齐备。
  在非 FreeBSD 上直接拒绝执行（防止在兼容层里误跑）。

## 0.9.13 — 2026-10-25

### Fixed

- **`run()` 钉死 utf-8：Windows 写脏的结果 JSON 不再打崩 `status` / `doctor`**（T-0143）：
  raccoon-win 交完 T-0142 之后，本机的 `hive status` / `hive doctor` **整条命令崩掉**，
  直接抛 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbf`，退出码非 0、看板全没了。
  - 根因就一处：`run()` 写成 `subprocess.run(..., text=True)`，**没指定 encoding**。
    `text=True` 的隐含语义是「stdin/stdout 都用 locale 编码」，Windows 上 locale 是 cp936，
    于是 `commit_file_to_ref` 用 `git hash-object -w --stdin` 送进去的结果 JSON **按 GBK 落盘**
    （实测 `results/T-0142.json` 的 `verify[].cmd` 与 `tail[]` 全是 GBK 字节，
    文件开头的 `"task": "T-0142"` 因为是纯 ASCII 才看不出问题）；Linux 侧 `git show`
    取回、按 UTF-8 解码就直接炸，而 `read_ref_file` 的调用方没有任何兜底。
    **Windows 上写得越认真、中文越多，Linux 上越容易崩。**
  - 修法：在 `run()` 这个唯一出口上钉死 `encoding="utf-8", errors="replace"`，两侧一起收口——
    encoding 保证进 git 的字节永远是 UTF-8（治本：跨平台同一份编码，卡面/结果都不再分叉）；
    errors 保证读进来的非法字节只替换成 U+FFFD 而不抛异常（兜底：最坏显示个问号，
    不是整个看板消失）。没去一个个 callsite 补 encoding——散落各处迟早会漏。
  - `run()` 是 hive 唯一的外部命令出口，所以这一处同时护住了结果 JSON、卡面提交
    和 verify 输出捕获三条路径。
  - 新增 `tests/test_encoding.py` 7 例作回归守卫：造一个 GBK 字节的 `res/<tid>` blob，
    断言读得出来且 ASCII 骨架一字不少；并有一条**直接证明它是真守卫**——同样的字节
    严格按 UTF-8 解码必然抛 `UnicodeDecodeError`。撤掉编码参数时 7 例中 3 例立刻变红。
  - 一个测试设计上的坑：本机 locale 恒为 utf-8（`LC_ALL=C` + `PYTHONCOERCECLOCALE=0`
    也还是 utf-8），**"不指定 encoding"也能碰巧写出 UTF-8**，光断言字节抓不到回归，
    只能盯住 `run()` 的参数。这条事实写进了测试里的
    `test_locale_is_not_the_thing_we_rely_on`，免得后来人误以为字节断言已经够。

## 0.9.12 — 2026-10-24

### Fixed

- **`land --apply` 不再被协调仓未提交改动卡死**（T-0136）：合并在临时 worktree 里
  成功推入 origin/main 后，收尾的 `cmd_close` 跑 `sync_main()`，而主仓工作区
  「落后 + 有未提交改动」（多 agent 共用一台机器时的日常）时守卫直接报错 →
  **代码已合入、卡片却没关**，操作者只看到一句「不敢自动 reset」，完全看不出合并
  已经成功。一晚被坑两次（T-0133 / T-0135），都靠人工 ff + close 补救。
  - 新增 `Hive._sync_for_commit()`：落后就试 `merge --ff-only`。git 保证 ff 只更新
    两个提交间真正变化的文件——未提交改动不在其中就原样保留，真在其中 git 自己会
    拒绝，不会丢任何东西；分叉等拿不到的才报错并列脏文件。
  - `cmd_close`（含 land 收尾、独立 `hive close`、MCP `hive_close`）换用它。
    `sync_main` 本身不动（T-0129 的防数据丢失底线保留）。
  - land 关卡万一仍失败，返回新状态 `merged-not-closed`：明说「合并已推入、只是
    关卡失败」并给出 `hive close <tid>` 收尾命令，不再裸抛同步错误。
  - 方案备查：初稿想让关卡也在 land 临时 worktree 里做，否决——那是**代码仓**的
    worktree，非自举卡（repo≠协调仓）会把卡片文件提交进错误的仓。

## 0.9.11 — 2026-10-24

### Fixed

- **`hive_block` / `hive_unblock` 从 MCP `tools/list` 消失**（T-0135，raccoon 发现）：
  `mcp_server.py` 的 `TOOLS` 列表里 `hive_block` 条目漏了收尾的 `}, {`，三个 dict
  （block/unblock/abort）被并成一个、同名 key 互相覆盖——最后只剩覆盖后的
  `hive_abort`。handler 其实都在（`HANDLERS` 照常注册），知道名字直接 `tools/call`
  仍能调通，所以 CLI 一直是好的；坏掉的只是 MCP 广告位，GUI / LLM 客户端根本
  看不到这两个工具。
  - 修法：把并掉的 dict 拆回三个，`TOOLS` 19 → 21，worker 视角 12 → 14。
  - `tests/test_mcp.py` 的 `EXPECTED_TOOLS` 补上 block/unblock（旧值把 bug 表面
    固化成了"预期"）。
  - 新增不变式测试 `test_every_handler_is_advertised_in_tools_list`：handler 与
    广告位必须一一对应、不许重名、条目结构合法——T-0123 踩过同类的坑，当时只有
    「改完打印 len(TOOLS)」的口头纪律，这次变成机器检查。

## 0.9.10 — 2026-10-24

### Added

- **重试熔断：自动回收达上限后转 `blocked`**（T-0130）。`retries` 以前只增不停，一张注定失败的卡
  会被「判死 → 退回 ready → 重新分派 → 再判死」无限循环，常驻的 `orchestrate` 会一直烧 agent 时间。
  - 卡面新增 `max_retries`（默认 `DEFAULT_MAX_RETRIES = 3`，`hive task new --max-retries N`，
    `0` = 一次都不许自动重派）；
  - 看门狗回收前先判额度：用尽则**不再退回 ready**，用 `_set_state(tid,"blocked",…)` 判死并在卡里
    写明原因。`blocked` 被 `dispatch` / worker / `orchestrate` 全部跳过，等于自动退出流水线；
  - watchdog `--json` 里这类卡 `action` 是 `block`（另有 `would_block` / `blocked` 计数）；
  - `hive doctor` 对「重试额度烧光」单独点名，与「等外部资源」的 blocked 区分开；
  - **人工显式 `hive retry` 不受此限**（那是人在明确要求重跑），只在超额度时提醒一句。
  - 新增 `tests/test_retry_cap.py` 9 例作回归守卫（关掉熔断时其中 7 例会红）。

## 0.9.9 — 2026-10-24

### Fixed

- **`Worker` 不再裸 `reset --hard`**（T-0129）：`Worker.__init__` 和 `Worker.loop()` 以前都是
  无条件 `git fetch` + `git reset --hard origin/main`。因为 `hive take` / `work` / `submit`
  跑在**主工作区**上，这会**静默抹掉协调仓里未提交的已跟踪改动**——比如正写到一半的 `drafts/`
  草稿（`Worker.loop()` 更糟，轮询期间每轮抹一次）。
  - 现改为复用 `Hive.sync_main()`：与 `origin/main` 对齐或本地领先 → 不动工作区；
    落后且干净 → 才 reset；落后且脏 → **报错**（`不敢自动 reset`），绝不静默丢改动。
  - 排查「草稿内容被还原」时抓到，已用「改脏文件 → 只构造 Worker → 文件变回原样」实锤确认。
  - **未动** `mcp_server.py` 的两处 reset（`HiveMcp.hive()` / 启动探测）：它们作用在 MCP 独占
    clone 上，reset 是其每次调用读到最新卡的设计前提。
  - 新增 `tests/test_worker_guard.py` 3 例作回归守卫（退回旧行为时这 3 例会红）。

## 0.9.8 — 2026-10-24

### Fixed

- **`hive_land` 冲突时的错误信息与实际 `apply` 一致**（T-0128）：旧代码无论有没有传
  `apply=true`，撞冲突都固定回「合入没成功（**默认只预演**，什么都没动）」。这句话只在没传
  `apply` 时成立，会误导 LLM 总控以为自己还在 dry-run 模式。现在：
  - 没传 `apply` → 仍说「默认只预演，什么都没动」（dry-run 语义不变）；
  - 传了 `apply=true` → 说「合入冲突：已传 apply=true，也未改动任何东西（绝不自动解冲突、
    绝不 force push）」；
  - 两种分支都补 `details.applied` 字段，调用方不依赖自然语言也能判断自己处在什么模式。
  - 该问题由 T-0127 交付后用真 MCP stdio 客户端实跑一轮 decision-point 走查时暴露。

## 0.9.7 — 2026-10-24

### Added

- **LLM 总控 `hive llm`**：用 controller MCP 工具面让 LLM 逐轮决策，补齐「带判断的总控」。
  和 0.9.6 的确定性 `hive orchestrate` 互补——orchestrate 跑稳定路径（verify 全绿就 land），
  `hive llm` 处理 `needs_review` / `conflict` / `failed` / `blocked` 这些需要判断的卡点，以及分派偏好。
  - 新建 `llm_controller.py`（标准库实现，零第三方依赖）：工具后端 `ToolBackend`
    （`InProcessBackend` 直接复用 `mcp_server.HiveMcp` 的 handler == controller MCP 工具面；
    `McpStdioBackend` 真·spawn `mcp_server.py` 走 JSON-RPC 握手）、可插拔 `LlmClient`
    （`OpenAiClient` 标准库 urllib 调 OpenAI 兼容端点 / `HeuristicClient` 无 key 回退 /
    `MockClient` 测试）、决策循环 `ControllerLoop`。
  - 每轮：拿 `hive_whoami`（controller）快照 → 渲染 → 问 LLM 下一个动作 → 执行（带 apply 安全闸）→ 记历史。
  - **安全闸**：没传 `--apply` 时，dry-run 类工具（land/dispatch/gc/watchdog）强制 apply=false；
    `hive_close/hive_retry/hive_assign` 这类无 dry-run 的破坏性操作直接跳过不执行。和全仓「破坏性操作默认 dry-run」一致。
  - 参数：`--agent`（默认 controller）、`--mcp`（走真 MCP stdio 客户端）、`--llm openai|heuristic`
    （默认 heuristic）、`--model/--base-url/--api-key`（覆盖 env）、`--apply`、`--max-iter`、
    `--stop-file`、`--once`。

## 0.9.6 — 2026-10-24

### Added

- **总控进程 `hive orchestrate`**：把「看门狗 → 分派 → 合入 → 体检」串成常驻循环，替代人工 drive。
  exec 类任务从 `ready` 到 `closed` 全自动跑完，人只需要在 `needs_review` / `conflict` / `blocked` 等卡点介入。
  - 每轮 tick 顺序：`watchdog --apply` → `dispatch --apply` → `land --all --apply` → `doctor`；
    任一阶段抛错都 catch 住、记 log、继续下一轮，绝不整进程崩。
  - 参数：`--interval`（默认 60s）、`--once`、`--iterations N`、`--no-dispatch` / `--no-land` /
    `--no-watchdog` / `--no-doctor`、`--stop-file PATH`、`--dispatch-limit N`、`--max-land N`、
    `--watchdog-minutes`（默认 180）、`--orphan-grace`（默认 45）、`--json`。
  - 只负责「脑」（分派+合入+回收）；worker 仍是各机器独立进程。manual agent 的卡不碰（人工优先保留）。
  - 不暴露成 MCP 工具（会阻塞调用方）；「带判断的总控」用 controller MCP 由 LLM agent 自己循环调。

### Changed

- **看门狗 `orphan_assigned` 判定修对**：`_assigned_at` 改读 `inbox/<owner>/<tid>.md` 里
  `_assign_local` 实际写的 `assigned_at`（原先读卡 body，导致「分派了但一直没来领」的死卡永远收不回）。
  新增 `--orphan-grace`（默认 30 分钟）：分派时间超过 grace 才回收，刚分派的卡在 grace 内不会被误杀。
- 版本号 `VERSION` 同步到 0.9.6。

## 0.9.5 — 2026-10-24

### Added

- **worker 看门狗**：自动回收「死掉 / 孤儿 / 僵尸」的 in-flight 卡，不再卡住并发额度。
  - **worker 租约心跳**：`Worker.run_task` 期间 daemon 线程周期性刷新 `claim/<tid>` 的
    `lease_until`（间隔 `max(5, lease_minutes//3)` 分钟，父提交必须是当前 claim 分支才能快进）。
    活着的 worker 自己续租，看门狗才能把「租约过期」可靠地判成死亡，不会错杀健康的长任务。
  - **`hive watchdog [--minutes M] [--apply] [--json]`**：扫描 `assigned`/`running` 的卡，
    判定 `orphan_running`（跑着却无 claim）/ `orphan_assigned`（assigned 很久没来领）/
    `zombie`（claim 的 agent 没登记）/ `stale`（租约过期）/ `land_lag`（有结果却没 land，只提示）。
    回收 = 退回 `ready` 并清空 owner（放回任务池，等 dispatch 重派）。**默认 dry-run 只报告，
    `--apply` 才动手**，与 gc/land 约定一致。
  - **`hive doctor` 增强**：in-flight 卡的孤儿 / 僵尸 / land_lag 现在会被点名。
  - **MCP 总控视角补 `hive_watchdog`**（controller 专属，复用 `hive_gc` 的接法）。
- 测试 9 例（dry-run 零改动 / apply 回收 / 健康卡不误杀 / 心跳续租 / doctor 报孤儿僵尸），
  全量 **95 passed**。

## 0.9.4 — 2026-10-24

### Added

- **MCP 总控视角**：`hive mcp --agent <name> --role controller`（或 `HIVE_ROLE=controller`）。
  以前 MCP 只有 worker 视角，总控还得敲 CLI；现在合入/关闭/重跑/分派/回收都能走 MCP。
  - controller 额外 6 个工具：`hive_land` / `hive_close` / `hive_retry` /
    `hive_assign` / `hive_dispatch` / `hive_gc`。
  - **权限隔离有明确证据**：worker 视角的 `tools/list` 里根本看不到这 6 个
    （测试直接断言集合交集为空），即使 worker 猜到工具名去 `tools/call` 也会被拒。
  - **破坏性操作一律默认 dry-run**：`hive_land` / `hive_dispatch` / `hive_gc`
    不传 `apply=true` 就只出计划；`hive_land` 遇冲突返回 error 并把冲突文件提到
    `details.conflicts`，绝不自动解冲突、绝不 force push。
  - `hive_whoami` 在 controller 下多返回 `role` 和 `todo`
    （待合入 / conflict / blocked / needs_review / ready 五类计数），
    让总控 agent 知道今天该干什么。
  - `initialize` 的 instructions 区分角色，controller 版明确写「你的操作会改动 main 分支」。
- 测试 6 例（含权限隔离与冲突不 force），全量 **86 passed**。

## 0.9.3 — 2026-10-24

### Added

- **verify 强度分级**：`_verify_strength(cmd)` / `_verify_verdict(verify)`。
  `strong` = 真的会执行被测对象（pytest / npm test / go test / cargo test /
  make test / unittest…）；`weak` = 只查痕迹（grep / test -f / which / echo /
  cat / wc…）。判断刻意保守，拿不准就算 weak；**混合 shell 管线按最弱的一环算** ——
  T-0101 那条 `grep && grep && pytest` 因此被判成 weak，正是想要的信号。
  - `hive result`：每条 verify 标强弱，整卡给结论 `strong-pass` / `weak-pass` /
    `fail`；弱验收时显式提醒「成果没有被真正执行验证」。`--json` 带
    `verify_strength` 与 `verify_entries`。
  - `hive doctor`：只有弱验收的卡点名 warning。
  - `draft lint`：**草稿没有强验收就报 error 挡转正**。
  - MCP `hive_result` 返回带上 `verify_strength`。
- 测试 5 组（含 15 个参数化用例），全量 **80 passed**。

## 0.9.2 — 2026-10-24

### Fixed

- **`changed_files` 两个 git 输出陷阱**（T-0121 提交时自己被咬到）：
  - `core.quotePath`：git 默认把非 ASCII 路径转义成八进制
    （`docs/操作手册.md` → `"docs/\346\223\245..."`），中文名文件会被误判成越界；
  - 未跟踪**目录**默认被折叠成目录名（只显示 `docs/`），里面的文件根本不出现在
    改动清单里，paths 检查会漏。加 `-uall` 逐文件列出。
  两条都加上对应测试。

## 0.9.1 — 2026-10-24

### Added

- **`hive digest [--days N] [--out FILE] [--json]`**：复盘摘要，把经验从结果里捞出来。
  - **失败归因**：从 `result.error` 聚类 —— 超时 / verify 未通过（附上挂掉的命令）/
    paths 越界（严格模式拦下）/ 分支推送失败 / 其它。每类列出卡号，
    一眼看出是「卡写得太难」还是「环境不稳」。
  - 耗时最长的 5 张卡（往往是「卡拆得不够小」的信号）；
    反复重跑的卡（第一版验收就没过）。
  - `--out docs/lessons.md` 写 markdown，`--json` 给机器读（`draft lint` 和看板要消费它）。
  - **只读**：不写任务卡、不推分支，`--out` 只写普通文件。
- 任务卡新增 `retries` 字段：`retry` 会覆盖上一次结果，所以重跑次数只能记在卡上，
  否则 digest 永远看不出「这张卡反复在重跑」。
- `docs/lessons.md` 第一版：五类教训（写卡 / 环境运维 / 并发提交 / 改 hive 自己 / 用法），
  全部来自真实踩过的坑。

### Fixed

- **`result.error` 在「agent 正常退出但 verify 挂掉」时会写「agent 退出码 0」** ——
  既误导人，也让 digest 的失败归因全跑偏。现在优先报 verify 未通过并附上命令。
- 测试 3 例，全量 **59 passed**。

## 0.9.0 — 2026-10-24

### Added

- **`hive draft`：任务卡草稿，让 agent 也能出题**。以前只有人会写卡 ——
  写一张卡要读代码、想 paths 和 verify、组织语言，几分钟到十几分钟；agent 一天能干
  20 张卡的活，但只能等人把卡写出来。吞吐卡在出题这一端。
  - `hive draft new [名字] [--repo hive]`：生成带「背景 / 目标 / 红线 / 验收」四段骨架的
    模板（文件名给不给 `.md` 都认）；
  - `hive draft list` / `hive draft lint [文件]`：草稿校验，不传文件就 lint 全部；
  - `hive draft adopt <文件>`：**过 lint 才转正**，自动分配任务号、从 `drafts/` 移进
    `tasks/`、草稿撤掉，并打印下一步。
  - `draft_lint()` 抽成模块级函数（error / warning 分级），`doctor` 和 `adopt` 共用。
    error 会挡住：没填 repo、repo 没被任何 agent 登记、**一条 verify 都没有**、
    `needs` 里有没有任何 agent 能满足的能力、依赖不存在的卡。
    最后两条是 T-0102 那种僵尸卡的成因，现在从源头拦住。
  - `tasks/` 仍只能由 `adopt` 或 `task new` 写入 —— 草稿是给人 review 的界面：
    agent 一次出十张是廉价的，人只做「同意/打回」这个 O(1) 判断。
- 测试 3 例，全量 **56 passed**。

## 0.8.0 — 2026-10-23

### Added

- **状态机补齐**：新增 `needs_review` / `conflict` / `blocked` 三个状态，
  异常卡终于有地方待，不用再靠反复 `retry` 清场。
  - `hive block T-xxxx --reason "..."` / `hive unblock T-xxxx`：等外部资源时挂起。
    dispatch 和 worker 都跳过它，`status` 里单独可见，`doctor` 计数。
  - `land` 撞上合并冲突时自动把卡置为 `conflict`（以前只在返回里说，没有落点）；
    `doctor` 把 `conflict` 当**错误**报（退出码非 0）—— 有卡冲突不解决，后面全堵着。
  - `needs_review`：状态机支持，给后面「agent 出卡草稿 → 人确认」的流程预留。
  - MCP 新增 `hive_block` / `hive_unblock`；`hive_next` 与 `whoami.claimable`
    都不再推荐这三个状态的卡。
- 状态常量集中到 `STATES` / `IN_FLIGHT_STATES` / `HANDOFF_STATES` / `ERROR_STATES`，
  「在飞」的判断不再散落在各处字符串比较里（原来有 4 处硬编码 `("assigned","running")`）。

### Ops

- T-0102（Windows 接入）置为 `blocked`：它 `needs: windows`，但 4 个 agent 没有一个
  声明这个能力，会永远躺在 ready 列表里骚扰 dispatch —— 这正是僵尸卡的成因。
- 测试 3 例，全量 **53 passed**。

## 0.7.1 — 2026-10-23

### Fixed

- **`commit_and_push` 收口**：新增 `paths` 参数，7 个调用点都改成只提交本命令该动的文件
  （`task new` 只交卡片、`assign`/`take` 交卡片+inbox、`close` 交卡片+归档结果、
  `retry` 交卡片+inbox+归档、`agent add` 只交该 agent 的配置）。
  以前无条件 `git add -A`，总控工作区里任何未提交改动都会被卷进这次提交 ——
  协调仓库的 main 是审计记录，提交信息和内容对不上之后就没法靠 `git log` 回溯。
  **真实事故两起**：mcp_server.py（705 行）被卷进 `task: add T-0110`；
  sync_main 修复被卷进 `close T-0110`。
  没给 paths 时保留全量行为（向后兼容），但会打一条告警日志。
- **`sync_main` 不再把未跟踪文件当「脏」**：`reset --hard` 不会动未跟踪文件，
  之前用 `git status --porcelain` 判断会把草稿、编辑器 lock 文件也算进去，
  导致 land 推完 main 之后紧接的 close 直接卡死。改成 `--untracked-files=no`。
- 测试 5 例，全量 49 passed。

## 0.7.0 — 2026-10-23

### Added

- **`hive audit [T-xxxx | --all] [--json]`**：合并前的一页纸。比对「agent 说做了什么」
  和「diff 里真有什么」—— 声明的 paths、实际改动文件、越界清单、每条 verify 的退出码、
  summary 全文，外加风险标记。风险来源：没有 verify、没声明 paths、有越界改动、
  **summary 里出现「没能跑 / 无法验证 / 未测试」这类口供**（T-0101 那张卡就是这么过的）。
- 结果 json 新增 `declared_paths` / `changed_files` / `out_of_paths` 三个字段。

### Changed

- **paths 越界检测**：worker 提交前算一次改动清单，跟任务卡声明的 `paths` 比对。
  自举时代码仓和协调仓库是同一个，agent 能顺手改掉自己的任务卡 —— 现在能看见了。
  - **默认只警告不阻断**（怕一上来把正常流程卡死）：越界写进结果和日志，任务照常完成；
  - `HIVE_STRICT_PATHS=1` 或 agent 配置 `"strict_paths": true` 才判失败：
    不提交、不跑 verify，error 里列出越界文件。
  - `paths` 为空表示不约束（不检查，但 audit 会提醒一句）。
- 测试 4 例，全量 44 passed。

## 0.6.1 — 2026-10-23

### Changed

- **`hive dispatch` 的并发锁从「按 repo 计数」细化成「按 paths 重叠计数」**：
  自举阶段 hive 是唯一在开发的仓，按 repo 计数等于把所有卡强制串行 ——
  方案文档阶段 2 原本写的就是「同时跑 3 张路径互斥的卡」，粒度一直对不上。
  - 新增 `Hive._paths_conflict(a, b)`：任一方没声明 paths 就算冲突（未声明=可能改任何地方）；
    相同或有父子目录关系（`docs/` vs `docs/操作手册.md`）算冲突；否则可以并行。
    前缀比较按目录边界走，`tests/` 与 `testing/` 不算冲突。
  - 跳过原因从笼统的「同仓并发已满」改成**点名跟谁抢**：
    「paths 冲突：与在飞的 T-0001 抢同一片文件（hive 同组上限 1）」。
  - `--max-per-repo` 的**名字和默认值都没变**（向后兼容），语义变成
    「同一 repo 里 paths 互相冲突的卡同时在飞的上限」。
  - 测试 4 例：路径互斥可并行 / 父子路径被拦且点名 / 空 paths 保守处理 / 判定规则单测；
    既有的 `test_dispatch_concurrency` 按新语义改写。全量 40 passed。

## 0.6.0 — 2026-10-23

### Added

- **`hive land T-xxxx [--apply] [--all] [--json]`**：把闭环最后一步自动化 ——
  以前 result=done 之后要人手工 merge + close（T-0101 就这样在分支上躺了两个小时）。
  - 合入条件：结果是 done、verify 全绿、卡未 close、依赖已全部 close；不满足就列原因。
  - 合并跑在临时 worktree（`~/.hive/land/<tid>`，用完即拆）里，**不碰主 checkout、不做 clone**；
    预演是**真的把 merge 跑一遍**（不是 `merge-tree` 猜），所以报冲突就是真冲突。
  - 冲突一律不自动解、不 force push；`conflicts` 给文件清单。非冲突性失败会带上 git 的原话。
  - merge 显式带 `-c user.name/-c user.email`，不依赖机器有没有配 git 身份 —— 测试环境里
    `git merge` 就是以「请告诉我你是谁」失败的，而且不留任何冲突标记，很难查。
  - 默认 dry-run；`--all` 按优先级合所有能合的，遇冲突停下。
  - 测试 4 例：合入并关闭 / 条件不满足被拦 / 真冲突且不 force / `--all` 只挑能合的。

### Ops

- T-0110 由 WorkBuddy 以 `workbuddy` agent 身份走 manual 模式（`hive take` + `hive submit`）
  完成 —— 第一次真正用 hive 自己的流程开发 hive，绕开了缺 DeepSeek key 的 exec 后端。

## 0.5.0 — 2026-10-23

### Added

- **`hive mcp`：MCP stdio server**（`mcp_server.py`，零第三方依赖）。任意支持 MCP 的
  agent 接上后能自助闭环：查阅任务 → 申请领取 → 干活 → 提交 → 干不下去还回去。
  12 个工具：`hive_whoami` / `hive_list_tasks` / `hive_get_task` / `hive_next` /
  `hive_claim` / `hive_submit` / `hive_abort` / `hive_extend_lease` / `hive_progress` /
  `hive_result` / `hive_stats` / `hive_doctor`。见 `docs/MCP接入.md`。
  - 协议层用标准库直接实现 MCP 的 stdio JSON-RPC 2.0（换行分隔），不引 `fastmcp`。
    支持 `initialize` / `tools/list` / `tools/call` / `ping` / `resources/list` / `prompts/list`。
  - 身份模型：**一个 server 进程 = 一个 agent**，所有写操作只作用在自己身上。
    不能替别人领卡、不能提交别人的卡、不能终止/续租别人的租约，由代码强制。
  - 沿用 LightMem 的返回约定：不抛异常，统一 `{status, message, details}` 信封；
    失败时 `isError=true`，`details.hint` 给下一步动作。
  - 工具执行期间把 `sys.stdout` 重定向到 stderr —— 底层 `cmd_submit` 有 print，
    不接住就会把 JSON-RPC 流搅坏（测试里专门盯了这条）。
  - 默认用独占 clone `~/.hive/clones/mcp-<agent>`：worker 初始化要 `reset --hard origin/main`，
    共用主工作区会把人未提交的改动冲掉；自动克隆时取源仓的 origin URL，
    不克隆成本地路径（否则 `push main` 被 `receive.denyCurrentBranch` 拒）。
- 测试：`tests/test_mcp.py` 15 个用例，起真实 server 子进程走 stdio 端到端。
- 夹具：`World.sync()` —— 卡片存在工作区文件里，别的 clone 推完不 `reset` 读到的还是旧卡。

### Fixed

- **`hive submit` 找 worktree 时忽略 `HIVE_STATE`**：路径推导和 `Worker.prepare` /
  `take` 不一致（后者认 `HIVE_STATE`），设了 `HIVE_STATE` 就报「worktree 不存在」。
  现在三处（`take` / `submit` / MCP）共用同一套推导：
  `worktrees` > `$HIVE_STATE/wt` > `~/.hive/agents/<agent>/wt`。

### Ops

- T-0101（分配健全化）合入 main（`32870dd`）并 close，17 用例全绿；
  `abilities` 注册表、dispatch 逐卡「派/不派」原因、`--max-per-repo` 同仓并发检测正式生效。

## 0.4.0 — 2026-10-23

### Added

- `hive prune [--repo <name>] [--apply]`：清理**代码仓**里已合并的 `task/*`、`verify/*`
  分支。安全规则：只认这两种前缀（`main`/`integration`/`release/*` 不碰）；必须同时满足
  「已合并进 `origin/main`（真合并或 squash 后内容一致）」和「不在飞（卡没有结果）」才删；
  默认 dry-run 只列计划，`--apply` 才推 `:refs/heads/<branch>`，本地分支能删就删。
  本地 `verify/*` 遗留（verify 模式不推远端产生的）也一并清。
  覆盖测试：合并+在飞+未合并+本地 verify 遗留四种各一条，断言只删该删的。

## 0.3.0 — 2026-10-23

### Added

- `hive take T-xxxx --agent <name>`：不启动 worker 的人工领取（assign + claim + 建 worktree，
  并打印工作目录 / 任务卡 / summary 路径），适合人盯着 GUI agent 干活的场景。
  与该 agent 的 worker 二选一，不要同时用。
- `hive submit T-xxxx`：人工提交（commit + push `task/T-xxxx` + 跑 verify + 写结果）；
  验证不通过会写 `failed`，改完再 submit 一次即可。配套 `docs/操作手册.md` §4.1 和测试覆盖。
- `dashboard/`：零依赖静态看板（`serve.py` 只用标准库）。五列分布、租约过期实时变红、
  仓库/owner/搜索筛选、详情抽屉、深浅色；`scripts/serve-dashboard.sh` 用
  `hive status --json` 定时刷新。jsdom 固定时钟 19 条 DOM 断言 +
  `dashboard/verify.sh`（MIME、目录穿越、端口隔离）全部通过。
- `scripts/gitea-credential.sh`：host-scoped 凭据 helper，从 `~/dswork/.env` 读 token，
  不进 git 配置、不进进程参数。
- `scripts/start-worker.sh`：启动 worker 时把 `OPENAI_API_KEY` 映射成 `DEEPSEEK_API_KEY`；
  多 worker 用 `HIVE_DIR` 各自 clone，避免抢同一个 git index。
- `scripts/dispatch-loop.sh`：总控侧自动分派循环（默认 60s 一次 `dispatch --apply`）。
- `docs/CHROOT_GUI_AGENTS.md`：chroot 里小浣熊 / WorkBuddy 的完整接法——共享 `/home`、
  manual worker、WorkBuddy root 权限坑、GUI 卡超时与「迟到 summary」的恢复步骤。
- manual 交接时打印 worktree 绝对路径（flush 到终端），避免 GUI agent 找错目录。

### Changed

- 任务卡模板与 chroot 指南：GUI 卡统一 `timeout_minutes: 180`、`lease_minutes: 240`
  （实测小浣熊一张文档卡约 65 分钟，默认 60 分钟会在它写完 summary 前超时）。

### Fixed

- `status` 在结果 summary 为空时不再崩（GUI agent 超时前只写了一半的场景）。

### Ops

- 工具仓推成 Gitea 仓 `skywalk/hive`（建仓为私有，10:45 改为公开，匿名可 clone；
  变更记录与收回命令见 `docs/Gitea接入.md` §2），登记 4 个 agent
  （fb88-verify / fb88-dsh / raccoon / workbuddy）。
- 真实任务闭环 4 张：T-0001 集成冒烟（verify 模式）、T-0002 / T-0003 known-issues
  状态同步、T-0004 VSCode 片段补齐，全部 `close`，代码合入 light main。

## 0.2.0 — 2026-10-23

### Added

- `hive dispatch`：自动分派。按 `needs` / `repo` / 能力标签匹配，按 `max_concurrency` 控负载，
  依赖未完成或没有空闲 agent 的卡会列出跳过原因；默认 dry-run，`--apply` 才写。
- `hive doctor`：体检任务卡、agent 配置、租约、结果、归档、inbox；有错误返回非 0。
- `hive stats`：成功率、中位/P90 耗时、按 agent、按仓库；`--json` 给看板用。
- 任务卡 `needs` 字段；agent `max_concurrency`。
- `verify` / `manual` 两种 worker 模式（不再只有 exec）；`manual` 支持 `--launch` 打开 GUI，
  Windows 下自动把提示词放进剪贴板。
- Windows：`windows/bootstrap.ps1`、`windows/run-worker.ps1`、接入文档。
- 测试：`tests/` pytest 套件（10 个用例）+ `tests/soak.py` 浸泡测试。
- 文档：`docs/操作手册.md`、`docs/任务卡模板.md`、`docs/迁移指南.md`、`check.sh`。

### Fixed

- **任务卡解析把列表里的 `true`/`false` 转成布尔值**，导致 `verify: ["true"]` 这类命令
  传给 subprocess 时报 `TypeError: 'bool' object is not iterable`。列表项现在一律按字符串解析。
- **领取提交共用工作区 index**：多个 worker 共用一个 clone 时，`read-tree/update-index`
  会互相踩，可能推出一个不含 `claims/T-xxxx.json` 的 claim 分支。现在用独立的
  `GIT_INDEX_FILE` 构造提交，不碰工作区。
- `dispatch` 的负载统计漏掉「已分派但未 claim」的卡，会把同一台机器塞爆。
- `stats` 只把有 claim 的卡算进行中，漏掉已分派未领取的卡；现在区分「进行中」和「待合并」。
- `doctor` 把「有结果但还没 close」的租约也算进行中；现在只统计真正在跑的。
- `dispatch` 的负载扫描只遍历在飞的卡，不再为几百张历史卡逐个 `git show`。
- `close` 现在把 `res/` 结果归档到 `main:results/T-xxxx.json`，再删 `claim/`、`res/` 分支；
  `retry` 会同时撤掉归档，worker 能自愈重跑。

### Ops

- 复现并规避了裸仓 `receive.autogc` 与并发 clone 的竞态：高频 push 后 clone 可能报
  `unable to read sha1 file`。测试仓默认 `receive.autogc=false` + `gc.auto=0`；
  Gitea 仓建议夜间手动 `git gc`，见 `docs/操作手册.md` §8。
