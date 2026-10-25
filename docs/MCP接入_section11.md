## 11. Windows 客户端接入（小浣熊，实测）

本节记录在 Windows 本机把 `hive mcp` 接进 GUI 客户端（商汤小浣熊）的完整过程和真实坑，
2026-10 实测，客户端版 Box-Agent 1.0.0、Python 3.12.6、hive 0.9.12。

本机用独立身份 **`raccoon-win`**，与 Linux chroot 里的 `raccoon` 是两个 agent，不要混用：
一个 server 进程只认一个身份，同一台机器上两个客户端各起各的 server，名字别重。

### 11.1 配置写法

```json
{
  "mcpServers": {
    "hive-raccoon-win": {
      "command": "C:\\Users\\<你>\\.box-agent\\box-agent-runtime\\runtime\\python\\python.exe",
      "args": [
        "G:\\dswork\\duan-light-merge\\hive\\mcp_server.py",
        "--agent",
        "raccoon-win"
      ],
      "env": {
        "PYTHONPATH": "G:\\dswork\\duan-light-merge\\hive",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8"
      },
      "alwaysLoad": true,
      "connect_timeout": 60,
      "execute_timeout": 600
    }
  }
}
```

三个要点：

1. **`command` 必须写绝对路径的解释器**，不能写裸 `python`。客户端拉起 stdio 子进程时
   不一定带着你 shell 里的 `PATH`，裸 `python` 会解析失败，而且**失败是静默的**——
   日志里只有一个 `mcp/status count=N` 的数字对不上，不会提示哪个 server 起不来。
2. **直接跑 `mcp_server.py`，不要走 `hive.py mcp`**。`mcp_server.py` 只依赖标准库和同目录的
   `hive.py`，配好 `PYTHONPATH` 就能起；多一层 `hive.py` 转发只会多一个出错点。
   （`hive.py mcp` 在 Linux 上是能用的，见 §2，两者工具面完全一样。）
3. **路径用正斜杠或成对转义的反斜杠**。JSON 里写 `"G:\dswork\..."` 会被 `\d` 当转义吃掉，
   正确写法是 `"G:\\dswork\\..."` 或 `"G:/dswork/..."`。这个错不会报错，只会让路径变形。

### 11.2 本机实测值

| 项 | 实测值 |
|---|---|
| `serverInfo` | `{"name": "hive", "version": "0.9.12"}` |
| `protocolVersion` | `2024-11-05` |
| 工具数（worker） | 14 |
| 启动行（stderr） | `hive mcp: agent=raccoon-win role=worker dir=C:\Users\skywalk\.hive\clones\mcp-raccoon-win` |
| `hive_whoami` 的 `hive_dir` | `C:\Users\skywalk\.hive\clones\mcp-raccoon-win` |
| 注册后客户端日志 | `mcp/ready count=15`（系统 1 个 + hive 14 个） |

**自检：`hive_whoami` 返回的 `hive_dir` 必须是 `~/.hive/clones/mcp-<agent>`。**
如果它指向你正在改代码的主工作区，立刻停手——MCP 每次工具调用前都会
`fetch` + `reset --hard origin/main`（见 §8），会把未提交的改动冲掉。

### 11.3 工具何时出现

配置写进 `mcp.json` 后**不保证当场生效**：实测本会话内热加载没把 14 个工具注册进来，
重启客户端后才有（日志出现 `mcp/ready count=<原数+14>`）。

判断成功的标准，按从便宜到贵的顺序：

1. `tool_search` 能搜到 `hive_whoami`；
2. 直接调 `hive_whoami`，看 `agent`、`mode`、`repos`、`claimable` 对不对；
3. 看客户端日志的 `mcp/ready count` 有没有加上 14。

只看到「配置项是 enabled」**不能**当成已连上——那是文件状态，不是连接状态。

### 11.4 本机第一个闭环

接上之后走一遍最小闭环，证明写入链路（不只是读）是通的：

```
hive_whoami  → 我名下 / 能领什么
hive_claim   → 抢 claim 租约 + 备 worktree（返回 C:\Users\<你>\.hive\wt\<agent>\<任务号>）
  …在 worktree 里改，写 .hive/summary.md…
hive_submit  → commit + push task/<id> + 跑 verify + 写结果
```

`manual` 身份下**不要自己 `git commit`**，提交交给 `hive_submit`，否则改动清单和 summary 对不上。

Windows 上手工验证 server 能不能起（不经客户端，最快定位问题）：

```powershell
'"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"p","version":"0"}}}' |
  & "C:\Users\<你>\.box-agent\box-agent-runtime\runtime\python\python.exe" `
    "G:\dswork\duan-light-merge\hive\mcp_server.py" --agent raccoon-win
```

stdout 应有一行 `initialize` 响应，stderr 有一行带 `dir=` 的启动日志。手动能起、客户端起不来，
问题就在客户端的 `command` / `env`，不在 server。
