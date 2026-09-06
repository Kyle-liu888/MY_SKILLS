---
name: requirement-classifier
description: 使用大模型直接理解 IR/SR/AR 需求语义，将每个节点映射到所选产品线规则中的一个或多个“一级需求→二级需求”路径，并通过“需求→规则”和“规则→需求”双向语义复核统计覆盖情况。存在多个顶层 IR 时，为每个 IR 及其完整 SR/AR 子树创建独立子 Agent 任务并并行分类，最后统一合并和统计。用于需求分类、需求覆盖校验和未覆盖规则分析；分类决策禁止使用关键词、正则、字符串包含、固定映射、相似度阈值或生成的分类脚本。
---

# 需求语义分类与覆盖校验

## 目标

依靠大模型逐节点理解需求的业务含义，完成一对一或一对多分类；多个顶层 IR 由多个子 Agent 并行处理，主 Agent 统一合并并反向检查规则覆盖。覆盖率必须反映真实语义结果，不得作为需要刻意提高的目标。

## 分类执行边界

### 必须由大模型完成

- 理解需求表达的动作、指标、约束、条件和预期结果。
- 拆分一个节点中的并列业务断言。
- 判断每个断言与规则完整业务含义是否一致。
- 选择一级或二级规则，并完成双向语义复核。

### 严禁用于分类决策

- 生成或执行 `classify*.py`、`classify_node` 等分类脚本或分类函数。
- 将规则名称拆成关键词后做包含匹配。
- 使用正则、词频、编辑距离、字符串相似度、向量阈值或分数阈值自动决定分类。
- 使用 `if/else`、字典、模板或固定路径硬编码分类关系。
- 未命中时统一回退到某个固定一级需求。
- 因节点数量较多而跳过大模型判断，或让程序批量填充 `classification`。

本 Skill 只允许执行两个随包提供的机械脚本：

- `scripts/prepare_ir_tasks.py`：分类前按 `irIndex` 提取完整 IR 子树，复制本次产品线规则；大 IR 机械切分为仅保留当前节点及直接父子语境的语义批次，并生成短派发 Prompt 与统一叶子执行规则文件。该脚本只切分数据，不做分类。
- `scripts/merge_and_report.py`：逐个校验子 Agent 结果，并在全部合法后合并分类、生成完整需求树和覆盖报告。

这两个脚本不得根据需求文本选择分类。禁止临时生成其它提取脚本、分类脚本、合并脚本或报告脚本；禁止为了完成本流程改写上述两个固定脚本。

## 硬性约束

