# Windows：verify 模式的「已验证」基准

> 本文不是 exotic 的 GUI 接入笔记，而是**官方基准**：一台真实的 Windows 机器上，
> `hive verify` 这条路端到端跑通、且有可重复的判据。它回答的是
> 「异构集群里最大的平台不确定性——Windows——到底能信到哪一步」。

一键复验：

```powershell
# 在 hive 仓根执行；不需要网络、不需要 LLM、不需要装 MCP 客户端
powershell -ExecutionPolicy Bypass -File .\windows\verify-smoke.ps1

# 保留现场（失败时自己进去看）
powershell -ExecutionPolicy Bypass -File .\windows\verify-smoke.ps1 -Keep
```

退出码 `0` = 全部判据通过；`1` = 有失败，末尾会逐条列出 `FAILED <判据> -> <证据>`。

---

## 1. 这个基准覆盖什么

`windows/verify-smoke.ps1` 在临时目录里**从零**造一个小世界（裸代码仓 + 协调仓 +
`mode=verify` 的 agent），然后只用真实 hive 命令跑：

| # | 判据 | 证明什么 |
|---|---|---|
| 1 | `python -V` / `git --version` / `hive -V` | 工具链在位 |
| 2 | `init --remote`、`agent add --mode verify` | 协调仓与验收机可登记 |
| 3 | `task new` + `assign` + `work --once` | verify 模式能领卡、只跑验收 |
| 4 | 结果 `status=done mode=verify verify=[0,0,0]` | 验收结论正确落盘 |
| 5 | 结果尾部含中文（CJK） | **UTF-8 全链路**：verify 输出 → 结果 JSON → `git show` |
| 5b | `verify: true` / `test -f README.md` 也绿 | 跨平台卡（POSIX 惯用法）经 `hive_compat.py` 垫片在 Windows 生效 |
| 6 | 代码仓里**没有** `task/T-9001` 分支 | verify 模式是只读的，不产出代码 |
| 7 | `status --json` 行 `result=done fence>=1` | 看板/统计口径一致（含 fencing 字段） |
| 8 | 红灯 verify → `status=failed` 且 `code=3` | 失败不被吞掉、退出码被记录 |
| 9 | `hive lease list` 有本机留底 | 租约 fencing 的第二道防线在 Windows 上可用 |
| 10 | 旧 worker 写结果 → `FENCED` | 过期租约写入被拒（见 [租约与fencing.md](租约与fencing.md)） |
| 11 | `close` 后 main 上有归档、claim/res 分支清掉 | 收口正常 |

**不覆盖**：GUI 客户端真实粘贴 MCP 配置（见 `docs/windows-smoke.md` §5.4）、
exec 模式调用 CLI agent（需要 API key）、跨机 host 路由。

---

## 2. 实测记录（最近一次）

| 项 | 值 |
|---|---|
| 时间 | 2026-10-30 08:47 (+08:00) |
| OS | Windows 10 Pro `10.0.19045.6466`（`DESKTOP-6LFQUO8`，VM） |
| Python | `3.14.7`（`C:\Python314\python.exe`） |
| Git | `2.55.0.windows.3` |
| hive | `0.15.0` |
| 结果 | **17/17 PASS**（含 3 条 verify、UTF-8、fencing、close） |

失败样例（修复前，留档用）：

```
[FAIL] verify result done -> status=failed mode=verify codes=0,0,1
[FAIL] stale write fenced -> JSONDecodeError...
```

第一个是 `cmd /c ver` 在 `shell=True` 下被引号吃掉（应为 `cmd.exe /c ver`），
第二个是 PowerShell 传 JSON 参数时内层引号丢失——两个都已在脚本里绕开并写进注释。

---

## 3. 在真机上执行时踩过的坑（脚本已固化）

| 坑 | 现象 | 规避 |
|---|---|---|
| PS 5.1 把原生命令的 stderr 当终止性错误 | `git push` 成功但脚本抛 `NativeCommandError` | `Invoke-Native` 把 stderr 重定向到文件，局部 `Continue`，只看 `$LASTEXITCODE` |
| `$Args` 是自动变量 | 尾参被吞、`hive.py` 后面拼成一整个字符串 | 参数名 `$NativeArgs`，`@argList` 展开 |
| `param($X = $PSScriptRoot …)` | 5.1 绑默认值时 `$PSScriptRoot` 为空 → `Split-Path` 报错 | 在 body 里用 `$MyInvocation.MyCommand.Path` 解析 |
| 双引号包参数传 JSON | 内层引号丢失 → `{task:...}` 解析失败 | 走临时文件而不是命令行参数 |
| `cmd /c ver` 作 verify 命令 | `'\ver"'` 不是命令，rc=1 | 用 `cmd.exe /c ver`（ps1 默认 CRLF，见 `.gitattributes`） |
| 控制台编码 cp936 | 中文输出在 `cmd.exe` 里花屏 | hive 自身 `run()` 钉死 UTF-8；基准里用 Python 脚本打印 CJK，并用 `git show` 断言 JSON 内容 |

---

## 4. 怎么把 Windows 接进日常流水线

```powershell
# 1) 登记成 verify 机（不需要装 agent CLI）
powershell -ExecutionPolicy Bypass -File .\windows\bootstrap.ps1 `
  -Apply -Register -InstallTask -Agent win10 -HiveHost win10 -Mode verify

# 2) 本机巡检：端口、集群、健康
python .\hive.py --dir . ports --check
python .\hive.py --dir . cluster status

# 3) 回归门禁（建议放在部署后/升级后）
powershell -ExecutionPolicy Bypass -File .\windows\verify-smoke.ps1
```

三卡闭环（与 `windows/README.md` §2 一致）：

1. **诊断卡**（`needs: [windows]`、`mode=verify`）：在 Windows 上跑复现命令，日志进结果；
2. **修复卡**（fb88 / exec）：读诊断、改代码、推 `task/T-xxxx`（可带 `--pr`）；
3. **验收卡**（Windows / verify，`base: task/T-xxxx`）：在 Windows 上复跑，绿了才算完。

---

## 5. 什么时候需要重跑基准

- 升级 Python/Git/hive 任一；
- 改了 `hive.py` 的 `run()` / `Worker` / 租约相关代码；
- 换了 VM 快照、改了 `core.autocrlf`、改了 `PYTHONIOENCODING`；
- 新机器接入前（先 `-Keep` 跑一遍看现场）。

---

## 6. 与其它文档的关系

- 接入清单与计划任务：`windows/README.md`
- 2026-10-26 的 MCP/CLI 实测：`docs/windows-smoke.md`
- 租约与 fencing：`docs/租约与fencing.md`
- 集群（Windows 默认以 observer 参战）：`docs/集群服务.md`
