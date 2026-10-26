# Windows 10 节点接入（Hive）

这台 Windows 的定位是 **best-effort 验收机**：能访问 Gitea 就拉任务、跑 `verify`、写结果；
它经常死机，所以不放在关键路径上，租约短、任务小、死了靠 `gc/retry` 回收。

> 所有脚本都是 ASCII，兼容 Windows PowerShell 5.1；路径里的中文不会影响执行。

## 照做清单（Checklist）

> 从零到接上，按顺序做。每步都有验收判据，卡住就看最后一节排障。
> 全部命令在 **PowerShell** 里执行（不要用 cmd）。

### A. 前提

- [ ] Windows 能打开 `http://192.168.1.5:3000`（浏览器试一下）
- [ ] 装 Git：`winget install --id Git.Git -e`
- [ ] 装 Python 3.12：`winget install --id Python.Python.3.12 -e`
- [ ] **开新终端**再验证（PATH 要重开才生效）：
  ```powershell
  git --version
  python --version
  ```
  ✅ 两个都有版本号输出

### B. 拿工具仓

- [ ] ```powershell
  git clone http://192.168.1.5:3000/skywalk/hive.git C:\hive
  cd C:\hive
  ```
- [ ] 确认拿到的是新代码（含多机 host 支持）：
  ```powershell
  git log --oneline -3
  ```
  ✅ 能看到 `feat(host)` 或更新的提交

### C. 先干跑（不动任何东西）

