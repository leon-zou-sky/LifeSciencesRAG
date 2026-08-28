"""
③ 病例并案：多渠道重复上报判重（GVP Module VI 场景）

和 mi_inquiries 判重的本质区别：判重对象不是"文本像不像"，而是"是不是同一个病例"——
  四要素：患者（性别/年龄±5）/ 怀疑药品（集合重合度）/ 不良事件 / 发病时间窗

两段式架构（检索用影子，规则用源）：
  ① 向量粗筛（Milvus 影子库）：事件描述找语义最相似的现存病例
  ② 结构化精排（MySQL source_meta）：对候选病例做四要素规则打分
     —— 影子里只有文本和向量，结构化字段在源层，所以规则打分必须回 MySQL

hybrid = 0.5 × 向量分 + 0.5 × 规则分（语义和法规要素各占一半，不给单一信号独断权）
  ≥ duplicate  duplicate → status='已合并'：进 MySQL 留痕审计，不进影子（影子只保留主病例）
  gray~duplicate gray    → status='草稿'：人工闸，等 PV 专员确认（不进影子）
  < gray       new       → status='现行'：独立病例，正常同步进影子
  （两条线的值唯一事实源在 config/thresholds.yaml case_report 段，2026-08-19 经 14 对病例标定重锚）

用法:
    python scripts/init_cases.py    # 幂等：已存在的 case_id 直接跳过
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import logging
from datetime import datetime

from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from src.case_reports_data import REPORTS
from src.chunking import chunk_document
from src.db import get_conn
from src.thresholds import load_thresholds, load_model_path
from scripts.migrate_to_mysql import sync_pending_chunks

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MILVUS_URI = "http://localhost:19531"
_MODEL_PATH = load_model_path()  # 模型路径与阈值同源（thresholds.yaml model 字段）

W_VEC, W_RULE = 0.5, 0.5
# 阈值唯一事实源：config/thresholds.yaml（case_report 段，混合分判重）
_TH = load_thresholds()["case_report"]
TH_DUP, TH_GRAY = _TH["duplicate"], _TH["gray"]


# ============ ② 结构化精排：四要素规则打分 ============
#
# 三道硬否决（2026-08-19 标定暴露后补，证据 config/case_calibration_pairs.json）：
#   性别不符 / 事件不符 / 药品零重合 → 规则分直接 0（不同患者、不同事件、
#   完全不同药的"病例"不可能是同一病例，不应靠向量分抬进灰色带）。
#   标定前裸公式在 CP-05/07/10 上漏判：性别不符仅清零患者项、事件压根不参与打分，
#   药品+时间满分照样把总分抬到 0.8 落人工闸——规则缺项挪阈值救不了，只能补规则。

def _event_match(e1: str, e2: str) -> bool:
    """事件比对：互相包含即算同一事件（"横纹肌溶解" vs "横纹肌溶解症"）。
    生产上应由 MedDRA 编码归一后再精确比对——PoC 用包含匹配兜底。"""
    e1, e2 = e1.strip(), e2.strip()
    return e1 in e2 or e2 in e1

def _time_score(d1: str, d2: str) -> tuple[float, str]:
    days = abs((datetime.fromisoformat(d1) - datetime.fromisoformat(d2)).days)
    if days <= 7:
        return 1.0, f"发病差{days}天(≤7天)"
    if days <= 30:
        return 0.6, f"发病差{days}天(≤30天)"
    if days <= 60:
        return 0.3, f"发病差{days}天(≤60天)"
    return 0.0, f"发病差{days}天(超窗)"


def _patient_score(p1: dict, p2: dict) -> tuple[float, str]:
    if p1["sex"] != p2["sex"]:
        return 0.0, "性别不符"
    d = abs(p1["age"] - p2["age"])
    if d <= 5:
        return 1.0, f"{p1['sex']}/年龄差{d}岁"
    return 0.5, f"{p1['sex']}/年龄差{d}岁(>5)"


def rule_score(report: dict, cand_meta: dict) -> tuple[float, str]:
    """四要素规则分 ∈ [0,1]，返回 (分数, 判定依据)。命中硬否决直接 0 分。"""
    # 硬否决①：性别不符 → 必然不同患者
    if report["patient"]["sex"] != cand_meta["patient"]["sex"]:
        return 0.0, "性别不符→否决（不同患者必然不同病例）"
    # 硬否决②：不良事件不符 → 同患者同药也可发生两种独立不良反应（CP-07）
    if not _event_match(report["event"], cand_meta["event"]):
        return 0.0, f"事件不符（{report['event']}≠{cand_meta['event']}）→否决"
    # 硬否决③：怀疑药品零重合（问询侧 dedup_guard 同思想，case 侧落地）
    d1, d2 = set(report["drugs"]), set(cand_meta["drugs"])
    if not (d1 & d2):
        return 0.0, f"药品零重合（{'/'.join(sorted(d1))} vs {'/'.join(sorted(d2))}）→否决"
    drug = len(d1 & d2) / len(d1 | d2)
    drug_desc = f"药品重合{'/'.join(sorted(d1 & d2))}({drug:.2f})"
    t, t_desc = _time_score(report["onset_date"], cand_meta["onset_date"])
    p, p_desc = _patient_score(report["patient"], cand_meta["patient"])
    score = 0.5 * drug + 0.3 * t + 0.2 * p
    return score, f"{drug_desc} + {t_desc} + {p_desc}"


# ============ MySQL 读写 ============

def _exists(cur, case_id: str) -> bool:
    cur.execute(
        "SELECT id FROM documents WHERE doc_type='病例报告' AND title=%s", (case_id,)
    )
    return cur.fetchone() is not None


def _load_meta(cur, case_id: str) -> dict | None:
    cur.execute(
        "SELECT source_meta FROM documents WHERE doc_type='病例报告' AND title=%s", (case_id,)
    )
    row = cur.fetchone()
    if not row:
        return None
    meta = row["source_meta"]
    return json.loads(meta) if isinstance(meta, str) else meta


def _insert_report(cur, report: dict, status: str, master_case_id: str | None) -> None:
    meta = {
        "case_id": report["case_id"],
        "channel": report["channel"],
        "channels": [{"case_id": report["case_id"], "channel": report["channel"]}],
        "patient": report["patient"],
        "drugs": report["drugs"],
        "event": report["event"],
        "onset_date": report["onset_date"],
    }
    if master_case_id:
        meta["master_case_id"] = master_case_id
    cur.execute(
        "INSERT INTO documents (doc_type, title, authority, version, status, source_meta) "
        "VALUES ('病例报告', %s, 'C', '', %s, %s)",
        (report["case_id"], status, json.dumps(meta, ensure_ascii=False)),
    )
    doc_id = cur.lastrowid
    for seq, chunk in enumerate(chunk_document(report)):
        cur.execute(
            "INSERT INTO document_chunks (document_id, section, seq, text) VALUES (%s, %s, %s, %s)",
            (doc_id, chunk["section"], seq, chunk["text"]),
        )


def _merge_into_master(cur, report: dict, master_case_id: str) -> None:
    """把重复报告的渠道信息并到主病例的 channels 里（源层留痕，不动影子）"""
    meta = _load_meta(cur, master_case_id)
    meta.setdefault("channels", []).append(
        {"case_id": report["case_id"], "channel": report["channel"]}
    )
    cur.execute(
        "UPDATE documents SET source_meta=%s WHERE doc_type='病例报告' AND title=%s",
        (json.dumps(meta, ensure_ascii=False), master_case_id),
    )


# ============ 主流程：按收到顺序流式处理 ============

def main():
    model = SentenceTransformer(_MODEL_PATH)
    client = MilvusClient(uri=MILVUS_URI)
    conn = get_conn()

    try:
        for report in REPORTS:
            cid = report["case_id"]
            print("═" * 60)
            print(f"收到上报 {cid}（{report['channel']}）"
                  f" 患者:{report['patient']['sex']}/{report['patient']['age']}岁"
                  f" 药品:{'+'.join(report['drugs'])} 事件:{report['event']}")
            with conn.cursor() as cur:
                if _exists(cur, cid):
                    print("  已存在，跳过（幂等）")
                    continue

                # ① 向量粗筛：影子库找语义最相似的现存病例
                query_text = " ".join(s["text"] for s in report["sections"])
                vec = model.encode([query_text], normalize_embeddings=True).tolist()
                hits = client.search(
                    collection_name="case_reports", data=vec, limit=5,
                    output_fields=["source_title", "section"],
                )[0] if client.has_collection("case_reports") else []

                # ② 结构化精排：候选回 MySQL 取四要素做规则打分
                best = None  # (hybrid, vec_score, rule, case_id)
                seen = set()
                for h in hits:
                    cand = h["entity"]["source_title"]
                    if cand in seen:
                        continue
                    seen.add(cand)
                    cand_meta = _load_meta(cur, cand)
                    if not cand_meta or "drugs" not in cand_meta:
                        print(f"  候选 {cand}（{h['distance']:.4f}）：无结构化四要素，跳过规则打分")
                        continue
                    rule, basis = rule_score(report, cand_meta)
                    hybrid = W_VEC * h["distance"] + W_RULE * rule
                    print(f"  候选 {cand}：向量={h['distance']:.4f} 规则={rule:.2f}（{basis}）→ hybrid={hybrid:.4f}")
                    if best is None or hybrid > best[0]:
                        best = (hybrid, h["distance"], rule, cand)

                # ③ 判定 + 落库
                if best is None or best[0] < TH_GRAY:
                    status, master = "现行", None
                    verdict = "new → 独立病例入库"
                elif best[0] >= TH_DUP:
                    status, master = "已合并", best[3]
                    verdict = f"duplicate → 并入主病例 {master}"
                else:
                    status, master = "草稿", best[3]
                    verdict = f"gray → 人工闸（疑似 {master}，待 PV 专员确认）"
                _insert_report(cur, report, status, master)
                if status == "已合并":
                    _merge_into_master(cur, report, master)
                conn.commit()
                print(f"  判定: {verdict}")

            # 只有"现行"会被 sync 进影子（复用同步标记机制）
            sync_pending_chunks(model, client)
            # 军规：upsert 后紧接着要读（下一条上报的粗筛）必须 flush，
            # 否则 Bounded 一致性窗口内新行不可见——主病例搜不到，判重必误判
            client.flush("case_reports")

        # 汇总
        client.flush("case_reports")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, status FROM documents WHERE doc_type='病例报告' "
                "AND title LIKE 'CASE-2024-1%' ORDER BY title"
            )
            rows = cur.fetchall()
        print("═" * 60)
        print("源层（MySQL，全量留痕）:")
        for r in rows:
            print(f"  {r['title']}: {r['status']}")
        print(f"影子层（Milvus case_reports）: {client.get_collection_stats('case_reports')['row_count']} 行"
              "（只含现行：主病例 + 阴性对照，已合并/草稿不进）")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
