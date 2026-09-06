#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并 IR 语义分类结果，生成完整需求树和覆盖报告。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Sequence, Tuple

logger = logging.getLogger(__name__)

L1Path = str
L2Path = Tuple[str, str]
PYTHON_SCAN_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
}
FORBIDDEN_CLASSIFIER_SCRIPTS = {
    "extract_nodes.py",
    "classify_ir.py",
    "classify_ir_complete.py",
}

for console_stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(console_stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="机械合并子 Agent 已完成的语义分类；本脚本不读取需求文本做分类。"
    )
    parser.add_argument("--requirements", default="requirements_transformed.json")
    parser.add_argument(
        "--rules",
        help="可选；默认按本 Skill 的 product_line_mapping.json 自动选择",
    )
    parser.add_argument(
        "--manifest", default="requirements_ir_work/ir_manifest.json"
    )
    parser.add_argument(
        "--audit-results-dir",
        default="requirements_ir_work/audit-results",
        help="全局反向复核的追加分类目录",
    )
    parser.add_argument("--product-line", required=True)
    parser.add_argument(
        "--resolved-package",
        help="可选；默认使用产品线映射命中的 packages 键",
    )
    parser.add_argument("--execution-mode", default="parallel-by-ir")
    parser.add_argument("--max-concurrency", type=int, default=5)
    parser.add_argument(
        "--validate-result",
        help="只机械校验一个 IR resultFile；不合并、不生成最终文件",
    )
    parser.add_argument(
        "--expected-attempt",
        type=int,
        choices=(1, 2),
        help="配合 --validate-result，要求结果 attempts 与本次尝试一致",
    )
    parser.add_argument(
        "--global-audit-completed",
        action="store_true",
        help="只有完成全局规则到需求复核后才能设置",
    )
    parser.add_argument(
        "--classified-output", default="requirements_classified.json"
    )
    parser.add_argument(
        "--report-json", default="requirements_coverage_report.json"
    )
    parser.add_argument("--report-md", default="requirements_coverage_report.md")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def atomic_dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temp_path, path)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        if not value.endswith("\n"):
            stream.write("\n")
    os.replace(temp_path, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def display_path(path: Path, workspace: Path) -> str:
    return Path(os.path.relpath(path, workspace)).as_posix()


def scan_workspace_python_files(workspace: Path) -> Dict[str, str]:
    files: Dict[str, str] = {}
    for root, dir_names, file_names in os.walk(workspace):
        dir_names[:] = [
            name
            for name in dir_names
            if name not in PYTHON_SCAN_EXCLUDED_DIRS
        ]
        root_path = Path(root)
        for file_name in file_names:
            if not file_name.casefold().endswith(".py"):
                continue
            path = root_path / file_name
            files[display_path(path, workspace)] = sha256_file(path)
    return dict(sorted(files.items()))


def validate_no_generated_python_scripts(
    manifest: Dict[str, Any],
    workspace: Path,
) -> None:
    baseline = manifest.get("workspacePythonBaseline")
    if not isinstance(baseline, dict):
        raise ValueError(
            "任务清单缺少 workspacePythonBaseline；请重新执行 "
            "prepare_ir_tasks.py 生成新版任务清单"
        )
    current = scan_workspace_python_files(workspace)
    forbidden = [
        path
        for path in current
        if Path(path).name.casefold() in FORBIDDEN_CLASSIFIER_SCRIPTS
        or Path(path).name.casefold().startswith("classify_ir")
    ]
    if forbidden:
        raise ValueError(
            "检测到禁止使用的分类辅助脚本："
            f"{forbidden}；结果不得通过校验"
        )
    added_or_modified = [
        path
        for path, digest in current.items()
        if baseline.get(path) != digest
    ]
    if added_or_modified:
        raise ValueError(
            "分类任务开始后新增或修改了 Python 脚本："
            f"{added_or_modified}。叶子 Agent 只能直接语义分类，"
            "不得创建或修改辅助脚本"
        )


def walk_nodes(node: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    yield node
    children = node.get("children")
    if children is None:
        return
    if not isinstance(children, list):
        raise ValueError(
            f"logicid={node.get('logicid', '')} 的 children 必须是数组或 null"
        )
    for child in children:
        if not isinstance(child, dict):
            raise ValueError("children 中存在非对象节点")
        yield from walk_nodes(child)


def load_rule_paths(
    rules_path: Path,
) -> Tuple[List[L1Path], List[L2Path], set[L1Path], set[L2Path]]:
    rules = load_json(rules_path)
    if not isinstance(rules, dict) or not isinstance(rules.get("data"), list):
        raise ValueError("规则文件根节点必须包含 data 数组")

    l1_order: List[L1Path] = []
    l2_order: List[L2Path] = []
    l1_seen: set[L1Path] = set()
    l2_seen: set[L2Path] = set()

    for l1_node in rules["data"]:
        if not isinstance(l1_node, dict):
            raise ValueError("规则 data 中存在非对象元素")
        l1_name = str(l1_node.get("一级需求", "")).strip()
        if not l1_name:
            raise ValueError("规则 data 中存在空一级需求")
        l1_path = l1_name
        if l1_path not in l1_seen:
            l1_seen.add(l1_path)
            l1_order.append(l1_path)
        l2_children = l1_node.get("children") or []
        if not isinstance(l2_children, list):
            raise ValueError(f"一级规则 {l1_path} 的 children 不是数组")
        for l2_node in l2_children:
            if not isinstance(l2_node, dict):
                raise ValueError("二级规则不是对象")
            l2_name = str(l2_node.get("二级需求", "")).strip()
            if not l2_name:
                raise ValueError(f"一级规则 {l1_path} 存在空二级需求")
            l2_path = (l1_name, l2_name)
            if l2_path not in l2_seen:
                l2_seen.add(l2_path)
                l2_order.append(l2_path)

    return l1_order, l2_order, l1_seen, l2_seen


def normalize_product_name(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value).casefold()


def resolve_rule_file(args: argparse.Namespace) -> Tuple[Path, str]:
    if args.rules:
        rules_path = Path(args.rules)
        resolved_package = args.resolved_package or args.product_line
        return rules_path, resolved_package

    classifier_dir = Path(__file__).resolve().parent.parent
    mapping_path = classifier_dir / "references" / "product_line_mapping.json"
    mapping = load_json(mapping_path)
    packages = mapping.get("packages") if isinstance(mapping, dict) else None
    if not isinstance(packages, dict):
        raise ValueError("product_line_mapping.json 缺少 packages 对象")

    target = normalize_product_name(args.product_line)
    for package_name, config in packages.items():
        if not isinstance(config, dict):
            continue
        candidates = [str(package_name), str(config.get("canonical_name", ""))]
        aliases = config.get("aliases") or []
        if isinstance(aliases, list):
            candidates.extend(str(value) for value in aliases)
        if target not in {normalize_product_name(value) for value in candidates}:
            continue
        rule_file = str(config.get("rule_file", "")).strip()
        if not rule_file:
            raise ValueError(f"产品线 {package_name} 缺少 rule_file")
        return classifier_dir / "references" / rule_file, str(package_name)

    raise ValueError(f"productLine={args.product_line!r} 未命中任何产品线映射")


def normalize_classification(
    raw_value: Any,
    logicid: str,
    legal_l1: set[L1Path],
    legal_l2: set[L2Path],
) -> List[Dict[str, Any]]:
    if not isinstance(raw_value, list):
        raise ValueError(f"logicid={logicid} 的 classification 必须是数组")

    normalized: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str | None]] = set()
    for index, item in enumerate(raw_value):
        if not isinstance(item, dict):
            raise ValueError(
                f"logicid={logicid} 的 classification[{index}] 不是对象"
            )
        expected_keys = {"一级需求", "二级需求"}
        actual_keys = set(item)
        if actual_keys != expected_keys:
            raise ValueError(
                f"logicid={logicid} 的 classification[{index}] 字段必须严格为"
                f"{sorted(expected_keys)}；当前字段={sorted(actual_keys)}"
            )
        l1_name = str(item.get("一级需求", "")).strip()
        raw_l2 = item.get("二级需求")
        l2_name = None if raw_l2 is None else str(raw_l2).strip()
        if not l1_name:
            raise ValueError(f"logicid={logicid} 存在空一级需求")
        if l2_name == "":
            raise ValueError(
                f"logicid={logicid} 的空二级需求必须写为 null，不得写空字符串"
            )

        l1_path = l1_name
        if l1_path not in legal_l1:
            raise ValueError(
                f"logicid={logicid} 使用非法一级路径：{l1_path}。"
                "一级需求必须逐字存在于规则 data 中；"
                "不得把二级需求上移为一级需求"
            )
        if l2_name is not None:
            l2_path = (l1_name, l2_name)
            if l2_path not in legal_l2:
                raise ValueError(
                    f"logicid={logicid} 使用非法二级路径：{l2_path}。"
                    "一级需求、二级需求必须来自同一条合法规则路径，"
                    "不得跨层级或跨分支拼接"
                )

        signature = (l1_name, l2_name)
        if signature in seen:
            continue
        seen.add(signature)
        normalized.append(
            {
                "一级需求": l1_name,
                "二级需求": l2_name,
            }
        )
    return normalized


