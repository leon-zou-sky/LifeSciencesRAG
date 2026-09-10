"""Embedding 模型与 Milvus schema 的一致性校验。

保持为纯 Python 模块，使安全门禁不依赖 pymilvus、模型权重或外部服务。
"""


def embedding_dimension(model) -> int:
    """从实际加载的 embedding 模型读取向量维度。"""
    getter = getattr(model, "get_embedding_dimension", None) or getattr(
        model, "get_sentence_embedding_dimension", None
    )
    if not callable(getter):
        raise TypeError("Embedding 模型未提供维度读取方法，拒绝创建向量 Collection")
    dim = getter()
    if not isinstance(dim, int) or dim <= 0:
        raise ValueError(f"Embedding 模型返回了非法维度: {dim!r}")
    return dim


def collection_embedding_dimension(client, name: str) -> int:
    """读取已有 Collection 的 embedding 字段维度。"""
    desc = client.describe_collection(collection_name=name)
    fields = desc.get("fields") or desc.get("schema", {}).get("fields") or []
    for field in fields:
        if field.get("name") != "embedding":
            continue
        try:
            dim = int((field.get("params") or {}).get("dim"))
        except (TypeError, ValueError):
            break
        if dim > 0:
            return dim
    raise RuntimeError(f"无法读取 Collection {name!r} 的 embedding 维度，拒绝执行破坏性重建")


def assert_collection_dimension(client, name: str, expected_dim: int) -> None:
    """已有索引与当前模型维度不一致时失败关闭，禁止普通脚本覆盖它。"""
    actual_dim = collection_embedding_dimension(client, name)
    if actual_dim != expected_dim:
        raise RuntimeError(
            f"Collection {name!r} 的向量维度为 {actual_dim}，当前模型输出为 {expected_dim}。"
            "拒绝普通重建，避免混合向量空间；请走 rebuild_shadow.py → 标定/回归 → --cutover。"
        )
