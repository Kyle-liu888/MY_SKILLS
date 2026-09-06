#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从完整需求树一次性生成 IR 子树、短派发 Prompt 和任务清单。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

logger = logging.getLogger(__name__)

PROMPT_SCHEMA_VERSION = 4
SEMANTIC_BATCH_NODE_LIMIT = 20
LARGE_IR_NODE_THRESHOLD = 40
LARGE_IR_BYTE_THRESHOLD = 60_000
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按顶层 data 顺序提取每个 IR 的完整子树，生成可直接交给子 Agent 的独立文件。"
    )
    parser.add_argument(
        "--requirements",
        default="requirements_transformed.json",
        help="完整需求树 JSON；默认 requirements_transformed.json",
    )
    parser.add_argument(
        "--output-dir",
        default="requirements_ir_work",
        help="任务工作目录；默认 requirements_ir_work",
    )
    parser.add_argument(
        "--product-line",
        required=True,
        help="用户原始 productLine，用于选择并复制本次唯一规则文件",
    )
    return parser.parse_args()


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} 的根节点必须是 JSON 对象")
    return value


def atomic_dump(path: Path, value: Any) -> None:
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


def reject_forbidden_classifier_scripts(workspace: Path) -> None:
    matches = [
        path
        for path in scan_workspace_python_files(workspace)
        if Path(path).name.casefold() in FORBIDDEN_CLASSIFIER_SCRIPTS
        or Path(path).name.casefold().startswith("classify_ir")
    ]
    if matches:
        raise ValueError(
            "工作区存在禁止使用的分类辅助脚本："
            f"{matches}。请移出这些脚本后重新执行，避免子 Agent 复用关键词分类。"
        )


def semantic_fields(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "logicid": str(node.get("logicid", "")),
        "categorys": node.get("categorys"),
        "name": node.get("name"),
        "description": node.get("description"),
        "requirementDetailInfo": node.get("requirementDetailInfo"),
    }


def semantic_node_records(
    node: Dict[str, Any],
    parent: Dict[str, Any] | None = None,
) -> Iterable[Dict[str, Any]]:
    children = node.get("children") or []
    yield {
        "node": semantic_fields(node),
        "parent": semantic_fields(parent) if parent is not None else None,
        "children": [
            semantic_fields(child)
            for child in children
            if isinstance(child, dict)
        ],
    }
    for child in children:
        if not isinstance(child, dict):
            raise ValueError("children 中存在非对象节点")
        yield from semantic_node_records(child, node)


def create_semantic_batches(
    subtree: Dict[str, Any],
    ir_index: int,
    logicid: str,
    file_stem: str,
    batches_dir: Path,
    workspace: Path,
) -> Tuple[str, str | None, int]:
    records = list(semantic_node_records(subtree))
    serialized_bytes = len(
        json.dumps(subtree, ensure_ascii=False).encode("utf-8")
    )
    if (
        len(records) <= LARGE_IR_NODE_THRESHOLD
        and serialized_bytes <= LARGE_IR_BYTE_THRESHOLD
    ):
        return "full-subtree", None, 0

    ir_batches_dir = batches_dir / file_stem
    chunks = [
        records[index : index + SEMANTIC_BATCH_NODE_LIMIT]
        for index in range(0, len(records), SEMANTIC_BATCH_NODE_LIMIT)
    ]
    batch_files: List[str] = []
    for batch_index, chunk in enumerate(chunks, start=1):
        batch_path = ir_batches_dir / f"batch_{batch_index:03d}.json"
        atomic_dump(
            batch_path,
            {
                "irIndex": ir_index,
                "irLogicid": logicid,
                "batchIndex": batch_index,
                "batchCount": len(chunks),
                "nodeCount": len(chunk),
                "records": chunk,
            },
        )
        batch_files.append(display_path(batch_path, workspace))

    index_path = ir_batches_dir / "batch_index.json"
    atomic_dump(
        index_path,
        {
            "irIndex": ir_index,
            "irLogicid": logicid,
            "totalNodeCount": len(records),
            "batchNodeLimit": SEMANTIC_BATCH_NODE_LIMIT,
            "batchCount": len(chunks),
            "batchFiles": batch_files,
        },
    )
    return (
        "semantic-batches",
        display_path(index_path, workspace),
        len(chunks),
    )


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._")
    return (cleaned or "no-logicid")[:80]