def build_node_index(tree: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    data = tree.get("data")
    if not isinstance(data, list):
        raise ValueError("需求树根节点的 data 必须是数组")
    index: Dict[str, Dict[str, Any]] = {}
    for top_node in data:
        if not isinstance(top_node, dict):
            raise ValueError("需求树 data 中存在非对象元素")
        for node in walk_nodes(top_node):
            logicid = str(node.get("logicid", "")).strip()
            if not logicid:
                raise ValueError("需求树存在缺少 logicid 的节点")
            if logicid in index:
                raise ValueError(f"需求树 logicid 重复：{logicid}")
            index[logicid] = node
    return index


def resolve_result_path(task: Dict[str, Any]) -> Path:
    result_file = task.get("resultFile")
    if not isinstance(result_file, str) or not result_file.strip():
        raise ValueError(f"任务 {task.get('taskKey')} 缺少 resultFile")
    return Path(result_file)


def validate_ir_result(
    original_ir: Dict[str, Any],
    task: Dict[str, Any],
    expected_index: int,
    legal_l1: set[L1Path],
    legal_l2: set[L2Path],
    *,
    result_path: Path | None = None,
    expected_attempt: int | None = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], int, int]:
    ir_index = task.get("irIndex")
    if ir_index != expected_index:
        raise ValueError(
            f"任务顺序错误：位置 {expected_index} 的 irIndex={ir_index}"
        )
    expected_logicid = str(original_ir.get("logicid", "")).strip()
    if str(task.get("logicid", "")).strip() != expected_logicid:
        raise ValueError(f"IR[{ir_index}] 的任务 logicid 与原树不一致")

    expected_result_path = resolve_result_path(task)
    actual_result_path = result_path or expected_result_path
    if actual_result_path.resolve() != expected_result_path.resolve():
        raise ValueError(
            f"指定结果文件 {actual_result_path} 不属于任务 "
            f"{task.get('taskKey')}；预期 {expected_result_path}"
        )
    if not actual_result_path.is_file():
        raise FileNotFoundError(f"IR 结果文件不存在：{actual_result_path}")

    result = load_json(actual_result_path)
    if not isinstance(result, dict):
        raise ValueError(f"{actual_result_path} 的根节点不是对象")
    if result.get("irIndex") != ir_index:
        raise ValueError(f"{actual_result_path} 的 irIndex 不匹配")
    if str(result.get("irLogicid", "")).strip() != expected_logicid:
        raise ValueError(f"{actual_result_path} 的 irLogicid 不匹配")
    attempts = result.get("attempts")
    if not isinstance(attempts, int) or attempts < 1 or attempts > 2:
        raise ValueError(f"{actual_result_path} 的 attempts 必须为 1 或 2")
    if expected_attempt is not None and attempts != expected_attempt:
        raise ValueError(
            f"{actual_result_path} 的 attempts={attempts}，"
            f"本次任务要求 attempts={expected_attempt}"
        )

    entries = result.get("classifications")
    if not isinstance(entries, list):
        raise ValueError(f"{actual_result_path} 缺少 classifications 数组")
    returned: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(
                f"{actual_result_path} 的 classifications 存在非对象"
            )
        expected_entry_keys = {"logicid", "classification"}
        actual_entry_keys = set(entry)
        if actual_entry_keys != expected_entry_keys:
            raise ValueError(
                f"{actual_result_path} 的 classifications 项字段必须严格为"
                f"{sorted(expected_entry_keys)}；当前字段={sorted(actual_entry_keys)}"
            )
        logicid = str(entry.get("logicid", "")).strip()
        if not logicid:
            raise ValueError(f"{actual_result_path} 存在空 logicid")
        if logicid in returned:
            raise ValueError(
                f"{actual_result_path} 重复返回 logicid={logicid}"
            )
        returned[logicid] = normalize_classification(
            entry.get("classification"), logicid, legal_l1, legal_l2
        )

    ir_nodes = list(walk_nodes(original_ir))
    expected_node_count = task.get("nodeCount")
    if expected_node_count != len(ir_nodes):
        raise ValueError(
            f"任务 {task.get('taskKey')} 的 nodeCount={expected_node_count}，"
            f"实际子树节点数={len(ir_nodes)}"
        )
    expected_ids = {str(node.get("logicid", "")).strip() for node in ir_nodes}
    returned_ids = set(returned)
    if returned_ids != expected_ids:
        missing = sorted(expected_ids - returned_ids)
        extra = sorted(returned_ids - expected_ids)
        raise ValueError(
            f"{actual_result_path} 未完整覆盖自己的 IR 子树；"
            f"缺失logicid={missing}，越界logicid={extra}"
        )
    return returned, attempts, len(ir_nodes)


