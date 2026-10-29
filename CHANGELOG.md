# Changelog

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

### Changed

- **P2b 边界**：dispatch/assign/result accepted/archive/commit_index 已接；op log 压缩、
  snapshot、极端分区回滚、真四客户端压测仍待补。

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
