# 05_audit_regression 工作包

**分工编号：05**

**工作包：审计与回归**

## 负责范围

1. 校验题集，并从只读图谱生成参考答案候选。
2. 审计新旧参考答案及其图谱指纹。
3. 分析评测结果、比较多次运行，并遵守题集与图谱稳定性评分条件。
4. 对保存的 Cypher 执行只读回放，支持续跑和失败题重试。
5. 记录人工复核结论并重算成绩。
6. 检查 MQ 服务依赖、路由键以及可验证的闭合消费链。

## 实现入口

实现模块位于 `work_packages/audit_regression_05/`。从仓库根目录运行：

```bash
python -m work_packages.audit_regression_05.<module> [arguments]
```

工具使用说明和安全边界见 `evaluation/README.md` 的 05 章节。`tools/` 下同名模块继续
保留兼容入口，既有命令和导入路径可以继续使用。
