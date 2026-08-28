"""
医学问询底库灌库脚本（幂等）

流程：归一（商品名→通用名）→ 入 MySQL mi_inquiries → 建 Collection → 同步 Milvus

用法:
    python scripts/init_inquiries.py            # 灌底库 + 建 Collection + 同步
    python scripts/init_inquiries.py --rebuild  # 删除重建 Collection 后全量同步
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.thresholds import load_model_path

import argparse
import logging
from datetime import datetime

from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from src.milvus_collections import create_inquiry_collection
from src.db import get_conn
from src.messy_inquiries import BASE_INQUIRIES
from src.normalization import extract_drugs, normalize_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MILVUS_URI = "http://localhost:19531"
_MODEL_PATH = load_model_path()  # 模型路径与阈值同源（thresholds.yaml model 字段）


def seed_inquiries() -> int:
    """底库问询入 MySQL（归一在写入前完成）。幂等：按 question_std 判重。返回新增条数"""
    conn = get_conn()
    added = 0
    try:
        with conn.cursor() as cur:
            for inq in BASE_INQUIRIES:
                std = normalize_text(inq["question_raw"])
                cur.execute("SELECT id FROM mi_inquiries WHERE question_std=%s", (std,))
                if cur.fetchone():
                    logger.info(f"  已存在跳过: {std[:30]}")
                    continue
                cur.execute(
                    "INSERT INTO mi_inquiries (question_raw, question_std, answer, drugs, channel, status, authority) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (inq["question_raw"], std, inq["answer"],
                     ",".join(extract_drugs(inq["question_raw"])),
                     inq["channel"], inq["status"], inq["authority"]),
                )
                added += 1
                logger.info(f"  入库: {std[:40]}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return added


def sync_pending_inquiries(model, client: MilvusClient) -> int:
    """待同步问询 → Milvus。embedding 文本 = question_std（判重是问题比问题，
    带上长答案会稀释相似度；answer 作为 payload 存储供答复引用）。返回同步条数"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, question_std, answer, drugs, status, authority "
                "FROM mi_inquiries WHERE embedding_updated_at IS NULL"
            )
            rows = cur.fetchall()
            if not rows:
                return 0
            texts = [r["question_std"] for r in rows]
            vectors = model.encode(texts, normalize_embeddings=True).tolist()
            data = [
                {
                    "id": r["id"],
                    "question_std": r["question_std"],
                    "answer": r["answer"] or "",
                    "drugs": r["drugs"] or "",
                    "status": r["status"],
                    "authority": r["authority"],
                    "embedding": v,
                }
                for r, v in zip(rows, vectors)
            ]
            client.upsert(collection_name="mi_inquiries", data=data)
            ids = [r["id"] for r in rows]
            cur.execute(
                f"UPDATE mi_inquiries SET embedding_updated_at=%s WHERE id IN ({','.join(['%s'] * len(ids))})",
                [datetime.now(), *ids],
            )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="医学问询底库灌库")
    parser.add_argument("--rebuild", action="store_true", help="删除重建 Collection 后全量同步")
    args = parser.parse_args()

    added = seed_inquiries()
    logger.info(f"① MySQL 灌库完成: 新增 {added} 条")

    model = SentenceTransformer(_MODEL_PATH)
    client = MilvusClient(uri=MILVUS_URI)

    create_inquiry_collection(client, drop=args.rebuild)

    if args.rebuild:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE mi_inquiries SET embedding_updated_at=NULL")
            conn.commit()

    n = sync_pending_inquiries(model, client)
    logger.info(f"② Milvus 同步完成: {n} 条")


if __name__ == "__main__":
    main()
