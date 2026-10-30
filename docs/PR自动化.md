# PR 自动化（Gitea / GitLab）

> **可选件的最后一公里**：Hive 已经能管 `task/*` 分支和 verify，这里再把它接到
> 「自动开 PR」——人工的下一步从「合并」变成「review 一个 PR」。
> 默认关闭，失败绝不影响任务本身。

---

## 1. 什么时候用

| 场景 | 建议 |
|---|---|
| 代码仓挂在 Gitea/GitLab，有分支保护 / 必须 review | `hive work --pr` 或 `hive submit --pr` |
| 内网小团队、直接 `land` 合入 main | 不用开（保持现状） |
| 代码仓在 GitHub | 用 `gh pr create`；hive 内置不支持 GitHub |
| 想在 worker 之外手工补 PR | `hive pr <tid>` |

打开之后，worker 在**推送 `task/<tid>` 成功且 verify 全绿**时自动建 PR，并把 PR 信息
写进结果的 `pr` 字段（看板 `hive result` 可见）。

---

## 2. 一次性配置：token 放哪

PR 需要目标仓库的 **read + 创建 PR** 权限。token 只放在本机，**绝不进协调仓**。

优先级（从高到低）：

1. 环境变量 `HIVE_PR_TOKEN`（统一）、`HIVE_GITEA_TOKEN`、`HIVE_GITLAB_TOKEN`
2. `HIVE_PR_ENV` 指向的凭据文件
3. `HIVE_ENV` 指向的凭据文件（兼容 `docs/Gitea接入.md` 的老写法）
4. `~/.config/hive/gitea.env`、`~/.config/hive/pr.env`
5. 向上数 4 级目录里的 `.env`

凭据文件格式（两种都认）：

```ini
# 方式 A：一整行 URL（老写法，与 scripts/gitea-credential.sh 共用）
gitea = http://skywalk:abcdef123456@192.168.1.5:3000

# 方式 B：只放 token
token = abcdef123456
```

> token 在日志和错误信息里一律被替换成 `***`（`hive_pr._redact`）；
> 一个 agent 一个 token、只授目标仓，是最低风险的做法，见 `docs/Gitea接入.md` §6。

---

## 3. 怎么用

```sh
# ① worker 全自动：推完分支、verify 全绿就开 PR
python3 hive/hive.py --dir hive work --agent win10 --pr

# ② 人工提交：同样在 verify 全绿后开 PR
python3 hive/hive.py --dir hive submit T-0100 --pr

# ③ 事后补一张（worker 已经跑完、或者当初忘了带 --pr）
python3 hive/hive.py --dir hive pr T-0100

# ④ 先看看会发什么请求，不发
python3 hive/hive.py --dir hive pr T-0100 --dry-run
python3 hive/hive.py --dir hive pr T-0100 --provider gitlab --base main
```

`hive pr` 的正文自动带上：任务卡标题、repo/base、结果状态、verify 每条命令的退出码、
summary 摘要，末尾一行「由 hive 自动创建」。

---

## 4. 行为口径

| 项 | 行为 |
|---|---|
| 幂等 | 先查同 `head → base` 的 open PR/MR，存在就复用（`created: false`） |
| 平台 | 按远端域名推断；`gitlab` → GitLab，`github` → 报错让用 `gh`，其余按 Gitea |
| head/base | `task/<tid>` → 任务卡的 `base`（缺省 `main`） |
| 失败 | worker 侧 best-effort：结果里 `pr.error`，**任务仍判 done/failed 按 verify 走** |
| 合并 | 不做。合并/land 仍是人工或 `hive land` 的动作 |
| 网络超时 | `HIVE_PR_TIMEOUT`（默认 20 秒） |

---

## 5. 平台的接口差异

| 平台 | 查 | 建 | 鉴权头 |
|---|---|---|---|
| Gitea | `GET /api/v1/repos/{owner}/{repo}/pulls?state=open` | `POST …/pulls {head, base, title, body}` | `Authorization: token <t>` |
| GitLab | `GET /api/v4/projects/{urlencoded}/merge_requests?state=opened&source_branch=…` | `POST …/merge_requests {source_branch, target_branch, title, description}` | `PRIVATE-TOKEN: <t>` |

自建反代 / 测试桩可以用 `HIVE_PR_API` 覆盖 API 根地址（例如
`http://127.0.0.1:9000/api/v1`）。

---

## 6. 与 land 的关系

```
worker --pr                     hive land
   |                              |
task/T-0100  ──推送──▶  远端 ──▶  main
   |                              |
   └─ 自动开 PR ──▶ 人 review ──▶（PR 合入或 land 合入，二选一）
```

两条路并存：开了 PR 的卡**不要**同时 `hive land`，否则会出现「PR 还没合、分支已经被
land 合进 main」的情况（PR 会被自动标记 merged，但 review 的意义就没了）。
纪律：**PR 模式用人 review 合入，land 模式不建 PR**。

---

## 7. 排障

| 现象 | 处理 |
|---|---|
| `没有可用的 PR token` | 配 §2 的任一来源；确认 token 对目标仓有 PR 权限 |
| `HTTP 401` | token 过期/吊销：重新生成，`~/.config/hive/gitea.env` 改一行即可 |
| `HTTP 404` | owner/repo 推断错（嵌套 GitLab 组、自建路径带前缀）——用 `HIVE_PR_API` 覆盖 |
| `HTTP 409`（已存在） | 正常，幂等查询之后复用；如果真报 409，说明 open PR 查漏了，用 `hive pr --dry-run` 看 head |
| `GitHub 仓库请用 gh pr create` | 内置只支持 Gitea/GitLab，符合预期 |
| worker 日志 `建 PR 失败（不影响任务）` | 该卡的 done/failed 仍由 verify 决定；修好 token 后 `hive pr <tid>` 补上 |

---

## 8. 实现位置

| 内容 | 位置 |
|---|---|
| 模块 | `hive_pr.py`（只用标准库 urllib） |
| 远端解析 | `hive_pr.parse_remote` |
| token 查找 | `hive_pr.find_token`（环境变量 → 凭据文件 → 远端 URL 内嵌） |
| 创建/复用 | `hive_pr.create_pr` |
| worker 接线 | `Worker.open_pr` / `Worker.run_task` |
| 人工提交接线 | `Hive.cmd_submit(pr=True)` |
| CLI | `hive pr` |

回归测试：`tests/test_pr.py`（本机 HTTP 桩验证 Gitea/GitLab 的方法、路径、鉴权头、
幂等、dry-run、缺 token 的可操作提示、错误信息脱敏）。