- [ ] ```powershell
  powershell -ExecutionPolicy Bypass -File C:\hive\windows\bootstrap.ps1 `
    -Agent win10 -HiveHost win10
  ```
  ✅ 输出里会打印一段 MCP JSON，路径是 `C:\hive\hive.py`
  ✅ 没有真的登记、没有装计划任务（没加 `-Apply`）

### D. 登记 + 接入（二选一）

#### D1. GUI agent → 走 MCP（推荐，不需要 worker）

> 一台机器开 N 个 agent，就登记 N 个名字，**每个 chat 窗口一个**。
> 本文示例用 `raccoon-win` / `doubao-win` / `dumate-win` / `workbuddyai-win`。

- [ ] 逐个登记（每个名字跑一次）：
  ```powershell
  foreach ($a in "raccoon-win","doubao-win","dumate-win","workbuddyai-win") {
      powershell -ExecutionPolicy Bypass -File C:\hive\windows\bootstrap.ps1 `
          -Apply -Register -Agent $a -HiveHost win10 -Mode manual
  }
  ```
- [ ] **每个 agent 用自己那份配置**（`--agent` 必须不同，四份现成的见 §2.5）
- [ ] **重启各客户端**
- [ ] 每个 agent 各调一次 `hive_whoami`
      ✅ `agent` 是它自己、`host` 是 `win10`、`hive_dir` 是 `...\clones\mcp-<它自己>`
      ❌ 四个都返回 `raccoon-win` → 贴错配置了，回 §2.5 换

#### D2. 有命令行 agent → 走 worker（全自动）

- [ ] ```powershell
  powershell -ExecutionPolicy Bypass -File C:\hive\windows\bootstrap.ps1 `
    -Apply -Register -InstallTask -Agent win10 -HiveHost win10 -Mode verify
  ```
- [ ] ```powershell
  Get-ScheduledTask -TaskName HiveWorker-win10 | Format-List State
  ```
  ✅ `State` 是 `Ready` 或 `Running`
- [ ] ```powershell
  Get-Content C:\hive\logs\worker-win10.log -Tail 20
  ```
  ✅ 有 `worker win10 启动`

> **D1 和 D2 不要同时开**：两条路都会去领卡，会重复劳动。

### E. 在 FreeBSD 侧确认

- [ ] ```sh
  python3 hive.py --dir ~/dswork/hive agent list
  ```
  ✅ 看到 `win10  host=win10`
- [ ] ```sh
  python3 hive.py --dir ~/dswork/hive dispatch --host win10
  ```
  ✅ 能列出卡（没有卡会提示"没有可分派的任务"，也算命令通）

### F. 第一张真卡（端到端）

- [ ] 在 FreeBSD 写一张**只改文档**的小卡
- [ ] ```sh
  python3 hive.py --dir ~/dswork/hive dispatch --host win10 --apply
  ```
- [ ] Windows 侧领取并完成（D1 让 agent 调 `hive_claim`，D2 等 worker 自动领）
- [ ] ```sh
  python3 hive.py --dir ~/dswork/hive result T-xxxx
  ```
  ✅ `status: done`
- [ ] ```sh
  python3 hive.py --dir ~/dswork/hive land T-xxxx --apply
  ```

### G. 排障速查

| 现象 | 处理 |
|---|---|
| `禁止运行脚本` | 命令前加 `powershell -ExecutionPolicy Bypass -File` |
| `python 不是内部或外部命令` | 重装 Python 勾 Add to PATH；或 `-Python C:\Python312\python.exe` |
| Git 弹窗要密码 | 用 Gitea 账号 + **专门给 hive 仓的 access token** |
| `agent list` 里没有 win10 | `-Register` 没加，或 push 失败（看 bootstrap 输出） |
| host 显示的不是 win10 | 登记时漏了 `-HiveHost win10`，重跑一次 D 步 |
| worker 日志 `could not read` | `git -C C:\hive pull`，再重启计划任务 |
| 卡一直 running 但 VM 死了 | FreeBSD 上 `hive gc` 后 `hive retry T-xxxx` |
| MCP 工具没出现 | 配置粘错位置（各客户端 key 不同）；重启客户端；看客户端日志 |

## 0. 前提


- Windows 10（VM 或物理机都行），能打开 `http://192.168.1.5:3000`
- 已装 **Git for Windows** 和 **Python 3.10+**（安装时勾选 Add to PATH）

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
```

- Gitea 上已经有 `skywalk/hive` 仓库（自带 `hive.py`、`windows/`、`tests/`）。
  注意：hive 已从 light 等仓隔离（见 tasks/T-0133），这台机器只接触 hive 仓。

## 1. 一次性接入

```powershell
# 先 clone 工具仓（里面就有 bootstrap.ps1）
git clone http://192.168.1.5:3000/skywalk/hive.git C:\hive

# 先看它打算做什么（dry-run）
powershell -ExecutionPolicy Bypass -File C:\hive\windows\bootstrap.ps1

# 真正执行：克隆代码仓 + 注册 verify 验收机 + 装登录自启计划任务
powershell -ExecutionPolicy Bypass -File C:\hive\windows\bootstrap.ps1 `
  -Apply -Register -InstallTask -Mode verify -Agent win10
```

首次 push 会弹 Gitea 登录：用**单独账号 + access token**，只授权 `hive` 仓，
不要用管理员 token。

`bootstrap.ps1` 会做这些事：

1. 检查 git / python；
2. `core.autocrlf=false`、`core.longpaths=true`；
3. clone（或 pull）`C:\hive`（hive 仓；额外代码仓要用 `-Repos` 显式指定）；
4. `hive.py agent add --mode verify`，把本机登记成 `win10`
   （`-Abilities` 会传给 `--abilities`，否则 dispatch 按能力匹配时选不中这台机器）；
5. **打印 MCP 客户端配置**，并写成 `C:\hive\mcp-config-win10.json`（见 §2.5）；
6. 注册计划任务 `HiveWorker-win10`（登录时启动 `run-worker.ps1`）—— 只有走 worker
   路线才需要，**走 MCP 的话不用装**；
7. 尝试把 `C:\src`、`C:\hive-wt` 加进 Defender 排除项（需要管理员）。

## 2. 三种模式

| 模式 | 启动参数 | 用途 |
|---|---|---|
| `verify` | `-Mode verify` | 只跑验收，不调用 agent；**Windows 默认** |
| `manual` | `-Mode manual -Command ...` 或 `-Launch` | Trae / CodeArts / dumate 改完，人补 summary |
| `exec` | `-Mode exec -Command "claude -p"` | 有 CLI agent 时全自动 |

`verify` 模式不需要任何 agent CLI。Windows 专属 bug 的三卡闭环：

1. 诊断卡（win10 / verify）：跑复现命令，日志进结果；
2. 修复卡（fb88 / exec）：读诊断，改代码，推 `task/T-xxxx`；
3. 验收卡（win10 / verify，`base: task/T-xxxx`）：在 Windows 上复跑，绿了才算完。

## 2.5 用 MCP 接入（推荐，不需要 worker）

Windows 上的 agent（Claude Desktop / Cursor / VS Code / Codex CLI / 任何支持 MCP 的）
**不需要命令行入口**，走 MCP 就能自助闭环。跑完 bootstrap 后它会打印一段配置：

```json
{
  "mcpServers": {
    "hive-win10": {
      "command": "python",
      "args": ["C:\\hive\\mcp_server.py", "--agent", "win10"],
      "env": {
        "PYTHONPATH": "C:\\hive",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

> **注意：不要加 `--dir`。** 不传时 server 会自动用
> `~/.hive/clones/mcp-<agent>` 建**独占 clone**。传 `--dir <主工作区>` 会让多个
> agent 共用同一个 checkout，抢 git 索引、互相 `reset --hard`，隔离就没了。
> （`bootstrap.ps1` 早期版本生成过带 `--dir` 的配置，已修。）

### 多 agent：每个 chat 窗口一个名字

**最容易踩的坑**：把同一份配置原样贴给所有 agent，它们就都成了同一个身份。

`mcp_server.py` 只校验「这个 agent 名登记过」，**不会发现重名**，所以不报错、
静默出错。后果是几个 agent 抢同一张卡、共用同一个 worktree。

已登记的 Windows agent 用 `python hive.py agent list` 查（本文以
`raccoon-win` / `doubao-win` / `dumate-win` / `workbuddyai-win` 为例；你机器上
可能还有 `trae-win` / `codearts-win` 等）。每份配置只改 **key 名** 和
**`--agent` 值**：

**小浣熊**
```json
{"mcpServers":{"hive-raccoon-win":{"command":"C:\\Users\\skywalk\\.box-agent\\box-agent-runtime\\runtime\\python\\python.exe","args":["G:\\dswork\\duan-light-merge\\hive\\mcp_server.py","--agent","raccoon-win"],"env":{"PYTHONPATH":"G:\\dswork\\duan-light-merge\\hive","PYTHONUNBUFFERED":"1","PYTHONIOENCODING":"utf-8"}}}}
```

**豆包**
```json
{"mcpServers":{"hive-doubao-win":{"command":"C:\\Users\\skywalk\\.box-agent\\box-agent-runtime\\runtime\\python\\python.exe","args":["G:\\dswork\\duan-light-merge\\hive\\mcp_server.py","--agent","doubao-win"],"env":{"PYTHONPATH":"G:\\dswork\\duan-light-merge\\hive","PYTHONUNBUFFERED":"1","PYTHONIOENCODING":"utf-8"}}}}
```

**dumate**
```json
{"mcpServers":{"hive-dumate-win":{"command":"C:\\Users\\skywalk\\.box-agent\\box-agent-runtime\\runtime\\python\\python.exe","args":["G:\\dswork\\duan-light-merge\\hive\\mcp_server.py","--agent","dumate-win"],"env":{"PYTHONPATH":"G:\\dswork\\duan-light-merge\\hive","PYTHONUNBUFFERED":"1","PYTHONIOENCODING":"utf-8"}}}}
```

**WorkBuddy AI**
```json
{"mcpServers":{"hive-workbuddyai-win":{"command":"C:\\Users\\skywalk\\.box-agent\\box-agent-runtime\\runtime\\python\\python.exe","args":["G:\\dswork\\duan-light-merge\\hive\\mcp_server.py","--agent","workbuddyai-win"],"env":{"PYTHONPATH":"G:\\dswork\\duan-light-merge\\hive","PYTHONUNBUFFERED":"1","PYTHONIOENCODING":"utf-8"}}}}
```

> 路径按你自己机器改：`command` 是任意 3.10+ 的 python，
> `mcp_server.py` / `PYTHONPATH` 指向你的 hive 仓。上面用的是 box-agent 自带
> python + `G:\dswork\duan-light-merge\hive`。

### 自检：`hive_whoami` 看四个字段

每个 agent 连上后，先让它调一次 `hive_whoami`，返回里必须对得上：

| 字段 | 应该是什么 | 不对意味着 |
|---|---|---|
| `agent` | 它自己的名字（如 `doubao-win`） | 贴了别人的配置 → 4 个 agent 变同一个人 |
| `host` | 本机标签（如 `win10`） | agent 配置缺 `host`，或仓库没 pull 到最新 |
| `hive_dir` | `...\.hive\clones\mcp-<agent>` | 误传了 `--dir` 或设了 `HIVE_DIR` |
| `repos.hive` | 你的 hive 仓路径 | `repos` 登记错了 |

```jsonc
{
  "agent": "doubao-win",
  "host": "win10",
  "hive_dir": "C:\\Users\\skywalk\\.hive\\clones\\mcp-doubao-win",
  "mode": "manual",
  "repos": { "hive": "G:\\dswork\\duan-light-merge\\hive" }
}
```

> `host` 字段是后来加的（commit `8043474`），老仓库先 `git pull` 再重启客户端。

之后那个 agent 依次调用：

| 调用 | 作用 |
|---|---|
| `hive_whoami` | 确认身份：这台机器登记成了谁、有哪些能力、能领什么卡 |
| `hive_next` | 让它推荐一张现在能干的卡 |
| `hive_claim` | 领取：推租约、备好 worktree，返回工作目录 |
| （在返回的目录里改代码） | 活是 agent 自己干 |
| `hive_submit` | 提交：commit + push + 跑 verify + 写结果 |
| `hive_result` | 看验收过没 |
| `hive_abort` | 干不下去就把卡还回去，别耗到租约过期 |

**和 worker 二选一，不要同时用** —— 两条路都会去领卡，会重复劳动。
Windows 这种没有 agent CLI 的机器，优先走 MCP，计划任务（§3）可以不装。

### 权限

MCP server 以「一个 agent 的身份」运行，所有写操作只作用在自己身上：
不能替别人领卡、不能提交别人的卡、不能终止别人的租约。越权调用会直接返回错误。

想让它当**总控**（合入、关闭、重跑、分派），启动时加 `--role controller`，
详见 [`docs/MCP接入.md`](../docs/MCP接入.md) §4.5。

## 3. 自动登录 + 常开

- `netplwiz` 取消“必须输入密码”，或用 Sysinternals **Autologon**；
- 电源计划设为“从不睡眠”，合盖/关机策略按 VM 设置；
- 开机后计划任务会拉起 worker，日志在 `C:\hive\logs\worker-win10.log`。

## 4. 快照与回滚

```powershell
# 在 fb88（宿主）上
VBoxManage snapshot win10 take clean-2026-10-23
VBoxManage snapshot win10 restore clean-2026-10-23
```

回滚会丢 VM 里未推送的改动，只对 `verify` 模式安全；`manual` 模式下先确认 summary 已写、
worker 已 push，再回滚。

## 5. 验证接入成功

```powershell
Get-ScheduledTask -TaskName HiveWorker-win10 | Format-List State
Get-Content C:\hive\logs\worker-win10.log -Tail 30

# 在任意一台有仓库的机器上
python3 hive.py --dir ~/hive status
```

预期：`win10` 出现在 `agents/win10.json`；`inbox/win10/` 有任务时日志出现
`领取成功`、`验证 0`、`完成: done`。

走 MCP 的话（§2.5），在 agent 的对话里让它调 `hive_whoami`，能看到
`"agent": "win10"` 和自己的 `claimable` 列表就算接上了。

> **未在真机验证过**：`bootstrap.ps1` 的 PowerShell 语法只做过静态检查
> （括号配对、关键串存在），没有 pwsh 环境可以实跑。第一次请在 VM 里先不带
> `-Apply` 看 dry-run 输出，确认 MCP 配置那段对了再加 `-Apply`。

## 6. 排障

| 现象 | 处理 |
|---|---|
| `无法加载文件 ... 因为在此系统上禁止运行脚本` | 用 `powershell -ExecutionPolicy Bypass -File ...`，或 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `python 不是内部或外部命令` | 重装 Python 勾选 Add to PATH；或给 `-Python C:\Python312\python.exe` |
| Git 弹窗要凭据 | 用 Gitea 账号 + token；Git Credential Manager 会记住 |
| worker 日志里 `MISSING...` / `fatal: could not read` | 先 `git -C C:\hive pull`，再重启计划任务 |
| 任务一直 `running` 但 VM 死了 | 在总控机器上 `hive gc --minutes 0`（或等租约）再 `hive retry T-xxxx` |
| `unable to read sha1 file` | 关掉 Gitea 仓的 `receive.autogc`，改成夜间 `git gc`；见 docs/操作手册 |
| 时间不对导致租约乱 | VM 开时间同步；快照回滚后先同步时间再启动 worker |

## 7. 卸载

```powershell
Unregister-ScheduledTask -TaskName HiveWorker-win10 -Confirm:$false
```
