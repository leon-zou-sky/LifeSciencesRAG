"""
数据采集管道：data/inbox/ 多格式文件 → 解析 → MySQL（源）→ 同步 Milvus（影子）

按格式分发 parser，解析成统一结构后接入既有管道：
  .pdf  → 说明书（pypdf 抽取文本，按【章节】拆块）          → documents + chunks
  .txt  → 指南/共识（按【章节】拆块）                        → documents + chunks
  .xml  → 个例安全性报告（E2B 风格）                         → documents + chunks
  .csv  → 问询台账导出（归一 → 判重三档 → draft 入库）        → mi_inquiries

处理完的文件归档到 data/inbox/processed/，重跑不重复入库（判重+唯一键兜底）。

用法: python scripts/ingest.py            # 扫描 inbox 全流程
"""
import csv
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 复用同目录脚本的同步函数

import json
import logging
import re

from pypdf import PdfReader
from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from migrate_to_mysql import MILVUS_URI, _MODEL_PATH, sync_pending_chunks
from init_inquiries import sync_pending_inquiries
from src.db import get_conn
from src.dedup_guard import classify
from src.normalization import extract_drugs, normalize_text
from src.thresholds import load_thresholds

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

INBOX = Path(__file__).resolve().parent.parent / "data" / "inbox"
PROCESSED = INBOX / "processed"

# 阈值唯一事实源：config/thresholds.yaml（换模型重标定只改那一处）
_TH = load_thresholds()["inquiry"]
THRESHOLD_DUPLICATE = _TH["duplicate"]
THRESHOLD_NEW = _TH["new"]

_SECTION_RE = re.compile(r"【(.+?)】")


# ============ 通用：文档 + 块入 MySQL（幂等） ============

