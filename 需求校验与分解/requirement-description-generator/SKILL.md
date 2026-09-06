---
name: requirement-description-generator
description: |
  需求详细描述生成技能（v2）- 为待处理需求(IR/SR/AR)逐条生成 requirementDetailInfo，再转成
  批量保存格式写回系统。生成每条描述时，综合【本节点 categorys+name+description】(主信息，具体值
  的唯一来源) 与【上下层级 + 按产品线路由的经验包检索到的"历史父→子真实分解"】(定位、分解方式、
  措辞)，并遵守防编造边界与【纯文本输出】要求（下游按纯文本解析，禁止任何 HTML 标签）。
  产品线路由支持映射表回退：用户给的产品线在 packages/ 下无同名目录时，按
  references/product_line_mapping.json 的别名反查应使用的经验包（如 Optical Business ->
  Data_Communication、Data Storage -> Computing）。

  使用场景（命中任一即用）：
  - "生成需求详细描述""补全需求描述""写需求描述""需求分解"
  - 已由 das-manage-transformer 得到 requirements_transformed.json（含 logicid 与层级）
  - 用户给出含 logicid/categorys/description 的需求树，要求批量生成并入库

  依赖：保留 logicid 与 IR→SR→AR 层级；按需求所属产品线路由到对应经验包；
  MCP 工具 save_requirement_detail_info_tool 批量入库。
---

# 需求详细描述生成 v2

为待处理需求逐条生成 `requirementDetailInfo` 并批量入库。

## 先摆正任务定义（这是不乱编的前提）

一个没见过的需求，其详细描述里的**具体值**（具体温度曲线、具体接口数量、具体标准号）
**无法从"名称 + 简短描述 + 层级"推导出来**——这些在设计者脑中或关联文档里，不在输入里。
所以任务**不是**"把标题扩写成一份完整规格书"，而是：

> 把本节点**已知信息**（name+description）按该类型的常见结构组织成一段忠实、清晰的详细描述，
> 覆盖该类型通常涉及的维度；对本节点**没有给出具体值**的维度，明确标注"待补充/以实际设计为准"，
> **绝不编造**。

按这个定义，一段"结构完整、把已知信息归位、缺口诚实标注"的描述就是**成功**。逼模型从一个
十几字的名称产出上千字"落地"内容，才是乱编的根源。因此篇幅贴近经验包的**真实**基准即可，不硬凑。

## 输入

`das-manage-transformer` 输出的 `requirements_transformed.json`（IR→SR→AR 树）。每节点：
`categorys`(选哪套 type_rules) · `name`+`description`(**本节点主信息，具体值只来自这里**) ·
`logicid`(入库命脉，全程保留) · `children` · `requirementDetailInfo`(待生成目标)。

> **查询参数映射规则**：若需调用 `das-manage-transformer` 查询需求，构造 MCP 查询请求时，**必须逐一读取前端传入的"DAS需求管理查询请求参数"（payload_json）中存在的字段并传入**，payload_json 中有值的字段不可遗漏（尤其是 `idList`——用户勾选了需求时该字段必有值，遗漏将导致只查到全量数据而非勾选项）。payload_json 中不存在的字段不传。

## 输出

`build_save_payload.py` 产出的 `save_payload.json`：
`{"versionId","detailInfoList":[{"logicId","requirementDetailInfo"}]}`。
**requirementDetailInfo 必须是纯文本**（脚本入库前还会兜底清洗，见步骤4）。

## 执行流程

### 步骤1：展平 + 挂上下文 + 父条件化检索

先确定该需求树所属**产品线**（由用户显式给出），再解析到对应经验包目录。解析用脚本完成，
**不要靠人眼判断**，顺序固定、结果可回溯：

1. 直接命中 `references/packages/<productLine>/`；
2. 规范化命中（忽略大小写与 空格/下划线/连字符，如 `Data Communication` → `Data_Communication`）；
3. **映射表回退**：查 `references/product_line_mapping.json` 的 `aliases`
   （如 `Optical Business` / `Cloud Core Network` → `Data_Communication`；`Data Storage` → `Computing`）；
4. 都不中 → 置 `BLOCKED`：请用户确认产品线，或先为其建包 / 在映射表补一条别名。**不要退回其它包。**

```bash
# 推荐：传产品线，自动解析（含映射表回退）
python scripts/build_worklist.py requirements_transformed.json worklist.json \
    --product-line "<用户给的产品线>" \
    --packages-root references/packages \
    --mapping references/product_line_mapping.json
# 或显式指定经验包目录
python scripts/build_worklist.py requirements_transformed.json worklist.json --package <经验包目录>
```
解析失败时脚本退出码为 2，并打印可用经验包与已知别名——据此置 BLOCKED 并回报用户。
也可单独解析：`python scripts/resolve_package.py "Optical Business"`。

> 映射表是**独立的 JSON**（`references/product_line_mapping.json`），随时可直接编辑增删别名，
> 无需改动本 SKILL。表格更新后也可用 `requirement-experience-package-builder/scripts/
> build_product_line_mapping.py` 从"产品线匹配.xlsx"重新生成。

