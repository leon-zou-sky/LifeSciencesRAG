"""向量 schema 安全门禁（不依赖 Milvus 或模型文件）。

验证普通建库路径必须使用实际模型维度，并在已有 schema 不一致时失败关闭。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.embedding_schema import assert_collection_dimension, embedding_dimension


class _Model:
    def __init__(self, dim):
        self.dim = dim

    def get_embedding_dimension(self):
        return self.dim


class _Client:
    def __init__(self, dim):
        self.dim = dim

    def describe_collection(self, *, collection_name):
        return {"fields": [{"name": "embedding", "params": {"dim": str(self.dim)}}]}


def main():
    assert embedding_dimension(_Model(512)) == 512
    assert_collection_dimension(_Client(512), "drug_inserts", 512)

    try:
        assert_collection_dimension(_Client(1024), "drug_inserts", 512)
    except RuntimeError as exc:
        assert "拒绝普通重建" in str(exc)
    else:
        raise AssertionError("schema 维度不一致时必须失败关闭")

    try:
        embedding_dimension(_Model(0))
    except ValueError:
        pass
    else:
        raise AssertionError("非法模型维度必须失败关闭")

    print("✅ 向量 schema 安全门禁通过")


if __name__ == "__main__":
    main()