def apply_ir_results(
    full_tree: Dict[str, Any],
    manifest: Dict[str, Any],
    legal_l1: set[L1Path],
    legal_l2: set[L2Path],
) -> Tuple[int, int]:
    data = full_tree.get("data")
    tasks = manifest.get("tasks")
    if not isinstance(data, list) or not isinstance(tasks, list):
        raise ValueError("需求树 data 或任务清单 tasks 格式错误")
    if len(tasks) != len(data):
        raise ValueError(
            f"任务数 {len(tasks)} 与顶层 IR 数 {len(data)} 不一致"
        )

    completed = 0
    retry_count = 0
    missing_files: List[str] = []

    for expected_index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError("任务清单中存在非对象任务")
        result_path = resolve_result_path(task)
        if not result_path.is_file():
            missing_files.append(result_path.as_posix())
            continue
        original_ir = data[expected_index]
        returned, attempts, _ = validate_ir_result(
            original_ir,
            task,
            expected_index,
            legal_l1,
            legal_l2,
            result_path=result_path,
        )
        retry_count += attempts - 1
        ir_nodes = list(walk_nodes(original_ir))
        for node in ir_nodes:
            node["classification"] = returned[str(node["logicid"])]
        completed += 1

    if missing_files:
        raise ValueError(
            "以下 IR 结果文件不存在，禁止生成部分报告：" + ", ".join(missing_files)
        )
    return completed, retry_count


