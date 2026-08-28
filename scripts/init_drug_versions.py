"""
④ 说明书双版本：改版流程演练（两幕剧本）

真实场景：说明书被 NMPA 核准修订后，新旧版本并存。
  混乱态：旧版没标作废，和现行版一起进了影子库 → 检索可能命中过期口径（合规事故）
  改版流程：旧版 status='作废'（留 MySQL 审计）→ 影子按 id 精准摘除 → 回归验证只命中现行

复用现有机制，零新增组件：
  seed  → chunk_document + sync_pending_chunks（同 ingest）
  摘除  → init_guidelines.retire_obsolete（② 阶段的通用退役逻辑，按 doc_type 路由）

用法（两幕之间跑 query / regression 观察混乱态）:
  python scripts/init_drug_versions.py --phase chaos    # 第一幕：v2023 以"现行"入库（旧版未作废的混乱态）
  python scripts/init_drug_versions.py --phase retire   # 第二幕：改版生效，v2023 作废 + 影子摘除
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.thresholds import load_model_path

import argparse
import json
import logging

from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from scripts.init_guidelines import retire_obsolete
from scripts.migrate_to_mysql import sync_pending_chunks
from src.chunking import chunk_document
from src.db import get_conn
from src.drug_versions_data import OLD_INSERT_V2023

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MILVUS_URI = "http://localhost:19531"
_MODEL_PATH = load_model_path()  # 模型路径与阈值同源（thresholds.yaml model 字段）

TITLE = OLD_INSERT_V2023["drug_name"]
OLD_VERSION = OLD_INSERT_V2023["version"]


def seed_old_version() -> bool:
    """v2023 以'现行'入库（模拟旧版未标作废的混乱态）。幂等。返回是否新增"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM documents WHERE doc_type='说明书' AND title=%s AND version=%s",
                (TITLE, OLD_VERSION),
            )
            if cur.fetchone():
                logger.info(f"  已存在跳过: {TITLE} v{OLD_VERSION}")
                return False
            cur.execute(
                "INSERT INTO documents (doc_type, title, authority, version, status, source_meta) "
                "VALUES ('说明书', %s, 'A', %s, '现行', %s)",
                (TITLE, OLD_VERSION, json.dumps({"drug_name": TITLE}, ensure_ascii=False)),
            )
            doc_id = cur.lastrowid
            for seq, chunk in enumerate(chunk_document(OLD_INSERT_V2023)):
                cur.execute(
                    "INSERT INTO document_chunks (document_id, section, seq, text) VALUES (%s, %s, %s, %s)",
                    (doc_id, chunk["section"], seq, chunk["text"]),
                )
        conn.commit()
        logger.info(f"  入库: {TITLE} v{OLD_VERSION} [现行]（{len(OLD_INSERT_V2023['sections'])} 块）")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_obsolete() -> bool:
    """改版生效：v2023 标作废（留 MySQL 审计，不删）"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET status='作废' "
                "WHERE doc_type='说明书' AND title=%s AND version=%s AND status='现行'",
                (TITLE, OLD_VERSION),
            )
            n = cur.rowcount
        conn.commit()
        return n > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="说明书双版本演练")
    parser.add_argument("--phase", required=True, choices=["chaos", "retire"],
                        help="chaos=旧版未作废的混乱态 / retire=改版生效摘除旧版")
    args = parser.parse_args()

    client = MilvusClient(uri=MILVUS_URI)

    if args.phase == "chaos":
        if seed_old_version():
            model = SentenceTransformer(_MODEL_PATH)
            sync_pending_chunks(model, client)
            client.flush("drug_inserts")  # 军规：写完要读必 flush
        stats = client.get_collection_stats("drug_inserts")
        logger.info(f"混乱态就绪：drug_inserts 现有 {stats['row_count']} 行（新旧两版并存）")
        logger.info('→ 观察: python scripts/query.py "阿托伐他汀和红霉素联用需要注意什么"')
        logger.info("→ 观察: python scripts/regression.py  （版本断言应该报警）")

    else:  # retire
        if mark_obsolete():
            retired = retire_obsolete(client)  # 通用退役逻辑：影子按 id 摘除 + 重置标记
            client.flush("drug_inserts")
            logger.info(f"改版生效：v{OLD_VERSION} 已作废，影子摘除 {retired} 块（MySQL 留档审计）")
        else:
            logger.info("v2023 已是作废状态，无需操作")
        stats = client.get_collection_stats("drug_inserts")
        logger.info(f"drug_inserts 现有 {stats['row_count']} 行（应只剩现行版）")
        logger.info('→ 验证: python scripts/query.py "阿托伐他汀和红霉素联用需要注意什么"（应只命中 v2024.03）')
        logger.info("→ 验证: python scripts/regression.py  （应全绿）")


if __name__ == "__main__":
    main()
