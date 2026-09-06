---
name: requirement-decomposition
description: |
  需求分解 / 分解需求 / 生成需求详细描述并入库 的唯一入口。串行编排两个子 Skill：
  das-manage-transformer(查询转换) → requirement-description-generator(生成入库)。

  必须触发本 Skill 的场景（命中任一即用，不得自行绕过）：
  - "需求分解""分解需求""查询并补全需求描述""导出需求并生成描述"
  - "生成需求详细描述并入库""请帮我生成需求详细描述"
  - 用户给出版本ID(vrcId)并要求批量生成描述并写回系统

  重要：本 Skill 需要用户【显式指定产品线 productLine】（如 Data_Communication），
  用于路由到对应经验包。产品线的自动获取尚未实现，因此 productLine 只能由用户在提示词里给出；
  用户未给出时必须停下来询问，不得猜测或使用默认包。

  工作流程：
  1. 阶段1：调用 das-manage-transformer 查询DAS需求并转换为标准树形结构(保留logicid)
  2. 阶段2：调用 requirement-description-generator，按 productLine 路由到对应经验包，
     为每条需求生成 requirementDetailInfo，转换为批量保存格式，调用MCP写回系统
  3. 阶段3：入库成功后，调用 webmcp 工具 das-ai-requirement-analyse 刷新前端页面
  4. 阶段4：输出入库结果与需人工复核清单
---

# 需求分解入口 - Requirement Decomposition Workflow

本 Skill 是"需求分解 / 生成需求详细描述并入库"的**唯一编排入口**。它本身不实现任何查询、
转换、生成或入库逻辑，只负责：校验入参 → 按固定顺序调用两个子 Skill → 衔接输入输出 →
刷新前端并汇总结果。

## 核心原则（必须遵守）

1. **只编排，不实现。** 阶段1、阶段2 的一切具体步骤（MCP 调用、脚本、载荷构造、经验包应用、
   检索、防编造、纯文本清洗等）都由对应子 Skill 定义并执行。禁止在本 Skill 内凭记忆重写这些步骤。

2. **强制调用子 Skill。** 阶段1 必须调用 `das-manage-transformer`；阶段2 必须调用
   `requirement-description-generator`。子 Skill 才是权威实现，会独立演进；内联复制只会随实现失真。

3. **串行依赖。** 阶段2 依赖阶段1 的输出（含 `logicid`），必须等阶段1 产出后再启动。

4. **产品线由用户显式给定，缺失即停；找不到包时先查映射表。** `productLine` 决定阶段2 路由到
   哪个经验包。当前没有自动获取产品线的接口，所以：
   - 用户在提示词里给了 `productLine`（如"这批需求属于 Optical Business"）→ 原样透传给阶段2。
   - 用户没给 → 状态置 `NEEDS_CONTEXT`，**停下来问用户属于哪个产品线**，不得猜测、不得用默认包。
   - 用户给的产品线在 `references/packages/` 下**没有同名目录** → 不要立刻 BLOCKED，
     阶段2 会依次尝试：规范化匹配（忽略大小写/空格/下划线）→ **映射表回退**
     （`references/product_line_mapping.json`，如 `Optical Business`/`Cloud Core Network`
     → `Data_Communication`，`Data Storage` → `Computing`）。
   - 映射表也匹配不上 → 置 `BLOCKED`，告知用户：可先为该产品线建包
     （`requirement-experience-package-builder`），或在映射表里补一条别名。
     **绝不退回其它产品线的包**（错用别的经验包会导致跨域乱编）。

## 编排流程

```
用户输入 vrcId + productLine（productLine 必须由用户给出）
   │
   ├─ 校验：productLine 是否提供？
   │        缺 productLine → NEEDS_CONTEXT（问用户）
   │   解析经验包（阶段2 内 resolve_package.py，顺序固定）：
   │        直接命中目录 → 规范化命中 → 映射表 aliases 回退
   │        全部不中     → BLOCKED（建包 或 映射表补别名；不退回其它包）
   ▼
阶段1 ── 调用子 Skill: das-manage-transformer
   │        产出 requirements_transformed.json（树形结构，含 logicid）
   ▼
阶段2 ── 调用子 Skill: requirement-description-generator（传入 productLine 用于路由）
   │        产出 入库结果 + 人工复核清单
   ▼
阶段3 ── 调用 webmcp 工具 das-ai-requirement-analyse（刷新前端，本 Skill 直接执行）
   ▼
阶段4 ── 汇总并输出 入库结果 + 人工复核清单（本 Skill 直接执行）
```

