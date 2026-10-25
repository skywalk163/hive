# Windows 10 节点接入（Hive）

这台 Windows 的定位是 **best-effort 验收机**：能访问 Gitea 就拉任务、跑 `verify`、写结果；
它经常死机，所以不放在关键路径上，租约短、任务小、死了靠 `gc/retry` 回收。

> 所有脚本都是 ASCII，兼容 Windows PowerShell 5.1；路径里的中文不会影响执行。

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
**不需要命令行入口**，走 MCP 就能自助闭环。跑完 bootstrap 后它会打印这样一段：

```json
{
  "mcpServers": {
    "hive-win10": {
      "command": "python",
      "args": ["C:\\hive\\hive.py", "--dir", "C:\\hive", "mcp", "--agent", "win10"]
    }
  }
}
```

把它粘进 agent 的 MCP 配置（各家的 key 名略有差异：Claude/Cursor 用 `mcpServers`，
VS Code 用 `servers`，Codex 用 `mcp_servers`），重启客户端。之后那个 agent 依次调用：

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
