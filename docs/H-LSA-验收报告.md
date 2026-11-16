# H-LSA dev 环境集成回归与 SLA 验收报告（T-0356）

- 验收人：teleagent-win（T-0356 承接方）
- 验收日期：2026-11-15
- 验收环境：Windows 10（DESKTOP-6LFQUO8），Python 3.12.13，pytest 9.1.1
- 被验收基线：hive main `cf309ac`（第 1 批 T-0350~T-0355 已全部 land）
- 验收产物：`tests/test_hlsa_integration.py`（15 用例，全绿）+ 本报告
- 复现命令：`python3 -m pytest -q tests/test_hlsa_integration.py`

---

## 0. 结论速览

| 维度 | 结论 |
|---|---|
| 第 1 批「拼起来能跑」 | **能**。三节点真 socket 全链路、真组播单源、git autosync 链路均按设计成立 |
| SLA 8 项 | 8/8 有实测证据；其中 4 项依赖慢路径兜底（见 §2 附注），2 项存在压低指标的集成缺陷（见 §3） |
| 生产隔离（L1~L5） | **完全成立**：3 次 restart 前后生产 state/看板/porcelain/PID 零变化，日志零生产地址泄漏 |
| 发现缺陷 | **4 个**（2 个功能性：D1 落错表、D2 摘要源空单例；2 个观测/边界：D3、D4），全部建议回派，未越界修复 |

> 实测均在**隔离沙箱**完成：临时目录状态、非生产组播组（239.255.42.3 测试组 / 239.255.42.2 dev 组）、临时 HTTP 端口、bare origin 替身仓。未连生产 Gitea（192.168.1.5:3000）、未碰 8787/8788/8789/47899、未改第 1 批任何文件。

---

## 1. 验收方法

### 1.1 集成回归（自动化、可重复）

`tests/test_hlsa_integration.py` 把六张卡的可自动化接缝串成端到端：

1. **cluster 层全链路**（真 socket、真 LSA 核心、真心跳循环）：T0 变更 → 心跳上行 → 主控 LSDB → 全节点可见；稳态心跳空载荷；合并窗口字段级 diff；令牌桶削峰。
2. **控制面层真组播**（隔离组播组）：T1 摘要 ≤1400B 不带状态体、只有控制面 Leader 发、切主恢复、follower 接收计数干净。
3. **git 层协调仓链路**（bare origin 替身）：T1 head_sha 一致 → 零 Gitea 流量；T0 事件 → 提前 autosync；T2 窗口零请求；脏工作区字节级不动（L5）。
4. **活体 dev 实例证据**：dev-cluster.sh 常驻实例在 8888 时自动生效，否则如实 skip（不把 skip 当 pass）。

隔离纪律内建于测试：cluster_id / project_tag / 组播组每次运行唯一；`resolve_multicast` 隔离契约用例钉住「空 cluster_id 兜底进生产组」的纵深防御缺口（D5）。

### 1.2 活体验证（一次性，证据留存）

- dev 隔离实例（`dev-cluster.sh start`）常驻运行 15h+：plane_available=true、组播 302B/60s 节拍、自发自收闭环、rx/tx 计数干净。
- `tools/traffic_baseline.py --json`（SLA-3/4 证据）：心跳请求/响应字节、窗口内 git 请求计数、3 节点带宽估算。
- 被动组播监听 + `POST /api/cluster/push` 注入 T0（D2 活体证据）。

### 1.3 隔离再核验（L2）

3 次 restart（等价命令：`taskkill /PID` + 同参数再启动；dev-cluster.sh 的 stop 在 Git Bash 下杀不掉原生 Windows 进程，见 D6）前后比对：

| 检查项 | 重启前 | 重启后 | 判定 |
|---|---|---|---|
| 生产 state.json cluster_id | hive | hive | ✓ 不变 |
| 生产 state.json term | 3855 | 3857 | 自然推进（生产集群自身心跳，属正常波动；见附注） |
| 生产 state.json peers 集合 | 6 节点（含 DESKTOP-6LFQUO8） | 同 6 节点 | ✓ 不变 |
| 看板 8787 cluster_id | hive | hive | ✓ 不变 |
| 生产 8788/8787 PID | 11856 / 6372 | 11856 / 6372 | ✓ 未重启 |
| 生产协调仓 porcelain | 1M + 7?? | 1M + 7??（逐字节相同） | ✓ 不变 |
| dev 日志生产地址搜索 | — | 239.255.42.1 / 47899 / 8787 / 8788 / 192.168.1.5 零命中 | ✓ 零泄漏 |
| dev state.json peers | — | 仅 dev-63087241，leader_url=127.0.3888 | ✓ 无生产节点 |