def apply_audit_results(
    full_tree: Dict[str, Any],
    audit_results_dir: Path,
    legal_l1: set[L1Path],
    legal_l2: set[L2Path],
) -> int:
    if not audit_results_dir.exists():
        return 0
    node_index = build_node_index(full_tree)
    applied = 0
    for result_path in sorted(audit_results_dir.glob("*.json")):
        result = load_json(result_path)
        if not isinstance(result, dict):
            raise ValueError(f"{result_path} 的根节点不是对象")
        additions = result.get("additions")
        if not isinstance(additions, list):
            raise ValueError(f"{result_path} 缺少 additions 数组")
        for entry in additions:
            if not isinstance(entry, dict):
                raise ValueError(f"{result_path} 的 additions 存在非对象")
            logicid = str(entry.get("logicid", "")).strip()
            if logicid not in node_index:
                raise ValueError(f"{result_path} 引用了不存在的 logicid={logicid}")
            new_items = normalize_classification(
                entry.get("classification"), logicid, legal_l1, legal_l2
            )
            current = node_index[logicid].get("classification")
            if not isinstance(current, list):
                raise ValueError(f"logicid={logicid} 尚未完成首轮分类")
            signatures = {
                (item["一级需求"], item["二级需求"])
                for item in current
            }
            for item in new_items:
                signature = (item["一级需求"], item["二级需求"])
                if signature not in signatures:
                    current.append(item)
                    signatures.add(signature)
                    applied += 1
    return applied


