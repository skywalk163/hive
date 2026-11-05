# 蜂巢 Hive · 多机任务看板

把 `hive status --json` 的输出渲染成一块「一眼看到全局」的六列看板，外加一个
承载**搁置区**（`shelved`：不能忘，但大部分时候可以忽略）与**计划区**（`plan`：
把一组任务当一个项目跟踪）的 Tab 区域。
纯静态、零依赖：没有 npm、没有构建步骤、没有 CDN、没有网络字体、没有图标库，也不请求任何外部域名。

## 目录

```
dashboard/
├── index.html          页面骨架（顶栏 / 状态面板 / 看板容器 / 详情抽屉）
├── app.js              全部前端逻辑：取数、分列、筛选、渲染、抽屉、主题、自动刷新
├── plan_store.py       任务计划的存储与领域逻辑（JSON + 原子写 + 状态机 + 留言指令解析）
├── style.css           深浅双主题样式、卡片与抽屉、≤768px 单列响应式
├── serve.py            零依赖静态服务器（只吃 Python 3 标准库）
├── sample-status.json  固定样例数据（开发 / 验收用；T-0101 的租约见 VERIFICATION.md）
├── README.md           本文件
├── REPORT.md           WorkBuddy 的完成报告
├── VERIFICATION.md     fb88 实跑验收记录（服务端 + jsdom + 真实数据）
├── dev/ui-test.js      jsdom 前端行为测试（可选，需 npm i jsdom）
├── dev/shelf-ui-test.js 搁置面板的 jsdom 行为测试（可选，需 npm i jsdom）
├── dev/plans-ui-test.js 计划面板的 jsdom 行为测试（可选，需 npm i jsdom）
└── verify.sh           一键复验服务端
```

## 怎么跑

```bash
cd dashboard
python3 serve.py --status sample-status.json --port 8787
```

启动后终端会打印：

```
http://127.0.0.1:8787
```

浏览器打开这个地址即可。

### 在蜂巢仓库里跑真实数据

```sh
# 在工作目录里（脚本会自己定位 hive 仓）
PORT=8787 INTERVAL=20 ./hive/scripts/serve-dashboard.sh
```

脚本会每 20 秒执行一次 `hive status --json > dashboard/status.json`，再起 `serve.py`；
`status.json` 已在 `.gitignore` 里。浏览器打开 `http://127.0.0.1:8787` 即可。

在 Windows 上看（服务只监听 127.0.0.1）：

```powershell
ssh -L 8787:127.0.0.1:8787 skywalk@192.168.0.88
# 然后浏览器打开 http://127.0.0.1:8787
```

