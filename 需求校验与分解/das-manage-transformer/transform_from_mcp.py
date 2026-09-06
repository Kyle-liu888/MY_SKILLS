#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transform_from_mcp.py

将 MCP 工具 mcp_query_all_requirement_detail_info_tool 的原始返回，
转换为标准的 IR / SR / AR 树形结构 JSON，并附带统计信息。

用法:
    python transform_from_mcp.py [输入原始文件] [输出文件]

默认:
    python transform_from_mcp.py mcp_raw_output.json requirements_transformed.json

设计目标: 鲁棒性。对以下情况均做兼容处理:
    - 原始文件内容是 JSON 字符串 / dict / list
    - list 中包裹 {"type":"text","text":"{...}"} 文本块(可能双重 JSON 编码)
    - 节点缺失某些字段(name/categorys/description/requirementDetailInfo/logicid)
    - children 为 null / [] / 嵌套数组
    - vrcId 位于顶层或 pageInfoVo 中
"""

import json
import logging
import sys

logger = logging.getLogger(__name__)

# 允许统计的合法需求层级
VALID_CATEGORYS = ("IR", "SR", "AR")


# --------------------------------------------------------------------------- #
# 读取与归一化
# --------------------------------------------------------------------------- #
def load_raw(path):
    """读取原始文件为文本。"""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _try_json(value):
    """尝试把字符串解析为 JSON；失败则原样返回。"""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _extract_from_list(lst):
    """从 list 中提取有效载荷(优先 text 块 / 含 data 的 dict)。"""
    for el in lst:
        if isinstance(el, dict):
            if "text" in el:
                parsed = _try_json(el["text"])
                if isinstance(parsed, (dict, list)):
                    return parsed
            if "data" in el:
                return el
        elif isinstance(el, str):
            parsed = _try_json(el)
            if isinstance(parsed, (dict, list)):
                return parsed
    return lst[0] if lst else {}


def to_parsed(raw):
    """将任意形态的 MCP 返回归一化为最终的业务 dict(含 data)。"""
    obj = raw
    if isinstance(obj, str):
        obj = _try_json(obj)

    # 可能多层包裹，最多迭代若干次直到拿到含 data 的 dict
    for _ in range(5):
        if isinstance(obj, list):
            obj = _extract_from_list(obj)
            continue
        if isinstance(obj, dict):
            # 已经是业务对象
            if "data" in obj:
                break
            # 仅是 text 包裹块
            if "text" in obj:
                obj = _try_json(obj["text"])
                continue
            break
        if isinstance(obj, str):
            obj = _try_json(obj)
            continue
        break

    if not isinstance(obj, dict):
        raise ValueError(f"无法解析为 dict 的 MCP 返回结构: {type(obj).__name__}")
    return obj


# --------------------------------------------------------------------------- #
# 转换
# --------------------------------------------------------------------------- #
def _as_str(value):
    """None -> ''; 其它 -> str。"""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def transform_node(node, counter):
    """递归转换单个需求节点，固定输出 6 个字段。"""
    if not isinstance(node, dict):
        return None

    cat = _as_str(node.get("categorys")).strip()
    if cat in counter:
        counter[cat] += 1

    # children: 数组则递归；无有效子节点则置 null
    children = None
    raw_children = node.get("children")
    if isinstance(raw_children, list):
        converted = [transform_node(c, counter) for c in raw_children]
        converted = [c for c in converted if c is not None]
        children = converted if converted else None

    return {
        "categorys": cat,
        "name": _as_str(node.get("name")),
        # 原始返回通常不含 description，缺失则为空串
        "description": _as_str(node.get("description")),
        "requirementDetailInfo": _as_str(node.get("requirementDetailInfo")),
        "logicid": _as_str(node.get("logicid")),
        "children": children,
    }


def _resolve_version_id(parsed):
    """定位版本号 vrcId(顶层优先, 其次 pageInfoVo)。"""
    for key in ("vrcId", "versionId"):
        v = parsed.get(key)
        if v not in (None, ""):
            return _as_str(v)
    page = parsed.get("pageInfoVo")
    if isinstance(page, dict):
        for key in ("vrcId", "versionId"):
            v = page.get(key)
            if v not in (None, ""):
                return _as_str(v)
    return ""


def build_output(parsed):
    """根据归一化后的 dict 构建最终输出。"""
    data = parsed.get("data")
    if not isinstance(data, list):
        data = []

    counter = {"IR": 0, "SR": 0, "AR": 0}
    transformed = [transform_node(n, counter) for n in data]
    transformed = [n for n in transformed if n is not None]

    ir, sr, ar = counter["IR"], counter["SR"], counter["AR"]
    statistics = {
        "versionId": _resolve_version_id(parsed),
        "grandTotal": len(transformed),   # 顶层需求数量
        "irCount": ir,
        "srCount": sr,
        "arCount": ar,
        "totalCount": ir + sr + ar,
    }
    return {"data": transformed, "statistics": statistics}


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv):
    in_path = argv[1] if len(argv) > 1 else "mcp_raw_output.json"
    out_path = argv[2] if len(argv) > 2 else "requirements_transformed.json"

    raw = load_raw(in_path)
    parsed = to_parsed(raw)
    output = build_output(parsed)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    s = output["statistics"]
    logger.info(f"转换完成 -> {out_path}")
    logger.info(f"  versionId  = {s['versionId']}")
    logger.info(f"  顶层需求数 = {s['grandTotal']}")
    logger.info(f"  IR={s['irCount']}  SR={s['srCount']}  AR={s['arCount']}  totalCount={s['totalCount']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
