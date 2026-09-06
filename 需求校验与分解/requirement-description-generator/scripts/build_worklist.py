#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
需求详细描述生成 - 工作清单构建 + 父需求条件化检索

把 das-manage-transformer 输出的 requirements_transformed.json（IR->SR->AR 树）展平为
"待生成工作清单"，并为每条 needs_generation 的需求挂上两类信息：

1) 当前树的上下文（定位与父子一致，防跑偏）
   - ancestors：从根到父的祖先链。父节点给【全文】（不是旧版的 200 字截断）——
     父需求描述往往是子需求最重要的 grounding 来源。
   - children ：直接子节点摘要（父需求应概述子需求覆盖面）。

2) 历史经验包的"父条件化检索"结果（学真实分解方式与措辞，是防乱编的主杠杆）
   对每条需求 N，用它的父需求 P_new 去经验包里检索"同产品线、同类型、分解语境相似"
   的历史父需求，取出它们【当年真实的整组子需求】，再在组内挑与 N 最像的一两条作样板：
     · 一级（选组）：BM25(P_new) + 领域词命中，在"有子节点的历史父需求"里排序，取 top1~2，
       顺父子指针拉出真实子需求组 —— 这一步给"分几项/每项管什么/整组覆盖哪些维度"。
     · 二级（选样板）：在选中组内用 N.name 排序，取 top1~2 真实子需求 —— 给"措辞/排版"。
   门槛：一级无候选过阈值就什么都不挂，退回"仅用本节点信息 + 类型规则"的保守生成
   （宁缺毋滥，挂错组正是跨域乱编的来源）。
   兜底：IR 无父 -> 只做二级（在同类型历史节点里按 N 自身检索，作风格校准）。

检索为离线、纯 Python（字符二元组 BM25），无需网络、无需向量模型。

用法：
    # 推荐：给产品线，自动解析经验包（含映射表回退）
    python build_worklist.py requirements_transformed.json worklist.json \
        --product-line "Optical Business" \
        --packages-root <references/packages> --mapping <references/product_line_mapping.json>
    # 或显式指定经验包目录
    python build_worklist.py requirements_transformed.json worklist.json --package <经验包目录>
    # 两者都省略则不检索（功能不受影响，只是少了 grounding）