1. 只从 `references/product_line_mapping.json` 选择规则文件，只从选中的规则 JSON 读取分类名称。
2. 每个需求节点必须包含 `classification` 数组；找到合法路径时写入一个或多个结果，无合法路径时保留空数组 `[]`。
3. 一个节点可对应任意数量的强相关规则，不设一对多数量上限。
4. 每条路径必须对应当前节点中的一个独立业务断言；仅主题相近或词面相同不得追加。
5. 父子节点只提供语境，不得机械继承、复制或汇总分类。
6. 只能确定一级时，输出合法一级路径并将 `二级需求` 设为 `null`。
7. 无法为节点找到合法路径时，不得伪造默认分类。完成全量语义复核后仍无法映射，将该节点的 `classification` 写为 `[]`，继续处理其它节点并正常统计覆盖情况。
8. 不生成待确认清单，也不输出所选规则文件之外的名称。
9. 并行子 Agent 只处理分配给自己的 IR 子树，不得修改全局需求树或最终报告。
10. 所有并行任务必须使用同一个产品线、同一个规则文件和同一套语义分类标准。
11. 只有执行本 Skill 的主 Agent 可以创建子 Agent；IR 分类子 Agent 和规则复核子 Agent 均为叶子 Agent，禁止继续创建子 Agent 或重新调用本 Skill。
12. 同时运行的子 Agent 最多为 5 个。任务队列只初始化一次，每个任务使用唯一 `taskKey` 防止重复创建。
13. 主 Agent 不得通过反复读取完整 JSON、尝试 PowerShell/Python/jq/find 等不同命令来定位 IR；只执行第 3 节规定的固定查询和固定脚本。
14. 子 Agent Prompt 必须使用固定预处理脚本生成的短派发 Prompt。主 Agent 将该文件全文原样放入创建子 Agent 工具的 Prompt/Message 字段，不得压缩、裁剪、概括、加前后缀或手工重写；详细执行规则和产品线规则使用 Prompt 中的明确文件路径，由子 Agent 完整读取。
15. 子 Agent 写入结果后必须先做语义自检，再亲自运行固定合并脚本的单结果校验模式；只有输出 `status: VALID` 才能返回完成。子 Agent 返回后，主 Agent必须再执行同一机械校验，只有通过才能标记 `completed`。
16. 叶子子 Agent 禁止创建、修改、读取或执行 `extract_nodes.py`、`classify_ir.py`、`classify_ir_complete.py`、`classify*.py` 及任何节点提取、关键词映射、批量分类或结果拼装脚本。叶子阶段唯一允许执行的命令是短 Prompt 中完整给出的固定单结果校验命令。
17. `prepare_ir_tasks.py` 记录任务开始前的工作区 Python 文件基线；`merge_and_report.py` 校验时发现新增或修改的 Python 文件，或发现上述已知分类脚本，必须拒绝结果。

## 输入

- 需求树 JSON 文件路径。
- `productLine`：必须显式提供；缺失时返回 `NEEDS_CONTEXT`。
- 从 `requirement-verification` 调用时，同时接收其已生成的 `runTimestamp`、`{classifiedFile}`、`{reportJsonFile}` 和 `{reportMdFile}`，必须原样复用，不得另取时间。

独立调用本 Skill 时，在流程开始只生成一次本地时间戳 `runTimestamp`，格式固定为 `YYYYMMDD_HHMMSS`。统一定义：

- `{requirementsFile}`：实际传入的需求树 JSON 路径；
- `{requirementsFileName}`：`{requirementsFile}` 的文件名；
- `{classifiedFile}` = `requirements_classified_{runTimestamp}.json`；
- `{reportJsonFile}` = `requirements_coverage_report_{runTimestamp}.json`；
- `{reportMdFile}` = `requirements_coverage_report_{runTimestamp}.md`。

同一次运行的三个分类/报告输出必须复用同一时间戳；从入口调用时，该时间戳还必须与 `requirements_transformed_{runTimestamp}.json` 一致。时间戳插在基础文件名和扩展名之间，不得生成无时间戳的旧输出名。

节点的主要语义字段为 `name`、`description`、`requirementDetailInfo`，辅助字段为 `categorys`、`logicid`、`children`。

## 执行流程

### 1. 解析产品线

读取 `references/product_line_mapping.json`，依次匹配：

1. `packages` 键；
2. `canonical_name`；
3. 忽略大小写以及空格、下划线、连字符差异后的名称；
4. `aliases`。

只加载命中项的 `rule_file`。未命中或文件不存在时返回 `BLOCKED`，禁止默认使用其它规则文件。

### 2. 完整理解规则目录

遍历规则文件的 `data`，保留原文并建立合法路径集合：

- 一级路径：`一级需求`
- 二级路径：`(一级需求, 二级需求)`

先由大模型理解每条完整路径表达的业务意图和边界，再开始需求分类。不得把规则名称拆词后直接匹配。

### 3. 固定定位 IR、预切分子树并创建子 Agent

只由主 Agent 调度，并且必须从 `{requirementsFile}` 所在的 workspace 目录执行以下步骤。

#### 3.1 一次查看全部 IR

只执行一次下面的固定查询。`path` 必须使用相对路径 `.`，不得写用户机器的绝对路径：

