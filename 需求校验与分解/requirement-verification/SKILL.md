---
name: requirement-verification
description: |
  需求校验入口Skill - 分阶段执行需求查询、转换、并行语义分类和覆盖分析的完整工作流。

  使用场景：
  - 用户要求"需求校验"、"校验需求"、"需求分类"
  - 用户提供了版本ID（vrcId）、所属领域（originBelongedAreas）、系统来源（sourceSystem）、角色视图（roleId）等，需要先查询需求再进行分类
  - 用户要求"查询并分类需求"、"导出需求并进行分类"

  工作流程：
  1. 第一阶段：调用 das-manage-transformer 查询DAS需求并转换为标准树形结构
  2. 第二阶段：调用 requirement-classifier，按顶层IR划分并行子Agent任务，由大模型对各IR完整子树进行语义分类（一级需求、二级需求），合并后分析规则覆盖情况
  3. 输出分类完成的需求JSON文件和覆盖情况分析

  第二阶段的每个IR子任务均可视化展示执行状态；禁止生成关键词、固定映射或硬编码分类脚本。
---

# 需求覆盖校验入口

## 目标

依次执行：

1. `das-manage-transformer`：查询 DAS 需求并转换为 IR/SR/AR 树形 JSON。
2. `requirement-classifier`：按顶层 IR 并行调用多个子 Agent，由大模型逐节点完成一对一或一对多语义分类，再统一合并并统计规则覆盖情况。

本入口只负责编排、参数透传、阶段门禁和产物检查。产品线映射及规则名称只以 `requirement-classifier/references` 中的 JSON 为准。

## 输入

必须提供：

- `productLine`：只传给 `requirement-classifier`，不得传给 DAS 查询接口。
- DAS 查询参数：至少包含 `vrcAndBoardArray.vrcId`、`originBelongedAreas`、`sourceSystem`、`roleId`。

从前端 `payload_json` 原样读取并透传已有且有值的 DAS 查询字段，包括但不限于：

- `vrcAndBoardArray`
- `idList`
- `pageSize`
- `currentPage`
- `originBelongedAreas`
- `sourceSystem`
- `roleId`
- `isFilterDelete`
- `searchCondition`

不得遗漏 `idList`，也不得自行补传前端不存在的可选字段。

## 本次运行时间戳与文件名

流程开始时只生成一次本地时间戳 `runTimestamp`，格式固定为 `YYYYMMDD_HHMMSS`。本次运行的四个输出文件必须复用同一个时间戳：

- `{transformedFile}` = `requirements_transformed_{runTimestamp}.json`
- `{classifiedFile}` = `requirements_classified_{runTimestamp}.json`
- `{reportJsonFile}` = `requirements_coverage_report_{runTimestamp}.json`
- `{reportMdFile}` = `requirements_coverage_report_{runTimestamp}.md`

时间戳插在基础文件名和扩展名之间。后续所有命令和文件检查均使用以上实际文件名，不得再生成无时间戳的四个旧文件名。

## 执行流程

### 阶段 1：查询与转换

1. 从 `payload_json` 排除 `productLine`，构造 DAS 查询请求。
2. 调用 `das-manage-transformer` 完成查询、原始响应落盘、兼容解析和树形转换；执行 `transform_from_mcp.py` 时显式将输出路径指定为 `{transformedFile}`，不得使用脚本的无时间戳默认输出名。
3. 确认生成 `{transformedFile}`。
4. 确认每个节点保留 `categorys`、`name`、`description`、`requirementDetailInfo`、`logicid`、`children`，并保留顶层 `statistics`。

阶段 1 未完成时不得进入阶段 2。

### 阶段 2：大模型语义分类