脚本为每条 `needs_generation=true` 的需求挂上：
- `ancestors`：祖先链，**父节点给全文**（`description_full`）——父需求常是子需求最重要的 grounding；
- `children`：直接子节点摘要（父需求应概述子需求覆盖面）；
- `retrieval`：**父条件化两级检索**结果（核心）——
  - `matched_parent_groups`：用本节点父需求检索到的"同产品线同类型、分解语境相似"的历史父需求，
    及其**当年真实的整组子需求**（`children_overview`）与组内最像的样板（`closest_samples`，含真文本）；
  - `style_only_samples`：无父(IR)或一级未命中时的兜底风格样板；
  - 命中不了就没有 `retrieval` 字段——属正常，按保守生成即可（宁缺毋滥，挂错组会诱导跨域乱编）。
- `needs_generation=false`（空描述/占位）不生成，交人工复核。

### 步骤2：加载经验包规则

```python
import json
exp = json.load(open("<经验包目录>/experience_package.json", encoding="utf-8"))
rules = exp["writing_rules"]; anti_fab = rules["anti_fabrication_rules"]
```

### 步骤3：逐条生成（核心）

对每条 `needs_generation=true`：

**(1) 信息来源分层**
- 具体值**只**来自本节点 `name`+`description`。
- `ancestors`(尤其父全文)/`children`/`retrieval`：只用于①定位所属能力域；②消歧；③保持父子一致；
  ④学**分解方式**（分几项、每项覆盖哪个维度、整组该覆盖哪些方面）与**措辞排版**。
- **红线**：`retrieval`/上下文里的具体数值、型号、标准号、链接一律**不得**搬进本节点。它们示范
  的是"这类需求长什么样、怎么分项、口吻如何"，不是"这条需求的答案"。

**(2) 用检索到的真实分解搭结构**（有 `retrieval` 时最有力）
- 看 `matched_parent_groups[0].children_overview`：和你父需求相似的历史父需求，当年被分解成这些方面。
  据此判断本节点在整组里该占的范围、别和兄弟重叠、别漂到上一层粒度。
- 看 `closest_samples`：与本节点最像的真实子需求怎么写的——学它分几项、每项管什么、纯文本怎么排。
- 用本节点已知信息填这套结构；本节点没有的具体值 → 标"待补充/以实际设计为准"。

**(3) 无检索时按经验包规则搭结构**
- 按 `type_rules[categorys].structure_template` 的 block 顺序，决定"点到哪些维度"，只填已知信息；
- 参考 `representative_examples` 的真实文本校准风格；
- 对照 `completeness_checklist`：`required` 维度若本节点无信息，标注缺口而非编造；
- 篇幅贴近 `typical_length`（真实均值），遵守 `avoid` 与 `anti_fabrication_rules`。

**(4) 纯文本格式（硬性）**
- **禁止任何 HTML 标签**（`<p>`/`<br>`/`<ol>`/`<li>`/`<strong>` 等）和 Markdown 标记。
- 分项用 `1、2、3、` 加换行；板块可用 `【xx】` 中文方括号标签（这是历史纯文本风格，非标记语言）。
- 例：`满足工作温度范围要求：\n1、长期工作温度以实际设计为准；\n2、短期高温规格待补充。`

**(5) 写回**：文本写入该条 `requirementDetailInfo`，保存为 `worklist_filled.json`。

### 步骤4：构建保存载荷（含纯文本兜底清洗）

```bash
python scripts/build_save_payload.py worklist_filled.json save_payload.json --version-id <VID>
```

脚本只导出非空条目，`logicid→logicId`，并对每条 `requirementDetailInfo` 做**纯文本兜底清洗**
（去标签、反转义实体、`<li>`转编号）。这样即便个别输出漏带了 HTML，入库内容也保证是纯文本
（提示词约束 + 代码兜底，双保险）。

### 步骤5：批量入库

```python
payload = json.load(open("save_payload.json", encoding="utf-8"))
result = save_requirement_detail_info_tool(request=payload)  # success/message/data
```
`versionId` 必填有效；`detailInfoList` 每项含 `logicId` 与纯文本 `requirementDetailInfo`。

### 步骤6：汇报
生成条数、跳过条数（空描述）、有多少条挂到了历史分解参考、入库结果、需人工复核的 `logicid` 清单
（含空描述未生成、以及检索未命中且信息很薄、只能给保守骨架的条目）。

## 防编造铁律（详见经验包 anti_fabrication_rules）
1. 具体数值/参数/规格**只**来自本节点 `name`/`description`。
2. 上下文与检索样例只用于定位、父子一致、学分解与措辞；其中具体值/型号/标准号/链接**不得**搬用。
3. 本节点缺某维度信息 → 保守表达("{维度}以实际设计为准"/"待补充")，不假设、不编造。
4. 空描述不生成，交人工复核。
5. 输出纯文本，禁止 HTML/Markdown 标记。

## 注意事项
- **logicid 全程保留**，不可丢失或改写。
- **name 与 description 并重**：name 常含关键定位，不要只看 description。
- **existing_detail 不复用**：系统已有描述仅供参考。
- **产品线路由要确定**：一律用 `resolve_package.py` 解析（直接命中 → 规范化命中 → 映射表回退）；
  解析不出就置 BLOCKED，**绝不退回其它产品线的包**（错用别的包正是跨域乱编的来源）。
- **映射表可热改**：`references/product_line_mapping.json` 独立于 SKILL，增删别名即时生效。