```json
{
  "tool_name": "grep",
  "args": {
    "context_after": 2,
    "include": "{requirementsFileName}",
    "path": ".",
    "query": "\"categorys\": \"IR\"",
    "result_mode": "content"
  }
}
```

该查询只用于快速可视化 IR 数量、名称和描述。不得改用 `read_file` 分页扫描完整需求树，也不得继续尝试 PowerShell、Python 临时代码、jq、find 或其它定位命令。

#### 3.2 一次生成 IR 任务文件

`{skillDir}` 表示当前已加载的 `requirement-classifier/SKILL.md` 所在目录，不是要原样传给命令行的文字；执行时直接用已知 Skill 目录替换，不得搜索或猜测。脚本路径相对本 Skill，输入输出路径相对当前 workspace：

```bash
python "{skillDir}/scripts/prepare_ir_tasks.py" --requirements "{requirementsFile}" --product-line "{productLine}" --output-dir "requirements_ir_work"
```

脚本一次性生成：

- `requirements_ir_work/ir_manifest.json`：唯一任务清单；
- `requirements_ir_work/rules/{规则文件名}.json`：从本 Skill 复制的本次唯一规则文件；子 Agent 与固定校验脚本读取同一文件；
- `requirements_ir_work/instructions/ir_worker_rules.md`：所有 IR 子 Agent 必须完整读取的统一语义分类、结果格式和自校验规则；
- `requirements_ir_work/tasks/ir_NNN_{logicid}.json`：每个文件只含一个 IR 及其完整 SR/AR 子树；
- `requirements_ir_work/semantic-batches/ir_NNN_{logicid}/`：仅在 IR 节点数超过 40 或子树文本超过 60KB 时生成；每批最多 20 个节点，每个节点保留自身及直接父子语义字段；
- `requirements_ir_work/prompts/ir_NNN_{logicid}.attempt_1.prompt.txt`：首轮短派发 Prompt；
- `requirements_ir_work/prompts/ir_NNN_{logicid}.attempt_2.prompt.txt`：唯一重试使用的短派发 Prompt；
- `requirements_ir_work/results/`：IR 分类结果目录；
- `requirements_ir_work/audit-results/`：全局反向复核补充结果目录。

主 Agent 只读取脚本标准输出或 `ir_manifest.json` 建队列，不再读取完整需求树寻找 IR。脚本对相同源文件重复执行时只复用原清单，不重新初始化任务；源文件变化时必须使用新的工作目录。

#### 3.3 按清单创建子 Agent

每个清单任务固定包含 `taskKey`、`irIndex`、`logicid`、`name`、`nodeCount`、`ruleFile`、`workerInstructionFile`、`validatorScript`、`inputFile`、`inputMode`、`semanticBatchIndexFile`、`semanticBatchCount`、`resultFile`、`promptFile`、`retryPromptFile`、两份 Prompt 的字节数与 SHA-256、`state` 和 `attempts`。一个任务始终对应一个完整 IR 子树，不拆成 SR 子 Agent。

严格执行：

1. 固定 `MAX_SUBAGENTS = 5`，当前批次大小为 `min(5, 剩余 pending 任务数)`；6 个 IR 按 `5 + 1` 两批执行。
2. 立即创建与本批任务数相同的子 Agent，并明确“子 Agent 1 处理 IR[i]、子 Agent 2 处理 IR[j]……”。不得反复输出“准备创建”或重新规划同一批。
3. 创建前将该任务 `attempts` 加 1；首次尝试完整读取一次 `promptFile`，第二次尝试完整读取一次 `retryPromptFile`。把读到的全文直接作为创建子 Agent 工具的 Prompt/Message 参数值；任务显示名使用工具的独立名称参数，不得写进 Prompt。
4. Prompt 参数与文件内容必须逐字一致，不得截断、概括、改写、转述、只传 Prompt 文件路径或追加任何前后缀。不得发送类似“读取某文件并完成分类”的自拟摘要。如果运行环境无法原样发送该短 Prompt，立即返回 `BLOCKED_PROMPT_NOT_VERBATIM`，禁止降级为摘要。
5. 创建成功后改为 `running`。只有通过第 3.4 节机械校验的 `completed` 任务永不重复创建。
6. 仅 `failed` 任务允许重试 1 次，`attempts` 最大为 2；重试仍失败则返回 `BLOCKED_IR_TASK_FAILED`。
7. 当前批全部结束后才启动下一批；等待期间不得重建清单或当前批。
8. 只有一个 IR 时由主 Agent 直接按相同 Prompt 处理并标记 `single-ir`；环境不支持子 Agent 时才允许 `serial-fallback`。

