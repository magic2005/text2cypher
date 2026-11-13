# 评测集与评测执行（04_evaluation）

本目录是 Text2Cypher 的评测模块：保存两套互相独立的 30 题评测集、评测执行工具、
逐题判定规则和历史报告。它不包含图谱构建、检索或生成代码，也不修改 `agent/core.py`。

## 1. 定位与隔离边界

- **独立题集** `evaluation/questions.json`：30 题，简单 10 / 中等 12 / 困难 8，
  覆盖单跳、多跳、计数、去重、闭合链路和 REST-MQ 组合题。题目与知识库解耦，
  参考答案在题集中冻结。
- **模拟教师题** `evaluation/mock_teacher_2026-10-14/`：另一套 30 题，
  用于验证“教师正式题尚未提供”时整条评测链路能跑通。
- `evaluation/benchmark.py` 只使用 Python 标准库，不 import `agent.core`、
  `neo4j` 或 `streamlit`。离线测试可以在没有图数据库、没有模型密钥的机器上运行。
- 只有 `tools/evaluate.py` 与 `tools/mock_teacher_eval.py` 的**实际运行路径**才懒加载
  `agent.core`；import 工具、校验题集、跑 pytest 都不会触发数据库连接。

## 2. 文件布局

```text
evaluation/
  __init__.py
  benchmark.py                      # 题集契约、只读检查、判定、报告聚合
  questions.json                    # 独立题集（30 题，入库）
  README.md
  mock_teacher_2026-10-14/
    questions.json                  # 模拟教师题号/题目（入库）
    README.md
    试题与测试报告.md                # 历史报告（入库）
    deepseek_baseline_report.md     # 历史报告（入库）
tools/
  evaluate.py                       # 独立题执行
  mock_teacher_eval.py              # 模拟教师题执行
  compare_mock_teacher.py           # 修复前后 local-oracle 对比
tests/
  test_evaluation.py                # 纯离线测试（40 项）
```

原始模型响应、逐题完整结果和 `latest_run.json` **不入库**，由根目录 `.gitignore`
统一排除；需要保留的实跑结果请用带日期的文件名，另存到本机或私有制品库。

## 3. 快速校验（不需要 Neo4j / 模型密钥）

在仓库根目录执行：

```bash
python -m pip install pytest
python -m pytest tests/test_evaluation.py -q
python -m tools.evaluate --check-dataset
python -m tools.mock_teacher_eval --validate
```

三条命令的离线边界：

- `pytest` 不使用 `neo4j` / `agent.core` / `streamlit`，测试文件里有一个子进程用例专门守护
  这个边界；
- `--check-dataset` 校验独立题集的结构、难度分布、参考行、指纹和只读性；
- `--validate` 校验 30 条内联模拟题与已提交的 `questions.json` 是否逐字一致。

当前题集事实：数据集 SHA-256 为
`a2aba22212ae1507097aca8c30676aa4fb69511b3c593f5d6aeef0975de0696f`，
参考行 218 行，所有题共享图谱指纹
`277d4b4c0a1dc9013efa44df289da0ca74cf2a2750d787a0855e2866066f6615`。

## 4. 独立题集字段

每道题至少包含以下字段；`validate_questions` 会逐项检查：

| 字段 | 含义 |
| --- | --- |
| `id` | `S01–S10` / `M01–M12` / `H01–H08`，顺序固定 |
| `level` | `简单` / `中等` / `困难`；评测难度归类把 `困难` 映射为 `复杂` |
| `question` | 发给模型的自然语言问题 |
| `reference_cypher` | 字符串（单条）或字符串列表（组合题） |
| `reference_rows` | 参考查询在该图谱快照上的结果，逐行保存 |
| `reference_result_count` | 参考行数，必须等于 `len(reference_rows)` |
| `reference_nonempty` | 参考结果非空 |
| `reference_error` | 参考执行错误；正常题集必须为 `null` |
| `reference_complete` | 参考查询未被截断 |
| `reference_unique` | 参考行按整行去重后无重复 |
| `reference_ordered` | 结果是否按题意要求顺序；多行题必须为 `true` |
| `reference_scope` | 结果范围说明，例如“全集或精确计数” |
| `reference_evidence_counts` | 仅组合题使用：REST/MQ/链路的证据行数与目标 |
| `graph_versions` / `graph_version_counts` | 冻结参考答案时的图谱版本分布 |
| `graph_fingerprint` | 上述计数排序后的 SHA-256，全题必须一致 |

约定：

- `H01/H02/H07/H08` 是组合题，`reference_cypher` 必须是 2–4 条参数化查询，
  且 `reference_evidence_counts` 必须包含 `rest_rows`、`service_dependency_rows`、
  `witnessed_chain_rows`、`mq_targets` 四个键；
