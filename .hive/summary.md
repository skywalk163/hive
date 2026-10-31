# T-0218 交付：hive top —— 纯标准库常驻 TUI（--once 可断言）

## 结论

新增 `hive top`：一个只读的常驻视图，默认每 2s 刷一屏，头部是集群角色/term/
在线节点/数据新鲜度，中部是「待领取 / 进行中 / 待合并 / 异常」四组，底部是
时间 + `q` 退出提示。零第三方依赖（没引 textual/rich/blessed），`--once` 打一帧
就退出且退出码恒 0，三条判据都能写成断言。

## 改了什么

| 文件 | 改动 |
| --- | --- |
| `hive_tui.py`（新） | 全部实现：中文双宽 `char_width/text_width`、`truncate/fit`、快照归一 `snapshot/_pick`、四组分类 `classify`、取数 `load_http/load_local/load_snapshot`、渲染 `render_frame`（含高度裁剪 `layout_counts`）、按键 `Keyboard`（POSIX select+cbreak / Windows msvcrt）、入口 `run` |
| `hive.py` | 只加两处：`top` 的 argparse（`--once/--width/--interval/--no-clear/--api`）+ `cmd_top` 一行派发（懒 import `hive_tui`，缺文件给可操作错误）。没动任何既有函数 |
| `tests/test_tui.py`（新） | 11 条用例：双宽/截断、80 列每行不超宽（含反证）、60 列下限、四组齐全且卡片数与快照一致、少一张就少一行、done 卡不进组、高度裁剪、`q` 干净退出、两源皆挂显示「无数据」、看板挂回落本地、分类优先级 |

## 关键取舍

1. **宽度自己算，不用 `len()`**：`len("中文")==2` 而终端占 4 列，靠 len 裁剪
   必然溢出 80 列。`char_width` 按 `east_asian_width` 取 W/F=2，歧义宽度（`…`、
   `─`）按 1 —— 这是 Windows Terminal / xterm 的默认，也是边框线整齐的前提。
2. **每行只从 `fit()` 出**：`render_frame` 返回前对整帧再过一遍 `fit`，所以
   「某一行漏了裁剪」不会靠单个调用点的自觉。
3. **高度裁剪写在组标题里**（`待领取 120 · 显示 5`），不额外占一行「还有 N 张」：
   小终端上多一行就可能把整帧顶出屏幕。实测 12/16/24 行终端分别输出 12/16/24 行。
4. **本地回落不 fetch**：看板够不着时才走本地快照，这时再去 fetch 只会把它也
   拖进同一个网络失败；读到什么显示什么。回落用 `hive.Hive(...).cards()`
   在内存里等价 `hive status --json` 的行，不打印、不落盘。
5. **不用 curses**：Windows 标准库里没有 curses（要装 windows-curses），而这
   个视图正是给 Windows 用的。ANSI 转义 + `msvcrt` 就够，且不多开窗口。

## 验证

```
$ python3 -m pytest -q tests/test_tui.py
...........                                                              [100%]
11 passed in 6.23s
```

三条判据的反证（都验证过「拆掉就红」）：

- 把 `truncate` 改成直接返回原串 → `test_once_width_80_每行都不超宽`、
  `test_width_下限是60`、`test_中文按双宽计算且截断带省略号` 三条同时失败
  （实测 `max(widths)` 从 60 涨到 91）；改回来即恢复全绿。
- 卡片数是跟着快照走的：`test_少一张卡就少一行` 直接拿掉一行断言总数减一。
- 数据源不可达：`test_两个数据源都不可达时显示无数据且退出码0` 断言退出码 0、
  输出含「无数据」、stderr 无 Traceback、头尾都还在（不是半截输出）。

真实终端（本机 0.88，`hive --dir . top --once --width 80`）：

```
hive top │ hive │ leader │ term 8 │ 节点 2/2 │ 数据 3s 前
────────────────────────────────────────────────────────────
 待领取 3 ──────────────────────────────────────────────────
  T-0213  光明P2：linter 内置名清单登记了但运行期不存在的名…
  T-0215  光明P2：原生腿 `参数列表()` 比转译腿多返回 argv[0…
  T-0219  冷热分离：摘要表 + 可回滚归档（status.json 已 362…
 进行中 1 ──────────────────────────────────────────────────
  T-0218  hive top：纯标准库常驻 TUI（--once 可断言）  @wor…
 ...
```

邻近回归：`tests/test_new_surfaces.py test_cli_daemon.py test_no_window.py
test_status_git.py` → 59 passed, 1 skipped；`tests/test_docs*.py` → 43 passed。

## 没做到的 / 需要人接

1. **截图没附**：本机是无显示器的 Linux（ssh），出不了 Windows Terminal 和
   0.88 终端窗口的截图。上面那段是同一台机器上的真实 stdout 捕获，
   需要人工在 Windows Terminal 里补两张图。
2. **领取走了 git 后端**：`hive claim`（store 后端）当前必然失败，原因是集群
   follower（DESKTOP-6LFQUO8）的 store 在 22:54 重启后没启用 ——
   `GET /api/cluster/op/since?since=0` 在它那里返回
   `{"ok":false,"error":"store 未启用"}`，于是 leader 每次 prepare 都凑不到
   quorum 2，报「未达到多数 prepare，写未确认」。本卡因此改用
   `HIVE_CLAIM_BACKEND=git` 领取（Windows 那台一直是这个模式）。
   这是集群侧的问题，不在本卡允许改的路径里，没碰。
3. `tests/test_core.py::test_block_skips_dispatch_and_worker` 在 HEAD 干净树上
   同样失败（`刚推送过（TTL 60 分钟内不重复派）`），与本卡无关，未处理。
