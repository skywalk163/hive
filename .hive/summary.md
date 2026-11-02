# T-0242 hive_compat 支持 test -x（T-0237 假失败的治本）

## 结论

`hive_compat` 现在认识 `test -x`（含 `! test -x`）：Windows 上用
`os.access(arg, os.X_OK)` 求值，不再把整条命令原样丢给 `cmd.exe`。
改动 3 个文件，全在卡面 `paths` 内。

## 改了什么

### `hive_compat.py`
- `_TEST_SUB` / `_NEG_TEST_SUB` 的 flag 字符类 `[efdrs]` → `[efdrsx]`；
- `evaluate()` 加 `x` 分支：`ok = os.access(arg, os.X_OK)`；
- 模块 docstring 补一行「覆盖范围」，把 `-x` 的语义弱化写在门口，而不是等人踩坑。

### `tests/test_compat.py`
- 参数化表补两条：`test -x no_such_file_xyz` → 1、`! test -x no_such_file_xyz` → 0；
- `test_evaluate_x_flag_semantics`：显式 `chmod 0644` 造「不可执行」，钉住平台差异
  （POSIX 上 644 → 1、755 → 0；Windows 上「存在」→ 0）；
- `test_x_flag_is_wrapped_not_dropped_to_cmd`：T-0237 回归——`should_wrap("test -x smoke.sh")`
  必须为真，且 `hive.verify_command` 必须把它包成垫片调用；
- `should_wrap` 的平台开关用例里补 `test -x x`、`! test -x x`。

### `docs/判据与平台.md`
- 平台表加 `test -x x` 一行；
- 「垫片覆盖范围」补 `-x`；
- 新增 ⚠️ 段：Windows 上 `test -x` 的语义是**弱化**的（NTFS 没有执行位，
  `os.access(X_OK)` 对文件等价于「存在」），能挡「文件没生成」，挡不住「忘了 chmod /
  提交里是 100644」；要验后者得看 `git ls-tree HEAD <file>` 的 `100755`。
  并记下 T-0237 这次事故（0.88 rc=0、Windows 上假失败）的因果。

## 关键取舍

`-x` 在 Windows 上没有可信的文件系统来源，所以只能退化为「存在即可执行」。
这个弱化**写进了代码注释和文档**，不假装 Windows 上能测出 exec 位。
另外没有改 `hive.py`：`verify_command` 本来就只是问垫片 `should_wrap`，加 flag 是唯一改动点。

## 验证

```
$ python -m pytest -q tests/test_compat.py -k "not worker_true_verify_is_done"
22 passed, 1 deselected in 2.73s

$ python -m pytest -q tests/test_compat.py        # 含 Windows 端到端（World/worker）
23 passed in 133.77s（改前基线 19 passed / 451.75s，新增 4 条）

$ python -m pytest -q tests/test_docs.py tests/test_verify_command_quotes.py \
      tests/test_compat.py -k "not worker_true_verify_is_done"
47 passed, 1 skipped, 1 deselected in 56.85s

# 端到端预演：用垫片跑 T-0237 的真实判据
$ python hive_compat.py run "test -x hive.py"          → 0
$ python hive_compat.py run "test -x no_such_file"     → 1
$ python hive_compat.py run "! test -x no_such_file"   → 0
$ hive.verify_command("test -x smoke.sh")
  → C:\Python314\python.exe "...\hive_compat.py" run 'test -x smoke.sh'
  → 在 T-0237 工作树里 rc=0；把文件名换成 no_such_file_xyz 则 rc=1
```

## 落地后要做的

1. `hive retry T-0237`，T-0237 重新提交时 verify 就会是 0（不再是 `'test' 不是内部或外部命令`）。
2. **MCP server 要重启才吃到这个修复**：`mcp_server.py` 在启动时 `import hive_compat`，
   跑着的进程里是旧模块；本次我用同一套 `cmd_submit` 代码的 CLI 入口验证。

## 边界自查

- 只改 `hive_compat.py`、`tests/test_compat.py`、`docs/判据与平台.md`（卡面 `paths`）；
- 没动 `hive.py`、没动其它卡、没动 agents 配置、没写任何 token。
