# 05_audit_regression 工作包

本目录是项目分工中的 **05_audit_regression** 实现，负责参考答案审计、运行结果分析、
运行对比、人工复核、历史 Cypher 回放和 MQ 目标核对。实现按工作包集中管理，评测题集
和通用评测执行仍位于仓库的 `evaluation/` 与 `tools/`。

## 命令入口

从仓库根目录运行：

```bash
python -m work_packages.audit_regression_05.build_evaluation --check-only
python -m work_packages.audit_regression_05.audit_references --help
python -m work_packages.audit_regression_05.audit_results --help
python -m work_packages.audit_regression_05.compare_runs --help
python -m work_packages.audit_regression_05.regression_live --help
python -m work_packages.audit_regression_05.review_evaluation --help
python -m work_packages.audit_regression_05.check_mq_targets --help
```

此前使用的 `python -m tools.<工具名>` 入口保留为兼容转发层。

## 文件

- `build_evaluation.py`：检查题集或只读重建参考答案候选。
- `audit_references.py`：对比旧题集与候选参考答案。
- `audit_results.py`：分析一次运行中的失败和待复核题。
- `compare_runs.py`：在题集和图谱稳定边界内对比运行。
- `regression_live.py`：用保存的 Cypher 做只读回放。
- `review_evaluation.py`：记录人工结论并按评分规则重算报告。
- `check_mq_targets.py`：核对服务依赖、路由键和 MQ 闭合链证据。
- `_audit_common.py`：离线安全的共享辅助函数。

图谱访问仅发生于明确请求在线执行的入口；离线检查不需要 Neo4j 或模型密钥。运行产物
默认保存在 `evaluation/`，遵守仓库的 `.gitignore` 规则。