参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--status` | `sample-status.json` | `hive status --json` 导出的状态文件路径 |
| `--port` | `8787` | 监听端口（唯一来源 `hive_ports.py`） |
| `--host` | `127.0.0.1` | 监听地址 |

> 端口不要记散：看板 **8787**、集群 HTTP **8788**、发现 UDP **8789**、冒烟 **8799**。
> 完整分配与防火墙见 [`docs/端口与服务规划.md`](../docs/端口与服务规划.md)；
> 现场用 `python3 hive.py ports --check` 体检。

## 怎么接真实数据

看板只读任务接口：同源的 `GET /api/status`，期望返回任务对象数组（字段见下）。
另有 `GET /api/cluster`：本机常驻集群的只读视图（`running/leader/term/nodes`），
以及 `/api/status` 里把集群实时进度合并到任务行（`push_source=cluster`）。
集群 HTTP 默认 `8788`，与看板的 `8787` 分开；发现端口是 UDP `8789`。
服务器**每次请求都重新读盘**，并在响应上带 `Cache-Control: no-store`，所以只要让
`--status` 指向的文件保持最新即可，不需要重启服务。

响应头 `X-Snapshot-Mtime` 是状态文件的 mtime（epoch 秒）：前端据此在右下角显示
「数据源：/api/status（快照 N 分钟前）」，超过 10 分钟加 ⚠。刷新循环停摆时接口
依然是 200，没有这个头就只能靠人肉发现「页面在放几小时前的旧数据」。

三种接法，任选其一：

1. **直接轮询落盘**：加个定时任务
   `*/1 * * * * hive status --json > /path/to/status.json.tmp && mv /path/to/status.json.tmp /path/to/status.json`
   然后 `python3 serve.py --status /path/to/status.json --port 8787`。
2. **符号链接**：`ln -sf ~/.hive/status.json dashboard/status.json`，用同样的命令启动。
3. **不启服务器**：把 `hive status --json > s.json` 得到的文件直接**拖到页面上**（或用顶栏「载入 JSON」按钮）。
   这是 `/api/status` 不可用时的降级通道，载入后会自动关掉自动刷新，避免被空数据覆盖。

任务对象字段（`hive status --json` 的每个元素）：

```
id, title, repo, priority(P1/P2/P3),
state(ready|assigned|running|done|blocked|shelved|...),
owner, claim_agent, claimed_at, lease_until, stale, depends_on[],
result_status(done|failed|""), duration_s, head, verify[{cmd, code, tail[]}],
result_summary, summary（旧字段，仅用于搜索）
shelved_at, shelved_from, shelve_reason（搁置卡才有；旧快照缺字段时显示「未填原因」）
```

所有时间均为 ISO 8601 带时区。

## 六列怎么分（外加搁置区）

判定是**单向、互斥**的，按下列顺序命中即停，一张卡只出现在一列：

0. **搁置**：`state === "shelved"` → 不进任何列，交给折叠搁置区（见下）
1. **已关闭**：`state === "done"`
2. **异常**：`result_status === "failed"` 或 `state === "blocked"` 或 租约过期
3. **待合并**：`result_status === "done"` 且 `state !== "done"`
4. **进行中**：有 `claim_agent`，或 `state` 为 `assigned` / `running`，且还没有结果
5. **待领取**：`state === "ready"` 且没有 `claim_agent`

**租约过期（stale）在渲染时重新计算**，不是照抄服务端字段：

```
stale === true  ||  ( lease_until 已过  &&  result_status 为空 )
```

也就是说即使 `status.json` 没刷新、时间已经过了租约，卡片也会自己变红并落进异常列。
已经出结果的任务（`result_status` 非空）不再因为租约到期判为异常。
卡片上显示「已过期 X 分钟」，不显示负的剩余时间；已关闭列默认折叠，点列头的「展开」可展开。

## 搁置区：为什么不是第 7 列

`state === "shelved"` 的卡当前做不了：打回待领取会白占一个名额、干扰 agent 领取；
挂在异常列又太显眼（情况已知、租约和结果等现场都已释放）；直接删则怕忘。
所以它要的是「**不能忘，但大部分时候可以忽略**」——加一列会跟活跃卡平级、
还会把本来就挤的六列压得更窄。

排版上做成两处**按钮**，平时只占一行：

- 顶栏统计条末尾常驻一枚低调的「搁置 N」（计数始终可见，这是「别忘了」）。
- 看板上方一条默认收起的横条，点开才展开卡片列表，用多列网格紧凑排布。
  每张卡显示搁置原因、时间、原状态，以及三个出口：`放回待领取` / `另立新卡` /
  `废弃`；「复制说明」拿到的是写明**不要领取**的搁置说明，不是派发指令。
- 搁置卡**不计入**「共 N 个任务」活跃统计，也不进异常列；但仍参与搜索。
  搜索/筛选命中搁置卡时，折叠态会显示「有匹配」提示，免得以为卡丢了。
- 活跃列（待领取 / 进行中 / 待合并 / 异常）的卡片上有「搁置」按钮，写操作
  走 `/api/task` 的 `shelve` / `unshelve` / `respawn`（限 controller 角色）。

## 计划区（与搁置同一个 Tab 区域）

搁置和计划共用看板上方那一个 section，靠两个 Tab 切换：点「搁置」显示搁置卡列表
（默认仍收起，点一下展开），点「计划」显示计划面板。切 Tab 不销毁对方的数据，
搁置的折叠/计数/三个出口、搜索命中的「有匹配」提示全都保持原样。

计划把一组任务当成一个项目跟踪：

| 字段 | 说明 |
| --- | --- |
| `id` | `P-0001`，由 `plan_store` 顺序分配 |
| `prefix` | 短码，如 `PAY` / `INFRA`，给人看的分组线索 |
| `title` / `description` | 标题必填，描述可留空 |
| `status` | `active` 进行中 / `completed` 已完成 / `closed` 已关闭 / `deleted` 已删除（软删） |
| `pinned` | 是否置顶；**只对进行中生效**，一完成/关闭自动撤掉 |
| `task_ids` | 关联任务号数组，进度就按它算 |
| `created_by` / `created_at` / `updated_at` / `completed_at` | 创建人、创建/更新时间、完成时间 |

排序：进行中**置顶在前**，其余按 `updated_at` 倒序；已完成（含已关闭）按
`completed_at` / `closed_at` 倒序。页面分组固定「进行中」在上、「已完成」在下。

手动完成时如果还有关联任务没做完，卡片徽标写「**手动完成**」、进度文案写
「N/M **关联任务完成**」并附一行提示 —— 不让「已完成」和 50% 的进度条互相打脸
（自动完成只有在全部任务完成时才会触发，所以那种情况下进度必然是 100%）。

进度：`total` / `done` / `rate` 每次读取时按当前 `status.json` 快照现算，
**不另存一份计数**。快照里找不到的任务会单列在 `progress.missing` 并在卡片上提示，
不会被静默算成「未完成」。全部关联任务完成且计划开了 `auto_complete` 时，
`GET /api/plans` 会顺手把它标成已完成 —— 页面关着、agent 交完最后一张卡也一样。

### 接口

```http
GET  /api/plans[?include_deleted=1&status=active]
POST /api/plans   {"action": "create", "prefix": "PAY", "title": "支付改造",
                   "task_ids": ["T-0231"], "pinned": false, "auto_complete": true}