"""

import argparse
import importlib.util
import json
import logging
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

EMPTY_TOKENS = {"", "/", "-", "无", "待补充", "n/a", "na", "none"}

# 与建包侧一致的维度词典（用于领域词命中加成；改动请两边同步）
DIMENSION_LEXICON = {
    "温度规格": ["温度", "℃", "高温", "低温"], "湿度规格": ["湿度", "凝露"], "海拔": ["海拔"],
    "振动/冲击": ["振动", "冲击"], "存储/运输": ["存储", "运输", "包装"],
    "腐蚀/三防": ["腐蚀", "硫化", "盐雾", "三防", "积尘"],
    "认证/合规": ["认证", "合规", "标准", "emc", "安规", "规范"],
    "接口/连接器": ["接口", "连接器", "端口", "serdes", "背板", "面板", "光模块"],
    "芯片/器件": ["芯片", "器件", "选型", "eeprom"], "功耗/电源": ["功耗", "电源", "供电"],
    "尺寸/重量/形态": ["尺寸", "重量", "形态", "槽位"], "时钟/同步": ["时钟", "1588", "同步"],
    "逻辑/软件": ["逻辑", "软件", "寄存器"], "可靠性/老化": ["可靠", "失效", "fit", "老化"],
    "散热": ["散热", "风扇", "风道", "液冷"], "结构/安装": ["结构", "安装", "拉手条"],
    "天线/阵列": ["天线", "阵列", "振子", "极化", "波束", "hbf", "移相器", "机械倾角", "赋形"],
    "射频指标": ["杂散", "谐波", "evm", "aclr", "邻道", "带外", "阻塞", "灵敏度", "底噪", "互调"],
    "功率/功放": ["功放", "发射功率", "输出功率", "trx", "通道", "载波", "pa"],
    "频段/制式": ["频段", "频率", "制式", "nr", "lte", "5g", "带宽", "频宽"],
    "波束赋形/MIMO": ["mimo", "波束赋形", "64t", "32t", "16t"],
    "内存/存储介质": ["内存", "硬盘", "ssd", "edsff", "sff", "cage", "nvme", "dimm", "存储盘"],
    "计算/CPU/加速卡": ["cpu", "算力", "npu", "gpu", "推理卡", "加速卡", "核隔离", "bist"],
    "电池/BMS/输入保护": ["电池", "bms", "过充", "过放", "过流", "短路", "过压", "储能"],
    "噪音": ["噪音", "噪声", "dba", "声功率", "分贝"],
    "静电/ESD": ["静电", "esd", "接触放电", "空气放电"],
    "机柜/节点": ["机柜", "整柜", "机架", "节点"],
}

# 检索预算
TOP_PARENTS = 2           # 一级：取几组历史父分解
TOP_STYLE = 2             # 二级：每组挑几条样板子需求
MAX_CHILDREN_PER_GROUP = 8
MIN_SCORE = 1.0           # BM25+领域加成的过阈值门槛（低于此视为不相关，不挂）
REF_DESC_CHARS = 300


# ----------------- BM25（字符二元组） -----------------
def bigrams(text):
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", (text or "").lower())
    if len(s) < 2:
        return [s] if s else []
    return [s[i:i + 2] for i in range(len(s) - 1)]


def domain_terms(text):
    t = (text or "").lower()
    return {d for d, kws in DIMENSION_LEXICON.items() if any(k.lower() in t for k in kws)}


class BM25:
    """字符二元组上的 BM25。docs: [(id, text, payload)]。"""
    def __init__(self, docs, k1=1.5, b=0.75, domain_weight=1.5):
        self.k1, self.b, self.dw = k1, b, domain_weight
        self.docs = docs
        self.doc_terms = [bigrams(t) for _, t, _ in docs]
        self.doc_dom = [domain_terms(t) for _, t, _ in docs]
        self.dl = [len(dt) for dt in self.doc_terms]
        self.avgdl = (sum(self.dl) / len(self.dl)) if self.dl else 0.0
        df = Counter_df(self.doc_terms)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def score(self, query):
        q_terms = bigrams(query)
        q_dom = domain_terms(query)
        q_tf = {}
        for t in q_terms:
            q_tf[t] = q_tf.get(t, 0) + 1
        results = []
        for i, (docid, _text, payload) in enumerate(self.docs):
            tf = {}
            for t in self.doc_terms[i]:
                tf[t] = tf.get(t, 0) + 1
            s = 0.0
            dl = self.dl[i] or 1
            for t in q_tf:
                if t not in tf:
                    continue
                idf = self.idf.get(t, 0.0)
                f = tf[t]
                s += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            s += self.dw * len(q_dom & self.doc_dom[i])  # 领域词命中加成
            results.append((s, docid, payload))
        results.sort(key=lambda x: x[0], reverse=True)
        return results


def Counter_df(list_of_term_lists):
    df = defaultdict(int)
    for terms in list_of_term_lists:
        for t in set(terms):
            df[t] += 1
    return df


# ----------------- 经验包加载 & 索引 -----------------
def load_examples(path):
    recs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return recs


def build_indexes(recs):
    by_id = {r["example_id"]: r for r in recs}
    # 一级索引：按类型分组的"有子节点的历史父需求"
    parent_docs = defaultdict(list)   # type -> [(id, text, payload)]
    for r in recs:
        if r.get("children_example_ids"):
            text = (r.get("requirement_name", "") + " " + r.get("requirement_description", ""))
            parent_docs[r["requirement_type"]].append((r["example_id"], text, r))
    parent_bm25 = {t: BM25(docs) for t, docs in parent_docs.items()}
    # 二级索引：按类型分组的所有 complete 节点（用于 IR 兜底 / 无父兜底）
    node_docs = defaultdict(list)
    for r in recs:
        if r.get("quality_tag") == "complete":
            text = (r.get("requirement_name", "") + " " + r.get("requirement_description", ""))
            node_docs[r["requirement_type"]].append((r["example_id"], text, r))
    node_bm25 = {t: BM25(docs) for t, docs in node_docs.items()}
    return by_id, parent_bm25, node_bm25


def clip(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n].rstrip() + "…"


def retrieve_for_item(item, by_id, parent_bm25, node_bm25):
    """返回 retrieval 字典或 None。父条件化两级检索 + 兜底。"""
    ctype = item["categorys"]
    parent_ctx = item["ancestors"][-1] if item.get("ancestors") else None

    matched_groups = []
    if parent_ctx and parent_ctx.get("categorys") in parent_bm25:
        p_type = parent_ctx["categorys"]
        p_text = (parent_ctx.get("name", "") + " " + parent_ctx.get("description_full", "")).strip()
        ranked = parent_bm25[p_type].score(p_text)
        for score, pid, payload in ranked[:TOP_PARENTS]:
            if score < MIN_SCORE:
                break
            children = [by_id[c] for c in payload.get("children_example_ids", []) if c in by_id]
            # 二级：组内按 N 自身挑样板
            n_text = item.get("name", "") + " " + item.get("description", "")
            child_docs = [(c["example_id"], c["requirement_name"] + " " + c["requirement_description"], c)
                          for c in children]
            samples = []
            if child_docs:
                cbm = BM25(child_docs)
                for _, cid, cpay in cbm.score(n_text)[:TOP_STYLE]:
                    samples.append({
                        "example_id": cid,
                        "requirement_name": cpay["requirement_name"],
                        "requirement_description": clip(cpay["requirement_description"], REF_DESC_CHARS),
                    })
            matched_groups.append({
                "parent_example_id": pid,
                "parent_name": payload.get("requirement_name", ""),
                "score": round(score, 2),
                "children_overview": [c["requirement_name"] for c in children[:MAX_CHILDREN_PER_GROUP]],
                "closest_samples": samples,
            })

    # 兜底：无父（IR）或一级未命中 -> 二级在同类型 complete 节点里按 N 自身检索
    style_only = []
    if not matched_groups and ctype in node_bm25:
        n_text = item.get("name", "") + " " + item.get("description", "")
        for cs, cid, cpay in node_bm25[ctype].score(n_text)[:TOP_STYLE]:
            if cs < MIN_SCORE:
                break
            if cid == item.get("_self_id"):
                continue
            style_only.append({
                "example_id": cid,
                "requirement_name": cpay["requirement_name"],
                "requirement_description": clip(cpay["requirement_description"], REF_DESC_CHARS),
            })

    if not matched_groups and not style_only:
        return None
    return {
        "matched_parent_groups": matched_groups,
        "style_only_samples": style_only,
        "usage_note": "仅用于学习分解方式（分几项/每项覆盖什么维度）与措辞排版；"
                      "禁止把其中的具体数值/型号/标准号/链接搬到新需求里。",
    }


# ----------------- 展平 + 上下文 -----------------
def is_empty(desc):
    return desc is None or str(desc).strip().lower() in EMPTY_TOKENS


def brief(node, full_desc=False):
    desc = (node.get("description") or "").strip()
    out = {"categorys": node.get("categorys", "") or "", "name": node.get("name", "") or ""}
    if full_desc:
        out["description_full"] = desc          # 父节点给全文
    else:
        out["description_brief"] = desc[:200] + ("…" if len(desc) > 200 else "")
    return out


def walk(nodes, ancestors, items):
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        logicid = node.get("logicid") or node.get("logicId")
        children_nodes = node.get("children") or []
        if logicid:
            desc = node.get("description", "") or ""
            # 祖先链：父节点（最后一个）给全文，更上层给摘要
            anc = []
            for idx, a in enumerate(ancestors):
                anc.append(brief(a, full_desc=(idx == len(ancestors) - 1)))
            items.append({
                "logicid": str(logicid),
                "_self_id": None,
                "categorys": node.get("categorys", "") or "",
                "name": node.get("name", "") or "",
                "description": desc,
                "existing_detail": node.get("requirementDetailInfo", "") or "",
                "ancestors": anc,
                "children": [brief(c) for c in children_nodes if isinstance(c, dict)],
                "requirementDetailInfo": None,
                "needs_generation": not is_empty(desc),
            })
        walk(children_nodes, ancestors + [node], items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output", nargs="?", default="worklist.json")
    ap.add_argument("--package", default=None, help="经验包目录（显式指定，优先于 --product-line）")
    ap.add_argument("--product-line", default=None, help="产品线名；经 resolve_package 解析到经验包")
    ap.add_argument("--packages-root", default="references/packages")
    ap.add_argument("--mapping", default="references/product_line_mapping.json")
    args = ap.parse_args()

    package_dir = args.package
    if not package_dir and args.product_line:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        resolve_module_path = os.path.join(script_dir, "resolve_package.py")
        spec = importlib.util.spec_from_file_location("resolve_package", resolve_module_path)
        resolve_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolve_module)
        resolve = resolve_module.resolve
        package_dir, reason = resolve(args.product_line, args.packages_root, args.mapping)
        if not package_dir:
            logger.error("产品线解析失败：%s", reason)
            logger.error("请置 BLOCKED：确认产品线，或为其建包/在映射表补别名。")
            sys.exit(2)
        logger.info("产品线路由：%s -> %s（%s）", args.product_line, package_dir, reason)

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        tree = data.get("data", [])
        version_id = (data.get("statistics", {}) or {}).get("versionId", "")
    else:
        tree, version_id = data, ""

    items = []
    walk(tree, [], items)

    matched = 0
    if package_dir:
        ex_path = Path(package_dir) / "examples.jsonl"
        if ex_path.exists():
            recs = load_examples(ex_path)
            by_id, parent_bm25, node_bm25 = build_indexes(recs)
            for it in items:
                if not it["needs_generation"]:
                    continue
                r = retrieve_for_item(it, by_id, parent_bm25, node_bm25)
                if r:
                    it["retrieval"] = r
                    matched += 1
            logger.info("检索：经验包 %s 条样例，为 %d 条需求挂上历史分解参考",
                        len(recs), matched)
        else:
            logger.warning("未找到 %s，跳过检索", ex_path)

    stats = {"total": len(items),
             "need_generation": sum(1 for i in items if i["needs_generation"]),
             "skip_empty": sum(1 for i in items if not i["needs_generation"]),
             "retrieval_matched": matched}
    for it in items:
        it.pop("_self_id", None)
    result = {"versionId": version_id, "stats": stats, "items": items}

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info("worklist -> %s ｜ 总 %d，待生成 %d，跳过空描述 %d，挂检索 %d",
                args.output, stats["total"], stats["need_generation"],
                stats["skip_empty"], matched)


if __name__ == "__main__":
    main()
