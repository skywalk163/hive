## 背景

任务详情抽屉打开后，顶栏右侧的筛选区（仓库 / Owner / 优先级 / 搜索）被盖住，
点不到。实测层级：

| 元素 | 定位 | z-index |
|---|---|---|
| `.topbar` | sticky | **20** |
| `.overlay`（抽屉遮罩） | fixed | 30 |
| `.drawer`（详情抽屉） | fixed，`top:0` | **31** |
| `.modal`（设置） | fixed | 32 |

抽屉 `top:0` + z 31 → 直接压在顶栏上；窄屏（≤768px）抽屉宽 100%，等于全遮。

**注意一个容易改错的地方**：筛选控件是原生 `<select>`，它的展开列表由浏览器
渲染在 top layer，理论上**不会被 DOM 层级盖住**。所以「被盖的」大概率是
`select` 控件本体（以及卡片里的自绘/半自绘浮层 `.agent-pick`），不是下拉列表。
必须先复现确认，否则容易改了一处却没用。

## 要做什么

1. **先复现**：写一个 headless Chromium 脚本（`dashboard/dev/` 下，复用现有
   ui-test 方式），打开抽屉后计算 `.drawer` 与 `#repo-filter` 的
   `getBoundingClientRect()` 交集面积；> 0 即复现。同时记录「点 `#repo-filter`」
   是否命中 `select` 本体（`document.elementFromPoint`），用来区分两种成因。
2. 按复现结论二选一实施：
   - **方案 A（默认）**：`.topbar` 提到 `z-index:33`（> drawer 31），`.drawer`
     加 `top: var(--topbar-h)` 让位，`.overlay` 保持 30 —— 抽屉打开时顶栏仍可点。
   - **方案 B（仅当 A 会破坏滚动/遮罩语义时）**：抽屉打开时给 `.controls` 加
     `inert`，并把筛选控件复制进抽屉顶部。
3. `tests/test_dashboard_layering.py`（新）：
   - 静态断言 z 序关系 `z(topbar) > z(drawer) > z(overlay)`（从 CSS 解析数值，不硬编码行号）；
   - 断言 `.drawer` 有 `top` 偏移；
   - UI 断言：抽屉打开时交集面积 == 0（用复现脚本同一套 DOM 计算，无浏览器时 skip 并标注）。
4. 附修复前后截图（浅色 + 深色各一张）到 summary。

## 不要做什么

- 不重做整体布局、不动 1180/768 断点策略。
- 不引入 UI 框架 / Portal 库（这是零依赖的 vanilla 页面）。
- 不为了「看起来对齐」改卡片网格。

## 完成标准

- `python3 -m pytest -q tests/test_dashboard_layering.py tests/test_dashboard_dispatch_ui.py` 全绿。
- **反证**：把 z-index 还原成 20/30/31，序关系断言必须变红（贴前后输出）。
- summary 里给出：交集面积修复前 / 修复后数值、`elementFromPoint` 的命中结论、
  两张截图路径。
