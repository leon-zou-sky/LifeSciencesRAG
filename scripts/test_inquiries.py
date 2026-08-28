"""
医学问询底库验证脚本 —— 对应设计文档 4.5 四项验证标准

  ① 归一正确性：商品名/错别字/口语化是否被正确归一
  ② 判重三档：新问询 vs 底库，相似度落在 ≥0.93 判重 / 0.85~0.93 人工 / <0.85 新问题
  ③ 权限过滤：检索默认只返回 status=approved
  ④ 幂等：init 重跑记录数不翻倍（由 init 脚本日志体现，这里复核 MySQL 计数）

用法: python scripts/test_inquiries.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging

from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from src.db import get_conn
from src.dedup_guard import classify
from src.messy_inquiries import BASE_INQUIRIES, NEW_INQUIRIES
from src.normalization import extract_drugs, normalize_text
from src.thresholds import load_thresholds, load_model_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MILVUS_URI = "http://localhost:19531"
_MODEL_PATH = load_model_path()  # 模型路径与阈值同源（thresholds.yaml model 字段）

# 阈值唯一事实源：config/thresholds.yaml（换模型重标定只改那一处）
_TH = load_thresholds()["inquiry"]
THRESHOLD_DUPLICATE = _TH["duplicate"]
THRESHOLD_NEW = _TH["new"]


def test_normalization():
    print("=" * 60)
    print("① 归一正确性")
    print("=" * 60)
    for inq in NEW_INQUIRIES:
        raw = inq["question_raw"]
        std = normalize_text(raw)
        drugs = extract_drugs(raw)
        changed = "→" if std != raw else "="
        print(f"  [{inq['channel']}] {raw}")
        print(f"    {changed} 归一: {std}")
        print(f"       药品: {drugs or '（未识别）'}   备注: {inq['_note']}")
        print()


def _search(client, model, text, limit=3, status_filter=None):
    vec = model.encode([text], normalize_embeddings=True).tolist()
    flt = f'status == "{status_filter}"' if status_filter else None
    res = client.search(
        collection_name="mi_inquiries", data=vec, limit=limit,
        filter=flt, output_fields=["question_std", "status", "drugs"],
    )
    return res[0]


def test_dedup(client, model):
    print("=" * 60)
    print("② 判重三档（新问询 vs 底库 approved top1）")
    print("=" * 60)
    for inq in NEW_INQUIRIES:
        raw = inq["question_raw"]
        std = normalize_text(raw)
        hits = _search(client, model, std, limit=1, status_filter="approved")
        if not hits:
            print(f"  {raw} → 无命中（库为空？）")
            continue
        top = hits[0]
        score = top["distance"]
        # 三档 + 药品集合护栏（与 ingest/answer/regression 同一 classify，唯一判定入口）
        c = classify(score, std, top["entity"]["question_std"],
                     THRESHOLD_DUPLICATE, THRESHOLD_NEW)
        verdict = {
            "duplicate": "🔁 判重（自动合并，计数+1）",
            "gray": "🟡 灰色区间（人工确认）",
            "new": "🆕 新问题（直接入库）",
        }[c["verdict"]]
        if c["guarded"]:
            verdict += " 🛡️ 护栏拦截（向量过线但药品集合不一致）"
        print(f"  {raw}")
        print(f"    top1 [{score:.4f}] {top['entity']['question_std'][:40]}")
        print(f"    {verdict}   备注: {inq['_note']}")
        print()


def test_status_filter(client, model):
    print("=" * 60)
    print("③ 权限过滤（status=approved）")
    print("=" * 60)
    # 插一条 draft 问询（与底库#1几乎同义），验证默认检索不会把它带出来
    draft_q = "阿托伐他汀跟红霉素一起吃行不行"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM mi_inquiries WHERE question_raw=%s", (draft_q,))
            row = cur.fetchone()
            if row:
                draft_id = row["id"]
            else:
                cur.execute(
                    "INSERT INTO mi_inquiries (question_raw, question_std, drugs, channel, status) "
                    "VALUES (%s, %s, %s, %s, 'draft')",
                    (draft_q, normalize_text(draft_q),
                     ",".join(extract_drugs(draft_q)), "热线"),
                )
                draft_id = cur.lastrowid
                conn.commit()
        # 手动同步这一条 draft 进 Milvus（answer 为 NULL，用问题本身编码）
        vec = model.encode([normalize_text(draft_q)], normalize_embeddings=True).tolist()
        client.upsert(collection_name="mi_inquiries", data=[{
            "id": draft_id, "question_std": normalize_text(draft_q), "answer": "",
            "drugs": ",".join(extract_drugs(draft_q)), "status": "draft",
            "authority": "C", "embedding": vec[0],
        }])
        client.flush("mi_inquiries")  # upsert 默认有界一致性，flush 保证立刻可搜
        with conn.cursor() as cur:
            from datetime import datetime
            cur.execute("UPDATE mi_inquiries SET embedding_updated_at=%s WHERE id=%s", (datetime.now(), draft_id))
            conn.commit()
    finally:
        conn.close()

    probe = "阿托伐他汀可以和红霉素联用吗"
    hits_all = _search(client, model, probe, limit=3)
    hits_approved = _search(client, model, probe, limit=3, status_filter="approved")
    print(f"  探针: {probe}")
    print(f"  未过滤 top3: {[(round(h['distance'],4), h['entity']['status']) for h in hits_all]}")
    print(f"  approved top3: {[(round(h['distance'],4), h['entity']['status']) for h in hits_approved]}")
    leaked = [h for h in hits_approved if h["entity"]["status"] != "approved"]
    print(f"  ✅ 过滤生效，draft 未泄漏" if not leaked else "  ❌ draft 泄漏!")
    print()


def test_idempotency():
    print("=" * 60)
    print("④ 幂等复核（MySQL 计数）")
    print("=" * 60)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM mi_inquiries")
        total = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM mi_inquiries WHERE status='approved'")
        approved = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM mi_inquiries WHERE embedding_updated_at IS NULL")
        pending = cur.fetchone()["c"]
    print(f"  mi_inquiries 总数: {total}（底库 {len(BASE_INQUIRIES)} + draft 验证 1）")
    print(f"  approved: {approved}   待同步: {pending}")
    print(f"  重跑 init 脚本，总数应保持 {total} 不变（按 question_std 判重）")


def main():
    test_normalization()

    model = SentenceTransformer(_MODEL_PATH)
    client = MilvusClient(uri=MILVUS_URI)

    test_dedup(client, model)
    test_status_filter(client, model)
    test_idempotency()


if __name__ == "__main__":
    main()