不得由主 Agent 手工拼接子 Agent Prompt。固定脚本生成的短派发 Prompt 必须包含：

- 本任务身份、唯一 `inputFile`、唯一 `resultFile`、节点数和实际尝试次数；
- 唯一 `workerInstructionFile` 和 `ruleFile`；
- `inputMode=full-subtree` 时读取唯一 `inputFile`；`inputMode=semantic-batches` 时读取唯一批次索引并按顺序逐批处理；
- 明确禁止创建或执行任何提取、分类、关键词映射或结果拼装脚本；
- 精确的固定机械校验命令与完成条件。

详细语义规则只保存在 `workerInstructionFile`；完整合法名称只保存在 `ruleFile`。每个非空分类必须从 `ruleFile.data` 的同一条树路径逐字复制。`一级需求` 只能来自规则 `data` 中的一级节点，`二级需求` 只能来自该一级直接子级或为 `null`；禁止把二级名称上移为一级，或跨路径拼接。

可视化任务名固定为 `IR[{irIndex}] {logicid} {name}`，展示 `pending → running → completed/failed`。只有运行记录中实际出现对应任务才标记为 `running`。

#### 3.4 每个结果立即校验

子 Agent 必须在返回前执行 Prompt 中同一条命令并得到 `status: VALID`。子 Agent 返回且 `resultFile` 存在后，主 Agent必须立即再次执行：

```bash
python "{skillDir}/scripts/merge_and_report.py" --requirements "{requirementsFile}" --manifest "requirements_ir_work/ir_manifest.json" --product-line "{productLine}" --validate-result "{resultFile}" --expected-attempt {attempts}
```

该模式只做机械校验，不生成最终文件。它必须确认：

1. 结果属于清单中的当前 IR，`irIndex`、`irLogicid`、`attempts` 和节点数一致；
2. 当前 IR 的每个 `logicid` 恰好返回一次，不缺失、不重复、不越界；
3. 每个非空分类只含 `一级需求`、`二级需求` 两个字段；
4. 两个字段保持正确层级，并且整条路径逐字存在于所选规则树。
5. 工作区未出现任务开始后新增或修改的 Python 脚本，也不存在 `extract_nodes.py`、`classify_ir.py`、`classify_ir_complete.py` 等禁止脚本。

退出状态为 0 且输出 `status: VALID` 时才将任务标记为 `completed`。校验失败时将任务标记为 `failed`，不得由主 Agent 修补或猜测分类；若 `attempts=1`，使用 `retryPromptFile` 覆盖原结果并重试一次，若 `attempts=2` 仍失败则返回 `BLOCKED_IR_TASK_FAILED`。恢复已有任务时也必须先校验现有结果，不能仅因文件存在或旧状态为 `completed` 就跳过校验。

### 4. 每个 IR 子 Agent 执行双向语义分类

每个子 Agent 都是禁止继续委派的叶子 Agent。子 Agent 只读取短派发 Prompt 明确指定的 `workerInstructionFile`、`ruleFile` 和当前输入。`full-subtree` 模式读取 `inputFile.subtree`；`semantic-batches` 模式读取批次索引并按 `batchFiles` 顺序逐批处理，每条记录已包含当前节点及直接父子语境。不得搜索工作区、查找其它说明文件、由大模型读取完整需求树或其它 IR；固定校验脚本对源文件的机械读取不受此限制。对其中每个节点同时读取：