## 子 Skill 契约

### 阶段1 · das-manage-transformer
| 项 | 内容 |
|----|------|
| 功能 | 查询 DAS 需求并转换为标准树形结构（IR→SR→AR） |
| 输入 | `vrcId`（及可选 `pageSize`、`isFilterDelete`） |
| 输出 | `requirements_transformed.json`：保留 `logicid` 与 `requirementDetailInfo` 字段 |

### 阶段2 · requirement-description-generator
| 项 | 内容 |
|----|------|
| 功能 | 按 `productLine` 路由到对应经验包，为每条需求生成 `requirementDetailInfo` 并入库 |
| 输入 | 阶段1 的 `requirements_transformed.json`（含 `logicid`）+ **`productLine`** |
| 路由 | 阶段2 用 `resolve_package.py` 解析 `productLine`：直接命中 → 规范化命中 → 映射表 `references/product_line_mapping.json` 回退；失败则 BLOCKED |
| 输出 | 入库结果（success/message/data）+ 需人工复核清单（空描述 & 检索未命中且信息薄的条目） |

## 输入要求

| 参数 | 类型 | 必填 | 说明 | 示例 |
|------|------|------|------|------|
| vrcId | string | 是 | 版本ID（查询 DAS 需求 & 入库权限校验） | "22091846" |
| productLine | string | 是 | 产品线，**由用户显式给出**，用于路由经验包 | "Data_Communication" |
| pageSize | int | 否 | 每页大小，默认 500 | 500 |
| isFilterDelete | bool | 否 | 是否过滤作废需求，默认 false | false |

## 阶段状态处理

| 状态 | 含义 | 处理方式 |
|------|------|----------|
| DONE | 阶段完成 | 进入下一阶段 |
| DONE_WITH_CONCERNS | 完成但有疑虑 | 阅读疑虑，解决后继续 |
| NEEDS_CONTEXT | 缺必要信息（如 vrcId 或 **productLine**） | 补齐后重试 |
| BLOCKED | 无法完成（子 Skill 不可用、logicid 缺失、MCP 报错、**productLine 经映射表仍无对应经验包**） | 评估原因，不得绕过子 Skill 或错用别的包 |

## 完成示例

用户请求：`帮我分解 vrcId 22091846 的需求，这批需求属于 Optical Business，生成详细描述并入库`

编排动作：
1. 校验：productLine=Optical Business 已给出。packages/ 下无同名目录 → 查映射表，
   命中 `Optical Business -> Data Communication`，使用经验包 `Data_Communication`。
2. 调用 `das-manage-transformer`（vrcId=22091846）→ `requirements_transformed.json`
3. 调用 `requirement-description-generator`（传入产物 + productLine=Data_Communication）→ 生成与入库
4. 调用 webmcp `das-ai-requirement-analyse` 刷新前端
5. 输出：

```
需求分解完成（产品线：Optical Business → 经验包 Data_Communication）：
  查询到需求 6 条（IR2/SR2/AR2）
  需生成 4 条，已生成 4 条，跳过（空描述）2 条
  其中 3 条挂到了历史父→子分解参考
  入库结果：success=true，已保存 4 条
  前端页面已自动刷新
  需人工复核：logicid 973829、973831（空描述未生成）
```

若用户没说产品线：
```
需要你先确认这批需求属于哪个产品线（如 Data Communication / Wireless / Computing，
或 Optical Business / Cloud Core Network / Data Storage 等匹配产品线），
我据此路由到对应经验包。产品线目前需你显式指定，我不会替你猜。
```

若产品线经映射表仍无对应经验包（如 `Digital Power`）：
```
BLOCKED：'Digital Power' 既无同名经验包，也不在映射表中。
可用经验包：Computing、Data_Communication、Wireless
处理方式二选一：① 用 requirement-experience-package-builder 为其建包；
② 若它应归属某条已有产品线，在 references/product_line_mapping.json 补一条别名。
（不会退回使用其它产品线的经验包。）
```

## 相关 Skill
- **das-manage-transformer**：阶段1，DAS 需求查询与转换。
- **requirement-description-generator**：阶段2，需求详细描述生成与入库（按 productLine 路由经验包）。
- **requirement-experience-package-builder**：离线为各产品线建包（新增产品线时先用它建包）；
  其 `scripts/build_product_line_mapping.py` 可从"产品线匹配.xlsx"重新生成映射表 JSON。
