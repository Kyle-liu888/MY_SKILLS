# MY_SKILLS

面向 DAS 需求管理的 Skill 集合，支持需求查询、语义分类、覆盖校验和详细描述生成。

## Skill 一览

| Skill | 功能 |
| --- | --- |
| [das-manage-transformer](需求校验与分解/das-manage-transformer/SKILL.md) | 通过 MCP 查询 DAS 需求，转换为 IR → SR → AR 树形 JSON，保留需求标识和层级信息。 |
| [requirement-classifier](需求校验与分解/requirement-classifier/SKILL.md) | 按产品线规则对需求进行语义分类，支持多个 IR 子树并行处理，输出分类结果、覆盖率及未覆盖规则报告。 |
| [requirement-verification](需求校验与分解/requirement-verification/SKILL.md) | 需求校验入口：串联查询转换、语义分类与覆盖分析，统一管理输出文件。 |
| [requirement-description-generator](需求校验与分解/requirement-description-generator/SKILL.md) | 结合需求原文、层级上下文和产品线经验包，逐条生成纯文本详细描述，标注缺失信息，并通过 MCP 批量写回系统。 |
| [requirement-decomposition](需求校验与分解/requirement-decomposition/SKILL.md) | 需求分解入口：串联查询转换与描述生成入库，完成后刷新前端并汇总入库结果和人工复核清单。 |

## 主要流程

- **需求校验**：`requirement-verification` → `das-manage-transformer` → `requirement-classifier`。
- **需求分解**：`requirement-decomposition` → `das-manage-transformer` → `requirement-description-generator`。

执行时需提供 DAS 查询参数，并显式指定产品线 `productLine`；查询、入库及页面刷新依赖对应的 MCP / WebMCP 工具。具体参数与执行约束见各目录的 `SKILL.md`。

另附 [SKILL 调优经验总结](SKILL调优经验总结.md)，记录优化实践。