- 当前节点全部非空语义字段；
- 直接父节点的语义字段；
- 直接子节点的语义字段；
- 当前节点在 IR/SR/AR 树中的位置。

始终以当前节点为主要证据。父节点用于确认所属主题，子节点用于理解概括性标题。分类不读取、不输出、不校验规则的“对象”层级，只比较一级需求和二级需求的完整语义。

先执行“需求 → 规则”：

1. 将当前节点拆成独立业务断言；没有并列要求时保持一个断言。
2. 对每个断言识别要求动作或指标、条件以及预期结果。
3. 比较断言与合法规则路径的完整含义，允许同义表达、上下位表达和行业常用表达不同。
4. 只有两者描述同一项要求或直接等价约束时才保留路径。
5. 排除背景、示例、引用标题、否定项及仅在父子语境中偶然出现的内容。
6. 对所有独立断言分别匹配，保留全部强相关路径并精确去重。
7. 明确命中二级规则时填写完整的一级、二级路径；只能可靠确定一级时填写 `二级需求: null`。

再执行“规则 → 当前 IR”反向复核：逐条理解规则业务含义，在当前 IR 子树中检查是否存在同义或间接表达；找到强语义证据时补入对应节点，只有弱相关时保持未覆盖。

为避免 IR[0] 等大子树进入思考循环，必须按输入树顺序执行：

1. 深度优先处理节点，每个节点首轮只判断一次。
2. 大 IR 按预生成的语义批次顺序处理，每批最多 20 个节点；每个节点仍由当前子 Agent 直接理解，禁止用程序提取、关键词表或固定映射批量处理。
3. 禁止重复复述完整子树、重复总结规则范围差异或重新开始首轮分析。
4. 只允许两轮：首轮“需求 → 规则”，第二轮“规则 → 当前 IR”；第二轮结束立即写结果。
5. 输出记录数必须等于输入文件的 `nodeCount`。

子 Agent 不返回完整原始子树，只将以下紧凑结果写入清单指定的 `resultFile`：

```json
{
  "irIndex": 0,
  "irLogicid": "1003409",
  "attempts": 1,
  "classifications": [
    {
      "logicid": "1003409",
      "classification": [
        {
          "一级需求": "规则原文",
          "二级需求": "规则原文或 null"
        }
      ]
    },
    {
      "logicid": "1003410",
      "classification": []
    }
  ]
}
```

`attempts` 必须填写主 Agent 传入的当前实际尝试次数。`classifications` 必须恰好包含本 IR 子树的全部节点，每个 `logicid` 恰好一次；无合法路径的节点也必须显式返回 `classification: []`。不得复制 `name`、`description`、`requirementDetailInfo`、`children` 等原始大字段，不再返回 `classifiedSubtree` 或 `ruleHits`。

优先由子 Agent 直接写入 `resultFile`，最终只返回：

```json
{
  "status": "completed",
  "irIndex": 0,
  "irLogicid": "1003409",
  "resultFile": "requirements_ir_work/results/ir_000_1003409.result.json",
  "nodeCount": 2
}
```

返回前必须逐条复查语义强相关性，并执行短派发 Prompt 中的固定校验命令。只有命令退出状态为 0 且输出 `status: VALID` 才能返回上述完成回执；首次机械校验失败时按报错修正一次并重新校验，仍失败则返回失败原因，不得返回 `completed`。

若子 Agent 环境确实没有文件写入能力，则只返回一次紧凑结果 JSON；主 Agent 必须原样保存到 `resultFile`，不得重新分类、改写内容或让子 Agent 重复生成。

### 5. 使用固定脚本合并首轮结果