- 组合题参考结果固定为两列（REST 侧一列、MQ 侧一列），列名允许不同但语义必须对应；
- `reference_rows` 是评分基准，**不能作为提示传给被评测模型**。`tools/evaluate.py`
  只把 `question` 和 `level` 传给 `ask()`；
- 修改题集必须重新执行参考查询、重算 `reference_result_count` 和指纹，并运行
  `--check-dataset`；不要手工拼接旧指纹。

## 5. 评分口径

自动判定使用集合语义，而不是逐行分组：

```python
def canonical(rows):
    return {
        json.dumps(sorted(str(value) for value in row.values()), ensure_ascii=False)
        for row in rows
    }
```

因此判定**忽略列名、列序和等价写法**，也**折叠重复行**。这是有意的评分口径：
题目要求的是目标集合/精确计数，同一目标被多条路径重复返回不应算错；
历史 30/30 基线也是按此口径统计的，不能改成分组多重集，否则会破坏可比性。

为了保留证据，报告同时记录三个诊断字段：

- `actual_row_count`：模型返回的原始行数；
- `actual_unique_row_count`：按整行去重后的行数；
- `duplicate_rows_collapsed`：被集合语义折叠掉的重复行数。

这些字段只用于人工复核，不改变 `correct`。

三态判定：

| 情况 | `correct` | `automatic_match` | `failure_reason` |
| --- | --- | --- | --- |
| 执行错误 | `false` | `false` | 错误文本 |
| 结果被截断或链路不完整 | `false` | 按集合比较 | “结果已截断或完整链路无法验证” |
| 结果与参考集合一致 | `true` | `true` | `null` |
| 结果不一致 | `null`（待人工复核） | `false` | “自动比对不一致；需人工复核语义、结果范围与返回列” |

`score` 的规则固定在 `evaluation/benchmark.py:SCORE_RULE`：只有 30 题全部尝试、
每题都有 `true`/`false` 结论、且运行前后图谱指纹未漂移时才计分；否则为 `null`。
`needs_review`、`pending` 和 `graph_drift` 都会让分数保持 `null`。

## 6. 运行独立题

前置条件（不在本模块内）：

- 可访问的 Neo4j 图谱，且其版本计数与题集指纹一致；
- 根目录 `.env` 中配置 `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD`；
- 至少一个模型密钥（`DEEPSEEK_API_KEY` / `GLM_API_KEY` / `KIMI_API_KEY`）。

典型命令：

```bash
# 新跑一遍，默认写 evaluation/latest_run.json
python -m tools.evaluate

# 保留已完成的逐题记录，只补未跑的题
python -m tools.evaluate --resume --output independent_2026-11-13.json

# 只重跑指定题，其余记录保留
python -m tools.evaluate --resume --output independent_2026-11-13.json --only S01 M04

# 覆盖一个自定义结果文件
python -m tools.evaluate --output independent_2026-11-13.json --force
```

行为约定：

- `--output` 必须是 `evaluation/` 内的纯文件名；默认 `latest_run.json`。自定义文件已存在时
  必须用 `--resume` 或 `--force`，避免静默覆盖证据；
- `--resume` 会读取旧报告，校验 `dataset_sha256`，题集变化时拒绝续跑；只保留已尝试且
  题号仍存在的记录；
- 启动时先计算图谱指纹，与 `questions.json` 中的所有题比对；不一致直接中止，提示
  “图谱版本计数与参考答案不同；请先重建参考答案，不能沿用旧标准”；
- 没有模型密钥时**不会**清空已有逐题记录，只补 `pending` 并写
  `run.status = "no_key"`；
- 每跑完一题就原子写一次报告（tmp → replace），中断时尽力写
  `run.status = "interrupted"`；
- 运行状态：`running` / `finished` / `partial` / `no_key` / `interrupted`；
- 报告顶层保存 `dataset_sha256`、`dataset_file`、`run` 元数据、前后指纹、逐题记录和
  汇总字段，便于从报告本身追溯题目版本与运行范围。

## 7. 运行模拟教师题

```bash
# 只校验题集，不连接图谱
python -m tools.mock_teacher_eval --validate

# 只执行参考查询，验证图谱与参考结果
python -m tools.mock_teacher_eval --gold-only --output gold_only_2026-11-13.json

# 用已知正确查询驱动整条流水线（不调用真实模型）
python -m tools.mock_teacher_eval --local-oracle --output local_oracle_2026-11-13.json

# 真实模型；必须使用独立文件名，不能覆盖 results.json
python -m tools.mock_teacher_eval --output external_2026-11-13.json
```

行为约定：

- 30 条 `CASES` 内联在工具中（题号、难度、问题、gold query），gold query 不带 LIMIT，
  由执行层自行控制返回规模；
- 每次运行都会比对 `CASES` 与已提交的
  `evaluation/mock_teacher_2026-10-14/questions.json`：
  - 内容一致：不重写文件；
  - 内容不同：直接报错，**不会静默覆盖已归档题集**；需要人工同步；
