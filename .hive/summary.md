T-0217 看板集群面板：节点表 + 心跳 age + 状态灯

做了什么：
1. `dashboard/app.js`：`#cluster` 从一行文本升级为点击展开的面板（默认折叠）。
   头部显示主节点名称 + `http://IP:Port`（复制按钮）、term、quorum、集群 ID；
   节点表为 名称 / 角色 / 状态灯 / 心跳 age / 在飞任务数 / 版本，self 行带「本机」。
2. age 用 `last_seen_epoch` 换算 `3s`/`2m`/`1h`；状态灯优先用后端 `status`，
   缺失时按 `peer_timeout`/`dead_timeout` 分档，后端没下发时按 15s/60s 估并在卡片注明。
3. 任务数在前端按 `/api/status` 的 `owner == 节点名` 统计在飞卡（不新增接口）；
   失败仍降级为「集群：未启动」。
4. `dashboard/style.css`：面板/表/三色状态灯样式；≤768px 只留名称 + 状态两列。
   未改 `.topbar` 层级，未新增 z-index。
5. `tests/test_dashboard_cluster.py`：新增 5 个用例，用 node 直接执行 app.js 的纯渲染函数，
   构造 3 节点（alive/suspect/dead）快照断言三档灯、2s/30s/2m age、任务数、本机标记与手机两列。

验证：
- `python3 -m pytest -q tests/test_dashboard_cluster.py tests/test_dashboard_freshness.py` → 12 passed。
- 邻近回归 `test_dashboard_layering.py` + `test_dashboard_dispatch_ui.py` → 32 passed, 1 skipped。
- 手测：假集群 mock 一台 dead 节点，展开面板显示红色「离线」；375px 下 `scrollWidth=375`，无横向滚动。

截图（浅色，已附）：
- 折叠态：![折叠态](.hive/cluster-collapsed.png)
- 展开态：![展开态](.hive/cluster-expanded.png)
- 手机 375px：![手机](.hive/mobile-cluster-expanded.png)

风险：后端 snapshot 当前不下发 peer_timeout/dead_timeout，面板会显示「按 15s/60s 估」；
节点灯仍优先用后端 status，因此不会与后端判定打架。截图时用个人设置关掉了既有空 CI 条，
避免它遮挡面板（与本卡无关）。
