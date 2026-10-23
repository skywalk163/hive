# 让小浣熊 / WorkBuddy 加入 Hive

## 结论

两者都用 **manual 模式**：worker 负责建 worktree、租约、提交、push、跑 verify；
GUI agent 只负责在 worktree 里改文件、写 summary。

原因是它们跑在 FreeBSD 的 Ubuntu 22.04 chroot（linuxulator）里：

| agent | 用户 | 现状 | 为什么不直接 exec |
|---|---|---|---|
| 小浣熊 | `skywalk` | Electron + 深链登录，`box-agent-acp` 是 ACP runtime | 没有稳定的 `-p prompt` CLI；ACP 需要额外适配 |
| WorkBuddy | `skywalk`（当前；历史上曾有 root 实例） | Electron，自带 `codebuddy` CLI | chroot 里没有 `node`，`codebuddy` 起不来；先 manual |

好消息：**`/home` 和 `/tmp` 是 nullfs 共享的**，宿主和 chroot 里看到的是同一份文件，
路径完全相同。所以 worker 建在 `/home/skywalk/.hive/wt/<agent>/T-xxxx` 的 worktree，
小浣熊/WorkBuddy 在 chroot 里用同一个路径就能打开。

## 已经登记好的两个 agent

```text
raccoon    ['gui', 'chroot', 'linux']   manual   light=/home/skywalk/src/light
workbuddy  ['gui', 'chroot', 'linux']   manual   light=/home/skywalk/src/light
```

worktree 根目录：

- `/home/skywalk/.hive/wt/raccoon/`
- `/home/skywalk/.hive/wt/workbuddy/`

## 已经跑起来的 worker

```text
tmux: hive-raccoon    -> worker raccoon   --interval 15 --keep
tmux: hive-workbuddy  -> worker workbuddy --interval 15 --keep
```

看实时输出：

```sh
tail -f /tmp/hive-raccoon.log
tail -f /tmp/hive-workbuddy.log
```

`--keep` 表示任务结束后保留 worktree，方便 GUI 还开着的时候排查；确认没用了再手工删（见文末）。

## 给它们派一张卡

```sh
cd ~/dswork/hive

# 1) 建卡：needs 写 gui，worker 才会匹配到 GUI agent
python3 hive.py --dir . task new \
  --id T-0100 \
  --title "补一段文档" \
  --repo light \
  --needs gui \
  --paths docs/ \
  --verify "grep -q '关键词' docs/xxx.md" \
  --body-file /tmp/card.md

# 2) 指定给谁（也可以 dispatch --apply 让调度器挑）
python3 hive.py --dir . assign T-0100 --agent raccoon
```

worker 会在 15 秒内领卡，并在日志里打印：

```text
=== 任务 T-0100 已就绪 ===
工作目录: /home/skywalk/.hive/wt/raccoon/T-0100
任务卡:   /home/skywalk/.hive/wt/raccoon/T-0100/.hive/task.md
完成后写: /home/skywalk/.hive/wt/raccoon/T-0100/.hive/summary.md
```

## 在 GUI 里怎么交接

1. 打开小浣熊 / WorkBuddy，把工作区指向日志里的「工作目录」（chroot 内路径相同）；
2. 把 `.hive/task.md` 的内容贴进它的对话框（或让它读这个文件）；
3. 让它**只改这个目录里的文件**，不要 `git push`；改完写 `.hive/summary.md`；
4. worker 每 5 秒看一次 summary，检测到就：`git add` → commit → push `task/T-xxxx` →
   跑 `verify` → 写 `res/T-xxxx`；
5. 你在看板/`hive result T-0100` 里 review，合并后 `hive close T-0100`。

小浣熊看不到隐藏目录 `.hive` 的话，直接把 task.md 内容粘贴过去即可；
summary 路径照抄绝对路径，避免它写错地方。

## 权限与坑

- **小浣熊**以 `skywalk` 跑，worktree 归 `skywalk`，读写都正常。
- **WorkBuddy 当前以 `skywalk` 跑**（进程命令行是 `sudo chroot ... su skywalk ...`），文件属主和 worker 一致；
  如果以后改回 root，新建文件会是 root 所有：
  - worker（skywalk）**能读、能 commit**（文件默认 644）；
  - 如果它建了 root 所有的子目录，worker 删 worktree 可能删不干净；需要时：
    ```sh
    sudo chown -R skywalk:wheel /home/skywalk/.hive/wt/workbuddy/T-0100
    ```
- **一个 app 同一时间只接一张卡**（manual worker 会阻塞等 summary），卡要小：
  一张 60–120 分钟能做完；GUI 卡统一 `timeout_minutes: 180`、`lease_minutes: 240`。
  T-0003 实测小浣熊用了约 65 分钟，默认 60 分钟会在它写完 summary 前超时。
- 如果 agent 没写 summary：worker 到点标 `failed`，worktree 保留（`--keep`）；
  看日志、补 summary，或 `hive retry` 重来。
- **如果 GUI 在超时之后才写完**（T-0003 遇到过）：
  1. 在保留的 worktree 里把它的改动 commit 并 push 到 `task/T-xxxx`；
  2. 补写 `.hive/summary.md`（内容按它的实际改动写）；
  3. `hive retry T-xxxx`（不要 `--fresh`），worker 会从已推送的分支重新 verify；
  4. 通过后合并、`close`。
- 不要让 GUI agent 自己 `git push`，冲突和凭据都不好处理；统一由 worker 提交。

## 清理 worktree

```sh
cd ~/src/light
git worktree list | grep hive
git worktree remove --force ~/.hive/wt/raccoon/T-0100
git worktree remove --force ~/.hive/wt/workbuddy/T-0100
```

## 以后能怎么自动化

1. **WorkBuddy**：在 chroot 里装 node（或用它 Electron 自带的 node 环境）后，
   `codebuddy` 若能接受 `-p`/`exec`，就把 agent 从 manual 换成 exec，
   `--command` 里用 `chroot` 包装并把 cwd 映射到同一路径。
2. **小浣熊**：`box-agent-acp` 是 ACP server；dsh 自带
   `@agentclientprotocol` SDK，理论上可以让 dsh 作为 ACP client 直接驱动它。
   这属于第三阶段，先把 manual 跑顺。
