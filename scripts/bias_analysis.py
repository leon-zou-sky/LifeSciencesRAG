"""
偏差测试（GAMP AI Guide 生命周期要求）：标定集分层偏差分析

问题意识：阈值是全局标的（24 对混合样本上 F1 最优），但全局最优不等于各子群公平——
某个药品类别如果系统性分数压缩（正样本得分偏低或负样本偏高），全局指标可能
掩盖该类别上的误判倾向。GAMP AI Guide 要求项目阶段做偏差测试（bias testing）。

本脚本（MVP，药品类别维度）：
  - 按 q1 锚点把 24 对标定对聚成药品类别簇（降脂联用/降糖联用/降糖×肾功能）
  - 每簇独立算：正/负样本分数分布、对现役阈值（0.85/0.70）的边际
  - 判定是描述性的，不设硬断言——样本量小（每簇 6~10 对），统计意义有限，
    本分析的价值是发现系统性趋势，不是出二进制结论（GAMP 风险驱动原则：
    样本不足时如实记录，胜过硬算无意义的数字）
  - 渠道/年龄段维度：标定对无分层字段且样本量不够，登记为缺口 G-10

产出：控制台表格 + reports/bias_analysis_<date>.md（证据目录，git 排除）

用法: conda run -n py311 python scripts/bias_analysis.py
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from sentence_transformers import SentenceTransformer

from src.encoding import encode_symmetric
from src.normalization import normalize_text
from src.thresholds import load_model_path, load_thresholds

_ROOT = Path(__file__).resolve().parent.parent

# q1 锚点 → 药品类别簇（MVP 维度：治疗类别。类别随标定集扩充而扩充，只增不改）
_CLUSTERS = {
    "阿托伐他汀可以和红霉素联用吗": "降脂药×抗感染（他汀×大环内酯）",
    "司美格鲁肽和恩格列净可以联合使用吗": "降糖药联用（GLP-1×SGLT2）",
    "二甲双胍在肾功能不全的患者中可以使用吗": "降糖药×肾功能禁忌",
}


def band(score: float, dup: float, new: float) -> str:
    return "duplicate" if score >= dup else ("gray" if score >= new else "new")


def main() -> None:
    pairs = json.loads((_ROOT / "config" / "calibration_pairs.json").read_text(encoding="utf-8"))["pairs"]
    th = load_thresholds()
    dup, new = th["inquiry"]["duplicate"], th["inquiry"]["new"]

    model = SentenceTransformer(load_model_path())
    # 走生产打分路径：归一（商品名/术语→通用名）先于编码（设计文档 6.6：绕过归一层的测量无效）。
    # 注意：calibrate.py 的历史标定用裸文本，本脚本数字与其不可直接比——本脚本量的是
    # "现役阈值在生产路径上的分层表现"，这才是偏差测试的对象
    texts = [normalize_text(t) for p in pairs for t in (p["q1"], p["q2"])]
    vecs = encode_symmetric(model, texts)  # 判重是裸编码（对称），与生产打分路径一致
    vecs = [np.asarray(v) for v in vecs]

    rows = []
    for i, p in enumerate(pairs):
        a, b = vecs[2 * i], vecs[2 * i + 1]
        score = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        rows.append({
            "cluster": _CLUSTERS.get(p["q1"], "未分类"),
            "label": int(p["label"]),
            "score": score,
            "pred": band(score, dup, new),
            "q2": p["q2"],
            "note": p.get("note", ""),
        })

    # 误判定义：正样本落 new（漏判重复）/ 负样本落 duplicate（误判重复）；gray=转人工，可接受但记录
    print(f"偏差分析（药品类别维度）  阈值 duplicate={dup} / new={new}  模型={th['model']}")
    print(f"{'类别簇':<24s} n  正/负   正样本min    负样本max    边际      误判")
    report = [f"# 偏差测试报告（{date.today().isoformat()}，药品类别维度 MVP）",
              "",
              f"模型 {th['model']}，阈值 duplicate={dup} / new={new}，标定对 {len(pairs)} 对。",
              "判定为描述性（样本量小，不设硬断言）；渠道/年龄维度样本不足，登记缺口 G-10。", ""]
    any_flag = False
    for cluster in dict.fromkeys(r["cluster"] for r in rows):
        sub = [r for r in rows if r["cluster"] == cluster]
        pos = [r["score"] for r in sub if r["label"] == 1]
        neg = [r["score"] for r in sub if r["label"] == 0]
        pos_min = min(pos) if pos else None
        neg_max = max(neg) if neg else None
        margin = (pos_min - neg_max) if (pos and neg) else None
        misses = [r for r in sub if (r["label"] == 1 and r["pred"] == "new")
                  or (r["label"] == 0 and r["pred"] == "duplicate")]
        grays = [r for r in sub if r["pred"] == "gray"]
        flag = f"❌ {len(misses)} 对" if misses else ("⚠️ 无误判" if grays else "✅ 无误判")
        any_flag = any_flag or bool(misses)
        print(f"{cluster:<24s} {len(sub):2d}  {len(pos)}/{len(neg)}   "
              f"{pos_min if pos_min is not None else float('nan'):.4f}       "
              f"{neg_max if neg_max is not None else float('nan'):.4f}       "
              f"{margin if margin is not None else float('nan'):+.4f}   {flag}"
              + (f"（gray {len(grays)} 对转人工）" if grays else ""))
        report += [f"## {cluster}（{len(sub)} 对：正 {len(pos)} / 负 {len(neg)}）", "",
                   f"- 正样本分数区间：{min(pos):.4f} ~ {max(pos):.4f}" if pos else "- 无正样本",
                   f"- 负样本分数区间：{min(neg):.4f} ~ {max(neg):.4f}" if neg else "- 无负样本",
                   f"- 边际（正样本最低分 − 负样本最高分）：{margin:+.4f}" if margin is not None else "- 边际不可算",
                   f"- 误判：{len(misses)} 对；落灰色带转人工：{len(grays)} 对", ""]
        for r in sub:
            mark = "❌" if r in misses else ("⚠️" if r["pred"] == "gray" else "✓")
            direction = ""
            if r in misses:
                # 方向性：安全向误判有药品集合护栏兜底；效率向漏判只是多走起草，无安全风险
                direction = ("【安全向：生产由药品集合护栏兜底（药品不同→强制人工闸）；"
                             "若药品相同则护栏为盲，需人工关注】" if r["label"] == 0
                             else "【效率向：漏判重复→进 LLM 起草不短路，无安全风险】")
            report.append(f"  - {mark} label={r['label']} score={r['score']:.4f} pred={r['pred']}"
                          f"  「{r['q2']}」{r['note']} {direction}")
        report.append("")

    verdict = ("⚠️ 存在类别簇误判——见各簇明细：安全向误判依赖药品集合护栏兜底（护栏为盲的同药误判需人工关注），"
               "效率向漏判无安全风险。处置选项：扩充该簇标定对 / 评估阈值 / 维持现状并记录"
               if any_flag else "✅ 各类别簇在现役阈值下无系统性误判（描述性结论，样本量有限）")
    print(f"\n总结论: {verdict}")
    report += ["---", "", f"**总结论**：{verdict}", "",
               "后续：标定集扩充时按簇配额补样本（每簇 ≥15 对后可做有统计意义的分层判定）。"]

    out = _ROOT / "reports" / f"bias_analysis_{date.today().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(report), encoding="utf-8")
    print(f"报告: {out.relative_to(_ROOT)}")


if __name__ == "__main__":
    main()
