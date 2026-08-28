"""
按文档结构拆块，每块携带元数据

与气象项目的关键区别：
- 气象：一条反馈 = 一个 chunk（30~80字）
- 生命科学：按文档章节拆，每 chunk 128~512 字，携带 doc_type/section/version/authority
"""

import hashlib


def chunk_drug_insert(doc: dict) -> list[dict]:
    """说明书：每个章节一个 chunk"""
    chunks = []
    for sec in doc["sections"]:
        chunks.append({
            "text": f"【{doc['drug_name']}】【{sec['section']}】{sec['text']}",
            "source_title": doc["drug_name"],
            "doc_type": "说明书",
            "section": sec["section"],
            "version": doc["version"],
            "authority": "A",
            "drug_name": doc["drug_name"],
        })
    return chunks


def chunk_clinical_paper(doc: dict) -> list[dict]:
    """论文：每个段落一个 chunk"""
    chunks = []
    for sec in doc["sections"]:
        chunks.append({
            "text": f"【{sec['section']}】{sec['text']}",
            "source_title": doc["paper_title"],
            "doc_type": "论文",
            "section": sec["section"],
            "journal": doc["journal"],
            "year": doc["year"],
            "authority": "B",
        })
    return chunks


def chunk_case_report(doc: dict) -> list[dict]:
    """病例报告：每个段落一个 chunk"""
    chunks = []
    for sec in doc["sections"]:
        chunks.append({
            "text": f"【{sec['section']}】{sec['text']}",
            "source_title": doc["case_id"],
            "doc_type": "病例报告",
            "section": sec["section"],
            "authority": "C",
            "case_id": doc["case_id"],
        })
    return chunks


def chunk_document(doc: dict) -> list[dict]:
    """根据文档类型选择拆块策略"""
    doc_type = doc.get("doc_type", "")

    if doc_type == "说明书":
        return chunk_drug_insert(doc)
    elif doc_type == "论文":
        return chunk_clinical_paper(doc)
    elif doc_type == "病例报告":
        return chunk_case_report(doc)
    else:
        return []


def generate_chunk_id(chunk: dict) -> int:
    """基于内容生成稳定 ID（用于去重）"""
    content = f"{chunk['source_title']}|{chunk['section']}|{chunk['text'][:50]}"
    return int(hashlib.md5(content.encode()).hexdigest()[:8], 16)