1. 将 `{transformedFile}` 路径、`runTimestamp`、`{classifiedFile}`、`{reportJsonFile}`、`{reportMdFile}` 和用户原始 `productLine` 传给 `requirement-classifier`；分类阶段不得自行生成新的时间戳。
2. 完整遵守 `requirement-classifier` 的分类执行边界和双向语义复核流程。
3. 本入口只调用一次 `requirement-classifier`，不得自行读取 IR 后再次创建子 Agent；所有子 Agent 调度只由 `requirement-classifier` 主 Agent 负责。
4. `requirement-classifier` 只执行一次第 3.1 节给出的固定相对路径 `grep` 查询，不分页读取完整 JSON，也不尝试其它命令定位 IR。
5. 随后直接执行随包的 `scripts/prepare_ir_tasks.py`，一次性生成 `requirements_ir_work/ir_manifest.json`、每个 IR 的独立子树文件、统一叶子执行规则、产品线规则副本及短派发 Prompt；节点数超过 40 或文本超过 60KB 的大 IR 同时机械切分为每批最多 20 个节点的语义批次。任务队列只从该清单初始化一次。
6. 固定每批最多 5 个任务；例如 6 个 IR 按 `5 + 1` 两批执行。每个 IR 使用唯一 `taskKey`；`running` 和已通过校验的 `completed` 禁止再次创建，失败最多重试 1 次。
7. 每个 IR 子 Agent 均为叶子 Agent。主 Agent 必须完整读取清单指定的 `promptFile` 或 `retryPromptFile`，将全文逐字放入创建子 Agent 工具的 Prompt/Message 字段；不得压缩、裁剪、概括、改写、只传 Prompt 文件路径或追加前后缀。子 Agent 按短 Prompt 完整读取 `workerInstructionFile`、`ruleFile` 和当前输入：小 IR 读取 `inputFile`，大 IR 读取批次索引并按顺序逐批处理。不得由大模型读取完整 `{transformedFile}`、搜索其它 IR 或继续创建子 Agent；固定校验脚本对源文件的机械读取不受此限制。无法原样派发时返回 `BLOCKED_PROMPT_NOT_VERBATIM`，禁止发送摘要。
8. 子 Agent 对自己子树的每个节点返回一条紧凑的 `logicid + classification` 记录；不回传完整原始子树。大 IR 每批最多 20 个节点，仍逐节点由模型理解，只执行“需求→规则”和“规则→当前IR”两轮。严禁创建、修改、读取或执行 `extract_nodes.py`、`classify_ir.py`、`classify_ir_complete.py`、`classify*.py` 及其它提取、关键词映射、批量分类或结果拼装脚本；叶子阶段除固定校验命令外禁止调用终端。
9. 子 Agent 写入 `resultFile` 后先逐条做语义自检，再亲自执行短 Prompt 中的 `merge_and_report.py --validate-result ... --expected-attempt ...` 命令；只有输出 `status: VALID` 才能返回完成。该校验同时检查工作区 Python 文件基线，发现分类期间新增或修改脚本时直接拒绝。主 Agent 收到回执后必须再次执行同一校验；非法结果只重试当前 IR 一次，主 Agent 不得修补或猜测。
10. 全部 IR 结果完成并通过单结果校验后直接执行随包的 `scripts/merge_and_report.py`；禁止主 Agent 临时编写提取、合并或报告脚本。全局反向复核完成后，使用同一脚本加 `--global-audit-completed` 生成最终文件。
11. 最终 `{classifiedFile}` 必须是固定脚本以原始完整需求树为底稿合并的结果：保留全部顶层字段、`statistics`、完整 `data` 数组和所有 IR/SR/AR 节点及原始字段，并保证每个节点都有 `classification` 数组。

入口 Agent 和所有子 Agent 均不得自行实现分类脚本，不得生成或执行 `extract_nodes.py`、`classify*.py`、关键词匹配、正则匹配、固定映射或默认兜底。入口 Agent 只允许执行随包的 `prepare_ir_tasks.py` 和 `merge_and_report.py`；叶子子 Agent 只允许执行短 Prompt 中的固定单结果校验命令。两个随包脚本只做机械切分、校验、合并和统计。

若工作区已有旧分类脚本或临时 `merge_and_report.py`，忽略且禁止执行。若当前流程开始生成新脚本，立即停止并改为执行 Skill 随包的固定脚本。

入口 Agent 不得与 `requirement-classifier` 重复调度同一批 IR。规划文本不代表子 Agent 已创建；只有运行记录中实际出现对应 IR 的子 Agent 任务，才算创建成功。

### 并行与串行边界

