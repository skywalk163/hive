# T-0237 hive 加 smoke.sh（第二次提交）

## 结论

`smoke.sh` 已在仓库根目录（`100755`），四步冒烟在 **Windows（Git Bash）** 与
**FreeBSD 15.1（0.88）** 上都跑过 4/4。

第一次提交记 failed：判据 `test -x smoke.sh` 在 Windows 上被原样丢给 `cmd.exe`，
报 `'test' 不是内部或外部命令`——**平台假失败，不是产物问题**。治本已落地：
T-0242 给 `hive_compat` 补了 `test -x`（`os.access(path, os.X_OK)`），已 land 到 main。
本次是 `hive retry T-0237` 之后的重提，判据现在由垫片执行，应当为 0。

## 做了什么

`smoke.sh`（唯一改动文件）只做四件事，全程在 `mktemp -d` 的临时目录里：

1. `git init --bare` 造临时远端 → `hive init --remote` 起临时协调仓；
2. `hive doctor`：要求 exit 0、输出里有 `[ OK ]`、**没有** `[ERR ]`；
3. `hive task new`（会真 commit + push，所以必须临时裸仓）→ `task list` 必须看得到；
4. 删掉这张卡（`git rm` + commit + push）→ `task list` 必须看不到（防第 3 步假阳性）。

跑完整个临时目录删掉，真实队列零写入（`KEEP=1` 或失败时保留目录并打印路径）。

刻意的写法：

- 除 `case` 子串判断外不用 `grep/sed/head/awk/!/$()` 做断言，脚本自己在 Windows 上也得能跑；
- `PATH="/usr/bin:/bin:$PATH"`：Git Bash 里 `sh smoke.sh` 时 PATH 常只有 Windows 那份，
  拿不到 `mkdir`/`rm`/`mktemp`；
- 本机状态全关进临时目录（`HIVE_STATE`/`HIVE_CLUSTER_STATE`/`HIVE_LEASE_DIR`/
  `HIVE_CLAIM_BACKEND=git`/`HIVE_CLUSTER_AUTOSTART=0`），不碰 `~/.hive`，
  也不会顺手拉起看板/集群；
- Python 先探测再选（Windows 上 `python3` 可能是应用商店占位 stub），判据是版本 ≥ 3.8；
- MSYS 路径（`/tmp/x`）用 `cygpath -m` 转成原生形式再交给原生 `python.exe`。

## 证据

### Windows 10 / Git MSYS 2.55.0 / Python 3.13.14

```
$ "C:\Program Files\Git\bin\sh.exe" ./smoke.sh
== 1/4 临时裸仓 ==      ok  裸仓：C:/Users/skywalk/AppData/Local/Temp/tmp.okTpPQVbVB/hive.git
                        ok  hive init --remote（临时仓，不动真实队列）
== 2/4 hive doctor 无 ERR ==                      ok
== 3/4 建卡 + task list 看得到 ==                 ok  task new T-9001 / list 看得到
== 4/4 删卡 + task list 看不到 ==                 ok
smoke: ✓ 全部通过（4/4）
```

### FreeBSD 15.1-RELEASE-p1（192.168.0.88，git 2.54.0 / Python 3.12.13）

用 `git bundle` 把任务分支传过去 clone，验的是**提交里记录的 100755**：

```
$ git ls-tree HEAD smoke.sh      → 100755 blob bc0a4401…  smoke.sh
$ ls -l smoke.sh                 → -rwxr-xr-x  1 workbuddy wheel  7342 smoke.sh
$ test -x smoke.sh; echo $?      → rc=0
$ sh -n smoke.sh; echo $?        → rc=0
$ sh smoke.sh                    → 4/4 全通过，rc=0
```

### 本次重提前的自查

```
$ git log --oneline -1           → a42e34d take T-0237 -> deepseek-win (via mcp)   # 已与 origin/main 同步
$ git ls-files -s smoke.sh       → 100755 bc0a4401… 0  smoke.sh
$ git status --short             → A  smoke.sh        （改动尚未提交，由 submit 落成一个提交）
$ T-0242 已 land：hive_compat.py 含 [efdrsx] 与 os.access(X_OK)
$ python hive_compat.py run "test -x smoke.sh"   → 0
```

## 边界自查

- 只改 `smoke.sh`（卡面 `paths`）；`.hive/summary.md` 是 hive submit 自带的既有约定
  （T-0231/T-0226 的提交里同样带它）；
- 没动其它任务卡、没动 agents 配置、没写任何 token；
- 冒烟全程临时裸仓，真实队列未被写入。
