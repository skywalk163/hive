# Windows 接入实测（T-0160）

> 结论先说：**Windows 10 节点按「MCP stdio + CLI 领卡」两条路都能接上 hive，8 项判据 8 项通过**，
> 但发现 1 个真坑（CLI `claim` 不写 owner，直接 `submit` 会报错）和 3 处已知限制，见 §5。
>
> 实测时间：2026-10-26 22:35–22:45 (+08:00)　机器：`win10`　agent：`workbuddyai-win`

## 1. 环境

| 项 | 值 |
|---|---|
| OS | Windows 10 Pro，`10.0.19045.6466` |
| Python | `3.13.14`（`sys.getdefaultencoding()` = `utf-8`，`locale.getpreferredencoding()` = `utf-8`） |
| Git | `2.55.0.windows.3` |
| hive | `0.9.13`（MCP `serverInfo.version`） |
| 协调仓 | `G:/dswork/duan-light-merge/hive`，origin = `http://192.168.1.5:3000/skywalk/hive` |
| worktree | `C:\Users\skywalk\.hive\wt\workbuddyai-win\T-0160`（分支 `task/T-0160`） |
| agent 登记 | `host=win10`、`mode=manual`、`abilities=[code,docs,verify]`、`capabilities=[windows,gui,linux]` |

## 2. 判据与实测结果

| # | 判据 | 结果 | 证据 |
|---|---|---|---|
| S1 | 前提工具链可用 | PASS | `git --version` → `2.55.0.windows.3`；`python -V` → `3.13.14` |
| S2 | 协调仓可达且同步 | PASS | `git fetch --all` 无报错；`git status -sb` → `## main...origin/main`（本地 = `a3681a1`，无待拉取） |
| S3 | CLI 领卡拿到租约 | PASS | `hive claim T-0160 --agent workbuddyai-win` → `[hive 22:35:11] T-0160 领取成功` |
| S4 | 租约互斥（别人抢不到） | PASS | `hive claim T-0160 --agent raccoon-win` → `[hive 22:39:11] T-0160 已被 workbuddyai-win 领取，不重试` |
| S5 | MCP stdio 握手 | PASS | `initialize` → `protocolVersion=2024-11-05`、`serverInfo={name:hive, version:0.9.13}` |
| S6 | MCP 工具面完整 | PASS | `tools/list` 返回 14 个工具（见 §3） |
| S7 | `hive_whoami` 带 host 且与 CLI 一致 | PASS | 返回 `"agent":"workbuddyai-win"`、`"host":"win10"`、`mine[0].lease_until=2026-10-27T01:35:01+08:00`，与 `hive status` 的 `workbuddyai-win@10-26T22:35` 同一租约 |
| S8 | worktree 就绪、`.hive/` 不入库 | PASS | `git worktree add -B task/T-0160` 成功；`git status` 里看不到 `.hive/`（`.git/info/exclude` 已含该行） |

## 3. MCP 实测原始输出（截取）

启动行（stderr，`dir=` 才是真正生效的路径）：

```
hive mcp: agent=workbuddyai-win role=worker dir=C:\Users\skywalk\.hive\clones\mcp-workbuddyai-win
```

`tools/list`（14 个，worker 视角无 controller 专属工具）：

```
hive_abort  hive_block  hive_claim  hive_doctor  hive_extend_lease  hive_get_task
hive_list_tasks  hive_next  hive_progress  hive_result  hive_stats  hive_submit
hive_unblock  hive_whoami
```

`hive_whoami` 关键字段：

```json
{
  "agent": "workbuddyai-win", "role": "worker", "mode": "manual",
  "capabilities": ["windows", "gui", "linux"],
  "abilities": ["code", "docs", "verify"],
  "repos": {"hive": "G:/dswork/duan-light-merge/hive"},
  "worktrees": "C:\\Users\\skywalk\\.hive\\wt\\workbuddyai-win",
  "host": "win10",
  "hive_dir": "C:\\Users\\skywalk\\.hive\\clones\\mcp-workbuddyai-win",
  "mine": [{"id": "T-0160", "state": "running", "claim_agent": "workbuddyai-win",
            "lease_until": "2026-10-27T01:35:01+08:00", "stale": false}],
  "claimable": []
}
```

`claimable: []` 在本次是**真没卡**（全总线只剩 T-0160，已被自己领走），不是故障——符合
`docs/AGENT快速上手.md` §4.3 的三种情况里的第 1 种。

