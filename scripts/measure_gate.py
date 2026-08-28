"""
资格闸标定测量：跑分层负样本的检索分数分布（只读检索，不调 LLM，秒级）

用法:
    python scripts/measure_gate.py

输出每条探针的 top-K 最高原始分 + 当前闸线判定，供人工定线。
与 answer.py 管道同路径（encode_query + 权威融合），测的就是上线后的真实分数。
相关侧参照（已在库证据）：文档探针基线 0.7271/0.7191；二甲双胍 trace top 0.55~0.67。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from src.thresholds import load_model_path, load_thresholds

# 复用检索层同一实现（与 answer.py/query.py 同路径，不生二套）
from scripts.query import authority_fusion, search_all_collections  # noqa: E402

_PROBES = Path(__file__).resolve().parent.parent / "config" / "gate_calibration.json"
MILVUS_URI = "http://localhost:19531"  # PoC 实例（与默认 19530 隔离）


def main():
    probes = json.loads(_PROBES.read_text(encoding="utf-8"))["probes"]
    floor = load_thresholds()["generation"]["draft_min_top1"]
    model = SentenceTransformer(load_model_path())
    client = MilvusClient(uri=MILVUS_URI)

    print(f"资格闸标定测量 | 当前闸线 draft_min_top1 = {floor} | {len(probes)} 条探针")
    print(f"{'类别':<18}{'top1分':<8}{'判定':<6} 问题")
    print("─" * 72)
    try:
        for p in probes:
            raw = search_all_collections(client, model, p["question"])
            chunks = authority_fusion(raw, top_k=5, role="general")
            top1 = max((c["score"] for c in chunks), default=0.0)
            verdict = "过闸" if top1 >= floor else "拦截"
            print(f"{p['category']:<18}{top1:<8.4f}{verdict:<6} {p['question']}")
            if chunks:
                top = max(chunks, key=lambda c: c["score"])
                print(f"{'':<18}↳ top块:《{top['source_title']}》{top['section'][:20]}")
    finally:
        client.close()
    print("─" * 72)
    print("判读要点：full_irrelevant 应全部拦截；partial_overlap / drug_in_topic_out 过闸与否")
    print("决定是否需要'部分覆盖'终态或重锚闸线。把输出贴回分析。")


if __name__ == "__main__":
    main()
