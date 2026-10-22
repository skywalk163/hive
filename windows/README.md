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

- Gitea 上已经有一个 `skywalk/hive` 仓库；如果还没有，把开发机上的 `~/dswork/hive`
  推上去即可（它自带 `hive.py`、`windows/`、`tests/`）。

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

首次 push 会弹 Gitea 登录：用**单独账号 + access token**，只授权 `hive` 和 `light`，
不要用管理员 token。

`bootstrap.ps1` 会做这些事：

1. 检查 git / python；
2. `core.autocrlf=false`、`core.longpaths=true`；
3. clone（或 pull）`C:\hive` 和 `C:\src\light`；
4. `hive.py agent add --mode verify`，把本机登记成 `win10`；
5. 注册计划任务 `HiveWorker-win10`（登录时启动 `run-worker.ps1`）；
6. 尝试把 `C:\src`、`C:\hive-wt` 加进 Defender 排除项（需要管理员）。

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