| 步骤 | 执行方式 | 原因 |
|---|---|---|
| DAS查询 → 树形转换 | 串行 | 转换依赖完整查询结果 |
| 产品线映射和规则预加载 | 可与DAS查询并行 | 只依赖用户输入的 `productLine` |
| 各顶层IR语义分类 | 最多5个并行，由 `requirement-classifier` 唯一调度 | IR子树之间无写入依赖 |
| 各IR内部反向语义复核 | 并行 | 每个子 Agent 只检查自己的IR |
| 全局未覆盖规则复核 | 最多5个并行，由 `requirement-classifier` 唯一调度 | 各规则组可独立寻找证据 |
| IR结果合并、最终覆盖统计和报告 | 串行 | 必须基于唯一完整结果，避免重复或覆盖写入 |

### 阶段 3：覆盖结果检查

1. 将 `{classifiedFile}` 与 `{transformedFile}` 递归对照：确认顶层字段、`statistics`、`data` 长度、节点总数、全部 `logicid`、父子层级、节点顺序和所有原始字段均完整保留。
2. 确认每个 IR、SR、AR 节点都存在 `classification` 数组；无合法路径的节点允许为空数组 `[]`，计入未映射节点并继续检查。
3. 确认根对象就是完整需求树，不是子 Agent 结果列表、单个 IR、仅命中节点或带有 `results`、`classifiedSubtrees` 等额外包装的对象。
4. 确认报告中的 `classificationMethod` 为 `llm-semantic`。
5. 确认报告标记“需求到规则复核完成”“规则到需求复核完成”，且“使用关键词或硬编码分类”为 `false`。
6. 确认节点映射率、一级规则覆盖率和二级规则覆盖率分开统计。
7. 确认全部未覆盖一级、二级规则均列出。
8. 确认覆盖明细中的已覆盖规则填写真实 `命中需求logicid`。
9. 确认所有 IR 子任务均通过单结果合法性校验并标记为 `completed`，每个 `taskKey` 无重复创建，且报告记录实际执行模式、IR任务数和不大于 5 的最大并发数。

## 固定输出

| 文件 | 内容 |
|---|---|
| `requirements_transformed_{runTimestamp}.json` | 阶段 1 的标准树形需求 |
| `requirements_classified_{runTimestamp}.json` | 以 `{transformedFile}` 的完整根对象为底稿，按 `logicid` 合并全部 IR 紧凑分类结果；保留全部顶层字段、`statistics`、完整 IR/SR/AR 层级和原始字段，每个节点均含 `classification`，无合法路径时为 `[]` |
| `requirements_coverage_report_{runTimestamp}.json` | 并行执行信息、节点映射、一级/二级覆盖统计、未覆盖项和命中明细 |
| `requirements_coverage_report_{runTimestamp}.md` | 便于查看和汇报的覆盖报告 |

不生成或交付分类脚本、关键词表、默认映射表、待确认清单。

## 用户界面最终结果

最终回复必须完整读取 `{reportJsonFile}` 中的 `未覆盖一级需求` 和 `未覆盖二级需求`，并在用户界面分别输出两个列表。每条记录固定展示以下两个字段：

| 一级需求 | 二级需求 |
|---|---|

- `未覆盖一级需求` 保持逐条展示，`二级需求` 固定显示为 `null`；显示顺序与报告数组一致，行数必须等于该数组的实际长度。
- `未覆盖二级需求` 按 `一级需求` 分组：相同 `一级需求` 只显示一行，把该组全部 `二级需求` 按报告原顺序写入同一个单元格，格式固定为 `二级需求1、二级需求2、二级需求3`，不得添加括号；只有一项时直接显示该二级需求原文。
- 分组行顺序按各 `一级需求` 在报告数组中首次出现的顺序。每条未覆盖二级需求必须恰好进入一个分组；分组行数必须等于该数组中唯一 `一级需求` 的数量，展开全部分组后的二级需求总数必须等于该数组的实际长度。
- 无论清单多长，都不得使用“等”“等等”“其余”“部分”“省略”或省略号代替记录，也不得只让用户查看附件。单个表格过长时，按连续记录区间拆成多个列表块（例如“第 1–50 条”“第 51–100 条”），表内仍只保留上述两个字段，直到全部记录输出完毕。
- 若某级没有未覆盖项，明确输出“无”。完整清单优先于过程说明；为控制长度，可省略非必要过程复述，但不得省略任何未覆盖项。
- 最终回复同时列出本次四个带时间戳的实际输出文件名。

