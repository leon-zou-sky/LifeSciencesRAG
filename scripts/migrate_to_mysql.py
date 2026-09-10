"""
迁移脚本：sample_data → MySQL（源数据层）→ 重建 Milvus（影子索引）

建立 "MySQL 是源、Milvus 可重建" 的分层：
  1. seed:   sample_data.py 的文档灌入 documents / document_chunks（幂等）
  2. rebuild: 删除并重建 Milvus 3 个 Collection（仅针对 PoC 实例 :19531）
  3. sync:   扫描 embedding_updated_at IS NULL 的块 → BGE 编码 → upsert Milvus → 回写标记

用法:
    python scripts/migrate_to_mysql.py              # 全量：seed + 重建 Milvus + 同步
    python scripts/migrate_to_mysql.py --seed-only  # 只灌 MySQL，不动 Milvus
    python scripts/migrate_to_mysql.py --sync-only  # 只同步待处理块（日常增量模式）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.thresholds import load_model_path

import argparse
import json
import logging
from datetime import datetime

from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from src.chunking import chunk_document
from src.embedding_schema import embedding_dimension
from src.milvus_collections import COLLECTIONS, create_collections
from src.db import get_conn
from src.sample_data import get_all_documents

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MILVUS_URI = "http://localhost:19531"  # PoC 实例（与默认 19530 隔离）
_MODEL_PATH = load_model_path()  # 模型路径与阈值同源（thresholds.yaml model 字段）

DOC_TYPE_TO_COLLECTION = {"说明书": "drug_inserts", "论文": "clinical_papers", "指南": "guidelines", "病例报告": "case_reports"}


# ============ ① seed：sample_data → MySQL ============

def _doc_meta(doc: dict) -> tuple[str, str, str, dict]:
    """从样本文档提取 (title, version, authority, source_meta)"""
    doc_type = doc["doc_type"]
    if doc_type == "说明书":
        return doc["drug_name"], doc.get("version", ""), doc["authority"], {"drug_name": doc["drug_name"]}
    if doc_type == "论文":
        return doc["paper_title"], "", doc["authority"], {"journal": doc["journal"], "year": doc["year"]}
    return doc["case_id"], "", doc["authority"], {"case_id": doc["case_id"]}


def seed_mysql() -> tuple[int, int]:
    """灌入 documents + document_chunks，幂等（按 doc_type+title+version 判重）。返回 (新文档数, 新块数)"""
    conn = get_conn()
    new_docs = new_chunks = 0
    try:
        with conn.cursor() as cur:
            for doc in get_all_documents():
                title, version, authority, meta = _doc_meta(doc)
                cur.execute(
                    "SELECT id FROM documents WHERE doc_type=%s AND title=%s AND version=%s",
                    (doc["doc_type"], title, version),
                )
                row = cur.fetchone()
                if row:
                    doc_id = row["id"]
                    logger.info(f"  已存在跳过: [{doc['doc_type']}] {title}")
                    continue
                cur.execute(
                    "INSERT INTO documents (doc_type, title, authority, version, source_meta) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (doc["doc_type"], title, authority, version, json.dumps(meta, ensure_ascii=False)),
                )
                doc_id = cur.lastrowid
                new_docs += 1

                for seq, chunk in enumerate(chunk_document(doc)):
                    cur.execute(
                        "INSERT INTO document_chunks (document_id, section, seq, text) "
                        "VALUES (%s, %s, %s, %s)",
                        (doc_id, chunk["section"], seq, chunk["text"]),
                    )
                    new_chunks += 1
                logger.info(f"  入库: [{doc['doc_type']}] {title} ({seq + 1} 块)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return new_docs, new_chunks


# ============ ② sync：MySQL 待同步块 → Milvus ============

def sync_pending_chunks(model, client: MilvusClient, batch_size: int = 64) -> int:
    """扫描 embedding_updated_at IS NULL 的块，编码后 upsert 进对应 Collection。返回同步条数"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.text, c.section,
                       d.title, d.doc_type, d.authority, d.version
                FROM document_chunks c JOIN documents d ON c.document_id = d.id
                WHERE c.embedding_updated_at IS NULL AND d.status = '现行'  -- 影子库只放现行内容
                ORDER BY c.id LIMIT %s
                """,
                (batch_size,),
            )
            rows = cur.fetchall()
            if not rows:
                return 0

            vectors = model.encode([r["text"] for r in rows], normalize_embeddings=True).tolist()

            grouped: dict[str, list] = {}
            for r, vec in zip(rows, vectors):
                coll = DOC_TYPE_TO_COLLECTION.get(r["doc_type"], "case_reports")
                grouped.setdefault(coll, []).append({
                    "id": r["id"],
                    "text": r["text"],
                    "source_title": r["title"],
                    "doc_type": r["doc_type"],
                    "section": r["section"],
                    "authority": r["authority"],
                    "version": r["version"] or "",
                    "embedding": vec,
                })
            for coll, data in grouped.items():
                client.upsert(collection_name=coll, data=data)

            ids = [r["id"] for r in rows]
            cur.execute(
                f"UPDATE document_chunks SET embedding_updated_at=%s "
                f"WHERE id IN ({','.join(['%s'] * len(ids))})",
                [datetime.now(), *ids],
            )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============ 主流程 ============

def main():
    parser = argparse.ArgumentParser(description="MySQL 源数据层迁移")
    parser.add_argument("--seed-only", action="store_true", help="只灌 MySQL")
    parser.add_argument("--sync-only", action="store_true", help="只同步待处理块到 Milvus（不重建）")
    args = parser.parse_args()

    # ① seed
    new_docs, new_chunks = seed_mysql()
    logger.info(f"① MySQL seed 完成: 新增 {new_docs} 文档 / {new_chunks} 块")
    if args.seed_only:
        return

    model = SentenceTransformer(_MODEL_PATH)
    client = MilvusClient(uri=MILVUS_URI)

    # ② 重建 Milvus（影子索引可随意重建，源在 MySQL）
    if not args.sync_only:
        create_collections(client, dim=embedding_dimension(model))  # 维度必须与当前模型同源
        # 重建后必须重置同步标记，否则已同步过的块不会重新进影子库
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE document_chunks SET embedding_updated_at=NULL")
            conn.commit()
        logger.info("② Milvus Collection 已重建 + 同步标记已重置（PoC 实例 :19531）")

    # ③ 增量同步循环（含 rebuild 后的全量）
    total = 0
    while True:
        n = sync_pending_chunks(model, client)
        if n == 0:
            break
        total += n
        logger.info(f"  同步批次: {n} 条")
    logger.info(f"③ 同步完成: 共 {total} 块 → Milvus")

    # 验证计数（先 flush，否则 Bounded 一致性下 upsert 的行还不可见，计数全是 0）
    for coll in COLLECTIONS:
        client.flush(coll)
    for coll in COLLECTIONS:
        stats = client.get_collection_stats(coll)
        logger.info(f"  {coll}: {stats['row_count']} 行")


if __name__ == "__main__":
    main()