## 4. 复现脚本

不依赖任何 MCP SDK，纯 JSON-RPC over stdio；脚本放在 worktree 的 `.hive/` 下（不入库）：

```python
# .hive/mcp_smoke.py  <hive.py> <clone 目录> <agent 名>
import json, subprocess, sys

def send(p, m):
    p.stdin.write(json.dumps(m, ensure_ascii=False) + "\n"); p.stdin.flush()

def recv(p):
    while True:
        line = p.stdout.readline()
        if not line:
            return None
        if line.strip():
            return json.loads(line)

hive_py, hive_dir, agent = sys.argv[1:4]
p = subprocess.Popen([sys.executable, hive_py, "mcp", "--agent", agent, "--dir", hive_dir],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, encoding="utf-8", bufsize=1)
send(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "win-smoke", "version": "0"}}})
print(json.dumps(recv(p), ensure_ascii=False)[:300])
send(p, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
send(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
print(len(recv(p)["result"]["tools"]), "tools")
send(p, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "hive_whoami", "arguments": {}}})
print(recv(p)["result"]["content"][0]["text"][:400])
p.stdin.close(); p.terminate()
```

跑法（**必须**指向独占 clone，不能指主工作区——MCP 每次调用都会 `fetch + reset --hard`）：

```powershell
python .hive\mcp_smoke.py G:\dswork\duan-light-merge\hive\hive.py `
  C:\Users\skywalk\.hive\clones\mcp-workbuddyai-win workbuddyai-win
```

## 5. 坑与限制（本次实测新增）

### 5.1 【真坑】CLI `claim` 只抢租约，不写 owner，`submit` 会直接失败

`hive claim` 的实现是 `Worker.claim()`，只 push `claim/T-<id>` 分支，**不动卡面**。
于是领完卡 `hive status` 显示 `state=running`，但卡文件里仍是 `state: ready / owner: null`。
接着 `hive submit` 会在 `cmd_submit` 的归属校验处止步：

```
错误: T-0160 没有 owner，先 take 或 assign
```

修法（本次走的路）：`hive assign T-0160 --agent workbuddyai-win` → `state: assigned / owner: workbuddyai-win`。
MCP 的 `hive_claim` 因为 T-0147 改成「先抢锁后 assign」，没有这个断层；**只有 CLI 的
`claim` + `submit` 组合会踩**。建议后续单开一张卡：要么 `cmd_claim` 补 `_assign_local`，
要么在 `AGENT快速上手.md` §3.2/§3.5 写明「CLI claim 之后必须 assign」。

### 5.2 `cmd.exe` 代码页与 UTF-8 不一致

`cmd /c ver` 输出 `Microsoft Windows [°æ±¾ 10.0.19045.6466]`（GBK 代码页 936 下的乱码），
而 Python/Git 侧是 UTF-8。hive 自身 `run()` 已钉死 `encoding="utf-8"`（T-0143），所以
**任务卡、结果 JSON、看板都不受影响**；只是人工在 cmd.exe 里读中文输出会花。用 PowerShell
或 Git Bash 即可。

### 5.3 行尾策略靠个人 `core.autocrlf`，仓库无 `.gitattributes`

本机 `git config core.autocrlf` = `true`，仓库根目录没有 `.gitattributes`。后果：Windows 检出
CRLF、FreeBSD 检出 LF，索引里统一 LF。本次产物只是文档，无断言受影响；但**任何以文件内容
为判据的测试都不能硬编码行尾**（读取后先归一化再比）。

### 5.4 未覆盖的面（本卡不宣称已验）

- **GUI 客户端真实粘贴 MCP 配置**：本卡只证到 stdio 协议层（握手/工具面/whoami 全部真跑），
  没有在 Cursor/VS Code/小浣熊里点一次工具调用。
- **worker 全自动模式**：本机 agent 登记为 `mode=manual`，`hive work` 循环未跑。
- **跨机 host 路由**：`dispatch --host win10` 未实测（要另一台机器上派一张卡才成立）。

## 6. 结论

Windows 10 作为 **best-effort 验收机**可以接入：CLI 领卡/互斥/分派链路通，MCP 协议层通，
`host=win10` 已在 `hive_whoami` 里正确回传，多机路由所需的字段齐备。日常用法按
`windows/README.md` 的 D1（MCP）走即可；走 CLI 时记得 `claim` 之后补 `assign`。