POST /api/plans   {"action": "update", "plan_id": "P-0001",
                   "fields": {"title": "…", "task_ids": ["T-0231"], "pinned": true}}
POST /api/plans   {"action": "complete" | "close" | "pin" | "unpin", "plan_id": "P-0001"}
POST /api/plans   {"action": "delete", "plan_id": "P-0001", "confirm": true}
```

读免鉴权（和 `/api/status` 同口径）；写要 token（与 `/api/task` 一致）。
权限：**创建者本人或 controller/admin** 可写，普通成员只读。删除额外要求
`confirm=true`（页面上的二次确认）或 controller 身份。每次写操作都往
`audit.log` 追加一行（含 `plan.*` / `chat.plan-cmd` 两类 action）。

数据落在 `dashboard/plans.json`（可用 `HIVE_PLANS_FILE` 指到别处），写盘是
tmp + `os.replace` 原子替换，进程内加锁串行化。文件已在 `.gitignore` 里 ——
它是本机运行态数据，不进协调仓。

### 留言指令

agent 没有浏览器，只有留言通道，所以计划也能从留言里操作：

```
/plan create 前缀 标题                      # 新建（标题可含空格）
@command:plan update P-0001 标题=… 描述=… 前缀=… 关联=T-0001,T-0002 置顶=on 自动完成=off
@command:plan complete P-0001
@command:plan close    P-0001
@command:plan delete   P-0001               # 仅 controller
@command:plan pin|unpin P-0001
```

指令不会当普通留言存掉：执行结果（成功/失败 + 用法）会作为一条 `📋` 系统留言
发回同一频道，同时写审计。解析失败返回 400 + 完整用法；无权限返回 403；
留言里没有「二次确认」这一步，所以删除只给 controller 做。

**两条来源都算**：

1. 看板留言框里直接发（走 `POST /api/chat`，当场执行）；
2. 别的机器上 agent 用 `hive chat post` / MCP `hive_chat_post` 发进集群 ——
   消息不经过看板，所以由**拥有计划文件的这台看板**在轮询留言时补执行
   （`GET /api/plans` 内部会读 `since=游标` 的新留言）。首次启动只把游标定位到
   当前最新，**不回头重放历史留言**；执行过的消息 id 记在 `plans.json` 的
   `applied` 里，重复轮询不会重跑。身份取留言的 `from`，按本机 token 表查角色，
   查不到就是普通成员（删不了东西）。

**Agent 发布一组任务时建跟踪计划**：一条指令就够了 —— 建计划的同时把这一组
任务号带进去，之后进度、完成率、自动完成都自动跟上：

```
@command:plan create 前缀=PAY 标题=支付二期 关联=T-0105,T-0106,T-0107
```

## 其他行为

- 顶栏：每列数量统计、最后更新时间、仓库筛选、owner 筛选、文本搜索（id / 标题 / 摘要）、
  自动刷新开关（默认开，15 秒）、深浅色切换（跟随系统，选择记 `localStorage`）。
  还有集群徽标（每 5 秒读 `/api/cluster`）：显示主节点 / term / 在线节点数；未启动时显示
  「集群：未启动」。
- 详情抽屉：点卡片从右侧滑出，展示完整 `result_summary`（保留换行）、每条 verify 的
  命令 + 退出码 + 末尾输出、`head`、耗时、`claimed_at`、`lease_until`、`depends_on`；
  按 `Esc` 或点遮罩关闭。
- 卡片可 Tab 遍历、`Enter` 打开；窄屏（≤768px）单列堆叠；`prefers-reduced-motion` 时关闭动效。
- 无数据 / 读不到 / JSON 坏了都会显示明确文案和「重试」按钮；数据为空显示「还没有任务」。

## 不做的事

登录（写操作只认 token/cookie）、拖拽改状态、后端数据库，以及任何伪造的实时数据
——样例就是样例。计划不跨机同步：它跟 `settings.json` / `audit.log` 一样属于
本机的看板运行态；跨机协作仍走任务卡与留言。
