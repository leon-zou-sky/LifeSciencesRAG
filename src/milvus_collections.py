"""
多 Collection 管理

按文档类型拆 Collection，每个 Collection 独立 Schema 和索引
"""

import logging
from pymilvus import MilvusClient, DataType

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1024  # BGE-large-zh-v1.5


def shadow_name(base: str, tag: str) -> str:
    """蓝绿影子命名：<base>__<tag>（tag 一般为模型标识，如 bgesmall）。
    不同模型的向量维度/分数分布不同，严禁混住同一 Collection（重标定 runbook A.1①）"""
    return f"{base}__{tag}"

# Collection 配置：每个文档类型一个 Collection
COLLECTIONS = {
    "drug_inserts": {
        "description": "药品说明书（权威级，关键词精确检索）",
        "authority": "A",
    },
    "clinical_papers": {
        "description": "临床论文（参考级，向量语义检索）",
        "authority": "B",
    },
    "case_reports": {
        "description": "病例分析/不良事件（经验级，混合检索）",
        "authority": "C",
    },
    "guidelines": {
        "description": "诊疗指南/专家共识（准权威级，按单条推荐意见拆块）",
        "authority": "A-",
    },
}


def _create_schema(client: MilvusClient, dim: int = EMBEDDING_DIM):
    """通用 Schema（所有 Collection 共用字段）。dim 随模型维度传入（蓝绿影子可能与现役不同维度）"""
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)

    schema.add_field("id", DataType.INT64, is_primary=True)
    schema.add_field("text", DataType.VARCHAR, max_length=2048)
    schema.add_field("source_title", DataType.VARCHAR, max_length=512)
    schema.add_field("doc_type", DataType.VARCHAR, max_length=32)
    schema.add_field("section", DataType.VARCHAR, max_length=64)
    schema.add_field("authority", DataType.VARCHAR, max_length=4)
    schema.add_field("version", DataType.VARCHAR, max_length=32)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)

    return schema


def create_collections(client: MilvusClient, dim: int = EMBEDDING_DIM, suffix: str = ""):
    """创建所有 Collection。suffix 用于蓝绿影子（如 "__bgesmall"），dim 随模型维度传入"""
    for name in COLLECTIONS:
        full = f"{name}{suffix}"
        if client.has_collection(full):
            client.drop_collection(full)
            logger.info(f"  删除旧 Collection: {full}")

    schema = _create_schema(client, dim)
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        index_type="IVF_FLAT",
        metric_type="COSINE",
        params={"nlist": 64},
    )

    for name in COLLECTIONS:
        full = f"{name}{suffix}"
        client.create_collection(
            collection_name=full,
            schema=schema,
            index_params=index_params,
        )
        logger.info(f"  创建 Collection: {full}")


def insert_chunks(client: MilvusClient, chunks: list[dict], embeddings: list[list[float]]):
    """按 doc_type 路由到对应 Collection 并写入"""
    doc_type_to_collection = {
        "说明书": "drug_inserts",
        "论文": "clinical_papers",
        "病例报告": "case_reports",
    }

    grouped = {}
    for i, chunk in enumerate(chunks):
        coll = doc_type_to_collection.get(chunk["doc_type"], "case_reports")
        if coll not in grouped:
            grouped[coll] = []
        grouped[coll].append({
            "id": chunk["id"],
            "text": chunk["text"],
            "source_title": chunk["source_title"],
            "doc_type": chunk["doc_type"],
            "section": chunk["section"],
            "authority": chunk["authority"],
            "version": chunk.get("version", ""),
            "embedding": embeddings[i],
        })

    for coll_name, data in grouped.items():
        client.insert(collection_name=coll_name, data=data)
        logger.info(f"  写入 {coll_name}: {len(data)} 条")


client = None


def create_inquiry_collection(client: MilvusClient, drop: bool = False,
                              name: str = "mi_inquiries", dim: int = EMBEDDING_DIM):
    """创建医学问询 Collection（事件流数据，schema 与文档类不同）。name/dim 支持蓝绿影子"""
    if client.has_collection(name):
        if not drop:
            logger.info(f"  Collection 已存在: {name}")
            return
        client.drop_collection(name)
        logger.info(f"  删除旧 Collection: {name}")

    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id", DataType.INT64, is_primary=True)
    schema.add_field("question_std", DataType.VARCHAR, max_length=512)
    schema.add_field("answer", DataType.VARCHAR, max_length=2048)
    schema.add_field("drugs", DataType.VARCHAR, max_length=256)
    schema.add_field("status", DataType.VARCHAR, max_length=16)
    schema.add_field("authority", DataType.VARCHAR, max_length=4)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        index_type="IVF_FLAT",
        metric_type="COSINE",
        params={"nlist": 64},
    )
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    logger.info(f"  创建 Collection: {name}")


def get_client(uri: str = "http://localhost:19531") -> MilvusClient:
    global client
    if client is None:
        client = MilvusClient(uri=uri)
    return client