## 统计口径

- 节点映射率：已获得合法非空分类的节点数 / 全部需求节点数。
- 一级规则按唯一 `一级需求` 统计。
- 二级规则按唯一 `(一级需求, 二级需求)` 统计。
- 同一规则被多个节点命中时，覆盖数只计 1，但保留全部命中节点 `logicid`。
- `二级需求: null` 只计一级覆盖，不计二级覆盖。
- 未覆盖一级数和未覆盖二级数均为 0 时才判定“完整覆盖”。
- 覆盖率是需求清单对规则目录的真实覆盖结果，不得为提高数字加入弱相关路径。

## 完成门禁

交付前逐项确认：

1. `productLine` 只参与规则选择，DAS 查询参数无遗漏。
2. 流程严格按“查询 → 转换 → 按IR并行语义分类 → 合并 → 全局复核 → 覆盖统计”执行。
3. 未创建、执行或交付任何分类脚本，未使用关键词、相似度或固定兜底。
4. 每个需求节点均包含 `classification` 数组；无法合法映射时保留 `[]` 并计入未映射节点，不伪造结果。
5. 一对多结果覆盖节点中全部独立要求，且每条都真正相关。
6. 分类名称均来自实际加载的规则 JSON。
7. 一级、二级均满足“已覆盖数 + 未覆盖数 = 总数”。
8. 覆盖报告分别给出节点映射率、一级覆盖率、二级覆盖率及全部未覆盖项。
9. `ir_manifest.json` 只初始化一次；每个 IR 使用与脚本生成文件逐字一致的短派发 Prompt，子 Agent 完整读取明确指定的执行规则和产品线规则，小 IR 读取完整子树，大 IR 按语义批次逐批直接判断，在返回前自行校验为 `VALID`，主 Agent 再次校验；所有 IR 结果完整覆盖各自节点，每个 `taskKey` 无重复创建，最大并发数不超过 5。
10. 工作区 Python 文件与任务清单记录的基线一致，不存在分类期间新增或修改的提取、分类、关键词映射或结果拼装脚本；叶子子 Agent 除固定单结果校验命令外未执行其它终端命令。
11. `{classifiedFile}` 与 `{transformedFile}` 的顶层字段、IR 数量、递归节点数、全部 `logicid`、父子层级、节点顺序及原始字段完全一致，且每个节点都新增了 `classification` 数组。
12. 最终三个分类/报告文件由随包固定合并脚本生成，报告中的“规则到需求复核完成”为 `true`；运行过程中没有临时编写提取、合并或报告脚本。
13. `{transformedFile}`、`{classifiedFile}`、`{reportJsonFile}`、`{reportMdFile}` 均存在，四者文件名复用同一个 `runTimestamp`，且三个 JSON 文件可正常解析；不存在本次运行生成的无时间戳旧输出名。
14. 用户界面已完整展示全部未覆盖一级和二级需求；未覆盖一级需求按报告数组顺序逐条展示，二级字段为 `null`；未覆盖二级需求按一级需求首次出现顺序分组，同一一级需求只占一行且全部二级需求按报告原顺序集中显示。一级需求行数、二级需求分组行数及展开后的二级需求总数均符合上述规则，且无任何省略表达。

## 状态处理

| 状态 | 处理 |
|---|---|
| `DONE` | 所有阶段和完成门禁均通过 |
| `DONE_WITH_CONCERNS` | 仅允许非分类语义问题；修正产物或统计后再交付 |
| `NEEDS_CONTEXT` | 补充缺失的 DAS 查询参数或 `productLine` |
| `BLOCKED` | 无法查询或无法解析产品线映射，停止流程 |
| `BLOCKED_IR_TASK_FAILED` | 某个 IR 子任务重试后仍失败，停止合并并列出失败 IR，禁止交付部分结果 |
