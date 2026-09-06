#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
需求详细描述生成 - 批量保存载荷构建（含纯文本兜底清洗）

把填好 requirementDetailInfo 的 worklist.json 转成批量保存 MCP 所需格式：
    {"versionId": "...", "detailInfoList": [{"logicId": "...", "requirementDetailInfo": "..."}]}

关键新增：入库前对每条 requirementDetailInfo 做【纯文本兜底清洗】。
下游系统按纯文本解析，任何 HTML 标签都会导致解析失败。即使生成阶段已要求纯文本，
这里再兜一道底，保证入库内容里不含标签/实体（提示词约束 + 代码兜底，双保险）。

清洗规则（保守、只去标记不改语义）：
    <br>, </p>, </li>, </div>  -> 换行
    <li>                        -> 行首编号点（沿用条目式风格）
    其余标签                    -> 去除
    &nbsp; &lt; &gt; &amp; 等   -> 反转义为普通字符
    连续空行折叠、行尾空白清理

用法：
    python build_save_payload.py worklist_filled.json save_payload.json [--version-id VID]
    # 默认即执行纯文本清洗；如需保留原文（排查用）加 --no-sanitize
"""

import argparse
import html
import json
import logging
import re
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

_BLOCK_BREAK = re.compile(r"(?i)</?\s*(br|p|div|/li)\s*/?>")
_LI_OPEN = re.compile(r"(?i)<\s*li\s*>")
_ANY_TAG = re.compile(r"<[^>]+>")
_MULTI_BLANK = re.compile(r"\n{3,}")


def to_plain_text(s):
    """把可能含 HTML 的富文本转成纯文本；无标签则基本原样返回。"""
    if not s:
        return s
    text = s
    # <li> -> 行首圆点式；块级标签 -> 换行
    text = _LI_OPEN.sub("\n- ", text)
    text = _BLOCK_BREAK.sub("\n", text)
    # 去掉其余所有标签
    text = _ANY_TAG.sub("", text)
    # 反转义 HTML 实体（&nbsp; &lt; &amp; 等）
    text = html.unescape(text)
    text = text.replace("\u00a0", " ")           # 不换行空格
    # 规整空白
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = _MULTI_BLANK.sub("\n\n", text).strip()
    return text


def looks_like_html(s):
    return bool(s) and bool(_ANY_TAG.search(s))


def is_blank(v):
    return v is None or str(v).strip() == ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output", nargs="?", default="save_payload.json")
    ap.add_argument("--version-id", default=None)
    ap.add_argument("--no-sanitize", action="store_true", help="跳过纯文本清洗（仅排查用）")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        wl = json.load(f)
    version_id = args.version_id or wl.get("versionId", "")
    items = wl.get("items", [])

    detail_list, skipped, sanitized = [], 0, 0
    for it in items:
        detail = it.get("requirementDetailInfo")
        if is_blank(detail):
            skipped += 1
            continue
        if not args.no_sanitize:
            if looks_like_html(detail):
                sanitized += 1
            detail = to_plain_text(detail)
        detail_list.append({"logicId": str(it.get("logicid") or it.get("logicId")),
                            "requirementDetailInfo": detail})

    payload = {}
    if version_id:
        payload["versionId"] = version_id
    payload["detailInfoList"] = detail_list

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info("save_payload -> %s ｜ versionId=%s，待保存 %d，跳过(未生成) %d，清洗掉HTML %d 条",
                args.output, version_id or "(缺失,入库前须补)", len(detail_list), skipped, sanitized)
    if not version_id:
        logger.warning("未获取到 versionId，批量保存需要它做权限校验，请补充。")


if __name__ == "__main__":
    main()
