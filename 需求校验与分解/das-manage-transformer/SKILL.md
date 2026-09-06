---
name: das-manage-transformer
description: |
  DAS需求查询与转换。当用户要求"查询/获取/导出需求"，或提供了 vrcId、originBelongedAreas、sourceSystem、roleId 等查询参数时触发。
  流程：用 MCP 工具 mcp_query_all_requirement_detail_info_tool 查询全部需求 → 解析返回 → 转换为 IR/SR/AR 树形结构 JSON 并保存。
---

# DAS需求查询与转换技能

## 1. 查询需求

必填参数：`vrcId`、`originBelongedAreas`、`sourceSystem`、`roleId`。

> **参数映射规则**：构造 MCP 查询请求时，**必须逐一读取前端传入的"DAS需求管理查询请求参数"（payload_json）中存在的字段并传入**，payload_json 中有值的字段不可遗漏（尤其是 `idList`——用户勾选了需求时该字段必有值，遗漏将导致只查到全量数据而非勾选项）。payload_json 中不存在的字段不传。

```python
from mcp_query_all_requirement_detail_info_tool import mcp_query_all_requirement_detail_info_tool

request = {                           # DAS需求管理查询请求参数, payload_json中的请求数据
    "pageSize": 500,                  # 每页大小，推荐100-500
    "currentPage": 1,                 # 起始页码
    "vrcAndBoardArray": {             # 版本ID和单板编码对象
        "boardNumber": [],            # 单板编码列表
        "vrcId": "22091846"           # 必填，版本ID（如22091846）
    }, 
    "isFilterDelete": False,          # 不过滤作废数据
    "originBelongedAreas": "mped",    # 必填，所属领域
    "sourceSystem": "ALM",            # 系统来源
    "roleId": "9",                    # 角色视图（如9，表示整机设计师角色）
    "idList": [111111,222222]         # 勾选查询的logicId列表
}

result = mcp_query_all_requirement_detail_info_tool(request=request)
```
## 2. 原样落盘（必做，先于任何解析）
 
拿到 MCP 返回后，**第一件事就是把原始返回值完整写入文件**，不做任何加工。用于核对真实结构、便于排查后续步骤。
 
```python
import json
 
# 原始返回可能是 str / list / dict，一律先转成可写文本再落盘，避免类型假设导致丢数据
raw_path = "mcp_raw_output.json"
with open(raw_path, "w", encoding="utf-8") as f:
    if isinstance(result, str):
        f.write(result)                                   # 已是字符串，直接写
    else:
        json.dump(result, f, ensure_ascii=False, indent=2)  # list/dict 序列化
 
print(f"原始MCP返回已保存: {raw_path}，类型={type(result).__name__}")
```
 
保存后先查看 `mcp_raw_output.json` 的实际结构，再决定如何解析，**不要凭假设写死** `result[0]['text']`。


## 3. 解析返回
 
常见格式为 `[{"type":"text","text":"{...}"}]`，取出 text 二次解析。但请以第 2 步落盘的实际结构为准做兼容处理：
 
```python
import json
 
# 兜底兼容：list 包 text / 直接是 dict / 是 JSON 字符串
if isinstance(result, str):
    parsed = json.loads(result)
elif isinstance(result, list) and result and isinstance(result[0], dict) and "text" in result[0]:
    parsed = json.loads(result[0]["text"])
elif isinstance(result, dict):
    parsed = result
else:
    raise ValueError(f"未知的MCP返回结构: {type(result)}")
 
items = parsed["data"]          # 真正的需求列表
print(f"共 {len(items)} 条顶层需求")
```
 
## 4. 转换并保存
 
```bash
python transform_from_mcp.py mcp_raw_output.json requirements_transformed.json
# 参数可省略，默认输入 mcp_raw_output.json、输出 requirements_transformed.json
```
 
`transform_from_mcp.py`（skill 目录内）会自动完成归一化 → 递归转换 → 统计汇总，无需手动做第 3 步解析：

- **输入归一化（鲁棒）**：兼容原始返回是 JSON 字符串 / dict / list 包 `{"type":"text","text":"{...}"}` 文本块（含双重 JSON 编码）等多种形态，自动逐层拆包直到拿到含 `data` 的业务对象。
- **递归转换**：对每条需求固定输出 6 个字段 `categorys / name / description / requirementDetailInfo / logicid / children`。
  - 字段缺失时按空串 `""` 兜底（**注意**：MCP 原始返回通常不含 `description`，因此该字段一般为空串）。
  - `logicid` 统一转为字符串；`children` 有有效子节点时为数组，否则为 `null`（`null` 与 `[]` 都归一为 `null`）。
- **统计汇总**：`grandTotal` 为顶层需求数量；`irCount/srCount/arCount` 为全树各层级计数；`totalCount = ir + sr + ar`；`versionId` 优先取顶层 `vrcId`，其次 `pageInfoVo.vrcId`。
 
## 输出格式

每个需求节点含 6 个字段：`categorys`(IR/SR/AR)、`name`、`description`、`requirementDetailInfo`(详细描述)、`logicid`(唯一ID)、`children`(子需求数组，叶子节点为 `null`)。顶层附带 `statistics` 汇总。

```json
{
  "data": [
    {
      "categorys": "IR",
      "name": "需求名称",
      "description": "需求描述",
      "requirementDetailInfo": "需求详细描述",
      "logicid": "1003409",
      "children": [
        {
          "categorys": "SR",
          "name": "需求名称",
          "description": "需求描述",
          "requirementDetailInfo": "需求详细描述",
          "logicid": "1003410",
          "children": [
            {
              "categorys": "AR",
              "name": "需求名称",
              "description": "需求描述",
              "requirementDetailInfo": "需求详细描述",
              "logicid": "1003431",
              "children": null
            }
          ]
        }
      ]
    }
  ],
  "statistics": {
    "versionId": "3996731",
    "grandTotal": 1,
    "irCount": 1,
    "srCount": 1,
    "arCount": 1,
    "totalCount": 3
  }
}
```

> 说明：`children` 有子需求时为数组，无子需求时为 `null`（不是空数组 `[]`）。`statistics.versionId` 即查询用的 vrcId；`grandTotal` 为顶层需求数；`totalCount = irCount + srCount + arCount`（各层级计数彼此独立，一个 SR 下可挂多个 AR，故 arCount 可大于 srCount）。

## 注意事项

- 四个必填参数缺一不可，否则无法查询。
- 统一 UTF-8 编码；`isFilterDelete=False` 保证数据完整。
- 转换完成后可清理临时文件。