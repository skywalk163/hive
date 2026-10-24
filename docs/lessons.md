# lessons —— 以后建卡 / 配环境时该注意什么

不是流水账，是「同类坑别再踩第二次」。格式：**现象 → 原因 → 以后怎么做**。

新的教训从 `hive digest` 的失败归因、`hive audit` 的风险标记、以及 agent 自己写的
summary 里来。总控每天 `hive digest --days 1` 看一眼；也可以建一张卡让 agent 读
digest + `hive doctor` 产出增补草稿（草稿走 `hive draft` 那套流程）。

## 一、写任务卡

- **verify 必须真跑过，光读一遍不算**。T-0114 的第三条验收我自己写错了
  （`sys.exit` 但没 `import sys`），submit 判 failed，代码其实全对。
  → 写完卡面先手动跑一遍所有 verify 命令再入库。
- **卡面的 `paths` 要把真正会改的文件写全**。写 T-0112 时我顺手把用法写进了 README，
  但 README 不在声明的 paths 里，被自己的越界检测抓了，只能回退重写。
  → 文档改动通常动 README + 手册 + CHANGELOG，三个都列上。
- **`needs` 里的能力要确认有 agent 能满足**。T-0102 写着 `needs: windows`，
  但 4 个 agent 一个都没声明，它就在 ready 列表里躺成僵尸卡。
  → 现在 `draft lint` 会拦这种情况；写正式卡时也该先 `hive agent list` 看一眼。
- **卡要拆小**。digest 里耗时最长的是 T-0004（83 分钟）—— 一张文档卡干了一个多小时。
  → 目标能在一屏内说清的卡才适合派出去；说不清就先写草稿。

## 二、环境与运维

- **worker 必须用 `setsid` 启动**。不用会随父 shell 回收，日志静默停在半路才发现
  （fb88-dsh 死过一次，6 小时后才注意到）。
- **`dsh-exec` 会自动判断运行环境**：看得到 `/compat/ubuntu` 是 FreeBSD 宿主，
  走 `sudo -A` + askpass；看不到就是已经在 linuxulator 里，直接跑不需要 sudo。
  WorkBuddy 的工具环境属于后者。
- **`.env` 的 `OPENAI_API_KEY` 被注释时 exec agent 起不来**：dsh 报
  `MISSING_CREDENTIAL: no API key for provider route "deepseek-official"`。
  `.dsh/.credentials.yaml` 里另有一个 key 但 dsh 不认那个路由名。
- **孤儿租约**：worker 被 kill 前抢到的 claim 分支不会自己消失，要用
  `git push origin :refs/heads/claim/<tid>` 回收（等价于 `gc`）。
- **残留 worktree 会挡住新建**：报「已经检出到」时先
  `git worktree remove --force` + `git worktree prune`。
- **`hive take` 的 `--fresh` 偶尔不生效**（基线还是旧 task 分支），兜底是
  `git -C <wt> fetch && git -C <wt> reset --hard origin/main`。

## 三、并发与提交

- **并发锁的粒度是 paths 不是 repo**。同一个仓里改互不相干目录的卡可以并行；
  paths 重叠（含父子目录）或没声明 paths 的才互相排队。
- **`commit_and_push` 已收口**（T-0114）：每个写命令只提交自己该动的文件。
  但**跑写命令前还是先把自己的手头活提交掉**更省心 —— 收口之前被咬过两次。
- **`git merge` 要显式带 git 身份**。测试环境/容器里常没有 `user.name`，
  merge 会以「请告诉我你是谁」失败，而且**不留任何冲突标记**，
  `--diff-filter=U` 查出来是空的，看起来像"合并成功"，极难查。
- **改动清单要在提交前抓**。提交后 `git status` 是空的，再算就恒为 `[]`。

## 四、改 hive 自己的代码时

- **批量替换脚本：锚点必须唯一**。`for name, a in agents.items():` 在
  `cmd_dispatch` 里先出现，把本该插进 `cmd_doctor` 的块插错了地方，直接 IndentationError。
  → 插入前先 `assert s.count(anchor) == 1`。
- **写文件的脚本断言失败 = 什么都没写**。有一次性替换因为第三个断言失败整体没落盘，
  但随后的 `py_compile` 照样显示"编译 OK"，差点以为成功。
  → 落盘后单独 grep 确认，别拿后续命令的输出当证据。
- **自然语言的口供别用固定词表匹配**。`hive audit` 上线后第一次实跑就漏了它自己的
  动机案例：T-0101 写的是「未能启动…未得到实际测试结果」，词表里一条都没命中。
  → 改成正则 `CONFESS_RE`。

## 五、hive 的用法

- 干活流程：`hive take --agent <name>` → 在 worktree 里改 → 写 `.hive/summary.md`
  → `hive submit` → `hive land --apply` 自动合入关闭。
- 合并前看一眼 `hive audit T-xxxx`：比对 agent 的 summary 和真实 diff。
- 每天 `hive digest --days 1`，`hive doctor` 有 error 就退出码非 0。
