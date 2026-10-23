# 蜂巢 Hive · 多机任务看板

把 `hive status --json` 的输出渲染成一块「一眼看到全局」的五列看板。
纯静态、零依赖：没有 npm、没有构建步骤、没有 CDN、没有网络字体、没有图标库，也不请求任何外部域名。

## 目录

```
dashboard/
├── index.html          页面骨架（顶栏 / 状态面板 / 看板容器 / 详情抽屉）
├── app.js              全部前端逻辑：取数、分列、筛选、渲染、抽屉、主题、自动刷新
├── style.css           深浅双主题样式、卡片与抽屉、≤768px 单列响应式
├── serve.py            零依赖静态服务器（只吃 Python 3 标准库）
├── sample-status.json  固定样例数据（开发 / 验收用；T-0101 的租约见 VERIFICATION.md）
├── README.md           本文件
├── REPORT.md           WorkBuddy 的完成报告
├── VERIFICATION.md     fb88 实跑验收记录（服务端 + jsdom + 真实数据）
├── dev/ui-test.js      jsdom 前端行为测试（可选，需 npm i jsdom）
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
cd ~/dswork/hive
PORT=8787 INTERVAL=20 ./scripts/serve-dashboard.sh
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
| `--port` | `8787` | 监听端口 |
| `--host` | `127.0.0.1` | 监听地址 |

## 怎么接真实数据

看板只读一个接口：同源的 `GET /api/status`，期望返回任务对象数组（字段见下）。
服务器**每次请求都重新读盘**，并在响应上带 `Cache-Control: no-store`，所以只要让
`--status` 指向的文件保持最新即可，不需要重启服务。

三种接法，任选其一：

1. **直接轮询落盘**：加个定时任务
   `*/1 * * * * hive status --json > /path/to/status.json.tmp && mv /path/to/status.json.tmp /path/to/status.json`
   然后 `python3 serve.py --status /path/to/status.json --port 8787`。
2. **符号链接**：`ln -sf ~/.hive/status.json dashboard/status.json`，用同样的命令启动。
3. **不启服务器**：把 `hive status --json > s.json` 得到的文件直接**拖到页面上**（或用顶栏「载入 JSON」按钮）。
   这是 `/api/status` 不可用时的降级通道，载入后会自动关掉自动刷新，避免被空数据覆盖。

任务对象字段（`hive status --json` 的每个元素）：

```
id, title, repo, priority(P1/P2/P3), state(ready|assigned|running|done|blocked),
owner, claim_agent, claimed_at, lease_until, stale, depends_on[],
result_status(done|failed|""), duration_s, head, verify[{cmd, code, tail[]}],
result_summary, summary（旧字段，仅用于搜索）
```

所有时间均为 ISO 8601 带时区。

## 五列怎么分

判定是**单向、互斥**的，按下列顺序命中即停，一张卡只出现在一列：

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

## 其他行为

- 顶栏：每列数量统计、最后更新时间、仓库筛选、owner 筛选、文本搜索（id / 标题 / 摘要）、
  自动刷新开关（默认开，15 秒）、深浅色切换（跟随系统，选择记 `localStorage`）。
- 详情抽屉：点卡片从右侧滑出，展示完整 `result_summary`（保留换行）、每条 verify 的
  命令 + 退出码 + 末尾输出、`head`、耗时、`claimed_at`、`lease_until`、`depends_on`；
  按 `Esc` 或点遮罩关闭。
- 卡片可 Tab 遍历、`Enter` 打开；窄屏（≤768px）单列堆叠；`prefers-reduced-motion` 时关闭动效。
- 无数据 / 读不到 / JSON 坏了都会显示明确文案和「重试」按钮；数据为空显示「还没有任务」。

## 不做的事

登录、编辑、拖拽改状态、后端数据库，以及任何伪造的实时数据——样例就是样例。
