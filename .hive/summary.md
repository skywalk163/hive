# T-0206 交付：Windows 子进程全局无窗（CREATE_NO_WINDOW 统一出口）

## 结论

看板每 30 秒闪一次黑窗的根因是 **spawn 方式**，不是调度：父进程（detached 的
`serve.py`）没有控制台，`refresh_loop` 每 30s 跑一次 `hive status --json`，而这个
console 子进程（以及它拉起的 git）会各自**新建一个可见控制台**。本卡把
`CREATE_NO_WINDOW` 收口成一个出口，并在 11 个调用点上接入；4 类**必须保留窗口**的
调用点（交互式 agent 拉起、DETACHED_PROCESS daemon 拉起、daemon 控制脚本、
非本卡允许路径的模块）显式进白名单，由 AST 判据钉住。

## 改了什么

| 文件 | 改动 |
| --- | --- |
| `hive_compat.py` | 新增 `no_window_kwargs()` + `NO_WINDOW_ENV="HIVE_NO_WINDOW"`：Windows 返回 `{"creationflags": CREATE_NO_WINDOW}`，其它平台 `{}`；`HIVE_NO_WINDOW=0/false/no/off` 一律 `{}`（逃生阀） |
| `hive.py` | 顶层 `from hive_compat import no_window_kwargs`（ImportError 兜底）；**`run()` 单点接入**（全仓唯一命令出口，`git()`/`git_net()` 都走它）；`_copy_clipboard()`、`Worker.run_task` 的 `clip`、`Worker._exec` 的 `taskkill` 也接入 |
| `dashboard/serve.py` | `refresh_loop`（30s 那一闪）、`serve_task_post`、`_refresh_status` 三处接入 |
| `hive_cluster.py` | `RepoSyncAgent._git_rev`、`RepoSyncAgent._remote_head`、`_pid_alive` 三处接入 |
| `tests/test_no_window.py`（新） | 25 条判据：AST 全量扫描 + 白名单精确集合 + 单点钉死 + 不许 DETACHED 混用 + 行为层 |

`hive_compat.no_window_kwargs()` 的实现要点：`os.name != "nt"` 直接返回 `{}`
（POSIX 口径一个字节不动）；flag 取 `getattr(subprocess, "CREATE_NO_WINDOW", 0)`，
取不到也返回 `{}`（残缺 subprocess 不炸）。

## 调用点清单（AST 实测，生产模块 25 处）

扫描面 = 仓库根 `*.py` + `dashboard/*.py`（不含 `tests/`：pytest 前台驱动，不是
常驻周期路径）。逐条：

| # | 位置 | 处置 | 理由 |
| --- | --- | --- | --- |
| 1 | `hive.py::_copy_clipboard → run` | 封装 | 剪贴板 `clip`，输出走管道 |
| 2 | `hive.py::run → run`（无 input） | 封装 | **全仓命令出口**，覆盖 `git()`/`git_net()`/`run_verify()` |
| 3 | `hive.py::run → run`（有 input） | 封装 | 同上（二进制 stdin 分支） |
| 4 | `hive.py::Worker.run_task → run`（clip） | 封装 | 手动模式复制提示词 |
| 5 | `hive.py::Worker.run_task → Popen`（manual `--launch`） | **白名单** | 交互式 GUI agent 拉起：人要在那个窗口里干活 |
| 6 | `hive.py::Worker._exec → Popen`（agent 命令） | **白名单** | 交互式 agent 拉起：实时输出要给人看，隐藏会吞掉 |
| 7 | `hive.py::Worker._exec → run`（taskkill 超时清场） | 封装 | 纯清场，无输出需求 |
| 8 | `hive.py::_dashboard_run_script → run` | **白名单** | daemon 控制脚本（start/stop/status），前景调用、用户在终端执行；卡明确划出 daemon 拉起边界 |
| 9 | `hive.py::_dashboard_spawn → Popen` | **白名单** | DETACHED_PROCESS，与 CREATE_NO_WINDOW **互斥** |
| 10 | `hive_cluster.py::RepoSyncAgent._git_rev → run` | 封装 | 周期同步里的 `git rev-parse` |
| 11 | `hive_cluster.py::RepoSyncAgent._remote_head → run` | 封装 | 周期同步里的 `git ls-remote` |
| 12 | `hive_cluster.py::_pid_alive → run`（tasklist） | 封装 | Windows 存活探测 |
| 13 | `hive_cluster.py::_spawn_background → Popen` | **白名单** | DETACHED_PROCESS，互斥 |
| 14 | `hive_pr.py::git_remote → run` | **白名单** | `hive pr` 交互命令，非 30s 刷新路径；**不在本卡允许修改路径** ⇒ 留后续卡 |
| 15 | `llm_controller.py::McpStdioBackend.__init__ → Popen` | **白名单** | MCP stdio 子进程（全管道、控制器启动时一次）；**不在本卡允许路径** |
| 16-22 | `selftest.py`（`sh` 1 处 + `main` 6 处） | **整文件白名单** | 端到端自测脚本，人工/CI 前台跑；**不在本卡允许路径** |
| 23 | `dashboard/serve.py::refresh_loop → run` | 封装 | **用户看到的那一闪** |
| 24 | `dashboard/serve.py::build_handler.HiveHandler.serve_task_post → run` | 封装 | 看板操作按钮 |
| 25 | `dashboard/serve.py::build_handler.HiveHandler._refresh_status → run` | 封装 | 操作后重生成状态 |

