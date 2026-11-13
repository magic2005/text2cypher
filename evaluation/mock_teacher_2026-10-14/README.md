# 模拟教师题（2026-10-14）

本目录保存“模拟教师题”场景的题集与历史报告。它与 `evaluation/questions.json`
的独立题集**不是同一套题**：这套题用于在教师正式题尚未提供时，验证“出题 → 参考执行 →
模型作答 → 逐题判定 → 修复前后对比”的完整链路。

## 内容

| 文件 | 说明 |
| --- | --- |
| `questions.json` | 30 道题的题号、难度、问题；由 `tools/mock_teacher_eval.py` 的 `CASES` 生成并归档 |
| `试题与测试报告.md` | 出题范围、参考执行、早期本地替身结果与失败分析 |
| `deepseek_baseline_report.md` | 真实 DeepSeek 运行的公开摘要 |
| `README.md` | 本文件 |

`result*.json`、`comparison*.json` 等逐题原始结果**不在仓库中**。它们包含完整模型
响应与内部图谱结果，按仓库证据策略只保存在本机或私有制品库；需要时可用下列命令
重新生成。

## 题集边界

- 30 题：简单 10 / 中等 12 / 复杂 8；题号、问题、gold 查询内联在
  `tools/mock_teacher_eval.py:CASES`，`questions.json` 是它的公开归档副本。
- `questions.json` 只含 `id` / `level` / `question`，不含 gold 查询和参考结果；
  gold 查询只存在于执行工具中，避免被当作模型提示。
- gold 查询不带 `LIMIT`；执行层负责只读校验和返回规模控制。静态检查会拒绝写操作、
  多语句、注释、未闭合字符串和无界变长路径，但**不要求** gold 带 `LIMIT`。
- 难度标签使用“简单/中等/复杂”，与独立题集的“简单/中等/困难”不同；两者在统计时
  都归为 10/12/8。

## 重新生成与校验

在仓库根目录执行：

```bash
# 题集一致性校验（不需要图谱/密钥）
python -m tools.mock_teacher_eval --validate

# 只执行 30 条 gold 查询，确认参考结果非空、图谱稳定
python -m tools.mock_teacher_eval --gold-only \
  --output gold_only_2026-11-13.json

# 用注入的本地 oracle 驱动完整流水线，不发起真实模型请求
python -m tools.mock_teacher_eval --local-oracle \
  --output local_oracle_2026-11-13.json

# 真实模型；必须使用独立文件名，不能覆盖 results.json
python -m tools.mock_teacher_eval --output external_2026-11-13.json

# 对比修复前后两次 local-oracle 运行
python -m tools.compare_mock_teacher \
  --before local_oracle_before.json \
  --after local_oracle_after.json \
  --output comparison_2026-11-13.json
```

约定：

- `results.json` 是本地替身基线的保留名；外部模型运行必须用其他文件名。
- 默认输出已存在时必须加 `--resume` 或 `--force`；`--resume` 还要求
  `evaluation_mode` 与 `cases_sha256` 一致。
- 运行前会比对 `CASES` 与已提交的 `questions.json`。内容相同不重写；内容不同直接
  报错，工具不会静默覆盖已归档题集。
- 每跑完一题原子写一次结果；中断时尽力写 `status = "interrupted"`。
- 中途图谱指纹变化会记录 `graph_changed_between_segments`，该次结果不能当作稳定基线。

## 历史结果（摘要）

- **真实 DeepSeek**：30/30（简单 10/10、中等 12/12、复杂 8/8），总耗时 504.540 秒，
  57 次逻辑模型调用（生成 30、修正 0、总结 27）；运行前后图谱指纹一致。
  `H03` 多 1 行中间结果、`H08` 多 5 行服务汇总行，属于展示层附加行，已按题意人工复核。
- **本地替身（早期）**：26/30。失败原因包括：
  - `S02` / `M04`：字符串字面量中的 `DELETE` 被误判为写操作；
  - `H05` / `H06`：标量投影导致完整链路被丢弃。
  这些缺陷随后在 `agent/core.py` 修复；上表是当时快照，不代表当前代码的通过率。
- 原始逐题 JSON 未入库；历史数字只对应当时的图谱快照与模型版本，不应被新结果覆盖。

## 与独立题集的区别

| 维度 | 独立题集 | 模拟教师题 |
| --- | --- | --- |
| 文件 | `evaluation/questions.json` | 本目录 `questions.json` |
| 题号 | `S/M/H` 前缀，30 题 | `S/M/H` 前缀，30 题 |
| 难度标签 | 简单/中等/困难 | 简单/中等/复杂 |
| gold 位置 | 题集内 `reference_cypher` + `reference_rows` | 工具内 `CASES`，题集只存问题 |
| 主要用途 | 正式评分与逐题追溯 | 验证评测链路、对比修复前后 |
| 评分 | 集合语义 + 人工复核 | 本地替身逐行多重集对比（`compare_mock_teacher`） |

两套题都独立于知识库；不要把任意一套的结果直接当作另一套的基线。
