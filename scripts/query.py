"""
查询脚本：多 Collection 检索 + 权威优先融合 + 引用溯源

用法:
    python scripts/query.py "阿托伐他汀和红霉素联用需要注意什么"
    python scripts/query.py "GLP-1受体激动剂的减重效果如何"
    python scripts/query.py "二甲双胍和乳酸酸中毒的关系"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.thresholds import load_model_path
from src.encoding import encode_query

import argparse
import logging
from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from src.normalization import extract_drugs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_MODEL_PATH = load_model_path()  # 模型路径与阈值同源（thresholds.yaml model 字段）

# Collection 列表
ALL_COLLECTIONS = ["drug_inserts", "guidelines", "clinical_papers", "case_reports"]

# 权威权重（轻微加权，避免高相关性低权威结果被完全压死）
AUTHORITY_WEIGHT = {"A": 1.0, "A-": 0.97, "B": 0.95, "C": 0.9}

# 角色画像：不同岗位的默认视图（生产上配在租户/角色配置里，不用每次判意图）
# boost 叠在权威权重之上：final = score × AUTHORITY_WEIGHT × role_boost
# 分数只当召回门槛；门槛之上的排序是业务问题——角色 > 权威 > 时效 > 分数
ROLE_PROFILE = {
    "general": {
        "desc": "默认视图（无角色倾斜）",
        "boost": {},
        "with_inquiries": False,
    },
    "hotline": {
        "desc": "患者热线专员：说明书/指南优先，附历史问询（approved）参考",
        "boost": {"drug_inserts": 1.05, "guidelines": 1.0, "clinical_papers": 0.92, "case_reports": 0.88},
        "with_inquiries": True,
    },
    "medical": {
        "desc": "医学官：指南/论文优先",
        "boost": {"guidelines": 1.05, "clinical_papers": 1.03, "drug_inserts": 0.95, "case_reports": 0.88},
        "with_inquiries": False,
    },
    "pv": {
        "desc": "PV（药物警戒）专员：病例优先",
        "boost": {"case_reports": 1.10, "drug_inserts": 0.95, "guidelines": 0.93, "clinical_papers": 0.92},
        "with_inquiries": False,
    },
}


def search_all_collections(client, model, query: str, top_k_per_collection: int = 5) -> list[dict]:
    """在所有文档 Collection 中检索（非对称：查询侧加 BGE 指令前缀，见 src/encoding.py）"""
    query_vec = encode_query(model, [query])
    results = []

    for coll_name in ALL_COLLECTIONS:
        if not client.has_collection(coll_name):
            continue
        client.load_collection(coll_name)
        hits = client.search(
            collection_name=coll_name,
            data=query_vec,
            limit=top_k_per_collection,
            output_fields=["text", "source_title", "doc_type", "section", "authority", "version"],
        )
        for hit in hits[0]:
            entity = hit["entity"]
            results.append({
                "text": entity["text"],
                "source_title": entity["source_title"],
                "doc_type": entity["doc_type"],
                "section": entity["section"],
                "authority": entity["authority"],
                "version": entity.get("version", ""),
                "score": hit["distance"],
                "collection": coll_name,
            })

    return results


def authority_fusion(results: list[dict], top_k: int = 5, role: str = "general") -> list[dict]:
    """
    权威 × 角色 双重加权融合

    核心逻辑：score × authority_weight × role_boost，不再严格按级别排序，
    而是让高相关性的低权威结果也能排上来；角色 boost 体现岗位默认视图。
    """
    boost = ROLE_PROFILE[role]["boost"]
    for r in results:
        r["weighted_score"] = r["score"] * AUTHORITY_WEIGHT[r["authority"]] * boost.get(r["collection"], 1.0)

    # 按加权分统一排序
    results.sort(key=lambda x: x["weighted_score"], reverse=True)
    return results[:top_k]


def search_inquiries(client, model, query: str, limit: int = 3) -> list[dict]:
    """历史问询检索（仅 approved）——热线专员答用户时的草稿参考"""
    if not client.has_collection("mi_inquiries"):
        return []
    vec = model.encode([query], normalize_embeddings=True).tolist()
    hits = client.search(
        collection_name="mi_inquiries", data=vec, limit=limit,
        filter='status == "approved"', output_fields=["question_std", "answer"],
    )[0]
    return [
        {"question_std": h["entity"]["question_std"], "answer": h["entity"]["answer"], "score": h["distance"]}
        for h in hits
    ]


def format_answer(results: list[dict], query: str) -> str:
    """格式化输出：正文 + 引用溯源"""
    if not results:
        return "未找到相关信息。"

    output = []
    output.append(f"查询: {query}")
    output.append(f"找到 {len(results)} 条相关结果:\n")

    for i, r in enumerate(results, 1):
        authority_label = {"A": "权威", "A-": "准权威", "B": "参考", "C": "经验"}.get(r["authority"], "未知")
        version_info = f" (版本: {r['version']})" if r["version"] else ""
        year_info = f" ({r.get('year', '')})" if r.get("year") else ""

        output.append(f"[{i}] 【{r['doc_type']}】{r['source_title']}{version_info}{year_info}")
        output.append(f"    章节: {r['section']} | 权威级别: {authority_label} | 相似度: {r['score']:.4f}")
        output.append(f"    内容: {r['text'][:200]}{'...' if len(r['text']) > 200 else ''}")
        output.append("")

    return "\n".join(output)


def main():
    parser = argparse.ArgumentParser(description="生命科学 RAG 查询")
    parser.add_argument("query", type=str, help="查询内容")
    parser.add_argument("--top-k", type=int, default=5, help="返回数量")
    parser.add_argument("--role", choices=list(ROLE_PROFILE), default="general",
                        help="角色画像：hotline=热线专员 medical=医学官 pv=药物警戒专员")
    args = parser.parse_args()

    # 加载模型
    model = SentenceTransformer(_MODEL_PATH)

    # 连接 Milvus
    client = MilvusClient(uri="http://localhost:19531")

    # 检索
    profile = ROLE_PROFILE[args.role]
    logger.info(f"查询: {args.query} | 角色: {args.role}（{profile['desc']}）")
    raw_results = search_all_collections(client, model, args.query)
    logger.info(f"原始命中: {len(raw_results)} 条")

    # 覆盖缺口检测：查询涉及的药品有没有 A/A- 级原文命中
    # 没有 → 明示降级（诚实降级 > 静默给"最像的同类药"，药企合规要求）
    drugs = extract_drugs(args.query)
    if drugs:
        missing = [d for d in drugs if not any(
            d in r["source_title"] and r["authority"] in ("A", "A-") for r in raw_results
        )]
        if missing:
            print("\n" + "!" * 60)
            print(f"⚠️ 覆盖缺口：库中暂无【{'、'.join(missing)}】的说明书/指南原文")
            print("   以下结果为同类药或相关文档的参考口径，仅供参用；正式答复请升级医学官")
            print("!" * 60)

    # 融合
    fused = authority_fusion(raw_results, top_k=args.top_k, role=args.role)

    # 输出
    print("\n" + "=" * 60)
    print(format_answer(fused, args.query))
    print("=" * 60)

    # 热线专员附加：历史问询（approved）草稿参考
    if profile["with_inquiries"]:
        inquiries = search_inquiries(client, model, args.query)
        if inquiries:
            print("【历史问询参考（approved，可直接作答复草稿）】")
            for i, q in enumerate(inquiries, 1):
                print(f"[{i}] 相似度 {q['score']:.4f} | {q['question_std']}")
                print(f"    答复: {q['answer'][:150]}{'...' if len(q['answer']) > 150 else ''}")
            print("=" * 60)

    client.close()


if __name__ == "__main__":
    main()