合计：**11 处走统一封装**、**7 处白名单（4 类理由）**、**7 处整文件白名单**。

与卡上「29 处」的口径差异：29 是 `grep -rn subprocess\.` 的**行数**（含
`hive.py:82`/`hive.py:4057` 两行注释文字、含 `tests/` 夹具 15 处、不含
`selftest.py` 的 7 处）。本卡以 AST 判据为准，口径可复跑：
`python -c "import sys;sys.path[:0]=['.','tests'];import test_no_window as t;print(len(list(t.unguarded_sites())))"`。

## 反证（判据有牙）

把 `hive.run()` 里两处 `**no_window_kwargs()` 删掉（在隔离副本里做，不动交付树）：

```
FAILED tests/test_no_window.py::test_every_subprocess_call_is_guarded
FAILED tests/test_no_window.py::test_run_is_the_single_guarded_exit
FAILED tests/test_no_window.py::test_run_passes_creationflags_on_windows
3 failed, 22 passed in 11.17s
```

恢复后：`25 passed`。白名单也是精确集合——多一条（僵尸豁免）就红
（`test_whitelist_has_no_zombie_entries`）。

## Windows 真机证据

计数脚本（一次性探针，未入库；核心就这 30 行）：

```python
import ctypes, ctypes.wintypes as wt, subprocess, sys, time
from pathlib import Path
u = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
def count():
    n = [0]
    def cb(hwnd, _l):
        if u.IsWindowVisible(hwnd):
            b = ctypes.create_unicode_buffer(256); u.GetClassNameW(hwnd, b, 256)
            if b.value in ("ConsoleWindowClass", "PseudoConsoleWindow"): n[0] += 1
        return True
    u.EnumWindows(WNDENUMPROC(cb), 0); return n[0]
# 父进程用 pythonw.exe（GUI 子系统，天生无控制台 = 看板 daemon 的处境），
# 每 10ms 采样一次；循环体与 refresh_loop 同一条命令 + 同一个 no_window_kwargs()
```

父进程必须是**无控制台**的 pythonw，否则测不出来（终端里跑 `hive status`，子进程
直接继承父控制台，永远不会新建窗口）。隔离实验（1 次 `git --version`）：

```
A_outer_no_window_inner_no_window: rc=0 base=4 peak=4 delta=0
B_outer_plain_inner_plain:         rc=0 base=4 peak=5 delta=1
```

A/B（`hive status --json`，pythonw 父进程）：

```
A_fixed      (HIVE_NO_WINDOW 默认) 峰值 4 / 基线 4 → 增量 0
B_valve_off  (HIVE_NO_WINDOW=0)    峰值 5 / 基线 4 → 增量 1
```

结论：`CREATE_NO_WINDOW` 确实把可见控制台挡掉；关掉逃生阀立刻复现窗口。

## verify 状态（重要，未按 90 分钟预算跑完）

