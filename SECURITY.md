# 安全策略

轻量版：hive 是**内网协同工具**，默认假设网络边界可信。下面写清「哪些是设计边界、
不算漏洞」，避免把已知取舍当安全问题报。

## 报告渠道

不公开安全邮箱。发现疑似漏洞请**私下**联系维护者（见 `git log` 里的提交者 / 仓库
托管方的成员列表），不要在公开 issue 里贴可利用细节或真实内网拓扑。

## 已知设计边界（不是漏洞）

- **MCP HTTP 必须带 token**：`mcp_http.py` 对 `/mcp` 一律校验 token（`Authorization:
  Bearer ...` 或 `X-Hive-Mcp-Token`），没有有效 token 直接 `401`。token 只存本机
  `~/.hive/cluster/mcp_tokens.json`（目录 0700、文件 0600，存 SHA-256 而非明文，
  一个 agent 一个 token），不进协调仓、不随 Git 同步；`HIVE_MCP_TOKENS` 可改路径。
  服务默认只绑 `127.0.0.1:8790`。
- **worker claim 靠 `git push` 的原子性**：抢锁 = 把 claim 文件推上远端，先推成功者得卡。
  这里**没有额外的鉴权层**，能推仓库的人就能参与领取 —— 这是设计（git 即队列），
  不是提权漏洞。要隔离权限，请用仓库侧的 ACL / 分支保护。
- **看板默认监听 `0.0.0.0:8787`**：`HIVE_DASHBOARD_HOST` 默认 `0.0.0.0`，方便局域网
  直接看；这是**内网假设**，不建议把 8787 暴露到公网。收回本机写 `127.0.0.1`。
- **集群跨机通信建议共用 `HIVE_CLUSTER_TOKEN`**：不设时按内网可信处理；跨网段部署
  请显式设置。
- **`.env` 不入库**：`.gitignore` 已覆盖 `.env` 与 `.env.*`；不要把 token / 密钥 /
  内网 IP 写进源码、任务卡、summary 或日志。

## 凭据

- 所有敏感配置走环境变量（`HIVE_MCP_TOKENS`、`HIVE_MCP_TOKEN`、`HIVE_CLUSTER_TOKEN`、
  `HIVE_DASHBOARD_HOST` 等），不要把明文写进仓库。
- 本机 token 文件权限由 `mcp_auth.save()` 强制 0600（Windows 上 `chmod` 可能无效，
  依赖目录 ACL）。
