"""
病例并案断言探针（G-02）：把 14 对病例标定对固化为自动化断言

和 measure_case_gate.py 的分工：
  measure_case_gate.py  → 定线工具：打印分布、人工看空档，用于标定/重标定
  test_cases.py         → 守门工具：每对断言「系统判定 == 人工标注」，任一不符即失败退出

冒烟级（同 test_answers.py 定位）：暂不进 PQ 门禁，样本 14 对仍薄，
badcase 只增不改持续扩充后再谈升格。

用法:
    python scripts/test_cases.py    # 全绿 exit 0，任一断言失败 exit 1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.measure_case_gate import VERDICT_LABEL, score_pairs


def main():
    results = score_pairs()
    failed = 0
    for r in results:
        ok = r["sys"] == r["expect"]
        if not ok:
            failed += 1
        mark = "✓" if ok else "✗"
        hybrid = f"{r['hybrid']:.4f}" if r["hybrid"] is not None else "召回失败"
        print(f'{r["id"]:6s} {mark}  expect={VERDICT_LABEL[r["expect"]]:4s}'
              f' 系统={VERDICT_LABEL.get(r["sys"], r["sys"]):4s}  hybrid={hybrid}  {r["category"]}')
    print("═" * 60)
    if failed:
        print(f"FAILED: {failed}/{len(results)} 对判定与标注不符——"
              f"分数分布漂移或规则被破坏，需重标定（docs/model-selection-recalibration.md）")
        sys.exit(1)
    print(f"OK: {len(results)}/{len(results)} 对全部符合标注（duplicate/gray 阈值与三道硬否决生效）")


if __name__ == "__main__":
    main()