主 Agent 等待全部 IR 任务完成且均通过第 3.4 节校验。随后直接执行随包提供的固定脚本；禁止现场编写或修改合并脚本：

```bash
python "{skillDir}/scripts/merge_and_report.py" --requirements "{requirementsFile}" --manifest "requirements_ir_work/ir_manifest.json" --product-line "{productLine}" --execution-mode "{executionMode}" --max-concurrency {actualMaxConcurrency} --classified-output "{classifiedFile}" --report-json "{reportJsonFile}" --report-md "{reportMdFile}"
```

脚本自动通过本 Skill 的 `references/product_line_mapping.json` 选择规则文件，并完成：

1. 校验任务清单与源需求树哈希一致。
2. 校验每个 `irIndex`、`irLogicid`、结果文件和 `attempts`。
3. 校验每个 IR 结果恰好覆盖其子树全部 `logicid`，不存在缺失、重复或越界节点。
4. 再次校验每条非空分类都是规则文件中的合法一级或二级路径。
5. 深拷贝 `{requirementsFile}`，只按 `logicid` 添加 `classification`，因此完整保留顶层字段、`statistics`、IR/SR/AR 层级、节点顺序和原字段。
6. 生成 `{classifiedFile}`、`{reportJsonFile}` 和 `{reportMdFile}`。

任何 IR 结果缺失或非法时，脚本以非零状态退出，禁止生成或交付部分结果。不得用手工拼接、临时 Python、PowerShell 或复制子 Agent 文本代替该脚本。

### 6. 执行全局“规则 → 需求”反向复核

读取首轮 `{reportJsonFile}` 中的未覆盖规则：

1. 未覆盖规则较少时，由主 Agent 直接对 `{classifiedFile}` 进行语义复核。
2. 未覆盖规则较多时，按唯一 `一级需求` 一次性建立复核队列，`taskKey` 为 `audit:{一级需求}`，复用第 3 节状态机，每批最多 5 个。
3. 复核子 Agent 是叶子 Agent，只返回真正新增的“规则路径 + 强相关节点 logicid”；弱相关、词面相似或推测性关系保持未覆盖。
4. 每个复核任务将新增结果写入 `requirements_ir_work/audit-results/` 下互不重名的 JSON 文件，格式固定为：

```json
{
  "taskKey": "audit:一级需求",
  "additions": [
    {
      "logicid": "1003410",
      "classification": [
        {
          "一级需求": "规则原文",
          "二级需求": "规则原文或 null"
        }
      ]
    }
  ]
}
```

全局复核完成后，重新执行第 5 节同一固定命令并追加 `--global-audit-completed`。脚本会从原始需求树重新合并首轮结果和所有 audit additions，精确去重并覆盖写入最终三个文件。若没有需要新增的分类，也必须执行带该标志的最终命令，使报告明确记录全局复核已经完成。

此阶段用于发现跨 IR 的同义表达和一对多遗漏，不得为提高覆盖率而放宽标准。

### 7. 检查退化分类

出现以下现象时，不得直接交付；重新阅读原文并执行语义分类：

- 大量语义明显不同的节点被统一填成同一路径。
- 详细 AR 节点普遍只落到宽泛一级规则，未分析可用的二级规则。
- 有并列要求的节点全部只有一条分类。
- 二级覆盖为零或接近零，但输入包含大量具体指标、功能或约束。
- 覆盖明细中的命中节点与规则业务含义不一致。

覆盖率本身偏低不等于错误；只有分类过程或证据异常才需要重做。

### 8. 写入分类结果

由第 5、6 节的固定脚本写入分类结果。输出必须以 `{requirementsFile}` 的完整根对象为基础，保持全部顶层字段、完整 `data` 数组、IR/SR/AR 树形结构、节点顺序和原字段不变，只在每个需求节点新增 `classification`。

最终文件应保持以下完整树形轮廓；省略号代表必须原样保留的原始字段，而不是可以省略的内容：

