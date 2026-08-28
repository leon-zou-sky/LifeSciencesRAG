"""
② 医学问询助手（Medical Inquiry Assistant）

对应 Veeva Medical 场景：HCP 向药企医学信息部(MI)提问，
系统检索图谱+文档后由 LLM 生成带引用溯源的回答。

链路：问题 → 图谱遍历(机制链) + 向量检索(文档语义) → 组装上下文
     → 豆包生成回答（结论标注引用编号） → 输出答案+参考列表

用法:
    python scripts/ask.py "阿托伐他汀和红霉素可以联用吗"
    python scripts/ask.py "哪些药物会导致横纹肌溶解"
    python scripts/ask.py "二甲双胍在肾功能不全患者中使用要注意什么"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging

from neo4j import GraphDatabase
from sentence_transformers import SentenceTransformer

from scripts.query_graphrag import (
    NEO4J_PASS, NEO4J_URI, NEO4J_USER, _MODEL_PATH,
    extract_drug_names, extract_entity_names,
    graph_query_causes, graph_query_drug_info, graph_query_interactions,
    vector_search,
)
from src.llm_config import get_ark_client

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是药企医学信息部(MI)的问询应答助手，服务对象是医疗卫生专业人士(HCP)。

回答要求：
1. 只依据下方提供的参考资料回答；资料未覆盖的内容，明确说"现有资料未覆盖"，不要编造
2. 关键结论后标注引用编号，如【1】【2】；图谱关系引用标注【图谱】
3. 涉及相互作用/禁忌/剂量时，以说明书(A级)口径为准；论文和病例仅作补充证据
4. 专业、简洁、直接回答问题
5. 结尾固定附一句：本回复仅供医学专业人士参考，临床决策请结合患者情况并查阅最新批准说明书。"""


def retrieve(query: str, model, driver, top_k: int = 3):
    """图谱 + 向量双路检索（复用 query_graphrag 的函数）"""
    from src.graph_data import DRUGS

    drug_names = [d["name"] for d in DRUGS]
    matched = extract_drug_names(query, drug_names)

    graph_results = []
    if len(matched) >= 2:
        graph_results.extend(graph_query_interactions(driver, matched))
    elif len(matched) == 1:
        graph_results.extend(graph_query_drug_info(driver, matched[0]))
    for entity in extract_entity_names(query):
        graph_results.extend(graph_query_causes(driver, entity))

    vector_results = vector_search(query, model, top_k=top_k)
    return graph_results, vector_results


def _format_graph_line(g: dict) -> str:
    t = g["type"]
    if t == "drug_interaction":
        return f"{g['from']} —[{g['relationship']}]→ {g['to']}：{g['description']}"
    if t == "shared_enzyme":
        return f"{g['drug_a']} 和 {g['drug_b']} 共同作用于代谢酶 {g['enzyme']}"
    if t in ("drug_causes_reaction", "reaction_caused_by_drug"):
        return f"{g['drug']} —[CAUSES]→ {g['reaction']}（严重度:{g['severity']}）：{g.get('description', '')}"
    if t == "indication":
        return f"{g['drug']} —[TREATS]→ {g['disease']}（{g['role']}）"
    if t == "interaction":
        return f"{g['drug']} —[{g['relationship']}]→ {g['target']}：{g.get('description', '')}"
    if t == "adverse_reaction":
        return f"{g['drug']} —[CAUSES]→ {g['reaction']}（严重度:{g['severity']}）：{g.get('description', '')}"
    return str(g)


def build_context(graph_results: list[dict], vector_results: list[dict]) -> tuple[str, list[str]]:
    """组装 LLM 上下文，返回 (context_text, 引用列表)"""
    parts = []
    refs = []

    if graph_results:
        lines = ["【图谱关系】（来自知识图谱的确定性关系，标注【图谱】引用）"]
        for g in graph_results:
            lines.append(f"- {_format_graph_line(g)}")
        parts.append("\n".join(lines))

    if vector_results:
        lines = ["【文档资料】"]
        for i, v in enumerate(vector_results, 1):
            ref = f"【{i}】{v['doc_type']}《{v['source_title']}》·{v['section']}章节（{v['authority']}级，相似度{v['score']:.2f}）"
            refs.append(ref)
            lines.append(f"{ref}\n{v['text']}")
        parts.append("\n\n".join(lines))

    return "\n\n".join(parts), refs


def ask(query: str, top_k: int = 3) -> str:
    model = SentenceTransformer(_MODEL_PATH)
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    client, llm_model = get_ark_client()

    try:
        graph_results, vector_results = retrieve(query, model, driver, top_k)
        context, refs = build_context(graph_results, vector_results)

        if not context:
            return "未检索到任何相关资料，无法回答。"

        user_msg = f"【HCP 问题】\n{query}\n\n【参考资料】\n{context}"
        resp = client.chat.completions.create(
            model=llm_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
        )
        answer = resp.choices[0].message.content

        ref_block = ""
        if refs:
            ref_block = "\n\n── 引用来源 ──\n" + "\n".join(refs)
        return answer + ref_block
    finally:
        driver.close()


def main():
    parser = argparse.ArgumentParser(description="医学问询助手（GraphRAG + LLM 生成）")
    parser.add_argument("query", type=str, help="HCP 问题")
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    print(f"\n问题：{args.query}\n{'=' * 60}")
    print(ask(args.query, top_k=args.top_k))
    print("=" * 60)


if __name__ == "__main__":
    main()
