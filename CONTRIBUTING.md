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

## 3.5 术语：`合入` 与 `合并` 分工（别再飘回去）

这两个词在中文技术圈**都有权威用法**，所以不能靠语感决定：

- Git 官方中文文档（[git-scm.cn/docs/git-merge](https://git-scm.cn/docs/git-merge)）
  用「**合并**」：快进合并 / 真正合并 / 合并冲突 / 合并策略。
- Gitee 官方帮助也用「**合并** Pull Request」「合并到目标分支」。
- 但企业研发流程普遍用「**合入**」：华为云 HCIP 教材「合入开发分支」、
  阿里云代码评审「评审通过后自动合入 master」。

区别在**语义**：

| 词 | 指什么 | 用法 |
|---|---|---|
| **合并** | git 的 `merge` **动作**，两条历史线结合 | 技术术语 |
| **合入** | 工作流**概念**，把改动并入主线，**带方向** | 产品/UI/给 agent 的指令 |

hive 的 `land` 不是单纯的 `git merge`，而是「并入 main + verify + 关闭任务卡」
的**工作流操作**，有明确方向性。所以：

- 面向人/agent 的**工作流文案**一律用 **合入**：
  看板列名（待合入）、按钮（复制合入指令）、`hive land` 的提示、
  `dispatch_prompt` 生成的指令、MCP 工具描述、CLI 汇总输出、TUI 分组名。
- **git 技术术语**保持 **合并**：合并冲突、快进合并、合并策略、
  配置/数据结构的合并（如 `cur.update(incoming)   # 合并后持久化`）。

**为什么必须分**：`"任务 T-0001 已完成，请合并"` 没有宾语，agent 会理解成
「把两个任务合并到一起」。现场实测用户说过上百次「合并」，有几次就是被这么
误解的。改成 `"任务 T-0001 已完成，请把它合入"` 后方向明确。

**回归守卫**：`tests/test_dashboard_dispatch_ui.py` 与 `tests/test_tui.py` 里
钉了「请把它合入」「复制合入指令」「待合入」，改动文案必须同步改测试 ——
别让测试站在旧措辞那一边（这是 lessons 里记过的坑）。

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
