# H-LSA 开发隔离与流量基线（T-0349）

> 本卡是 H-LSA 第 0 批唯一前置：产出后续实现卡共同引用的「开发副本路径 +
> 接入方式 + 流量基线分母 + 合入闸门清单」。在这套隔离与基线落地之前，
> 第 1 批任何一张卡都不许开工改 hive 代码。
> 母规范 `H-LSA-协议设计-借鉴OSPF.md` §5（指标）/ §7（隔离五条）待母文档入库后对齐。

## 1. 开发副本（L1 代码隔离）

### 1.1 路径与基线

| 项 | 值 |
|---|---|
| 开发副本根目录 | `G:\dswork\hive-dev-src`（POSIX 视角 `/g/dswork/hive-dev-src`；FreeBSD 上按对应挂载为 `~/dswork/hive-dev-src`） |
| clone 来源 | 协调仓 `origin/main`：`http://192.168.1.5:3000/skywalk/hive` |
| 基线参照 sha（派发时点） | `e0acfa62fb2934ff2f3fab06e787e9a71c915017`（卡面派发记录，已过期，仅备查） |
| 副本当前 HEAD | `d7a888c725588852c9acfcbb3f7715c013056e3e`（clone 时刻 `origin/main`，工作区干净，无在途改动） |
| clone 入库时间 | 2026-11-14 14:07 +08:00 |

**隔离要点**

- 副本是从 `origin/main` 单独 clone 的，**不带**生产工作区
  （本机 `G:\dswork\duan-light-merge\hive`）里的任何未提交改动；那些是别人的
  在途产物，本批一律不复制、不提交进开发副本。
- 后续卡在开发副本内改脚本/工具/测试；`scripts/dev-cluster.sh` 只改开发副本里
  的这份，生产工作区一个字节不动。
- 若该路径将来被占用或不可用，按 §1.2 的覆盖方式改到新路径，并把新路径与 HEAD
  回填到本表——后续卡只引用本表的值，不自己猜路径。

### 1.2 接入方式

开发集群由 `scripts/dev-cluster.sh` 拉起，默认参数已经与生产逐项错开；
用 `HIVE_DEV_REPO` 把代码根指向开发副本：

```sh
# cwd = 本卡 worktree 仓库根目录（脚本在 scripts/ 下）
HIVE_DEV_REPO=G:/dswork/hive-dev-src scripts/dev-cluster.sh start
HIVE_DEV_REPO=G:/dswork/hive-dev-src scripts/dev-cluster.sh status
HIVE_DEV_REPO=G:/dswork/hive-dev-src scripts/dev-cluster.sh restart
HIVE_DEV_REPO=G:/dswork/hive-dev-src scripts/dev-cluster.sh stop
```

- 不传 `HIVE_DEV_REPO` 时，脚本默认取「脚本上级目录」，保持既有默认路径语义不变；
  只有显式指向独立副本时才启用 L1 代码隔离。
- 健康检查：`GET http://127.0.0.1:8888/api/cluster/health`，返回 200 且
  `cluster_id=hive-dev`。
- Python 解释器：POSIX 开发机用 `python3`；本机 WIN10 无 `python3`，用 `python`。
- 基线采集：

```sh
python3 tools/traffic_baseline.py --json --out /tmp/hlsa-baseline.json
```

### 1.3 隔离参数对照（dev vs 生产）

| 维度 | 生产 | 开发实例（dev-cluster.sh 默认） | 环境变量覆盖 |
|---|---|---|---|
| cluster_id | `hive` | `hive-dev` | `HIVE_DEV_CLUSTER_ID` |
| HTTP 端口 | 8788 | 8888 | `HIVE_DEV_PORT` |
| UDP 发现端口 | 8789 | 8889 | `HIVE_DEV_DISCOVERY_PORT` |
| MCP 端口 | 8790 | 关闭（`--no-mcp`） | — |
| 看板端口 | 8787 | 不启动 | — |
| 组播组 | `239.255.42.1` | `239.255.42.2` | `HIVE_DEV_MC_GROUP` |
| 组播端口 | 47899 | 47900 | `HIVE_DEV_MC_PORT` |
| project_tag | （空） | `hive-dev` | `HIVE_DEV_PROJECT_TAG` |
| state_dir | `~/.hive/cluster` | `~/.hive/dev-cluster` | `HIVE_DEV_STATE` |
| 绑定地址 | `0.0.0.0` | `127.0.0.1` | — |
| 广播发现 / 同步 / store / MCP | 开 | 全关（`--no-discover --no-sync --no-store --no-mcp`） | — |

