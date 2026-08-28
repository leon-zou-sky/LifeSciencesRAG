"""
阈值标定报告生成器（产品化交付形态）

把 calibrate.py 的标定结论写成可交付的 Markdown/JSON 报告：
  - 客户可读的阈值建议与定线理由
  - 审计可追溯的标定证据
  - 可直接转给 QA/医学团队的"能不能上线"判断

用法：
    python scripts/calibration_report.py                          # 用现役模型 + 默认标定集
    python scripts/calibration_report.py --model-path <模型路径>   # 换模型标定
    python scripts/calibration_report.py --pairs data/customer_pairs.json

输出：
    reports/calibration_report_YYYYMMDD_HHMMSS.md（人读）
    reports/calibration_report_YYYYMMDD_HHMMSS.json（机器读，留痕）
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse

import numpy as np
from sentence_transformers import SentenceTransformer

from src.thresholds import load_thresholds, models_dir


_PAIRS = Path(__file__).resolve().parent.parent / "config" / "calibration_pairs.json"
_MODELS_DIR = models_dir()
_REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def best_f1_line(pos: np.ndarray, neg: np.ndarray):
    cands = np.linspace(min(neg.min(), pos.min()), max(neg.max(), pos.max()), 200)
    best_f1, best_youden = (0.0, None), (-1.0, None)
    for t in cands:
        tp, fn = (pos >= t).sum(), (pos < t).sum()
        fp, tn = (neg >= t).sum(), (neg < t).sum()
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        youden = rec + tn / (tn + fp) - 1 if tn + fp else -1.0
        if f1 > best_f1[0]:
            best_f1 = (f1, t)
        if youden > best_youden[0]:
            best_youden = (youden, t)
    return best_f1, best_youden


def ascii_hist(pos: np.ndarray, neg: np.ndarray, bins: int = 20) -> str:
    lo, hi = min(pos.min(), neg.min()), max(pos.max(), neg.max())
    span = hi - lo or 1e-9
    lines = []
    for label, arr in (("同问题(+)", pos), ("异问题(-)", neg)):
        counts = [0] * bins
        for v in arr:
            counts[min(int((v - lo) / span * bins), bins - 1)] += 1
        bar = "".join("▁▂▃▄▅▆▇█"[min(c, 7)] if c else "·" for c in counts)
        lines.append(f"  {label} |{bar}|")
    lines.append(f"           {lo:.3f}{' ' * (bins - 8)}{hi:.3f}")
    return "\n".join(lines)


def build_report(model_path: str, pairs_path: Path) -> dict:
    model = SentenceTransformer(model_path)
    model_name = Path(model_path).name
    pairs = json.loads(pairs_path.read_text(encoding="utf-8"))["pairs"]

    texts = sorted({t for p in pairs for t in (p["q1"], p["q2"])})
    vecs = model.encode(texts, normalize_embeddings=True)
    emb = dict(zip(texts, vecs))

    pos, neg = [], []
    for p in pairs:
        sim = float(np.dot(emb[p["q1"]], emb[p["q2"]]))
        (pos if p["label"] == 1 else neg).append(sim)
    pos, neg = np.array(pos), np.array(neg)

    overlap_lo, overlap_hi = neg.max(), pos.min()
    gap = overlap_hi - overlap_lo
    n_overlap = int((neg >= overlap_hi).sum() + (pos <= overlap_lo).sum()) if gap <= 0 else 0
    ratio = n_overlap / len(pairs) if pairs else 0.0
    if gap > 0:
        separability = "可分"
    elif ratio > 0.3:
        separability = "不可分"
    else:
        separability = "灰色"

    (f1, f1_t), (youden, youden_t) = best_f1_line(pos, neg)
    p5 = float(np.percentile(pos, 5)) if len(pos) else 0.0
    mid = (overlap_lo + overlap_hi) / 2

    cur = load_thresholds()["inquiry"]

    # 推荐策略：代理指标（标注对直接余弦）只负责定初值，终值必须走 regression 终审
    # ——附录 C 已知结论：同可分性下代理与端到端 top1 两套数字可差 0.05~0.10，
    # 难负样本高分是纯向量判重的物理上限，代理层"不可分"≠生产不可用。
    if separability == "可分":
        recommended_duplicate = round(float(np.median([mid, f1_t, p5])), 4)
        recommended_new = round(float(np.percentile(neg, 95)), 4)
        recommendation = ("两簇可分，推荐取三条建议线的共识区间作初值；new 线取负簇 P95 留灰色缓冲带。"
                          "初值落地前仍须 regression.py 终审。")
    elif separability == "不可分" and f1 < 0.7:
        recommended_duplicate = None
        recommended_new = None
        recommendation = (f"代理指标不可分且 F1 最优仅 {f1:.3f}（<0.70 退出线，对标 MiniLM 负对照 0.667）"
                          "——本模型在本标定集上确实无解，建议换候选模型或将标定集移交算法团队微调。")
    else:
        recommended_duplicate = round(f1_t, 4)
        # 重叠大时负簇 P90 可能高过 duplicate 初值（灰档倒挂），此时退回负簇中位数，
        # 保证 new < duplicate 的档位次序；灰档宽度本质是人工闸人力函数，终审再调
        new_cand = float(np.percentile(neg, 90))
        if new_cand >= recommended_duplicate:
            new_cand = float(np.median(neg))
        recommended_new = round(new_cand, 4)
        recommendation = (f"代理指标层面{separability}（F1 最优 {f1:.3f}），但这不是终审结论："
                          "直接余弦是 top1 检索分的严格代理，端到端分布通常更开。"
                          "建议以 F1 最优点为 duplicate 初值、负簇分位点为 new 初值，"
                          "然后必须跑 scripts/regression.py 用黄金集终审定线（附录 C：对余弦定初值、回归定终值）。")

    return {
        "generated_at": datetime.now().isoformat(),
        "model": {"path": model_path, "name": model_name},
        "pairs": {"total": len(pairs), "positive": len(pos), "negative": len(neg), "source": str(pairs_path)},
        "distribution": {
            "positive": {"min": float(pos.min()), "median": float(np.median(pos)), "max": float(pos.max())},
            "negative": {"min": float(neg.min()), "median": float(np.median(neg)), "max": float(neg.max())},
        },
        "separability": {
            "level": separability,
            "gap": float(gap),
            "overlap_count": n_overlap,
            "overlap_ratio": round(ratio, 4),
        },
        "proposed_lines": {
            "visual_mid": round(float(mid), 4),
            "f1_best": {"threshold": round(f1_t, 4), "f1": round(f1, 4)},
            "youden_best": {"threshold": round(youden_t, 4), "youden": round(youden, 4)},
            "positive_p5": round(p5, 4),
        },
        "recommendation": {
            "duplicate": recommended_duplicate,
            "new": recommended_new,
            "reason": recommendation,
        },
        "current": {"duplicate": cur["duplicate"], "new": cur["new"]},
        "histogram": ascii_hist(pos, neg),
    }


def to_markdown(r: dict) -> str:
    lines = [
        "# 问询判重阈值标定报告",
        "",
        f"- 生成时间：{r['generated_at']}",
        f"- 标定模型：{r['model']['name']}（`{r['model']['path']}`）",
        f"- 标定对来源：{r['pairs']['source']}",
        f"- 样本量：共 {r['pairs']['total']} 对（同问题 {r['pairs']['positive']} / 异问题 {r['pairs']['negative']}）",
        "",
        "## 1. 两簇分布",
        "",
        "| 簇 | min | median | max |",
        "|---|---|---|---|",
        f"| 同问题(+) | {r['distribution']['positive']['min']:.4f} | {r['distribution']['positive']['median']:.4f} | {r['distribution']['positive']['max']:.4f} |",
        f"| 异问题(-) | {r['distribution']['negative']['min']:.4f} | {r['distribution']['negative']['median']:.4f} | {r['distribution']['negative']['max']:.4f} |",
        "",
        "```",
        r["histogram"],
        "```",
        "",
        "## 2. 可分性判断",
        "",
        f"- 判断：{r['separability']['level']}",
    ]
    if r["separability"]["level"] != "可分":
        pos_min = r["distribution"]["positive"]["min"]
        neg_max = r["distribution"]["negative"]["max"]
        lines.append(f"- 重叠区间：[{pos_min:.4f}, {neg_max:.4f}]（gap={r['separability']['gap']:.4f}）")
        lines.append(f"- 重叠样本：{r['separability']['overlap_count']} / {r['pairs']['total']}（{r['separability']['overlap_ratio']*100:.1f}%）")
        lines.append("- 注意：本判断基于标注对直接余弦（top1 检索分的代理指标），代理层重叠≠生产不可用，终值以 regression.py 终审为准")
    lines.extend([
        "",
        "## 3. 阈值建议线",
        "",
        "| 方案 | 阈值 | 指标 |",
        "|---|---|---|",
        f"| 分布目视线（谷点中点） | {r['proposed_lines']['visual_mid']} | — |",
        f"| F1 最优线 | {r['proposed_lines']['f1_best']['threshold']} | F1={r['proposed_lines']['f1_best']['f1']:.3f} |",
        f"| Youden 最优线 | {r['proposed_lines']['youden_best']['threshold']} | Youden={r['proposed_lines']['youden_best']['youden']:.3f} |",
        f"| 正簇 P5（代价敏感） | {r['proposed_lines']['positive_p5']} | — |",
        "",
        "## 4. 推荐阈值与定线理由",
        "",
        f"- duplicate（≥此值判重复）：{r['recommendation']['duplicate'] if r['recommendation']['duplicate'] is not None else '**无法确定，建议换模型**'}",
        f"- new（<此值判新）：{r['recommendation']['new'] if r['recommendation']['new'] is not None else '**无法确定，建议换模型**'}",
        f"- 理由：{r['recommendation']['reason']}",
        "",
        "## 5. 与现役阈值对比",
        "",
        f"- 现役 duplicate：{r['current']['duplicate']}；推荐：{r['recommendation']['duplicate'] if r['recommendation']['duplicate'] is not None else 'N/A'}",
        f"- 现役 new：{r['current']['new']}；推荐：{r['recommendation']['new'] if r['recommendation']['new'] is not None else 'N/A'}",
        "",
        "## 6. 后续动作",
        "",
        "1. 若推荐阈值与现役不同，使用 `scripts/set_threshold.py` 写入（必须带 `--by`/`--reason` 留痕）。",
        "2. 写入后跑 `scripts/regression.py` 做端到端黄金集复核。",
        "3. 若推荐值为空（代理不可分且 F1<0.70），请勿直接定线——换候选模型或将标定集移交算法团队微调。",
        "",
        "---",
        "本报告由 scripts/calibration_report.py 自动生成，仅含标定阶段证据；是否上线以 regression.py 终审结论为准。",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="阈值标定报告生成器")
    parser.add_argument("--model-path", help="模型路径（默认读 thresholds.yaml 现役模型）")
    parser.add_argument("--pairs", default=str(_PAIRS), help="标定对 JSON 文件路径")
    args = parser.parse_args()

    model_path = args.model_path or str(_MODELS_DIR / load_thresholds()["model"])
    pairs_path = Path(args.pairs)

    report = build_report(model_path, pairs_path)

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = _REPORTS_DIR / f"calibration_report_{stamp}.json"
    md_path = _REPORTS_DIR / f"calibration_report_{stamp}.md"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")

    print(f"报告已生成：\n  {md_path}\n  {json_path}")
    print(f"\n可分性：{report['separability']['level']}")
    print(f"推荐阈值：duplicate={report['recommendation']['duplicate']}, new={report['recommendation']['new']}")


if __name__ == "__main__":
    main()