- `results.json` 是本地替身基线的保留文件名；外部模型运行必须用其他 `--output`；
- 默认输出已存在时必须 `--resume` 或 `--force`；`--resume` 还要求
  `evaluation_mode` 和 `cases_sha256` 一致；
- 结果逐题原子写，顶层记录 `started_at_utc`、`updated_at_utc`、`finished_at_utc`、
  `status`、`planned_count`、`complete`、前后指纹、`graph_stable`/`graph_changed`、
  `cases_sha256` 和检查点；
- 每项记录 gold 查询/参考行、模型实际查询/结果、`answer_complete`、错误和阶段信息；
- 图谱在中途变化时会记录 `graph_changed_between_segments`，该次运行不能当作稳定基线。

## 8. 对比修复前后结果

```bash
python -m tools.compare_mock_teacher \
  --before results_before_validation_and_chain_fix.json \
  --after results.json \
  --output comparison_2026-11-13.json
```

工具要求两次运行都是 `local_oracle` 模式，题号集合、题目和 gold 查询完全一致；
逐行比较使用完整行的多重集（`Counter`），这会保留重复行次数，与自动判定的集合语义
不同——对比工具回答的是“流水线输出是否逐行稳定”，不是“目标集合是否正确”。

输出包含 `graph_stable`、`same_graph_between_runs`、`all_reference_rows_same`、
`before_passed`、`after_passed` 和逐题明细。图谱不稳定时 `after_passed` 为 `null`。
默认文件名 `results.json` / `results_before_validation_and_chain_fix.json` 在交付仓库中
并不存在，这是有意的证据边界；缺少文件时工具会提示先用
`python -m tools.mock_teacher_eval --local-oracle --output ...` 生成。

## 9. 证据与归档策略

入库：

- 两套 `questions.json`；
- 题集校验/评分代码；
- 简短公开报告（`试题与测试报告.md`、`deepseek_baseline_report.md`）；
- 本 README。

不入库：

- 模型原始响应、逐题完整 JSON、`latest_run.json`、`results.json`、`comparison.json`；
- 任何包含密钥或内部地址的运行日志。

历史报告中的数字只代表当时的图谱快照与模型版本；不要用新图谱覆盖旧基线。
需要新的基线时，用带日期的 `--output` 另存，并在报告中写明日期、图谱指纹和模型。

## 10. 与其它工作包的兼容契约

`tools/evaluate.py` 保留以下公开名字，供后续合并的 05 审计包和 03 测试包复用：

- `ROOT`：仓库根目录；
- `DEST`：默认结果路径 `evaluation/latest_run.json`；
- `QUESTIONS`：加载后的独立题集；
- `canonical(rows)`：集合语义判定；
- `snapshot_fingerprint(graph)`：图谱指纹；
- `save_report(records, cfg, graph_before=None, graph_after=None, ...)`：
  前四个位置参数保持兼容，新增参数全部为关键字参数。

05 的 `review_evaluation.py` 依赖 `DEST`、`save_report` 和四个位置参数；
`check_mq_targets.py`、`regression_live.py` 依赖 `ROOT`。
03 的 `tests/test_core.py` 依赖 `canonical`，并另外依赖 05 的 `build_evaluation`；
本分支不包含 `build_evaluation.py`，因此 03 的完整测试要在 05 合并后才能运行。

## 11. 维护流程

1. 新增/修改题目：先在真实图谱上执行参考查询，保存逐行结果、行数、唯一性、顺序和
   指纹；组合题还要保存四类证据计数。
2. 更新 `evaluation/questions.json`，运行 `python -m tools.evaluate --check-dataset`；
3. 运行 `python -m pytest tests/test_evaluation.py -q`；
4. 重新生成一份带日期的基线，不要覆盖旧报告；
5. 如修改题集，`--resume` 会因 `dataset_sha256` 变化被拒绝，这是预期行为。

## 12. 已知限制

- 图谱指纹只覆盖 `n.图谱版本` 的节点计数，不能证明节点/关系属性完全未变；
  参考答案仍以冻结时的图谱快照为准。
- 自动判定只比较“目标值集合”，不验证返回列名、列序和重复次数；这些由人工复核和
  诊断字段补充。
- `H02` 的 `reference_evidence_counts.rest_rows` 为 11，而参考结果去重后为 10 行，
  这是服务级去重的预期差异，不是数据错误。
- `H03`/`H08` 在历史真实模型运行中出现过额外展示行（分别 +1/+5），属于展示层瑕疵；
  相关题目的正确性仍以目标集合判定和人工复核为准。
- 本分支不包含 `data/`、`agent/` 之外的运行依赖，也不包含 05 的参考构建脚本；
  实跑前需要完整项目环境。