```json
{
  "...原始顶层字段": "原值保持不变",
  "statistics": {},
  "data": [
    {
      "logicid": "1003409",
      "...其它原始IR字段": "原值保持不变",
      "classification": [
        {
          "一级需求": "规则原文",
          "二级需求": "规则原文或 null"
        }
      ],
      "children": [
        {
          "logicid": "原SR logicid",
          "...其它原始SR字段": "原值保持不变",
          "classification": [],
          "children": [
            {
              "logicid": "原AR logicid",
              "...其它原始AR字段": "原值保持不变",
              "classification": []
            }
          ]
        }
      ]
    }
  ]
}
```

无合法路径时：

```json
{
  "logicid": "1003409",
  "classification": []
}
```

### 9. 统计覆盖

仅在语义分类和反向复核完成后统计：

- 一级覆盖：存在相同 `一级需求`。
- 二级覆盖：存在相同且非空的 `(一级需求, 二级需求)`。
- 同一规则被多个节点命中时，覆盖数只计 1，并保存全部命中节点 `logicid`。
- `二级需求: null` 只计一级覆盖。
- 分别计算需求节点映射率、一级规则覆盖率、二级规则覆盖率。

```text
规则覆盖率 = 已覆盖规则数 / 规则总数 × 100%
节点映射率 = 已映射节点数 / 需求节点总数 × 100%
```

百分比保留两位小数。规则总数为 0 时，该维度覆盖率记为 `100.00`。

## 输出

保存：

- `{classifiedFile}`：该文件根对象必须是固定脚本从原始完整需求树合并得到的结果，不得写成子 Agent 结果列表或额外包装对象。
- `{reportJsonFile}`
- `{reportMdFile}`

覆盖报告 JSON 至少包含：

```json
{
  "productLine": "用户输入值",
  "resolvedPackage": "映射后的包名",
  "ruleFile": "实际规则文件名",
  "完整覆盖": false,
  "classificationMethod": "llm-semantic",
  "executionMode": "parallel-by-ir",
  "parallelExecution": {
    "IR任务总数": 0,
    "最大并发数": 0,
    "已完成IR任务数": 0,
    "失败IR任务数": 0,
    "重试IR任务数": 0
  },
  "semanticAudit": {
    "需求到规则复核完成": true,
    "规则到需求复核完成": true,
    "使用关键词或硬编码分类": false
  },
  "统计": {
    "需求节点映射": {
      "总数": 0,
      "已映射数": 0,
      "未映射数": 0,
      "映射率": 0.00
    },
    "分类路径总数": 0,
    "一对多节点数": 0,
    "一级需求": {
      "总数": 0,
      "已覆盖数": 0,
      "未覆盖数": 0,
      "覆盖率": 0.00
    },
    "二级需求": {
      "总数": 0,
      "已覆盖数": 0,
      "未覆盖数": 0,
      "覆盖率": 0.00
    }
  },
  "未覆盖一级需求": [],
  "未覆盖二级需求": [],
  "一级需求覆盖明细": [],
  "二级需求覆盖明细": []
}
```

`executionMode` 按实际执行填写：

- 多个 IR 使用子 Agent 并行：`parallel-by-ir`
- 只有一个 IR：`single-ir`
- 环境不支持子 Agent：`serial-fallback`

`最大并发数` 填写实际同时运行的 IR 子任务数，不得填写计划值，且不得大于 5。

覆盖明细必须为每条已覆盖规则填写真实的 `命中需求logicid`，不得统一留空。Markdown 报告按“结论 → 节点映射情况 → 一级/二级覆盖统计 → 全部未覆盖一级 → 全部未覆盖二级”输出。

### 用户界面最终结果

最终回复必须完整读取 `{reportJsonFile}` 中的 `未覆盖一级需求` 和 `未覆盖二级需求`，并在用户界面分别输出两个列表。每条记录固定展示以下两个字段：

| 一级需求 | 二级需求 |
|---|---|