def percent(part: int, total: int) -> float:
    return round(part / total * 100, 2) if total else 100.0


def build_report(
    full_tree: Dict[str, Any],
    l1_order: Sequence[L1Path],
    l2_order: Sequence[L2Path],
    args: argparse.Namespace,
    completed_count: int,
    retry_count: int,
) -> Dict[str, Any]:
    node_index = build_node_index(full_tree)
    l1_hits: DefaultDict[L1Path, set[str]] = defaultdict(set)
    l2_hits: DefaultDict[L2Path, set[str]] = defaultdict(set)
    mapped_count = 0
    classification_path_count = 0
    one_to_many_count = 0

    for logicid, node in node_index.items():
        classifications = node.get("classification")
        if not isinstance(classifications, list):
            raise ValueError(f"logicid={logicid} 缺少 classification 数组")
        if classifications:
            mapped_count += 1
        if len(classifications) > 1:
            one_to_many_count += 1
        classification_path_count += len(classifications)
        for item in classifications:
            l1_path = item["一级需求"]
            l1_hits[l1_path].add(logicid)
            if item["二级需求"] is not None:
                l2_path = (
                    item["一级需求"],
                    item["二级需求"],
                )
                l2_hits[l2_path].add(logicid)

    covered_l1 = set(l1_hits)
    covered_l2 = set(l2_hits)
    missing_l1 = [path for path in l1_order if path not in covered_l1]
    missing_l2 = [path for path in l2_order if path not in covered_l2]
    total_nodes = len(node_index)
    task_count = len(full_tree.get("data", []))

    return {
        "productLine": args.product_line,
        "resolvedPackage": args.resolved_package,
        "ruleFile": Path(args.rules).name,
        "完整覆盖": not missing_l1 and not missing_l2,
        "classificationMethod": "llm-semantic",
        "executionMode": args.execution_mode,
        "parallelExecution": {
            "IR任务总数": task_count,
            "最大并发数": min(args.max_concurrency, task_count)
            if task_count > 1
            else 1,
            "已完成IR任务数": completed_count,
            "失败IR任务数": task_count - completed_count,
            "重试IR任务数": retry_count,
        },
        "semanticAudit": {
            "需求到规则复核完成": completed_count == task_count,
            "规则到需求复核完成": args.global_audit_completed,
            "使用关键词或硬编码分类": False,
        },
        "统计": {
            "需求节点映射": {
                "总数": total_nodes,
                "已映射数": mapped_count,
                "未映射数": total_nodes - mapped_count,
                "映射率": percent(mapped_count, total_nodes),
            },
            "分类路径总数": classification_path_count,
            "一对多节点数": one_to_many_count,
            "一级需求": {
                "总数": len(l1_order),
                "已覆盖数": len(covered_l1),
                "未覆盖数": len(missing_l1),
                "覆盖率": percent(len(covered_l1), len(l1_order)),
            },
            "二级需求": {
                "总数": len(l2_order),
                "已覆盖数": len(covered_l2),
                "未覆盖数": len(missing_l2),
                "覆盖率": percent(len(covered_l2), len(l2_order)),
            },
        },
        "未覆盖一级需求": [
            {"一级需求": path, "二级需求": None} for path in missing_l1
        ],
        "未覆盖二级需求": [
            {
                "一级需求": path[0],
                "二级需求": path[1],
            }
            for path in missing_l2
        ],
        "一级需求覆盖明细": [
            {
                "一级需求": path,
                "二级需求": None,
                "命中需求logicid": sorted(l1_hits[path]),
            }
            for path in l1_order
            if path in l1_hits
        ],
        "二级需求覆盖明细": [
            {
                "一级需求": path[0],
                "二级需求": path[1],
                "命中需求logicid": sorted(l2_hits[path]),
            }
            for path in l2_order
            if path in l2_hits
        ],
    }