- `tests/test_no_window.py`：**25 passed**（反复跑 3 次均绿）。
- 反证：删掉 `run()` 的守卫 → 3 failed / 22 passed（见上）。
- **全量三文件套件在本机没跑完**：
  - 串行跑 40 分 10 秒仍在跑（被 90 分钟预算打断）；
  - `-n 4` 并行跑 45 分 42 秒仍在跑，其中 `test_claim_is_atomic` /
    `test_concurrent_claims_share_one_clone` / `test_retry_fresh_clears_archive`
    的 3 个 git 子进程（`git init -b main` / `git status --porcelain` /
    `git hash-object -w --stdin`）**CPU 时间 0s 阻塞 40 分钟**，把 gw0/gw1/gw2
    三个 worker 全卡住。
  - **排除本卡回归**：单跑 `test_claim_is_atomic` → `1 passed in 38.33s`；
    且「无控制台父进程 + CREATE_NO_WINDOW」下 `hive init --remote` 三种组合
    （带/不带 flag、带 stdin=DEVNULL）全部 rc=0（12.8~15.8s），**不挂**。
  - 现场观察：同机另一个 agent 正在并发跑
    `pytest -q tests/test_result_head_guard.py tests/test_core.py`（PID 7060），
    两个 test_core 抢同一台机器的 git/磁盘；本机单个 world 夹具约 30~40s
    （54 个用例 ⇒ 串行 30 分钟以上）。
- 因此本卡按卡上失败策略处理：**代码与判据已交付且绿，全量套件受环境争用未跑完**，
  由 `hive submit` 自己重跑 verify 定论（已加 `--timeout=600` 兜底，防止再次无限挂）。


## 边界与已知限制

- **没动**调度架构、同步间隔（T-0211）、`git_net` 超时语义。
- **没给** `_dashboard_spawn` / `_spawn_background` 的 DETACHED_PROCESS 叠
  CREATE_NO_WINDOW（互斥，会起不来）；`test_detached_spawn_never_mixes` 用 AST 守这条。
- **没给**交互式 agent 拉起（`Worker._exec` / manual `--launch`）加隐藏窗口。
- 遗留：`hive_pr.py::git_remote`、`llm_controller.py::McpStdioBackend.__init__`、
  `selftest.py` 全部调用点仍在白名单里——它们**不在本卡允许修改路径**内，
  建议后续卡收口（都是非周期路径，闪窗概率低）。

## 第二次尝试（2026-10-31）新增证据

- **verify 在门禁机 0.88（fb98, FreeBSD, Python 3.12.13）全绿**：
  `python3 -m pytest -q tests/test_no_window.py tests/test_core.py tests/test_dashboard_freshness.py`
  → **98 passed, 1 skipped in 170.81s**（同一 commit `89a5222`，同一三条命令，只把 worktree
  去掉 `.git/.hive/_archive` 打包上传到 `/tmp/t0206` 跑）。
- **本机 Windows 的卡死与本卡无关**：用 `origin/main`（不含本卡任何代码）建 worktree 跑同一套件，
  同样在 24 个用例后卡死在 `test_gc_reclaims_expired_claim`；本卡树卡在
  `test_land_survives_dirty_coordination_tree` / `test_concurrent_claims_share_one_clone` /
  `test_retry_fresh_clears_archive` / `test_dispatch_respects_max_concurrent` 等**不同**位置。
  每个卡住的用例单跑都能过（38.33s / 44.36s / 108.05s）。
- **死锁形态**：卡住时 `hive.py` + `git receive-pack` 存活、CPU≈0、`git push` 客户端已消失
  ⇒ 孙进程持有继承来的 stdout 管道，父进程 `subprocess.run(capture_output=True)` 等不到 EOF。
  同一机制也适用于 `git maintenance run --auto --detach` 这类「孤儿持管道」。
- **hive 自己的 verify 上限是 1800s**（`run_verify(..., timeout=1800)`，无环境变量可调）：
  本机这套件串行需要 30~50 分钟 ⇒ 在 Windows 上必然被 1800s 掐掉；`-n 4` 能压到 ~15 分钟，
  但并发用例会随机触发上面的死锁。
- 已排除的变量：`--capture=sys`、basetemp 移出工作区、全局关 `maintenance.auto`/`gc.auto`、
  沙箱外运行、`-n 4`（前期快、后期仍卡）。
- 结论：**代码与判据就绪且已在门禁机全绿；本机 submit 过不了是环境 + 1800s 上限**。
  建议换一台健康的 Windows 机器 submit，或先修本机「git 孙进程持管道」问题；
  **不要为了过门去改 `run_verify` 的超时，也不要改代码**。