控制面（`hive_control`）组播配置由 `HIVE_CONTROL_MC_GROUP` /
`HIVE_CONTROL_MC_PORT` / `HIVE_CONTROL_PROJECT_TAG` 环境变量装配；未设时保持
模块默认，既有生产行为与既有测试不变。

## 2. 流量基线（优化前的分母）

> 口径：跑不出来的项如实标 `unavailable` 并写原因，**不拿历史估算值冒充实测**。
> 历史参照（CHANGELOG 0.16.05 / 0.16.04，两节点本地空闲 25s 实测，3 节点为估算）
> 单独列在 §2.3，不写入本次实测字段。

### 2.1 采集字段（输出 JSON，可复现、带时间戳与参数）

| 字段 | 含义 | 取不到时 |
|---|---|---|
| `heartbeat.request_bytes` | 心跳请求字节：平均 / 峰值 / 最小 / 样本数 | 实例不可达 → `unavailable` |
| `heartbeat.response_bytes` | 心跳响应体字节：平均 / 峰值 / 最小 / 样本数 | 同上 |
| `persistence.state_json_bytes` | 当前 `state.json` 字节数 | 状态文件不存在 → `unavailable` |
| `persistence.writes_per_minute` | 短窗采样折算的落盘次/分钟（精确计数需进程内埋点，见 T-0352） | 无文件 → `unavailable` |
| `git_activity.log_command_counts_per_window` | 窗口内日志中的 `ls-remote`/`fetch`/`pull` 次数 | 无日志 → 0 |
| `git_activity.ls_remote_probe` | 各仓库 `ls-remote origin HEAD` 可达性探测（**不**主动 fetch/pull） | 仓库不可达如实记录 |
| `bandwidth.bandwidth_kbps` | 由心跳往返字节 × 节点对密度估算（显式标注 `estimate:true`） | 心跳取不到 → `unavailable` |

### 2.2 本轮实测记录（2026-11-14 14:24 +08:00，WIN10，单机 dev 实例）

dev 实例按 §1.3 隔离参数在 127.0.0.1:8888 起过一次（`cluster_id=hive-dev`，
`peers=1`，mcp/store/sync/discover 全关），采集后已停止、释放 8888。
离线复核（实例已停）：`state.json` 留存于 dev 状态目录，心跳/带宽项不可达时标
`unavailable`，不回填历史估算。

| 指标 | 本轮值 | 口径 / 限制 |
|---|---|---|
| 心跳请求 avg / peak | `204 B` / `204 B`（12 采样） | 空闲轻量心跳，不含全量 progress/leases；实例停止后不可复核，以工具重跑为准 |
| 心跳响应 avg / peak | `3367.2 B` / `3368 B`（12 采样） | HTTP 响应体字节；同上 |
| `state.json` 大小 | 留存复核 `23938 B`（采集时刻快照 `23568 B`） | dev `~/.hive/dev-cluster/state.json`；采集后实例退出又落盘一次 |
| 落盘次数 | 短窗折算 `12 次/分钟` | 5s 短窗采样折算；精确计数需进程内埋点（T-0352） |
| 30min 窗口 `ls-remote`/`fetch`/`pull` 日志计数 | `0 / 0 / 0` | 离线无法回放历史；`ls-remote` 探测可达，HEAD `d7a888c` |
| 控制面空闲带宽 | `1.866 kbps`（估算，`estimate=true`） | 心跳往返字节 × 3 节点 3 对 × 5s 间隔；真实多节点实测归 T-0355 / T-0357 |

离线口径（实例未起时）：`heartbeat` / `persistence` / `bandwidth` 标
`unavailable`，原因分别为健康检查连接拒绝（WinError 10061）、`state.json` 不存在、
依赖心跳采样；`git_activity` 仍可做 `ls-remote` 静态探测。

> 后续卡（T-0352 / T-0355 / T-0356）在 dev 实例常驻后重跑本工具复核分母；
> 禁止用 §2.3 的历史参照值冒充本次实测。

### 2.3 历史参照基准（0.16.05，非本次实测）