def display_path(path: Path, workspace: Path) -> str:
    return Path(os.path.relpath(path, workspace)).as_posix()


def normalize_product_name(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value).casefold()


def resolve_rule_file(product_line: str) -> tuple[str, Path]:
    classifier_dir = Path(__file__).resolve().parent.parent
    mapping_path = classifier_dir / "references" / "product_line_mapping.json"
    mapping = load_json(mapping_path)
    packages = mapping.get("packages")
    if not isinstance(packages, dict):
        raise ValueError("product_line_mapping.json 缺少 packages 对象")

    target = normalize_product_name(product_line)
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
        rule_path = classifier_dir / "references" / rule_file
        if not rule_path.is_file():
            raise FileNotFoundError(f"规则文件不存在：{rule_path}")
        return str(package_name), rule_path
    raise ValueError(f"productLine={product_line!r} 未命中任何产品线映射")


def build_legal_path_whitelist(
    rule_tree: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    data = rule_tree.get("data")
    if not isinstance(data, list):
        raise ValueError("规则文件根节点必须包含 data 数组")

    legal_paths: List[Dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    l1_count = 0
    l2_count = 0

    for l1_node in data:
        if not isinstance(l1_node, dict):
            raise ValueError("规则 data 中存在非对象元素")
        l1_name = str(l1_node.get("一级需求", "")).strip()
        if not l1_name:
            raise ValueError("规则 data 中存在空一级需求")
        l1_signature = (l1_name, None)
        if l1_signature not in seen:
            seen.add(l1_signature)
            legal_paths.append(
                {
                    "一级需求": l1_name,
                    "二级需求": None,
                }
            )
            l1_count += 1

        l2_nodes = l1_node.get("children") or []
        if not isinstance(l2_nodes, list):
            raise ValueError(f"一级规则 {l1_name} 的 children 不是数组")
        for l2_node in l2_nodes:
            if not isinstance(l2_node, dict):
                raise ValueError("二级规则不是对象")
            l2_name = str(l2_node.get("二级需求", "")).strip()
            if not l2_name:
                raise ValueError(f"一级规则 {l1_name} 存在空二级需求")
            l2_signature = (l1_name, l2_name)
            if l2_signature in seen:
                continue
            seen.add(l2_signature)
            legal_paths.append(
                {
                    "一级需求": l1_name,
                    "二级需求": l2_name,
                }
            )
            l2_count += 1

    return legal_paths, {
        "一级路径数": l1_count,
        "二级路径数": l2_count,
        "合法路径总数": len(legal_paths),
    }


def build_agent_prompt(
    product_line: str,
    task: Dict[str, Any],
    attempt: int,
    worker_instruction_file: str,
    validator_script: str,
    requirements_file: str,
    manifest_file: str,
) -> str:
    receipt_example = {
        "status": "completed",
        "irIndex": task["irIndex"],
        "irLogicid": task["logicid"],
        "resultFile": task["resultFile"],
        "nodeCount": task["nodeCount"],
    }
    receipt_json = json.dumps(receipt_example, ensure_ascii=False)
    retry_notice = (
        "这是第2次且最后一次尝试。上一份结果未通过机械合法性校验；"
        "覆盖旧结果并从头重做。\n"
        if attempt == 2
        else ""
    )
    validation_command = " ".join(
        [
            "python",
            shlex.quote(validator_script),
            "--requirements",
            shlex.quote(requirements_file),
            "--manifest",
            shlex.quote(manifest_file),
            "--product-line",
            shlex.quote(product_line),
            "--validate-result",
            shlex.quote(task["resultFile"]),
            "--expected-attempt",
            str(attempt),
        ]
    )
    if task["inputMode"] == "semantic-batches":
        input_instruction = (
            f"大IR语义批次索引：{task['semanticBatchIndexFile']}\n"
            f"语义批次数：{task['semanticBatchCount']}\n"
            "按索引顺序逐个完整读取批次文件。每条 record 已包含当前节点、"
            "直接父节点和直接子节点语义；全部批次合起来恰好覆盖本 IR。"
        )
    else:
        input_instruction = (
            f"IR子树文件：{task['inputFile']}\n"
            "完整读取该文件的 subtree。"
        )

    return f"""你是叶子 IR 语义分类 Agent。禁止创建任何子 Agent。
{retry_notice}本文件是脚本生成的完整派发 Prompt。直接执行，不得把它概括成另一份提示词。
严禁创建、修改或执行任何提取/分类辅助脚本，包括 extract_nodes.py、classify_ir.py、classify_ir_complete.py 或其它 *.py。除下方固定机械校验命令外，禁止调用 execute/shell/终端；节点提取和分类必须由你直接阅读并进行语义判断。

【任务】
productLine：{product_line}
执行规则文件：{worker_instruction_file}
产品线规则文件：{task["ruleFile"]}
输入模式：{task["inputMode"]}
{input_instruction}
结果文件：{task["resultFile"]}
IR[{task["irIndex"]}]，logicid={task["logicid"]}，name={task["name"]}，节点总数={task["nodeCount"]}，当前尝试次数={attempt}。

【固定执行顺序】
1. 完整读取“执行规则文件”，不得只看摘要。
2. 完整读取“产品线规则文件”，直接从其 data 树获取全部合法“一级需求→二级需求”路径；不得凭记忆缩写名称。
3. 按“输入模式”直接阅读全部节点。大 IR 必须按语义批次顺序逐批处理，不得自行抽取节点、生成关键词表、建立固定映射或编写程序；不得由你打开 requirements_transformed.json 或其它 IR。第6步固定校验脚本对源文件的机械读取不受此限制。
4. 严格按执行规则文件完成两轮语义分类，将 UTF-8 JSON 写入结果文件。attempts 必须为 {attempt}。
5. 写入后先逐节点做语义自检：每条路径必须与当前节点中的独立业务断言强相关；删除弱相关、父子机械继承、跨层拼接和规则文件中不存在的路径。
6. 亲自执行下面的固定机械校验命令：
{validation_command}
7. 只有命令退出状态为 0 且输出包含 `"status": "VALID"` 才能返回完成。若失败，按错误信息修正结果后再校验一次；仍失败则返回失败原因，禁止谎报完成。

【完成回执】
{receipt_json}
"""


def build_worker_instructions() -> str:
    return """# IR 叶子 Agent 固定执行规则

## 语义分类

1. 直接理解每个节点的 `name`、`description`、`requirementDetailInfo`，以及直接父节点、直接子节点和所在层级；当前节点始终是分类的主要证据。
2. 将当前节点拆成独立业务断言，分别识别要求动作或指标、条件和预期结果；没有并列要求时保持一个断言。
3. 只有当前断言与规则路径表达同一项要求或直接等价约束时才分类。允许同义表达、上下位表达和行业常用表达不同；仅主题相近、词面相同、背景、示例、引用标题、否定项或推测关系均不匹配。
4. 父子节点只提供语境，不得机械继承、复制或汇总分类。一个节点允许一对多，但每条路径都必须对应当前节点中的独立业务断言。
5. 禁止用关键词包含、正则、词频、字符串或向量相似度阈值、固定映射、脚本分类、`if/else` 规则或默认兜底决定分类。
6. 无合法路径时写 `classification: []`，继续处理其它节点；不生成待确认项，不伪造分类。

## 工具边界

1. 只能使用文件读取工具读取派发 Prompt 明确指定的执行规则、产品线规则和输入文件，使用文件写入工具写结果。
2. 禁止创建、修改、读取或执行 `extract_nodes.py`、`classify_ir.py`、`classify_ir_complete.py`、`classify*.py`、节点提取脚本、规则映射脚本及任何其它 Python、PowerShell 或批处理辅助程序。
3. 禁止调用 `execute`、shell、终端、Python、PowerShell、`jq`、`grep`、正则或字符串程序提取节点、生成映射、批量分类或拼装结果。
4. 唯一允许执行的命令是派发 Prompt 末尾完整给出的固定 `merge_and_report.py --validate-result` 机械校验命令；不得修改该命令或执行其它参数。
5. 节点再多也必须由当前模型逐条理解，不能改用关键词、固定映射、循环脚本、默认一级需求或统一填空。

## 大 IR 处理

1. `输入模式=full-subtree` 时，完整读取指定 IR 子树文件。
2. `输入模式=semantic-batches` 时，先读取批次索引，再严格按 `batchFiles` 顺序一次处理一个批次。每条 `record.node` 是当前节点，`record.parent` 和 `record.children` 是直接语境；全部批次合起来恰好覆盖完整 IR。
3. 每个批次仍逐节点执行语义判断，不得按词语相同批量复制路径。完成一个批次后继续下一个，不回头重做已完成批次。
4. 两轮复核必须覆盖所有批次。第二轮发现强相关补充时，只修改对应 `logicid` 的分类。
5. 直接维护最终 `classifications` 记录并用文件写入工具写入结果文件，不得编写提取、分类、合并或补全脚本。

## 合法路径

1. 从派发 Prompt 指定的产品线规则 JSON 的 `data` 树读取合法路径。
2. `一级需求` 只能是 `data` 中一级节点的原文；`二级需求` 只能是该一级直接 `children` 中的原文或 `null`。
3. 每条非空分类的两个字段必须来自同一条树路径并逐字复制。禁止上移、下移、重命名、缩写、重复填层级或跨路径拼接。
4. 明确命中二级规则时填写对应二级原文；只能可靠确定一级时填写该一级，并将 `二级需求` 设为 `null`。

## 两轮执行

1. 第一轮“需求→规则”：按输入树顺序深度优先处理；大 IR 按 SR 分支依次完成；每个节点首轮只判断一次，记录全部强相关合法路径。
2. 第二轮“规则→当前 IR”：逐条理解规则路径的完整业务含义，在当前 IR 子树中查找同义或间接表达；有强语义证据才补入对应节点，弱相关保持未覆盖。
3. 第二轮结束立即写结果，不得重新开始首轮或反复总结规则。

## 结果格式

结果文件必须是 UTF-8 JSON：

```json
{
  "irIndex": 0,
  "irLogicid": "当前IR logicid",
  "attempts": 1,
  "classifications": [
    {
      "logicid": "节点logicid",
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

- `irIndex`、`irLogicid`、`attempts` 使用派发 Prompt 中的当前值。
- `classifications` 恰好覆盖指定 IR 子树的全部 `logicid`，每个恰好一次，不能缺失、重复或越界。
- 每项只含 `logicid` 和 `classification`；每条分类只含 `一级需求`、`二级需求`。
- 不返回完整子树或原始大字段。

## 子 Agent 自校验

写入结果后必须先完成语义自检，再执行派发 Prompt 中的固定机械校验命令。机械校验会检查任务身份、尝试次数、节点全集、字段层级和合法路径。只有校验输出 `status: VALID` 才能返回完成回执；校验失败时按错误修正一次并重新执行，仍失败则明确返回失败。
"""


def ensure_prompt_files(
    manifest: Dict[str, Any],
    manifest_path: Path,
    output_dir: Path,
    workspace: Path,
    product_line: str,
    legal_paths: List[Dict[str, Any]],
    path_counts: Dict[str, int],
) -> Dict[str, Any]:
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("任务清单缺少 tasks 数组")
    prompts_dir = output_dir / "prompts"
    instructions_dir = output_dir / "instructions"
    batches_dir = output_dir / "semantic-batches"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    instructions_dir.mkdir(parents=True, exist_ok=True)
    batches_dir.mkdir(parents=True, exist_ok=True)
    worker_instruction_path = instructions_dir / "ir_worker_rules.md"
    atomic_write_text(worker_instruction_path, build_worker_instructions())
    worker_instruction_file = display_path(worker_instruction_path, workspace)
    validator_script = display_path(
        Path(__file__).resolve().parent / "merge_and_report.py", workspace
    )
    requirements_file = str(manifest.get("requirementsFile", "")).strip()
    if not requirements_file:
        raise ValueError("任务清单缺少 requirementsFile")
    manifest_file = display_path(manifest_path, workspace)

    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("任务清单中存在非对象任务")
        ir_index = task.get("irIndex")
        logicid = str(task.get("logicid", "")).strip()
        if not isinstance(ir_index, int) or not logicid:
            raise ValueError("任务清单存在无效 irIndex 或 logicid")
        file_stem = f"ir_{ir_index:03d}_{safe_component(logicid)}"
        input_file = str(task.get("inputFile", "")).strip()
        if not input_file:
            raise ValueError(f"任务 {task.get('taskKey')} 缺少 inputFile")
        input_path = Path(input_file)
        if not input_path.is_absolute():
            input_path = workspace / input_path
        task_payload = load_json(input_path)
        subtree = task_payload.get("subtree")
        if not isinstance(subtree, dict):
            raise ValueError(f"{input_path} 缺少 subtree 对象")
        input_mode, batch_index_file, batch_count = create_semantic_batches(
            subtree,
            ir_index,
            logicid,
            file_stem,
            batches_dir,
            workspace,
        )
        task["inputMode"] = input_mode
        task["semanticBatchIndexFile"] = batch_index_file
        task["semanticBatchCount"] = batch_count
        first_prompt_path = prompts_dir / f"{file_stem}.attempt_1.prompt.txt"
        retry_prompt_path = prompts_dir / f"{file_stem}.attempt_2.prompt.txt"
        task["promptFile"] = display_path(first_prompt_path, workspace)
        task["retryPromptFile"] = display_path(retry_prompt_path, workspace)
        task["workerInstructionFile"] = worker_instruction_file
        task["validatorScript"] = validator_script
        task["legalPathCount"] = len(legal_paths)
        atomic_write_text(
            first_prompt_path,
            build_agent_prompt(
                product_line,
                task,
                1,
                worker_instruction_file,
                validator_script,
                requirements_file,
                manifest_file,
            ),
        )
        atomic_write_text(
            retry_prompt_path,
            build_agent_prompt(
                product_line,
                task,
                2,
                worker_instruction_file,
                validator_script,
                requirements_file,
                manifest_file,
            ),
        )
        task["promptSha256"] = sha256_file(first_prompt_path)
        task["retryPromptSha256"] = sha256_file(retry_prompt_path)
        task["promptBytes"] = first_prompt_path.stat().st_size
        task["retryPromptBytes"] = retry_prompt_path.stat().st_size

    manifest["schemaVersion"] = 4
    manifest["promptSchemaVersion"] = PROMPT_SCHEMA_VERSION
    manifest["legalPathCounts"] = path_counts
    manifest["workerInstructionFile"] = worker_instruction_file
    manifest["validatorScript"] = validator_script
    atomic_dump(manifest_path, manifest)
    return manifest


def validate_tree(tree: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = tree.get("data")
    if not isinstance(data, list):
        raise ValueError("需求树根节点的 data 必须是数组")

    top_logicids: set[str] = set()
    all_logicids: set[str] = set()
    category_counts = {"IR": 0, "SR": 0, "AR": 0}
    ir_nodes: List[Dict[str, Any]] = []

    for index, node in enumerate(data):
        if not isinstance(node, dict):
            raise ValueError(f"data[{index}] 不是 JSON 对象")
        if node.get("categorys") != "IR":
            raise ValueError(
                f"data[{index}] categorys={node.get('categorys')!r}，预期为 IR"
            )
        logicid = str(node.get("logicid", "")).strip()
        if not logicid:
            raise ValueError(f"data[{index}] 缺少 logicid")
        if logicid in top_logicids:
            raise ValueError(f"顶层 IR logicid 重复：{logicid}")
        top_logicids.add(logicid)

        for descendant in walk_nodes(node):
            descendant_id = str(descendant.get("logicid", "")).strip()
            if not descendant_id:
                raise ValueError(f"IR[{index}] 中存在缺少 logicid 的节点")
            if descendant_id in all_logicids:
                raise ValueError(f"需求树 logicid 重复：{descendant_id}")
            all_logicids.add(descendant_id)
            category = str(descendant.get("categorys", "")).strip()
            if category not in category_counts:
                raise ValueError(
                    f"logicid={descendant_id} 的 categorys={category!r} 非 IR/SR/AR"
                )
            category_counts[category] += 1
        ir_nodes.append(node)

    statistics = tree.get("statistics")
    if not isinstance(statistics, dict):
        raise ValueError("需求树缺少 statistics 对象")
    expected_counts = {
        "grandTotal": len(ir_nodes),
        "irCount": category_counts["IR"],
        "srCount": category_counts["SR"],
        "arCount": category_counts["AR"],
        "totalCount": len(all_logicids),
    }
    for key, actual_count in expected_counts.items():
        expected_count = statistics.get(key)
        if not isinstance(expected_count, int):
            raise ValueError(f"statistics.{key} 必须是整数")
        if expected_count != actual_count:
            raise ValueError(
                f"statistics.{key}={expected_count}，但树中实际为 "
                f"{actual_count}；需求树可能不完整"
            )

    return ir_nodes


def reusable_manifest(
    manifest_path: Path,
    source_hash: str,
    requirements_path: Path,
    resolved_package: str,
    rule_hash: str,
) -> Dict[str, Any] | None:
    if not manifest_path.exists():
        return None
    manifest = load_json(manifest_path)
    if manifest.get("sourceSha256") != source_hash:
        raise ValueError(
            f"{manifest_path} 已对应另一份需求树；请改用新的 --output-dir，"
            "不要覆盖或重复初始化现有任务队列"
        )
    source_file = Path(str(manifest.get("requirementsFile", "")))
    if source_file.name != requirements_path.name:
        raise ValueError(f"{manifest_path} 中记录的源文件与当前输入不一致")
    if manifest.get("resolvedPackage") != resolved_package:
        raise ValueError(
            f"{manifest_path} 已使用另一产品线；请改用新的 --output-dir"
        )
    if manifest.get("ruleSha256") != rule_hash:
        raise ValueError(
            f"{manifest_path} 对应的规则文件已经变化；请改用新的 --output-dir"
        )
    return manifest


def main() -> int:
    args = parse_args()
    workspace = Path.cwd().resolve()
    requirements_path = Path(args.requirements).resolve()
    output_dir = Path(args.output_dir).resolve()
    manifest_path = output_dir / "ir_manifest.json"

    if not requirements_path.is_file():
        raise FileNotFoundError(f"需求树不存在：{args.requirements}")
    reject_forbidden_classifier_scripts(workspace)
    workspace_python_baseline = scan_workspace_python_files(workspace)

    resolved_package, source_rule_path = resolve_rule_file(args.product_line)
    source_hash = sha256_file(requirements_path)
    rule_hash = sha256_file(source_rule_path)
    rule_tree = load_json(source_rule_path)
    legal_paths, path_counts = build_legal_path_whitelist(rule_tree)
    existing = reusable_manifest(
        manifest_path,
        source_hash,
        requirements_path,
        resolved_package,
        rule_hash,
    )
    if existing is not None:
        if not isinstance(existing.get("workspacePythonBaseline"), dict):
            existing["workspacePythonBaseline"] = workspace_python_baseline
        existing = ensure_prompt_files(
            existing,
            manifest_path,
            output_dir,
            workspace,
            args.product_line,
            legal_paths,
            path_counts,
        )
        logger.info(
            json.dumps(
                {
                    "status": "reused",
                    "manifest": display_path(manifest_path, workspace),
                    "irCount": existing.get("irCount", 0),
                    "ruleFile": existing.get("ruleFile"),
                    "promptSchemaVersion": existing.get(
                        "promptSchemaVersion"
                    ),
                    "legalPathCounts": existing.get("legalPathCounts"),
                    "tasks": existing.get("tasks", []),
                },
                ensure_ascii=False,
            )
        )
        return 0

    tree = load_json(requirements_path)
    ir_nodes = validate_tree(tree)
    tasks_dir = output_dir / "tasks"
    results_dir = output_dir / "results"
    audit_results_dir = output_dir / "audit-results"
    rules_dir = output_dir / "rules"
    prompts_dir = output_dir / "prompts"
    batches_dir = output_dir / "semantic-batches"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    audit_results_dir.mkdir(parents=True, exist_ok=True)
    rules_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)
    batches_dir.mkdir(parents=True, exist_ok=True)
    copied_rule_path = rules_dir / source_rule_path.name
    atomic_dump(copied_rule_path, rule_tree)
    copied_rule_display = display_path(copied_rule_path, workspace)

    tasks: List[Dict[str, Any]] = []
    for ir_index, subtree in enumerate(ir_nodes):
        logicid = str(subtree["logicid"])
        file_stem = f"ir_{ir_index:03d}_{safe_component(logicid)}"
        input_path = tasks_dir / f"{file_stem}.json"
        result_path = results_dir / f"{file_stem}.result.json"
        task_key = f"ir:{ir_index}:{logicid}"
        node_count = sum(1 for _ in walk_nodes(subtree))
        input_mode, batch_index_file, batch_count = create_semantic_batches(
            subtree,
            ir_index,
            logicid,
            file_stem,
            batches_dir,
            workspace,
        )

        task = {
            "taskKey": task_key,
            "irIndex": ir_index,
            "logicid": logicid,
            "name": str(subtree.get("name", "")),
            "nodeCount": node_count,
            "ruleFile": copied_rule_display,
            "inputFile": display_path(input_path, workspace),
            "inputMode": input_mode,
            "semanticBatchIndexFile": batch_index_file,
            "semanticBatchCount": batch_count,
            "resultFile": display_path(result_path, workspace),
            "state": "pending",
            "attempts": 0,
        }
        task_file = {
            "taskKey": task_key,
            "irIndex": ir_index,
            "irLogicid": logicid,
            "irName": task["name"],
            "nodeCount": node_count,
            "resultFile": task["resultFile"],
            "subtree": subtree,
        }
        atomic_dump(input_path, task_file)
        tasks.append(task)

    manifest = {
        "schemaVersion": 4,
        "promptSchemaVersion": PROMPT_SCHEMA_VERSION,
        "requirementsFile": display_path(requirements_path, workspace),
        "sourceSha256": source_hash,
        "productLine": args.product_line,
        "resolvedPackage": resolved_package,
        "ruleSourceName": source_rule_path.name,
        "ruleFile": copied_rule_display,
        "ruleSha256": rule_hash,
        "workspacePythonBaseline": workspace_python_baseline,
        "irCount": len(tasks),
        "statistics": tree.get("statistics"),
        "legalPathCounts": path_counts,
        "tasks": tasks,
    }
    manifest = ensure_prompt_files(
        manifest,
        manifest_path,
        output_dir,
        workspace,
        args.product_line,
        legal_paths,
        path_counts,
    )

    logger.info(
        json.dumps(
            {
                "status": "created",
                "manifest": display_path(manifest_path, workspace),
                "irCount": len(tasks),
                "ruleFile": copied_rule_display,
                "promptSchemaVersion": PROMPT_SCHEMA_VERSION,
                "legalPathCounts": path_counts,
                "tasks": tasks,
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
