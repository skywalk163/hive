# T-0216 完成报告：status fetch 前置比对（SHA + TTL）

## 改了什么

### hive.py

1. **新增 `remote_head(path) -> str`**
   - `git ls-remote origin refs/heads/main refs/heads/master`，超时 10s
   - `GIT_TERMINAL_PROMPT=0`（交互提示绝不弹出）
   - 失败/分支不存在返回空串 `""`

2. **新增 `fetch_if_changed(path, ttl=10) -> bool`**
   - 先 `remote_head()` 拿远端 SHA，与本地 `refs/remotes/origin/main`（或 master）比对
   - 相同 → 返回 `False`，**不启动 fetch 子进程**
   - 不同 / ls-remote 失败 → 返回 `True`，回落无条件 fetch
   - 进程内按路径记 TTL（默认 10s），看板 30s 一轮、多仓时不至于 3×N 次往返
   - `HIVE_FETCH_ALWAYS=1` 强制回到「每次都 fetch」的旧行为（回退阀）

3. **`cmd_status` 改用条件 fetch**
   - 原来：`self.fetch()` 无条件 `git fetch --prune origin`
   - 现在：`if fetch_if_changed(self.dir): self.fetch()`
   - 远端 SHA 没变时，跳过整个 fetch（连网络请求都不发）

4. **`_sync_one_repo` 改用条件 fetch**
   - 原来：开头无条件 `git fetch --prune origin`
   - 现在：`if fetch_if_changed(path): git_net(["fetch", "--prune", "origin"], path)`
   - 安全边界不变：只 `merge --ff-only`，绝不 `reset --hard`

### tests/test_status_git.py

新增 8 个测试：
- `test_remote_head_returns_sha`：ls-remote 拿到正确 SHA
- `test_remote_head_empty_on_bad_path`：非 git 仓返回空串
- `test_fetch_if_changed_false_when_up_to_date`：远端没变 → 不 fetch
- `test_fetch_if_changed_true_when_remote_changes`：远端变了 → 要 fetch
- `test_fetch_if_changed_true_on_ls_remote_failure`：ls-remote 失败 → 回落无条件 fetch
- `test_fetch_if_changed_ttl_caches_result`：进程内 TTL 缓存 ls-remote 结果
- `test_fetch_if_changed_always_env_forces_fetch`：`HIVE_FETCH_ALWAYS=1` 强制 fetch
- `test_cmd_status_no_fetch_when_remote_unchanged`：cmd_status 远端没变不跑 fetch

## 反证（三条）

1. **远端 SHA 未变 → git fetch 调用次数 == 0**
   - `test_fetch_if_changed_false_when_up_to_date`：spy git_net，远端没变时零 fetch 调用
   - `test_cmd_status_no_fetch_when_remote_unchanged`：cmd_status 远端没变零 fetch

2. **远端 SHA 变化 → 调用次数 == 1**
   - `test_fetch_if_changed_true_when_remote_changes`：hive2 push 新提交后，fetch_if_changed 返回 True

3. **ls-remote 不可达 → 回落为 1**
   - `test_fetch_if_changed_true_on_ls_remote_failure`：mock remote_head 返回空串，fetch_if_changed 仍返回 True

## 测试结果

```
python -m pytest tests/test_status_git.py -v
17 passed in 288.86s

python -m pytest tests/test_sync_repos.py -q
10 passed in 313.96s
```

test_core.py 有部分 Windows 兼容性预存失败（`true` / `test -f` 等 Unix 命令在 Windows 不存在），与本次改动无关——前 10 个 dispatch 测试全部通过。

## 没动什么

- `HIVE_NET_TIMEOUT` 语义：不变
- `merge --ff-only` 安全边界：不变
- `run()`（T-0206 的地盘）：没碰
- `sync_main()` / `_sync_for_commit()` 里的 `self.fetch()`：没碰（写操作必须硬同步）

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `HIVE_FETCH_ALWAYS` | 未设 | `1`/`true`/`always` 强制每次都 fetch（回退阀） |
