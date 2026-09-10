"""
蓝绿影子重建：用新模型从 MySQL 源层全量重编码 → 影子 Collection（重标定 runbook A.1①）

用法:
    python scripts/rebuild_shadow.py --model-path <模型路径> --tag <模型标识>
    python scripts/rebuild_shadow.py --model-path <模型路径> --tag bgesmall --cutover   # 标定+回归通过后切流

要点：
  - 影子命名为 <base>__<tag>（如 mi_inquiries__bgesmall），与现役 Collection 并存互不干扰
  - 维度从模型自动读取（get_sentence_embedding_dimension），不同模型 schema 不同
  - 只读 MySQL 源层，不动 embedding_updated_at 标记（那是主管道的状态，影子是独立视图）
  - --cutover：删除旧 Collection，给影子建别名（base 名）——下游脚本零改动完成切流。
    旧 Collection 删除是安全的：源在 MySQL，随时可用旧模型重建（源/影子分层的意义）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging

from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from src.db import get_conn
from src.embedding_schema import embedding_dimension
from src.milvus_collections import (
    COLLECTIONS, create_collections, create_inquiry_collection, shadow_name,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MILVUS_URI = "http://localhost:19531"  # PoC 实例（与默认 19530 隔离）
DOC_TYPE_TO_COLLECTION = {"说明书": "drug_inserts", "论文": "clinical_papers", "指南": "guidelines", "病例报告": "case_reports"}


def build_doc_shadows(model, client: MilvusClient, tag: str, dim: int) -> int:
    """文档四库影子：MySQL 现行 chunks → 新模型编码 → <base>__<tag>"""
    create_collections(client, dim=dim, suffix=f"__{tag}")
    conn = get_conn()
    total = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.text, c.section, d.title, d.doc_type, d.authority, d.version
                FROM document_chunks c JOIN documents d ON c.document_id = d.id
                WHERE d.status = '现行'
                ORDER BY c.id
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    grouped: dict[str, list] = {}
    batch = 64
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        vecs = model.encode([r["text"] for r in part], normalize_embeddings=True).tolist()
        for r, vec in zip(part, vecs):
            coll = shadow_name(DOC_TYPE_TO_COLLECTION.get(r["doc_type"], "case_reports"), tag)
            grouped.setdefault(coll, []).append({
                "id": r["id"], "text": r["text"], "source_title": r["title"],
                "doc_type": r["doc_type"], "section": r["section"],
                "authority": r["authority"], "version": r["version"] or "", "embedding": vec,
            })
    for coll, data in grouped.items():
        client.upsert(collection_name=coll, data=data)
        client.flush(coll)   # 军规：写完紧接着要读（计数/标定）必须 flush
        total += len(data)
        logger.info(f"  {coll}: {len(data)} 块")
    return total


def build_inquiry_shadow(model, client: MilvusClient, tag: str, dim: int) -> int:
    """问询库影子：approved 的 question_std → 新模型编码 → mi_inquiries__<tag>"""
    name = shadow_name("mi_inquiries", tag)
    create_inquiry_collection(client, drop=True, name=name, dim=dim)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, question_std, answer, drugs, status, authority "
                "FROM mi_inquiries WHERE status = 'approved'"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return 0
    vecs = model.encode([r["question_std"] for r in rows], normalize_embeddings=True).tolist()
    data = [
        {"id": r["id"], "question_std": r["question_std"], "answer": r["answer"] or "",
         "drugs": r["drugs"] or "", "status": r["status"], "authority": r["authority"],
         "embedding": vec}
        for r, vec in zip(rows, vecs)
    ]
    client.upsert(collection_name=name, data=data)
    client.flush(name)   # 军规：写完紧接着要读必须 flush
    logger.info(f"  {name}: {len(data)} 条")
    return len(data)


def cutover(client: MilvusClient, tag: str):
    """切流：删旧 Collection（源在 MySQL 可重建）→ 影子建 base 别名，下游脚本零改动"""
    for base in list(COLLECTIONS) + ["mi_inquiries"]:
        shadow = shadow_name(base, tag)
        if not client.has_collection(shadow):
            logger.warning(f"  影子不存在，跳过: {shadow}")
            continue
        if client.has_collection(base):
            client.drop_collection(base)
            logger.info(f"  删除旧 Collection: {base}（源在 MySQL，可随时重建）")
        client.create_alias(collection_name=shadow, alias=base)
        logger.info(f"  切流完成: {base} → {shadow}")


def main():
    parser = argparse.ArgumentParser(description="蓝绿影子重建（换模型/重标定）")
    parser.add_argument("--model-path", required=True, help="新模型本地路径")
    parser.add_argument("--tag", required=True, help="模型标识（影子名后缀，如 bgesmall）")
    parser.add_argument("--cutover", action="store_true", help="标定+回归通过后切流：删旧 Collection + 建别名")
    args = parser.parse_args()

    client = MilvusClient(uri=MILVUS_URI)

    if args.cutover:
        cutover(client, args.tag)
        return

    model = SentenceTransformer(args.model_path)
    dim = embedding_dimension(model)
    logger.info(f"模型: {args.model_path}（维度 {dim}），影子标签: __{args.tag}")

    n_docs = build_doc_shadows(model, client, args.tag, dim)
    n_inq = build_inquiry_shadow(model, client, args.tag, dim)
    logger.info(f"影子重建完成: 文档 {n_docs} 块 + 问询 {n_inq} 条 → *__{args.tag}")
    logger.info("下一步：scripts/calibrate.py 标定 → 回归验证 → --cutover 切流")


if __name__ == "__main__":
    main()