def insert_document(doc_type: str, title: str, authority: str, version: str,
                    meta: dict, sections: list[tuple[str, str]]) -> bool:
    """sections: [(章节名, 正文)]。块文本带【标题】【章节】前缀，与入向量库一致。返回是否新增"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM documents WHERE doc_type=%s AND title=%s AND version=%s",
                (doc_type, title, version),
            )
            if cur.fetchone():
                logger.info(f"    已存在跳过: [{doc_type}] {title}")
                return False
            cur.execute(
                "INSERT INTO documents (doc_type, title, authority, version, source_meta) "
                "VALUES (%s, %s, %s, %s, %s)",
                (doc_type, title, authority, version, json.dumps(meta, ensure_ascii=False)),
            )
            doc_id = cur.lastrowid
            for seq, (section, body) in enumerate(sections):
                cur.execute(
                    "INSERT INTO document_chunks (document_id, section, seq, text) VALUES (%s, %s, %s, %s)",
                    (doc_id, section, seq, f"【{title}】【{section}】{body}"),
                )
        conn.commit()
        logger.info(f"    入库: [{doc_type}] {title}（{len(sections)} 块）")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def split_sections(text: str) -> list[tuple[str, str]]:
    """按【章节】标记拆分（说明书/指南通用）"""
    parts = _SECTION_RE.split(text)
    # parts: [前缀, 章节1, 正文1, 章节2, 正文2, ...]
    sections = []
    for i in range(1, len(parts) - 1, 2):
        sections.append((parts[i].strip(), re.sub(r"\s+", "", parts[i + 1])))
    return sections


# ============ 各格式 parser ============

def ingest_pdf(path: Path) -> None:
    text = "".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    text = re.sub(r"[ \t]+", "", text).replace("\n", "")
    title = path.stem
    sections = split_sections(text)
    if not sections:
        logger.warning(f"    未解析出章节: {path.name}")
        return
    insert_document("说明书", title, "A", "", {"format": "pdf", "source_file": path.name}, sections)


def ingest_txt(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    title = path.stem
    sections = split_sections(text)
    if not sections:  # 无章节标记则整篇一块
        sections = [("全文", re.sub(r"\s+", "", text))]
    insert_document("指南", title, "B", "", {"format": "txt", "source_file": path.name}, sections)


def ingest_xml(path: Path) -> None:
    root = ET.parse(path).getroot()
    case_id = root.findtext("case_id", path.stem)
    drugs = "、".join(d.findtext("name", "") for d in root.findall("./drugs/drug"))
    reactions = "、".join(r.findtext("term", "") for r in root.findall("./reactions/reaction"))
    narrative = root.findtext("narrative", "")
    sections = [
        ("涉及药品与反应", f"涉及药品：{drugs}。不良反应：{reactions}。"),
        ("病例叙述", re.sub(r"\s+", "", narrative)),
    ]
    insert_document("病例报告", case_id, "C", "", {"format": "xml", "source_file": path.name}, sections)


def ingest_csv(path: Path, model, client: MilvusClient) -> None:
    """问询台账：归一 → 判重三档 → 全部以 draft 落 MySQL（等人工闸②答复审核）"""
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    logger.info(f"    问询 {len(rows)} 条:")
    for r in rows:
        raw = r["问题原文"].strip()
        std = normalize_text(raw)
        drugs = extract_drugs(raw)
        vec = model.encode([std], normalize_embeddings=True).tolist()
        hits = client.search(
            collection_name="mi_inquiries", data=vec, limit=1,
            filter='status == "approved"', output_fields=["id", "question_std"],
        )[0]
        score = hits[0]["distance"] if hits else 0.0
        matched_std = hits[0]["entity"]["question_std"] if hits else ""
        noise = not drugs and score < THRESHOLD_NEW
        # 三档 + 药品集合护栏（一字之差难负样本永不自动判重，见 src/dedup_guard.py）
        c = classify(score, std, matched_std, THRESHOLD_DUPLICATE, THRESHOLD_NEW)

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                if c["verdict"] == "duplicate":
                    cur.execute(
                        "UPDATE mi_inquiries SET occurrence_count=occurrence_count+1 WHERE id=%s",
                        (hits[0]["id"],),
                    )
                    verdict = f"🔁 判重 [{score:.4f}] → 底库#{hits[0]['id']} 计数+1"
                else:
                    cur.execute(
                        "INSERT INTO mi_inquiries (question_raw, question_std, drugs, channel, status) "
                        "VALUES (%s, %s, %s, %s, 'draft') "
                        "ON DUPLICATE KEY UPDATE occurrence_count=occurrence_count+1",
                        (raw, std, ",".join(drugs), r["渠道"]),
                    )
                    tag = "（疑似噪音，人工闸筛）" if noise else ""
                    if c["guarded"]:
                        tag = f"（🛡️ 护栏拦截：药品集合不一致）{tag}"
                    verdict = (
                        f"🆕 新问题 [{score:.4f}] draft 待答复{tag}"
                        if c["verdict"] == "new"
                        else f"🟡 灰色 [{score:.4f}] draft 待人工判重{tag}"
                    )
            conn.commit()
        finally:
            conn.close()
        logger.info(f"      {raw[:24]}… → 归一[{std[:24]}] {verdict}")


# ============ 主流程 ============

def main():
    if not INBOX.exists():
        logger.error(f"inbox 不存在，先跑: python scripts/make_inbox_samples.py")
        return
    files = sorted(p for p in INBOX.iterdir() if p.is_file())
    if not files:
        logger.info("inbox 无待处理文件")
        return

    logger.info(f"扫描到 {len(files)} 个文件")
    model = None
    client = None

    def lazy_model_client():
        nonlocal model, client
        if model is None:
            model = SentenceTransformer(_MODEL_PATH)
            client = MilvusClient(uri=MILVUS_URI)
        return model, client

    done = []
    for path in files:
        logger.info(f"▶ {path.name}")
        try:
            if path.suffix == ".pdf":
                ingest_pdf(path)
            elif path.suffix == ".txt":
                ingest_txt(path)
            elif path.suffix == ".xml":
                ingest_xml(path)
            elif path.suffix == ".csv":
                m, c = lazy_model_client()
                ingest_csv(path, m, c)
            else:
                logger.warning(f"    不支持的格式，跳过: {path.suffix}")
                continue
            done.append(path)
        except Exception as e:
            logger.error(f"    解析失败（不阻塞其他文件）: {e}")

    # 统一同步 MySQL → Milvus
    m, c = lazy_model_client()
    n1 = n2 = 0
    while True:  # 文档块同步（可能有多个批次）
        n = sync_pending_chunks(m, c)
        if n == 0:
            break
        n1 += n
    n2 = sync_pending_inquiries(m, c)
    for coll in ("drug_inserts", "clinical_papers", "case_reports", "mi_inquiries"):
        c.flush(coll)
    logger.info(f"同步 Milvus: 文档块 {n1} 条 + 问询 {n2} 条（已 flush）")

    PROCESSED.mkdir(exist_ok=True)
    for path in done:
        shutil.move(str(path), PROCESSED / path.name)
    logger.info(f"归档 {len(done)} 个文件 → {PROCESSED}")


if __name__ == "__main__":
    main()