- `未覆盖一级需求` 保持逐条展示，`二级需求` 固定显示为 `null`；显示顺序与报告数组一致，行数必须等于该数组的实际长度。
- `未覆盖二级需求` 按 `一级需求` 分组：相同 `一级需求` 只显示一行，把该组全部 `二级需求` 按报告原顺序写入同一个单元格，格式固定为 `二级需求1、二级需求2、二级需求3`，不得添加括号；只有一项时直接显示该二级需求原文。
- 分组行顺序按各 `一级需求` 在报告数组中首次出现的顺序。每条未覆盖二级需求必须恰好进入一个分组；分组行数必须等于该数组中唯一 `一级需求` 的数量，展开全部分组后的二级需求总数必须等于该数组的实际长度。
- 无论清单多长，都不得使用“等”“等等”“其余”“部分”“省略”或省略号代替记录，也不得只让用户查看附件。单个表格过长时，按连续记录区间拆成多个列表块（例如“第 1–50 条”“第 51–100 条”），表内仍只保留上述两个字段，直到全部记录输出完毕。
- 若某级没有未覆盖项，明确输出“无”。完整清单优先于过程说明；可省略非必要过程复述，不得省略任何未覆盖项。
- 最终回复同时列出本次三个带时间戳的实际输出文件名；从入口调用时还须列出同一时间戳的转换文件名。

## 完成门禁

交付前全部满足：

1. 所有节点均由大模型完成语义判断，未使用分类脚本、关键词规则或固定兜底。
2. 所有节点均存在 `classification` 数组；无合法路径时允许 `classification.length = 0`，计入未映射节点，不生成默认分类或待确认清单。
3. 所有非空分类名称与所选规则文件逐字一致。
4. 一对多节点中的每条路径均有独立语义依据。
5. “需求 → 规则”和“规则 → 需求”两轮复核均完成。
6. IR 任务清单只由 `prepare_ir_tasks.py` 初始化一次；每个子 Agent 收到与 `promptFile` 或 `retryPromptFile` 逐字一致的短派发 Prompt，完整读取指定执行规则和产品线规则，小 IR 读取完整子树，大 IR 按预生成语义批次逐批直接判断，并在返回前自行校验为 `VALID`；主 Agent 再次校验所有 `resultFile`，每个 `taskKey` 无重复创建，`attempts <= 2`，最大并发数不超过 5。
7. 工作区 Python 文件与任务清单基线一致，不存在新建或修改的提取、分类、映射或拼装脚本；叶子子 Agent 除固定单结果校验命令外未执行其它终端命令。
8. `已覆盖数 + 未覆盖数 = 总数`，覆盖明细、未覆盖清单和命中 `logicid` 相互一致。
9. 报告明确标记 `classificationMethod: llm-semantic`、实际 `executionMode` 和 `使用关键词或硬编码分类: false`。
10. 分类结果与输入需求树的顶层字段、IR 数量、递归节点数、全部 `logicid`、父子层级、节点顺序及原始字段完全一致；所有紧凑分类结果已由固定脚本合并到同一棵完整树，每个节点都存在 `classification`。
11. 最终报告的“规则到需求复核完成”为 `true`，且 `{classifiedFile}`、`{reportJsonFile}`、`{reportMdFile}` 均由带 `--global-audit-completed` 的固定命令生成并复用同一个 `runTimestamp`。
12. 只交付分类结果和两种覆盖报告，不交付内部 IR 任务结果、临时任务文件、任何临时脚本或待确认清单。
13. 用户界面已完整展示全部未覆盖一级和二级需求；未覆盖一级需求按报告数组顺序逐条展示，二级字段为 `null`；未覆盖二级需求按一级需求首次出现顺序分组，同一一级需求只占一行且全部二级需求按报告原顺序集中显示。一级需求行数、二级需求分组行数及展开后的二级需求总数均符合上述规则，且无任何省略表达。
