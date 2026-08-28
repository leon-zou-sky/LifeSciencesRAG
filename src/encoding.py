"""
查询编码策略：对称 vs 非对称（BGE 官方推荐用法落地）

两种检索模式的编码方式不同，不可混用：

  - 对称比较（问题 vs 问题，判重）：两侧都裸编码，保持对称性
      encode_symmetric(model, ["阿托伐他汀可以和红霉素联用吗"])
      用于：mi_inquiries 判重 / calibrate.py 标定对

  - 非对称检索（问题 vs 文档段落，取证）：问题侧加 BGE 查询指令前缀，文档侧裸编码
      encode_query(model, ["阿托伐他汀和克拉霉素联用需要注意什么"])
      用于：文档四库（drug_inserts/guidelines/clinical_papers/case_reports）的查询侧
      官方评测该指令可提升检索准确率 1~2 个点；文档侧（入库编码）永远裸编码，
      否则库内向量与历史基线不可比

注意：文档侧查询加指令后分数刻度会微移，黄金集文档探针基线需重录（--rebaseline）。
"""
from sentence_transformers import SentenceTransformer

# BGE 官方查询指令（仅短查询→长段落的非对称检索使用）
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def encode_query(model: SentenceTransformer, texts: list[str]) -> list[list[float]]:
    """非对称检索的查询侧编码：加指令前缀。仅用于文档库检索，禁止用于判重（判重须对称）"""
    return model.encode([BGE_QUERY_INSTRUCTION + t for t in texts],
                        normalize_embeddings=True).tolist()


def encode_symmetric(model: SentenceTransformer, texts: list[str]) -> list[list[float]]:
    """对称比较编码：裸编码。用于问题 vs 问题（判重/标定）"""
    return model.encode(texts, normalize_embeddings=True).tolist()
