"""
阈值标定工具：标注对 → 同/异问题两簇分数分布 → 多方案阈值建议线（重标定 runbook A.1③）

用法:
    python scripts/calibrate.py                          # 用 thresholds.yaml 里的现役模型
    python scripts/calibrate.py --model-path <新模型路径>  # 换模型标定（影子重建之后、改阈值之前）

输出三条建议线（对应附录B的多方案对比，标定时一次看全）：
  ② 分布目视线：两簇之间的谷点（正簇最小值与负簇最大值的区间中点）
  ③ 统计最优线：扫描全部候选阈值取 F1 最大者（同时报 Youden 指数最优点）
  ⑤ 分位数线：正簇 P5（宁可漏判重复，不可错杀新问题——判重场景的代价不对称）

判断三档（A.2）：
  可分     —— 两簇无重叠，任选阈值都行，取②③⑤共识区间
  灰色     —— 部分重叠，灰色带宽度 = 人工闸人力函数
  不可分   —— 两簇大面积重叠 → 触发退出标准：换候选模型或交算法团队微调，本标定集即微调验收接口

注意：这里算的是标注对的直接余弦相似度，是检索 top1 分数的代理指标；
标定结论落地后仍须跑 scripts/regression.py 用黄金集做端到端复核。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse

import numpy as np
from sentence_transformers import SentenceTransformer

from src.thresholds import load_thresholds, models_dir

_PAIRS = Path(__file__).resolve().parent.parent / "config" / "calibration_pairs.json"
_MODELS_DIR = models_dir()


def best_f1_line(pos: np.ndarray, neg: np.ndarray):
    """方案③：扫描候选阈值，报 F1 最优点和 Youden 最优点（阈值含义：≥线判重复）"""
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
    """方案②的目视材料：两簇分布直方图（终端直接看，不用开 matplotlib）"""
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


def main():
    parser = argparse.ArgumentParser(description="问询判重阈值标定")
    parser.add_argument("--model-path", help="模型路径（默认读 thresholds.yaml 的现役模型）")
    args = parser.parse_args()

    model_path = args.model_path or str(_MODELS_DIR / load_thresholds()["model"])
    pairs = json.loads(_PAIRS.read_text(encoding="utf-8"))["pairs"]
    model = SentenceTransformer(model_path)
    logger_name = Path(model_path).name

    texts = sorted({t for p in pairs for t in (p["q1"], p["q2"])})
    vecs = model.encode(texts, normalize_embeddings=True)
    emb = dict(zip(texts, vecs))

    pos, neg = [], []
    for p in pairs:
        sim = float(np.dot(emb[p["q1"]], emb[p["q2"]]))  # 已归一化，点积即余弦
        (pos if p["label"] == 1 else neg).append(sim)
    pos, neg = np.array(pos), np.array(neg)

    print("═" * 64)
    print(f"问询判重标定 | 模型: {logger_name} | 标注对: {len(pairs)}（正 {len(pos)} / 负 {len(neg)}）")
    print("═" * 64)
    print("两簇分布:")
    print(f"  同问题簇: min={pos.min():.4f} 中位={np.median(pos):.4f} max={pos.max():.4f}")
    print(f"  异问题簇: min={neg.min():.4f} 中位={np.median(neg):.4f} max={neg.max():.4f}")
    print(ascii_hist(pos, neg))

    overlap_lo, overlap_hi = neg.max(), pos.min()
    gap = overlap_hi - overlap_lo
    print("─" * 64)
    if gap > 0:
        print(f"分布判断: 可分（间隔 {gap:.4f}）")
    else:
        n_overlap = int((neg >= overlap_hi).sum() + (pos <= overlap_lo).sum())
        ratio = n_overlap / len(pairs)
        level = "不可分" if ratio > 0.3 else "灰色"
        print(f"分布判断: {level}（重叠区间 [{overlap_hi:.4f}, {overlap_lo:.4f}]，"
              f"涉及 {n_overlap}/{len(pairs)} 对）")
        if level == "不可分":
            print("⚠️ 触发退出标准：两簇大面积重叠，阈值无解——")
            print("   换候选模型，或将本标定集移交算法团队做微调（标定集即微调验收接口）")
            print("   注意：直接对余弦是 top1 检索分的严格代理（见文件头注释），")
            print("   落地前以 regression.py 的端到端结论为准")

    (f1, f1_t), (youden, youden_t) = best_f1_line(pos, neg)
    p5 = float(np.percentile(pos, 5))
    mid = (overlap_lo + overlap_hi) / 2
    print("─" * 64)
    print("阈值建议线（≥线判重复；附录B多方案对比）:")
    print(f"  ② 分布目视线（谷点中点）: {mid:.4f}")
    print(f"  ③ 统计最优线（F1={f1:.3f} @ {f1_t:.4f}；Youden={youden:.3f} @ {youden_t:.4f}）")
    print(f"  ⑤ 分位数线（正簇P5，代价敏感——宁漏判重复勿错杀新问题）: {p5:.4f}")
    cur = load_thresholds()["inquiry"]
    print("─" * 64)
    print(f"现役阈值（thresholds.yaml）: duplicate={cur['duplicate']} new={cur['new']}")
    print("下一步：选定阈值写入 config/thresholds.yaml → scripts/regression.py 端到端复核 → --record")


if __name__ == "__main__":
    main()
