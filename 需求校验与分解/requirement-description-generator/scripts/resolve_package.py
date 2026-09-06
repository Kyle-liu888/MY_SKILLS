#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
产品线 -> 经验包目录 解析器

按固定优先级解析，结果确定、可回溯（不依赖模型"看着办"）：
    1) 直接命中：packages/<productLine>/ 目录存在
    2) 规范化命中：忽略大小写与 空格/下划线/连字符 差异
       （如 "Data Communication" -> packages/Data_Communication）
    3) 映射表命中：references/product_line_mapping.json 的 aliases 反查
       （如 "Optical Business" -> Data_Communication）
    4) 都不中 -> 退出码 2，打印可用包与已知别名，交由上层置 BLOCKED

用法：
    python resolve_package.py "Optical Business" \
        [--packages-root references/packages] \
        [--mapping references/product_line_mapping.json]

输出（stdout 第一行为结果路径，便于脚本取用）：
    <解析出的经验包目录路径>
并在 stderr 打印解析理由。退出码 0=成功，2=无法解析。
"""

import argparse
import json
import logging
import os
import sys

logger = logging.getLogger(__name__)


def norm(s):
    """规范化：小写 + 去掉空格/下划线/连字符。"""
    return "".join(str(s or "").lower().replace("-", " ").replace("_", " ").split())


def load_mapping(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("packages", {})


def resolve(product_line, packages_root, mapping_path):
    if not product_line or not str(product_line).strip():
        return None, "未提供 productLine"

    available = []
    if os.path.isdir(packages_root):
        available = sorted(d for d in os.listdir(packages_root)
                           if os.path.isdir(os.path.join(packages_root, d)))

    # 1) 直接命中
    if product_line in available:
        return os.path.join(packages_root, product_line), "直接命中同名经验包目录"

    # 2) 规范化命中
    target = norm(product_line)
    for d in available:
        if norm(d) == target:
            return os.path.join(packages_root, d), "规范化命中目录 %s（忽略大小写/空格/下划线）" % d

    # 3) 映射表反查
    packages = load_mapping(mapping_path)
    for pkg, info in packages.items():
        for alias in info.get("aliases", []):
            if norm(alias) == target:
                # 映射目标目录仍需存在（规范化再匹配一次）
                for d in available:
                    if norm(d) == norm(pkg):
                        return (os.path.join(packages_root, d),
                                "经映射表：匹配产品线 '%s' -> 产品线 '%s'（经验包 %s）"
                                % (alias, info.get("canonical_name", pkg), d))
                return None, ("映射表把 '%s' 指向 '%s'，但 packages/ 下没有该经验包目录"
                              % (product_line, pkg))

    return None, ("无法解析 '%s'：既非现有经验包目录，也不在映射表 aliases 中" % product_line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("product_line")
    ap.add_argument("--packages-root", default="references/packages")
    ap.add_argument("--mapping", default="references/product_line_mapping.json")
    args = ap.parse_args()

    path, reason = resolve(args.product_line, args.packages_root, args.mapping)
    if path:
        logger.info(path)
        logger.info("[resolve] %s", reason)
        sys.exit(0)

    logger.error("[resolve] 失败：%s", reason)
    if os.path.isdir(args.packages_root):
        avail = sorted(d for d in os.listdir(args.packages_root)
                       if os.path.isdir(os.path.join(args.packages_root, d)))
        logger.info("  可用经验包：%s", ", ".join(avail) or "(无)")
    packages = load_mapping(args.mapping)
    if packages:
        logger.info("  映射表已知的匹配产品线：")
        for pkg, info in packages.items():
            logger.info("    %-20s <- %s", pkg, "、".join(info.get("aliases", [])))
    logger.error(
        "  处理：置 BLOCKED，请用户确认产品线，或先为其构建经验包/在映射表中补一条别名。"
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
