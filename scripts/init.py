"""
初始化脚本：建 Collection + 拆块 + Embedding + 灌库

用法: python scripts/init.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.thresholds import load_model_path

import logging
from src.milvus_collections import create_collections, insert_chunks, get_client
from src.chunking import chunk_document, generate_chunk_id
from src.sample_data import get_all_documents

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_MODEL_PATH = load_model_path()  # 模型路径与阈值同源（thresholds.yaml model 字段）


def load_embedding_model():
    from sentence_transformers import SentenceTransformer
    logger.info(f"加载 Embedding 模型: {_MODEL_PATH}")
    model = SentenceTransformer(_MODEL_PATH)
    logger.info("Embedding 模型加载成功")
    return model


def main():
    # 1. 连接 Milvus
    client = get_client()
    logger.info("Milvus 连接成功")

    # 2. 创建 Collection
    logger.info("创建 Collection...")
    create_collections(client)

    # 3. 拆块
    docs = get_all_documents()
    all_chunks = []
    for doc in docs:
        chunks = chunk_document(doc)
        for c in chunks:
            c["id"] = generate_chunk_id(c)
        all_chunks.extend(chunks)
    logger.info(f"共拆分 {len(all_chunks)} 个 chunk")

    # 4. 生成 Embedding
    model = load_embedding_model()
    texts = [c["text"] for c in all_chunks]
    logger.info("生成 Embedding...")
    embeddings = model.encode(texts, normalize_embeddings=True).tolist()
    logger.info(f"Embedding 完成，维度: {len(embeddings[0])}")

    # 5. 写入 Milvus
    logger.info("写入 Milvus...")
    insert_chunks(client, all_chunks, embeddings)

    # 6. 统计
    for name in ["drug_inserts", "clinical_papers", "case_reports"]:
        stats = client.get_collection_stats(name)
        logger.info(f"  {name}: {stats['row_count']} 条")

    client.close()
    logger.info("🎉 初始化完成")


if __name__ == "__main__":
    main()
