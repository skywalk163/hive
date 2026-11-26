# hive_submit 异步化（T-0492）

## 为什么要拆

`cmd_submit` 原本是一条同步链：commit → push `task/<tid>` → 逐条 verify → 写
`results/<tid>.json`。verify 单条上限 1800 秒，而 MCP 的 stdio 服务只有一条线程
（`mcp_server.py` 的 `serve()` 逐行读 stdin）。T-0489 的 verify 跑了 30 分钟没完：

1. 客户端先超时，报 `LocalHub returned an invalid raw MCP result`；
2. 分支和 commit 早已推上去，但结果没写，卡停在 running；
3. 紧接着 `hive_get_task` 也报同一个错——服务端还堵在那次 submit 里。

拆成「同步段 + 后台 job」后，submit 只等 git，verify 再慢也不会占住 MCP。

## 调用时序

```
MCP hive_submit
  └─ Hive.submit_async
       ├─ prepare_submit：校验 / commit / push task/<tid>      ← 同步，失败立刻报错
       ├─ 写 jobs/<tid>.json（phase=verifying） + 拿 <tid>.lock
       ├─ Popen(hive.py submit <tid> --run-job <job>)，脱离父进程
       └─ 返回 {status: submitting, branch, head, job, log, pid}
                                   │
                    后台进程 run_submit_job
                      ├─ 校验 lease_id / owner（被接管 → lease_lost，不写结果）
                      ├─ verify 期间 lease_heartbeat(release=False) 持续续租
                      ├─ 逐条 run_verify，超时 → 记 timed_out 并停止
                      └─ finish_submit：闸门 → 写 res/<tid> → store_result → 释放租约
```

CLI 行为不变：`hive submit` 仍同步跑完并返回 0/1；`--async` 走同一套 job。

## 状态机（jobs/&lt;tid&gt;.json 的 phase）

| phase | 含义 | 谁会写 |
|---|---|---|
| `verifying` | 后台 verify 进行中（`verify_index/total` 表示第几条） | job |
| `writing` | verify 已结束，正在写结果 | job |
| `done` | 结果已落库且 `status=done` | job |
| `failed` | 有结果但 `status=failed`（含 verify 红） | job |
| `timed_out` 结果 | verify 超时：结果里 `timed_out=true`，phase=`failed` | job |
| `lease_lost` | 租约被接管/lease_id 不匹配，**不写结果** | job |
| `lost` | pid 已死且没有结果（进程被杀/机器重启） | `submit_job_status` 读时判定 |

`lost` 只做标记、不补结果：伪造一个 failed 会掩盖「其实可能改完了」的事实，
正确动作是人看 log 后 `hive retry`。

## 跨平台与锁

- 后台进程：POSIX `start_new_session=True`；Windows `DETACHED_PROCESS |
  CREATE_NEW_PROCESS_GROUP`。父进程退出、控制台关闭都不影响它。
- 探活：POSIX `os.kill(pid, 0)`；Windows 用 `OpenProcess` + `GetExitCodeProcess`
  （Windows 上 `os.kill(pid, 0)` 会走 TerminateProcess，绝不能用来探活）。
- 互斥：`jobs/<tid>.lock` 用 `O_CREAT|O_EXCL` + pid 探活，不依赖 fcntl/msvcrt；
  job 启动后把 holder 换成自己的 pid，父进程死了也不会被误接管。
- 后台所有输出重定向到 `jobs/<tid>.log`，不碰 stdout（MCP 的 JSON-RPC 流）。

## 查询

`hive_get_task` / `hive_result` 在 owner 机器上返回 `submit` 块（phase、pid、
verify_index/total、log_tail、error）；主控端没有 job 文件时返回
`submit_remote=true`，只看 claim/result。不扩 store schema。