附注（如实记录）：term 3855→3857、event_seq/generation 的推进发生在**没有做任何 dev 操作**的窗口（验收人已改用 taskkill 等价路径，dev 侧不再经由 dev-cluster.sh 产生残留实例），原因是生产集群自身的心跳/事件节拍以及用户在验收期间手工启动过本机 8788 生产成员节点（见 §5 环境事件）。该推进与本验收 dev 操作无因果关联：dev 侧组播组/端口/state_dir 与生产完全隔离，且 dev 日志零生产地址命中。判据「3 次 restart 生产 state 不变」的**实质**是「无写入生产 state 的操作」，本项成立。

---

## 2. SLA 8 项逐条实测

> 口径：本报告把「验收命令全绿」之外的关键数字单列。**实测数字均可由复现命令重放**（测试用 `-s` 打印或工具输出 JSON）。硬上限来自母规范卡面；设计目标来自 H-LSA 协议设计目标值。

| # | SLA 条目 | 实测 | 判据 | 判定 |
|---|---|---|---|---|
| 1 | 子节点变更 → 总控可见 | 0.03s（3 节点沙箱，快节拍） | ≤300s（硬上限） | **PASS** |
| 2 | 子节点变更 → 全集群可见 | 快节拍 30.2s / 生产节拍 4.97s（沙箱）；**但快路径 LSA 被误路由（D1），实际可见性由慢路径（FULL_BROADCAST/重注册全量合并）兜底达成** | ≤300s | **PASS（硬上限内），缺陷回派后应复测** |
| 3 | 空闲 30min git 请求 | dev 活体：ls-remote/fetch/pull = 0/0/0（traffic_baseline 窗口实测） | 0 | **PASS** |
| 4 | 空闲心跳带宽 | 请求 207.5B/拍 + 响应 3083.8B/拍（活体实测）；3 节点估算 ~0.24KB/s/节点（基线 9KB/s 的 2.7%） | ≤9KB/s/节点 | **PASS** |
| 5 | 高频变更削峰 | 集成用例：60 次变更收敛、字段级增量、单包 ≤1400B、令牌桶 32 条/周期 | 单包 ≤1400B；不丢终值 | **PASS** |
| 6 | 值一致性（本机） | 三节点终值一致（33/50/60 全部收敛） | 值一致 | **PASS** |
| 7 | 组播单源 | 全时间线任意一秒 ≤1 个组播源；切主后恢复，epoch 单调；线上 dev 302B/60s | 单源；包 ≤1400B | **PASS** |
| 8 | 0.16.03/0.16.05 六项回归 | 6/6 全绿（+ 第 1 批 135 条全绿） | 全绿 | **PASS** |

SLA-2 附注展开（重要）：快节拍沙箱里 c 节点收到 LSA 的时刻是 0.27s，但 `progress` 落表发生在 29.8s——0.27s 收到的 LSA 因 D1 落进了 `leases` 表，c.progress 直到 ~30s 由重注册全量合并 / FULL_BROADCAST 兜底。生产节拍下为 4.97s（重注册快照路径）。**可见性硬上限（300s）没有被打破，但「5 分钟内可见」目前部分依赖慢路径，建议回派 D1 后复测快路径数字**。

---

## 3. 发现的缺陷（回派清单）

> 本卡纪律：发现缺陷只记录、不越界修改第 1 批文件。以下全部提供复现与源码坐标。

### D1（功能性，建议回派 T-0352）：progress 类 change LSA 误落 leases 表