| 指标 | 参照值 | 来源 |
|---|---|---|
| 心跳请求平均 | 685B | CHANGELOG 0.16.05（两节点本地空闲 25s） |
| 心跳响应平均 | ~2.9KB（2.4KB~2.9KB） | CHANGELOG 0.16.05 / 0.16.04 |
| 3 节点空闲控制面带宽 | ~9KB/s | CHANGELOG 0.16.04（3 节点全连通估算） |

## 3. 合入闸门检查清单（供 T-0357 引用）

合入生产前逐条勾选；任一条不满足则保持 `shelved`，仅交办方人工 `unshelve`
才进生产。

- [ ] **生产 term 不变**：连续 3 次 dev `restart` 后，生产
      `~/.hive/cluster/state.json` 的 `term` / `leader_id` / `leader_url` /
      `alive` / `known` peers / `event_seq` / `generation` 与操作前快照完全一致。
- [ ] **看板仍读 `cluster_id=hive`**：看板 8787 顶栏 `cluster_id` 未变成
      `hive-dev`，节点列表仍是生产成员；本卡全程未重启看板进程。
- [ ] **开发日志无生产地址**：dev 日志（`~/.hive/dev-cluster/cluster.log`、
      `cluster.err.log`）搜不到 `239.255.42.1`、`47899`、`8787`、`8788`、
      `8789`、`8790`、生产 leader URL、生产仓路径任一字符串。
- [ ] **无生产组播包**：开发实例未向生产组播组 `239.255.42.1:47899` 发任何包
      （组播组/端口已切到 `239.255.42.2:47900`）。
- [ ] **在途改动完好无损**：生产工作区原有未提交改动（别人的在途产物）逐条
      仍在，未被 stash / checkout / reset / merge / 复制到开发副本。
- [ ] **备份完成并留校验和**：合入前备份看板 `status.json`、
      `~/.hive/cluster/state.json`、`~/.hive/upgrade/last.json`，记录备份路径与
      校验和，写入 T-0357 收口报告。
- [ ] **无在飞卡 / 租约**：`python3 hive.py status --json` 无 `ready` / `assigned`
      在飞卡、无进行中租约。
- [ ] **集成 SLA 全绿**：T-0356 验收报告 8 项 SLA 通过，隔离清单 L1–L5 各有证据。
- [ ] **未碰生产常驻进程**：全程未重启 `service hive_cluster` / `master.sh` /
      `cluster serve`（8788）/ 看板 8787；未跑 `doctor --repair`，未执行
      `upgrade publish|apply|rollout`。

## 4. 自检命令与判据

```sh
# 隔离自检（纯 Python，可注入路径，不依赖真实网络）
python3 -m pytest -q tests/test_dev_cluster_isolation.py

# 开发实例状态（HIVE_DEV_REPO 指向开发副本）
HIVE_DEV_REPO=G:/dswork/hive-dev-src scripts/dev-cluster.sh status

# 基线采集
python3 tools/traffic_baseline.py --json --out /tmp/hlsa-baseline.json
```

测试内复用入口：`tests/test_dev_cluster_isolation.py` 暴露 `assert_isolated(cfg)`，
后续卡可直接调用逐项校验端口 / 组播组 / cluster_id / state_dir / 代码路径隔离。

## 5. 已知边界与风险

- 本机 WIN10 自带 Git for Windows 的 `sh.exe` 可用，但 `scripts/dev-cluster.sh`
  是 POSIX `.sh`；本轮 dev 实例是用与脚本等价的 `cluster serve` 隔离参数直接拉起
  验证的，脚本本身的 shell 行为由 pytest 静态隔离用例覆盖。需要在 POSIX 开发机
  （192.168.0.88 FreeBSD）复跑 `.sh` 子命令时，切到该主机执行。
- 控制面当前 `plane_available:false`（装配缺陷，归 T-0351 修）；本卡只把组播组/
  端口/标签的可配通路留好，不赌「误连生产也收不到」。控制面组播组/端口的环境变量
  装配落点属 T-0351 范围，本卡 `paths` 不含 `hive_cluster.py` / `hive_control/*`。
- 带宽、落盘次数为估算 / 短窗折算，不是生产级实测；多节点真实带宽待 T-0355 压测、
  T-0357 灰度观察复核。
- 本卡只改动 `paths` 列出的四个文件：`scripts/dev-cluster.sh`、
  `tools/traffic_baseline.py`、`tests/test_dev_cluster_isolation.py`、
  本文件；未改 `hive_cluster.py` / `hive_control/*` / `hive.py`。
