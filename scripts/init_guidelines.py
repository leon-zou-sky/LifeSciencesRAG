"""
指南库灌库脚本（幂等）——演练 A 类文档的版本管理

  现行版：documents + chunks 入库，正常同步进 Milvus
  作废版：documents + chunks 入库留档（审计），但不同步进影子库
  退役：  作废文档的 chunks 如果之前同步过，从 Milvus 按 id 精准摘除

用法: python scripts/init_guidelines.py   # 只动 MySQL；之后跑 migrate_to_mysql.py 重建影子
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import logging

from pymilvus import MilvusClient

from migrate_to_mysql import DOC_TYPE_TO_COLLECTION, MILVUS_URI
from src.db import get_conn
from src.guidelines_data import GUIDELINES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def seed_guidelines() -> int:
    """指南入 MySQL（现行+作废都入，留档审计）。幂等：按 类型+标题+版本 判重。返回新增文档数"""
    conn = get_conn()
    added = 0
    try:
        with conn.cursor() as cur:
            for g in GUIDELINES:
                cur.execute(
                    "SELECT id FROM documents WHERE doc_type='指南' AND title=%s AND version=%s",
                    (g["title"], g["version"]),
                )
                if cur.fetchone():
                    logger.info(f"  已存在跳过: {g['title']} {g['version']}")
                    continue
                cur.execute(
                    "INSERT INTO documents (doc_type, title, authority, version, status, source_meta) "
                    "VALUES ('指南', %s, %s, %s, %s, %s)",
                    (g["title"], g["authority"], g["version"], g["status"],
                     json.dumps(g["source_meta"], ensure_ascii=False)),
                )
                doc_id = cur.lastrowid
                for seq, (rec_title, grade, body) in enumerate(g["recommendations"]):
                    section = f"{rec_title}（{grade}）"
                    text = f"【{g['title']} {g['version']}版】【{section}】{body}"
                    cur.execute(
                        "INSERT INTO document_chunks (document_id, section, seq, text) VALUES (%s, %s, %s, %s)",
                        (doc_id, section, seq, text),
                    )
                added += 1
                logger.info(f"  入库: {g['title']} {g['version']}版 [{g['status']}]（{len(g['recommendations'])} 条推荐意见）")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return added


def retire_obsolete(client: MilvusClient) -> int:
    """作废文档的 chunks 若曾同步进 Milvus，按 id 摘除（影子只保留现行内容）。返回摘除条数"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, d.doc_type FROM document_chunks c
                JOIN documents d ON c.document_id = d.id
                WHERE d.status = '作废' AND c.embedding_updated_at IS NOT NULL
                """
            )
            rows = cur.fetchall()
            if not rows:
                return 0
            grouped: dict[str, list] = {}
            for r in rows:
                coll = DOC_TYPE_TO_COLLECTION.get(r["doc_type"], "case_reports")
                grouped.setdefault(coll, []).append(r["id"])
            for coll, ids in grouped.items():
                if client.has_collection(coll):
                    client.delete(collection_name=coll, ids=ids)
            ids = [r["id"] for r in rows]
            cur.execute(
                f"UPDATE document_chunks SET embedding_updated_at=NULL "
                f"WHERE id IN ({','.join(['%s'] * len(ids))})", ids,
            )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    added = seed_guidelines()
    logger.info(f"① MySQL 灌库完成: 新增 {added} 份指南")

    client = MilvusClient(uri=MILVUS_URI)
    retired = retire_obsolete(client)
    logger.info(f"② 作废版本摘除: {retired} 条（首次运行为 0，版本更替时才生效）")

    logger.info("③ 下一步: python scripts/migrate_to_mysql.py  重建影子库（含 guidelines Collection）")


if __name__ == "__main__":
    main()
