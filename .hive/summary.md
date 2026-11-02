# T-0231 交付摘要 — hive 提供 pipx 安装通道

## 交付物
- 新增 `pyproject.toml`（仓库根，唯一改动文件），使 `hive` 可通过 pipx 安装：
  - 分发名 `hive-cli`，`requires-python = ">=3.9"`，**零第三方依赖**（全 CLI 仅用标准库）。
  - 入口点 `hive = "hive:main"` → pipx 装完即得 `hive` 命令。
  - `[tool.setuptools] py-modules` 列出全部扁平模块（conflict / dispatch_* / fake_agent /
    hive / hive_* / llm_controller / mcp_* / pending_matcher / selftest，共 20 个），
    保证 init 拷贝所需模块不缺文件；「git 即队列」语义不变。

## 与任务卡描述的偏差（受执行契约约束）
- 任务卡建议入口点 `hive = hive:hive_cli_main` 并「现有 main 需要拆一层」。
- 执行契约限定**只允许修改 pyproject.toml**，故未在 `hive.py` 内新增 `hive_cli_main` 包装函数。
- 现有 `hive.py::main(argv=None) -> int`（L6426）签名即为 console_scripts 所需（无参调用
  等价于命令行入口），因此 `hive:main` 功能等价且可运行，无需拆层即可满足「pipx 装完 hive doctor 能跑」。
- 若后续放开 paths 限制，可再把 `main` 薄包一层 `hive_cli_main` 以对齐卡面命名。

## 验证（沙箱内）
- `test -f pyproject.toml` → PASS（即本卡 verify 判据）。
- `tomllib` 解析 TOML 通过；`[project.scripts] hive = "hive:main"` 解析为真实可调用函数。
- 20 个 py-modules 均在仓库中存在对应 `.py`，无拼写导致构建失败的风险。
- 运行时冒烟：`python -B hive.py --help` 正常加载全 CLI；`python -B hive.py doctor` 退出码 0
  （无 ImportError、无崩溃），印证「hive doctor 能跑」。
- 未执行：`pipx install` / `python -m build` 完整构建（沙箱无 build/setuptools 且出网受限）。
  打包配置为标准 setuptools 扁平模块布局，已逐项核对。