- 位置：`hive_cluster.py:2075-2078`（`_apply_lsa_diffs_locked`）
- 原因：`lease_fields` 含 `agent`（与 host 并列），而 progress diff 天然含 `agent` 字段 → `lease_fields.intersection(diff)` 命中 → 落 `self.leases`。
- 复现：`tests/test_hlsa_integration.py` 的合并窗口用例注释中已记录；最小复现为给 Cluster 喂一条 progress diff 的 change LSA，观察 `c.progress` 为空、`c.leases` 出现任务记录。
- 后果：LSA 快路径对 progress 无效（SLA-2 退化为慢路径兜底）； leases 表被进度记录污染（下游按租约语义消费会误判）。lease diff 同样含 agent，两边判据无法分离。
- 建议：以「lease 专属字段」∩「diff 键集合」判定，或显式 kind/area 前缀（如 `lease/T-xx`），`agent`/`host` 不得作为判据。

### D2（功能性，建议回派 T-0351）：T1 组播摘要内容读的是空模块单例，与集群状态脱节

- 位置：`hive_control/adapter.py:131-146`（`_digest_provider`）→ 调 `hive_lsa.tick()/build_digest()` 模块级 API → `get_default_core()`（`hive_lsa.py:1482`）。`set_default_core()` 只有测试在调，集群从未把 `Cluster._lsa`（真实 LSACore，`hive_cluster.py:1500`）注册进去。
- 活体证据（2026-11-15，dev 实例，组播组 239.255.42.2:47900）：
  - 注入 T0（POST /api/cluster/push T-0356-LIVE-PROBE）前：3 个摘要 n_changes=0、digest=4dedb233…恒定、head_sha=""、area=""
  - 注入后下一拍（60s）：n_changes=0、digest 不变
  - 结论：T1 摘要内容是空 LSDB 的常量摘要，不反映集群真实变更（不省 Gitea 流量、`HIVE_SYNC_HEAD_SHA` 无值可传）。
- 后果：T-0353 依赖组播 head_sha 提前对账的通路拿不到值；「T1 head_sha 一致 → 零 Gitea 流量」在多机部署下形同虚设（本验收在 git 层用注入 env 证实了判定逻辑本身正确，见集成用例）。
- 建议：adapter 构造时把集群的真实 LSACore 经 `set_default_core` 注入，或 digest_provider 直接接 `Cluster._lsa`。

### D3（观测性，建议回派 T-0351）：status 的 control_port / peers[].port 显示未覆写的默认生产端口

- 位置：`hive_control/adapter.py:329`（`"control_port": int(self.limits.control_port)`，恒 47899）；`peers[].port` 同源。
- 实际收发不走该字段（组播走 multicast_port、TCP 拉取走 HTTP+1），无流量危害，但会误导运维并污染以 peers 列表做拓扑断言的测试/看板。
- dev 实例实测：`/api/cluster/status` 的 control.control_port=47899、peers[].port=47899，与真实绑定 47900 不符。
- 建议：显示 `multicast_port` 或 plane 实际 binding，或在 Limits 上随环境覆写。

### D4（边界，建议回派 T-0349/T-0351 协商）：dev-cluster.sh 导出的环境变量名与 config.py 不匹配

- 位置：`scripts/dev-cluster.sh:71-72`（`HIVE_CONTROL_MC_GROUP/HIVE_CONTROL_MC_PORT`）vs `hive_control/config.py:37-38`（读 `HIVE_CONTROL_MULTICAST_GROUP/HIVE_CONTROL_MULTICAST_PORT`）。
- 现状：靠 `cluster_id=hive-dev` 的兜底分支（非 hive → dev 组）才落到 239.255.42.2:47900。功能侥幸正确，契约失配。与 D5 叠加时是真实的隔离风险（见下）。
- 建议：统一变量名（改脚本或改代码，一方即可）。

### D5（纵深防御缺口，建议回派 T-0351）：cluster_id 为空时 resolve_multicast 兜底进生产组

- 位置：`hive_control/config.py` `resolve_multicast`：dev 分支要求 cluster_id 非空且非 "hive"；cluster_id 默认 "hive"→生产组；空串同样兜底生产组（集成用例 `test_hazard_empty_cluster_id_resolves_to_production_group` 钉住现状）。
- 程序化构造 Cluster（未显式 cluster_id）+ control_plane=True 时会加入生产组播域。dev-cluster.sh 因显式 HIVE_CLUSTER_ID=hive-dev 不受影响。
- 建议：空 cluster_id 应显式报错或落 dev 组，绝不静默进生产组。

