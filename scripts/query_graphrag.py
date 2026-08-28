"""
GraphRAG 融合查询：Neo4j 图遍历 + Milvus 向量检索 + 融合输出

用法:
    python scripts/query_graphrag.py "阿托伐他汀和红霉素联用需要注意什么"
    python scripts/query_graphrag.py "哪些药物会导致横纹肌溶解"
    python scripts/query_graphrag.py "司美格鲁肽的临床试验结果如何"
    python scripts/query_graphrag.py "二甲双胍的禁忌症和不良反应"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.thresholds import load_model_path
from src.encoding import encode_query

import argparse
import logging
import os
from neo4j import GraphDatabase
from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

NEO4J_URI = os.environ.get("LS_RAG_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("LS_RAG_NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("LS_RAG_NEO4J_PASSWORD", "graphrag2024")  # 本地开发默认值
MILVUS_URI = "http://localhost:19531"
_MODEL_PATH = load_model_path()  # 模型路径与阈值同源（thresholds.yaml model 字段）


# ============ Neo4j 图查询 ============

def graph_query_interactions(driver, drug_names: list[str]) -> list[dict]:
    """查询两个药物之间的相互作用路径"""
    if len(drug_names) < 2:
        return []

    results = []
    with driver.session() as session:
        # 直接相互作用
        result = session.run(
            """
            MATCH (a:Drug)-[r]->(b:Drug)
            WHERE a.name IN $drugs AND b.name IN $drugs AND a <> b
            RETURN a.name AS drug_a, type(r) AS rel, b.name AS drug_b,
                   r.description AS desc, r.source AS source
            """,
            drugs=drug_names,
        )
        for record in result:
            results.append({
                "type": "drug_interaction",
                "from": record["drug_a"],
                "to": record["drug_b"],
                "relationship": record["rel"],
                "description": record["desc"],
                "source": record["source"],
            })

        # 通过共同酶的间接作用
        result = session.run(
            """
            MATCH (a:Drug)-[:SUBSTRATE|INHIBITOR]->(e:Enzyme)<-[:SUBSTRATE|INHIBITOR]-(b:Drug)
            WHERE a.name IN $drugs AND b.name IN $drugs AND a <> b
            RETURN a.name AS drug_a, e.name AS enzyme, b.name AS drug_b,
                   a.name + ' 和 ' + b.name + ' 都影响 ' + e.name AS path_desc
            """,
            drugs=drug_names,
        )
        for record in result:
            results.append({
                "type": "shared_enzyme",
                "drug_a": record["drug_a"],
                "enzyme": record["enzyme"],
                "drug_b": record["drug_b"],
                "description": record["path_desc"],
            })

    return results


def graph_query_causes(driver, entity_name: str) -> list[dict]:
    """查询某药物导致的不良反应，或某不良反应关联的药物"""
    results = []
    with driver.session() as session:
        # 药物 → 不良反应
        result = session.run(
            """
            MATCH (d:Drug)-[r:CAUSES]->(ar:AdverseReaction)
            WHERE d.name = $name
            RETURN d.name AS drug, ar.name AS reaction, ar.severity AS severity,
                   r.description AS desc, r.evidence AS evidence
            """,
            name=entity_name,
        )
        for record in result:
            results.append({
                "type": "drug_causes_reaction",
                "drug": record["drug"],
                "reaction": record["reaction"],
                "severity": record["severity"],
                "description": record["desc"],
                "evidence": record["evidence"],
            })

        # 不良反应 → 药物（反向）
        result = session.run(
            """
            MATCH (d:Drug)-[r:CAUSES]->(ar:AdverseReaction)
            WHERE ar.name = $name
            RETURN d.name AS drug, ar.name AS reaction, ar.severity AS severity,
                   r.description AS desc
            """,
            name=entity_name,
        )
        for record in result:
            results.append({
                "type": "reaction_caused_by_drug",
                "drug": record["drug"],
                "reaction": record["reaction"],
                "severity": record["severity"],
                "description": record["desc"],
            })

    return results


def graph_query_drug_info(driver, drug_name: str) -> list[dict]:
    """查询药物的完整图谱信息（适应症、相互作用、不良反应）"""
    results = []
    with driver.session() as session:
        # 适应症
        result = session.run(
            """
            MATCH (d:Drug)-[r:TREATS]->(dis:Disease)
            WHERE d.name = $name
            RETURN dis.name AS disease, r.role AS role
            """,
            name=drug_name,
        )
        for record in result:
            results.append({
                "type": "indication",
                "drug": drug_name,
                "disease": record["disease"],
                "role": record["role"],
            })

        # 所有相互作用
        result = session.run(
            """
            MATCH (d:Drug)-[r]->(other)
            WHERE d.name = $name AND (other:Drug OR other:Enzyme)
            RETURN other.name AS target, labels(other)[0] AS target_type,
                   type(r) AS rel, r.description AS desc
            """,
            name=drug_name,
        )
        for record in result:
            results.append({
                "type": "interaction",
                "drug": drug_name,
                "target": record["target"],
                "target_type": record["target_type"],
                "relationship": record["rel"],
                "description": record["desc"],
            })

        # 不良反应
        result = session.run(
            """
            MATCH (d:Drug)-[r:CAUSES]->(ar:AdverseReaction)
            WHERE d.name = $name
            RETURN ar.name AS reaction, ar.severity AS severity, r.description AS desc
            """,
            name=drug_name,
        )
        for record in result:
            results.append({
                "type": "adverse_reaction",
                "drug": drug_name,
                "reaction": record["reaction"],
                "severity": record["severity"],
                "description": record["desc"],
            })

    return results


# ============ Milvus 向量查询 ============

def vector_search(query: str, model, top_k: int = 3) -> list[dict]:
    """在所有 Milvus 文档 Collection 中检索（非对称：查询侧加 BGE 指令前缀）"""
    client = MilvusClient(uri=MILVUS_URI)
    query_vec = encode_query(model, [query])

    results = []
    for coll in ["drug_inserts", "clinical_papers", "case_reports"]:
        if not client.has_collection(coll):
            continue
        client.load_collection(coll)
        hits = client.search(
            collection_name=coll,
            data=query_vec,
            limit=top_k,
            output_fields=["text", "source_title", "doc_type", "section", "authority"],
        )
        for hit in hits[0]:
            e = hit["entity"]
            results.append({
                "text": e["text"],
                "source_title": e["source_title"],
                "doc_type": e["doc_type"],
                "section": e["section"],
                "authority": e["authority"],
                "score": hit["distance"],
            })
    client.close()

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


# ============ 融合 ============

def fuse_results(graph_results: list[dict], vector_results: list[dict]) -> dict:
    """融合图谱和向量结果"""
    return {
        "graph": graph_results,
        "vector": vector_results,
        "graph_count": len(graph_results),
        "vector_count": len(vector_results),
    }


def format_output(fused: dict, query: str) -> str:
    """格式化输出"""
    lines = []
    lines.append(f"查询: {query}")
    lines.append(f"图谱命中: {fused['graph_count']} 条 | 向量命中: {fused['vector_count']} 条")
    lines.append("")

    # 图谱结果
    if fused["graph"]:
        lines.append("=" * 50)
        lines.append("【图谱关系】")
        lines.append("=" * 50)
        for i, g in enumerate(fused["graph"], 1):
            if g["type"] == "drug_interaction":
                lines.append(f"  {i}. {g['from']} —[{g['relationship']}]→ {g['to']}")
                lines.append(f"     说明: {g['description']} (来源: {g.get('source', '-')})")
            elif g["type"] == "shared_enzyme":
                lines.append(f"  {i}. {g['drug_a']} 和 {g['drug_b']} 都影响 {g['enzyme']}")
            elif g["type"] == "drug_causes_reaction":
                lines.append(f"  {i}. {g['drug']} —CAUSES→ {g['reaction']} [{g['severity']}]")
                lines.append(f"     说明: {g['description']}")
            elif g["type"] == "reaction_caused_by_drug":
                lines.append(f"  {i}. {g['drug']} —CAUSES→ {g['reaction']} [{g['severity']}]")
            elif g["type"] == "indication":
                lines.append(f"  {i}. {g['drug']} —TREATS→ {g['disease']} ({g['role']})")
            elif g["type"] == "interaction":
                lines.append(f"  {i}. {g['drug']} —[{g['relationship']}]→ {g['target']} ({g['target_type']})")
                lines.append(f"     说明: {g['description']}")
            elif g["type"] == "adverse_reaction":
                lines.append(f"  {i}. {g['drug']} —CAUSES→ {g['reaction']} [{g['severity']}]")
                lines.append(f"     说明: {g['description']}")
        lines.append("")

    # 向量结果
    if fused["vector"]:
        lines.append("=" * 50)
        lines.append("【文档语义】")
        lines.append("=" * 50)
        for i, v in enumerate(fused["vector"], 1):
            lines.append(f"  {i}. [{v['doc_type']}] {v['source_title']} — {v['section']}")
            lines.append(f"     相似度: {v['score']:.4f} | 权威: {v['authority']}")
            text_preview = v['text'][:150] + "..." if len(v['text']) > 150 else v['text']
            lines.append(f"     内容: {text_preview}")
        lines.append("")

    return "\n".join(lines)


# ============ 主流程 ============

def extract_drug_names(query: str, known_drugs: list[str]) -> list[str]:
    """从查询中提取药物名称（简单匹配）"""
    found = []
    for drug in known_drugs:
        if drug in query:
            found.append(drug)
    return found


def extract_entity_names(query: str) -> list[str]:
    """从查询中提取实体名称"""
    known = [
        "横纹肌溶解", "肝毒性", "乳酸酸中毒", "低血糖",
        "高胆固醇血症", "2型糖尿病", "肥胖", "动脉粥样硬化",
        "CYP3A4",
    ]
    return [e for e in known if e in query]


def main():
    parser = argparse.ArgumentParser(description="GraphRAG 融合查询")
    parser.add_argument("query", type=str, help="查询内容")
    parser.add_argument("--top-k", type=int, default=3, help="向量检索返回数量")
    args = parser.parse_args()

    # 加载模型
    model = SentenceTransformer(_MODEL_PATH)

    # 连接 Neo4j
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

    query = args.query

    # 图谱查询
    graph_results = []

    # 1. 检查是否是药物相互作用查询
    from src.graph_data import DRUGS
    drug_names = [d["name"] for d in DRUGS]
    matched_drugs = extract_drug_names(query, drug_names)

    if len(matched_drugs) >= 2:
        graph_results.extend(graph_query_interactions(driver, matched_drugs))
    elif len(matched_drugs) == 1:
        graph_results.extend(graph_query_drug_info(driver, matched_drugs[0]))

    # 2. 检查是否是不良反应查询
    entity_names = extract_entity_names(query)
    for entity in entity_names:
        graph_results.extend(graph_query_causes(driver, entity))

    # 向量检索
    vector_results = vector_search(query, model, top_k=args.top_k)

    # 融合
    fused = fuse_results(graph_results, vector_results)

    # 输出
    print("\n" + format_output(fused, query))

    driver.close()


if __name__ == "__main__":
    main()