def build_markdown(report: Dict[str, Any]) -> str:
    stats = report["统计"]
    node_stats = stats["需求节点映射"]
    l1_stats = stats["一级需求"]
    l2_stats = stats["二级需求"]
    conclusion = (
        "所有规则均已覆盖，需求完备度校验通过"
        if report["完整覆盖"]
        else "存在未覆盖规则，请查看下方清单"
    )

    lines = [
        "# 需求完备度校验报告",
        "",
        "## 结论",
        "",
        conclusion,
        "",
        "## 基本信息",
        "",
        f"- 产品线：{report['productLine']}",
        f"- 规则文件：{report['ruleFile']}",
        f"- 分类方法：{report['classificationMethod']}",
        f"- 执行模式：{report['executionMode']}",
        "",
        "## 节点映射情况",
        "",
        "| 指标 | 值 |",
        "|---|---:|",
        f"| 需求节点总数 | {node_stats['总数']} |",
        f"| 已映射节点数 | {node_stats['已映射数']} |",
        f"| 未映射节点数 | {node_stats['未映射数']} |",
        f"| 映射率 | {node_stats['映射率']:.2f}% |",
        "",
        "## 一级/二级覆盖统计",
        "",
        "| 维度 | 总数 | 已覆盖 | 未覆盖 | 覆盖率 |",
        "|---|---:|---:|---:|---:|",
        (
            f"| 一级需求 | {l1_stats['总数']} | {l1_stats['已覆盖数']} | "
            f"{l1_stats['未覆盖数']} | {l1_stats['覆盖率']:.2f}% |"
        ),
        (
            f"| 二级需求 | {l2_stats['总数']} | {l2_stats['已覆盖数']} | "
            f"{l2_stats['未覆盖数']} | {l2_stats['覆盖率']:.2f}% |"
        ),
        "",
        f"## 未覆盖一级需求（{len(report['未覆盖一级需求'])}项）",
        "",
    ]
    if report["未覆盖一级需求"]:
        lines.extend(
            f"- {item['一级需求']}"
            for item in report["未覆盖一级需求"]
        )
    else:
        lines.append("无")

    lines.extend(
        [
            "",
            f"## 未覆盖二级需求（{len(report['未覆盖二级需求'])}项）",
            "",
        ]
    )
    if report["未覆盖二级需求"]:
        lines.extend(
            f"- {item['一级需求']} → {item['二级需求']}"
            for item in report["未覆盖二级需求"]
        )
    else:
        lines.append("无")

    lines.extend(
        [
            "",
            f"## 一级需求覆盖明细（{len(report['一级需求覆盖明细'])}项）",
            "",
        ]
    )
    for item in report["一级需求覆盖明细"]:
        hits = ", ".join(item["命中需求logicid"])
        lines.append(f"- {item['一级需求']}：{hits}")

    lines.extend(
        [
            "",
            f"## 二级需求覆盖明细（{len(report['二级需求覆盖明细'])}项）",
            "",
        ]
    )
    for item in report["二级需求覆盖明细"]:
        hits = ", ".join(item["命中需求logicid"])
        lines.append(
            f"- {item['一级需求']} → "
            f"{item['二级需求']}：{hits}"
        )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.max_concurrency < 1 or args.max_concurrency > 5:
        raise ValueError("--max-concurrency 必须在 1 到 5 之间")

    requirements_path = Path(args.requirements)
    rules_path, resolved_package = resolve_rule_file(args)
    args.rules = str(rules_path)
    args.resolved_package = resolved_package
    manifest_path = Path(args.manifest)
    if not requirements_path.is_file():
        raise FileNotFoundError(f"需求树不存在：{requirements_path}")
    if not rules_path.is_file():
        raise FileNotFoundError(f"规则文件不存在：{rules_path}")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"任务清单不存在：{manifest_path}")

    original_tree = load_json(requirements_path)
    manifest = load_json(manifest_path)
    if not isinstance(original_tree, dict) or not isinstance(manifest, dict):
        raise ValueError("需求树和任务清单的根节点必须是对象")
    if manifest.get("sourceSha256") != sha256_file(requirements_path):
        raise ValueError("任务清单与当前 requirements_transformed.json 不匹配")
    if manifest.get("resolvedPackage") != resolved_package:
        raise ValueError("任务清单与当前 productLine 的映射结果不匹配")
    if manifest.get("ruleSha256") != sha256_file(rules_path):
        raise ValueError("任务清单使用的规则文件与当前规则文件不匹配")
    validate_no_generated_python_scripts(manifest, Path.cwd().resolve())

    l1_order, l2_order, legal_l1, legal_l2 = load_rule_paths(rules_path)

    if args.validate_result:
        if args.expected_attempt is None:
            raise ValueError(
                "--validate-result 必须同时提供 --expected-attempt 1 或 2"
            )
        data = original_tree.get("data")
        tasks = manifest.get("tasks")
        if not isinstance(data, list) or not isinstance(tasks, list):
            raise ValueError("需求树 data 或任务清单 tasks 格式错误")
        requested_path = Path(args.validate_result)
        requested_resolved = requested_path.resolve()
        matches = [
            index
            for index, task in enumerate(tasks)
            if isinstance(task, dict)
            and resolve_result_path(task).resolve() == requested_resolved
        ]
        if len(matches) != 1:
            raise ValueError(
                f"--validate-result={requested_path} 未唯一命中任务清单中的 "
                "resultFile"
            )
        ir_index = matches[0]
        returned, attempts, node_count = validate_ir_result(
            data[ir_index],
            tasks[ir_index],
            ir_index,
            legal_l1,
            legal_l2,
            result_path=requested_path,
            expected_attempt=args.expected_attempt,
        )
        logger.info(
            json.dumps(
                {
                    "status": "VALID",
                    "taskKey": tasks[ir_index].get("taskKey"),
                    "irIndex": ir_index,
                    "irLogicid": tasks[ir_index].get("logicid"),
                    "resultFile": requested_path.as_posix(),
                    "attempts": attempts,
                    "nodeCount": node_count,
                    "mappedNodeCount": sum(
                        1 for items in returned.values() if items
                    ),
                    "classificationPathCount": sum(
                        len(items) for items in returned.values()
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.expected_attempt is not None:
        raise ValueError(
            "--expected-attempt 只能与 --validate-result 一起使用"
        )
    full_tree = copy.deepcopy(original_tree)
    completed_count, retry_count = apply_ir_results(
        full_tree, manifest, legal_l1, legal_l2
    )
    audit_additions = apply_audit_results(
        full_tree, Path(args.audit_results_dir), legal_l1, legal_l2
    )

    report = build_report(
        full_tree,
        l1_order,
        l2_order,
        args,
        completed_count,
        retry_count,
    )
    atomic_dump_json(Path(args.classified_output), full_tree)
    atomic_dump_json(Path(args.report_json), report)
    atomic_write_text(Path(args.report_md), build_markdown(report))

    logger.info(
        json.dumps(
            {
                "status": "DONE",
                "classifiedOutput": args.classified_output,
                "reportJson": args.report_json,
                "reportMarkdown": args.report_md,
                "irTasks": completed_count,
                "auditAdditions": audit_additions,
                "mappedNodes": report["统计"]["需求节点映射"]["已映射数"],
                "totalNodes": report["统计"]["需求节点映射"]["总数"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        logger.error("ERROR: %s", exc)
        sys.exit(2)
