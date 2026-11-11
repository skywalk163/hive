---
id: T-0333
title: 数据配比与 JSON 输出任务集
repo: lightprism
base: main
state: ready
owner: null
agent: null
priority: P1
needs:
  - python
paths:
  - data/sft/
  - src/lightprism/demo/sft_build.py
  - tests/test_sft_data.py
  - reports/数据配比报告.md
locks:
  - data/sft/json_tasks.jsonl
verify:
  - PYTHONPATH=src python -m pytest -q tests/test_sft_data.py
  - PYTHONPATH=src python -m lightprism.demo.sft_build --validate data/sft/json_tasks.jsonl
depends_on: []
lease_minutes: 180
timeout_minutes: 120
created: 2026-11-11T22:10:00+08:00
max_retries: 2
---

# 数据配比与 JSON 输出任务集

## 背景

Demo 要能「输入 prompt，输出代码**或 JSON**」。语料里只有代码，没有「prompt → JSON」的样本。
这张卡造那批数据。

**与 T-0332（训练）并行**：第一轮训练只用语料本身，这张卡的产出喂给第二轮。

## 现实约束（必须先读）

- 总语料 3MB、模型 3.4M 参数。**编造 10 万条任务描述只会让模型背台词**，
  在没见过的 prompt 上表现更差。
- 所以本卡的目标是**小而精**：200—500 条高质量样本，覆盖 5—8 类任务，
  每类都有明确模板与真实可执行的答案。

## 要做的事

### 1. JSON 输出任务集 → `data/sft/json_tasks.jsonl`

复用 `lightprism.data.Sample` 的 schema（当前 `source` 允许值没有这一项，
**请先给 `SOURCES` 加 `demo_json`**，并同步 `docs/样本导出.md`）。

建议覆盖的 6 类：

| 类别 | 示例 prompt | 期望输出 |
|---|---|---|
| 文件清单 | 「列出当前目录的 py 文件及其大小」 | `{"files":[{"name":"a.py","size":123}]}` |
| 诊断结果 | 「报告磁盘使用率」 | `{"disk":{"used_pct":82,"alert":true}}` |
| 任务分解 | 「把排序任务拆成子任务」 | `{"subtasks":[{"id":1,"desc":"..."}]}` |
| 决策选择 | 「这段代码该整体重写还是局部替换」 | `{"decision":"FR","reason":"..."}` |
| 验证结果 | 「检查这段代码的问题」 | `{"ok":false,"code":"L3001","line":2}` |
| 配置生成 | 「生成一个 JSON 配置」 | `{"mode":"fast","threads":1}` |

每类 30—80 条，**总共不超过 500 条**。

### 2. 格式约束（关键）

训练序列要把 schema 前缀也编进去，让模型学会先输出 `{`：

```
<FORMAT>json</FORMAT>
<TASK>列出当前目录的 py 文件及其大小</TASK>
<CODE>{"files":[{"name":"a.py","size":123}]}</CODE>
<V:PASS>
```

- `--validate` 模式必须检查：`<CODE>` 段内容能被 `json.loads` 解析
- 代码类样本的 `<CODE>` 段必须能被 `light ai check` 通过（若能调用；否则在报告里说明跳过）

### 3. 代码类样本的补充

从 T-0330 的语料里挑 **50—100 个短小完整的函数/脚本**，配一句自然语言任务描述。
描述要**从代码实际行为反推**，不许编造代码里没有的功能。

## 完成标准

- `data/sft/json_tasks.jsonl` 通过 `load_jsonl` 与 `--validate` 双重校验
- 条数在 200—500 之间，每类都有
- `reports/数据配比报告.md` 含：各类条数、来源说明、`json.loads` 通过率、
  代码类样本的 `light ai check` 结果（或跳过原因）
- `tests/test_sft_data.py` 覆盖：条数范围、JSON 可解析、schema 兼容、不超 500

## 禁止

- **不要用别的模型批量生成样本**（会引入不可控分布，且无法验证正确性）
- 不要超过 500 条。宁可少而准
- 不要编造代码里不存在的功能描述
- 不要修改 `data/samples.jsonl`（那是 v0.2 的示例，有测试引用）