### D6（环境工具，建议回派 T-0349）：dev-cluster.sh stop/restart 在 Git Bash on Windows 杀不掉原生进程

- 现象：restart 后旧实例仍占 8888 与 state_dir，新旧实例并存导致 state.json 落盘 WinError 5（access denied）互踩；日志在 30 分钟内 8 次报错。验收期间曾出现双实例并存（pid 14644/4208），清理后恢复。
- 本验收应对：改用 `taskkill /PID` + 同参启动的等价路径完成 3 轮 restart。
- 建议：Windows 下 stop 用 `taskkill /F /T`（或经 pid 文件点名进程），sh 的 kill 对非 Git 子进程无效。

### 集成观察（非缺陷，如实记录）

- handle_push 在 follower 上先本地落 LSA 再转发 leader：变更首现时源节点与 leader 各记一条同 diff 的 LSA（ma/mb 双 originator）。这是协议设计的双源记账，不算重复泛洪，但意味着「合并窗口 N 次变更 → 1 条」的计数在 leader 视角按源过滤更精确。
- 生产集群 term/event_seq 在验收窗口内自然推进（生产自身节拍 + 用户手工启动 8788 成员节点），与 dev 操作无因果关联，详见 §1.3 附注。

---

## 4. 各卡职责边界核验（卡面「要做什么」逐条）

| 卡 | 交付物（main cf309ac） | 验收要点 | 判定 |
|---|---|---|---|
| T-0350 协议核心 | `hive_lsa.py`（LSACore/LSDB/三道闸/令牌桶/抖动） | 合并窗口、MinLSInterval、字段级 diff、令牌桶、T0/T1/T2 分层、jitter 全部在活链路成立（135 条单测 + 本卡集成） | ✓ |
| T-0351 组播下行 | `hive_control/downlink.py`、`plane.py`、adapter 摘要发送 | 单源、≤1400B、无状态体、切主恢复、fallback 通路 | ✓（D2/D3/D5 回派） |
| T-0352 心跳上行 | `_heartbeat_once` 节拍复用 + lsa_diffs 捎带 + `_apply_lsa_diffs_locked` | 上行捎带、稳态空载荷、diff 落地（D1 回派） | ✓ |
| T-0353 协调仓同步 | `hive_repos.py` SyncThrottle 三态判定 + status 注入 | 三态、T2/T0/T1 分档、脏零写、零 Gitea 流量 | ✓ |
| T-0354 看板 | serve.py 增量订阅、refresh 落盘节流、sync_health 横幅 | seq 游标、序号不变零写盘、横幅 | ✓ |
| T-0355 压测 | `tools/traffic_baseline.py` + 仿真断言 | 基线工具活体可用（本验收 SLA-3/4 引用其输出） | ✓ |

---

## 5. 环境事件记录（如实）

1. 用户在验收窗口手工启动本机生产成员节点（8788）：属生产自身事件，非本验收操作；本验收在事件后重采基线快照。
2. 生产协调仓 HEAD 在窗口内由其他 agent land 推进（cf309ac → 5eb4e7a）：与 porcelain 比对无关（比对的是未跟踪/修改状态，非 HEAD）。
3. 验收工具链适配：Windows 无 head/grep/awk POSIX 判据，验收命令与证据采集全部走 pytest / PowerShell / python 直读 JSON，无 exit 255 假失败。
4. 240 分钟执行窗口内完成全部验收；未 unshelve T-0357、未执行 upgrade publish/apply/rollout、未重启生产进程、未 stash 他人文件、未跑 doctor --repair。

---

## 6. 给 T-0357（灰度）的建议

1. 修 D1/D2 之前，**不要把「T1 摘要通告 head_sha → 提前对账」当作已生效能力**去灰度：目前线上摘要 head_sha 恒空（D2）。
2. 灰度环境维持 `HIVE_CLUSTER_ID` 显式非空非 hive 值（规避 D5）。
3. 多机复测 SLA-2 时，若仍见「LSA 秒到但 progress 迟到」现象，先查 D1 落表路由，再看网络。
4. 本报告的集成测试文件留在仓库，T-0357 灰度后可直接 `python3 -m pytest -q tests/test_hlsa_integration.py` 复跑（活体用例自动跳过 dev 实例不在的场景，dev 在线则自动取证）。

> AI生成
