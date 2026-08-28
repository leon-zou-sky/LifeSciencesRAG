"""
病例并案标定测量（G-01/G-02 前置）：只打分不落库

对 config/case_calibration_pairs.json 里的每个病例对：
  ① 向量粗筛：候选报告文本编码后搜 Milvus case_reports 影子库（复刻 init_cases 主流程）
  ② 规则精排：回 MySQL 取主病例四要素，复用 init_cases.rule_score 原样打分
  ③ hybrid = 0.5×向量 + 0.5×规则，对照当前阈值给系统判定，与人工标注 expect 比对

用途：
  - 看三类标注（merge/gray/new）的 hybrid 分布有没有空档 → 定线依据
  - 暴露结构性缺口：expect 与系统判定不符的对，逐条看是阈值问题还是规则缺项
    （事件不在规则分 / 患者不符无硬否决 / case 侧无药品集合护栏）

军规：全程只读，不写 MySQL 不写 Milvus，不调 LLM。
用法:
    python scripts/measure_case_gate.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from src.db import get_conn
from src.thresholds import load_thresholds, load_model_path
from scripts.init_cases import W_RULE, W_VEC, _load_meta, rule_score

MILVUS_URI = "http://localhost:19531"
PAIRS_PATH = Path(__file__).resolve().parent.parent / "config" / "case_calibration_pairs.json"

_TH = load_thresholds()["case_report"]
TH_DUP, TH_GRAY = _TH["duplicate"], _TH["gray"]

VERDICT_LABEL = {"merge": "并案", "gray": "人工闸", "new": "独立"}


def verdict(hybrid: float) -> str:
    if hybrid >= TH_DUP:
        return "merge"
    if hybrid >= TH_GRAY:
        return "gray"
    return "new"


def score_pairs() -> list[dict]:
    """对全部标定对打分，返回带 vec/rule/hybrid/sys 字段的结果列表（只读）。"""
    pairs = json.loads(PAIRS_PATH.read_text(encoding="utf-8"))["pairs"]
    model = SentenceTransformer(load_model_path())
    client = MilvusClient(uri=MILVUS_URI)
    if not client.has_collection("case_reports"):
        raise RuntimeError("case_reports 影子库不存在，请先跑 scripts/init_cases.py")
    conn = get_conn()
    results = []
    try:
        for p in pairs:
            cand = p["candidate"]
            # ① 向量粗筛（复刻 init_cases：全文拼接 + 裸编码）
            vec = model.encode([cand["text"]], normalize_embeddings=True).tolist()
            hits = client.search(
                collection_name="case_reports", data=vec, limit=5,
                output_fields=["source_title"],
            )[0]
            target_hits = [h for h in hits if h["entity"]["source_title"] == p["vs_case"]]
            if not target_hits:
                results.append({**p, "vec": None, "rule": None, "hybrid": None, "sys": "召回失败"})
                continue
            vec_score = target_hits[0]["distance"]
            # ② 规则精排（回源取四要素）
            with conn.cursor() as cur:
                cand_meta = _load_meta(cur, p["vs_case"])
            rule, basis = rule_score(cand, cand_meta)
            hybrid = W_VEC * vec_score + W_RULE * rule
            results.append({**p, "vec": vec_score, "rule": rule, "basis": basis,
                            "hybrid": hybrid, "sys": verdict(hybrid)})
    finally:
        conn.close()
    return results


def main():
    results = score_pairs()

    # ── 逐对明细 ──
    print(f"当前阈值：duplicate={TH_DUP} / gray={TH_GRAY}（2026-08-19 经 14 对病例标定重锚）")
    print("═" * 78)
    mismatch = 0
    for r in results:
        if r["hybrid"] is None:
            print(f'{r["id"]:6s} expect={VERDICT_LABEL[r["expect"]]:4s} │ 系统=召回失败'
                  f'（粗筛 top5 未命中 {r["vs_case"]}） ⚠️')
            mismatch += 1
            continue
        ok = "✓" if r["sys"] == r["expect"] else "✗"
        if r["sys"] != r["expect"]:
            mismatch += 1
        print(f'{r["id"]:6s} expect={VERDICT_LABEL[r["expect"]]:4s} │ 系统={VERDICT_LABEL[r["sys"]]:4s} {ok}'
              f' │ 向量={r["vec"]:.4f} 规则={r["rule"]:.2f} → hybrid={r["hybrid"]:.4f}'
              f' │ {r["category"]}')
        if r["sys"] != r["expect"]:
            print(f'         规则依据: {r["basis"]}')
            print(f'         标注理由: {r["rationale"][:60]}...')

    # ── 分布汇总：三类各自的 hybrid 区间 ──
    print("═" * 78)
    print("按人工标注分组的 hybrid 分布（定线看空档）:")
    for expect in ("merge", "gray", "new"):
        scores = sorted(r["hybrid"] for r in results
                        if r["expect"] == expect and r["hybrid"] is not None)
        if not scores:
            continue
        lo, hi = scores[0], scores[-1]
        bar = " ".join(f"{s:.3f}" for s in scores)
        print(f"  {VERDICT_LABEL[expect]:4s} n={len(scores):2d}  [{lo:.4f} ~ {hi:.4f}]  {bar}")
    print(f"  当前线: gray={TH_GRAY}  duplicate={TH_DUP}")
    print(f"  expect 与系统判定不符: {mismatch}/{len(results)} 对")
    print("提示：不符的对先看规则依据——若是「事件没参与打分」「患者不符没否决」")
    print("      这类结构性问题，改规则比挪阈值更对症（阈值挪不平规则缺项）。")


if __name__ == "__main__":
    main()
