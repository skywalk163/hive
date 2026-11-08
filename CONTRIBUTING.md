# 参与 hive 开发

`hive` 是一个有真实协作历史的仓库：任务卡、结果、agent 登记都在 Git 里，任何一次提交都会
被其它机器和常驻进程看到。动手前请先读本节，尤其是「建卡规矩」和「跑测试」两条。

## 0. 环境

先进入工作目录，再统一用仓内相对路径（平台的差异、`python3` / `python` / `py -3` 的替换、
Windows 老机器的目录映射都在这里）：

- 见 [README「0. 约定：先进入工作目录」](README.md#0-约定先进入工作目录)

要点：hive 仓在你的工作目录下（Linux/FreeBSD 是 `~/dswork/hive`，Windows 是
`G:\dswork\hive`）；**不要**把仓 clone 到家目录再用绝对路径调用 `hive.py`。

## 1. 跑测试

统一入口（推荐，已在 CI 与日常使用中出现）：

```bash
bash tests/run_pytest.sh
```

如果没有 bash（例如纯 Windows PowerShell），退化成带 stdin 隔离与硬超时的调用：

```powershell
python -m pytest -q < $null
# 或者（Git Bash / MSYS）
timeout 600 python -m pytest -q </dev/null
```

**`</dev/null`（PowerShell 里的 `< $null`）或 `run_pytest.sh` 必须带上**。原因见 T-0261：
`hive.run()` 没有隔离子进程的 stdin，测试进程跑完不退出、整条命令挂住。这是新人最容易
踩的坑，不要省略。

只跑某个测试文件时同样要加 stdin 隔离：

```bash
python -m pytest tests/test_version_consistency.py -q </dev/null
```

## 2. 建卡与改动边界

任务卡的 `paths` 就是**允许修改的文件清单**。规矩只有一条：

> 只修改任务卡 `paths` 允许的路径；需要改其它文件时，停下来在 `.hive/summary.md` 里说明原因。

不要「顺手」改卡外的文件、别的卡面或 agent 配置；越界会被 submit 的 `out_of_paths` 检查
挡下来。做不完也要写 `.hive/summary.md`，把卡住的位置和下一步写清楚。

## 3. `locks` 字段：让同仓多卡并行

`paths` 只做目录前缀匹配，`src/` 一写就会和所有改 `src/` 下文件的卡判冲突。`locks` 用来
声明「我只碰这几个文件」，双方都写了 `locks` 时按精确集合判冲突（见 `conflict.py` 顶部
docstring）。三种写法：

| 写法 | 含义 | 与谁冲突 |
|---|---|---|
| `src/`（**带尾斜杠**） | 目录锁：覆盖该目录下所有文件 | `src/a.py`、`src/sub/b.py`、`src/*.py` |
| `src/a.py`（无尾斜杠、无通配） | 文件锁：只碰这一个文件 | 只有 `src/a.py` / `src/` |
| `src/*.py`、`**/test_*.py` | 简单通配（`fnmatch`，`*` 会跨 `/`） | 命中即冲突 |

注意：`src`（无尾斜杠）在 `locks` 里是**文件**，不是目录；任一方没写 `locks` 时保守判冲突。
`locks` 不参与 `out_of_paths` 检查 —— 写锁不代表允许改 `paths` 之外的文件。

## 4. commit message 风格

照仓库实际历史写（`git log --oneline -20` 可查）：

```
feat(cluster): ...
fix(T-0267): 归档结果订正为人工复验 done，保留 submit 零改动误判原文
close T-0269: ...
hive(T-0261): ...
task: add T-0285 ...
take T-0269 -> trae-win
```

约定：`type(scope):` 开头（`feat` / `fix` / `docs` / `task` / `close`），能带任务号就带
（`fix(T-xxxx): ...`）；正文说清「为什么」而不是重复 diff。不要 `--no-verify`，
不要强推共享分支。
