"""
生成层冒烟探针：断言五种终态的走向是否正确（不进 PQ 门禁，先观察稳定性）

用法:
    python scripts/test_answers.py           # 跑全部探针
    python scripts/test_answers.py --id ANS-03

断言对象是 disposition（终态路由），不是草稿文本——LLM 输出不可 diff。
  ANS-01 判重短路：standard_answer（LLM 不出场）
  ANS-02 正常起草：draft_passed（引用校验全过）
  ANS-03 拒答边界：insufficient_evidence（grounded 负对照）

与 regression.py 的区别：regression 断检索层（确定性，分数可比对）；
本脚本断生成层路由（LLM 有长尾波动，故冒烟级，达标稳定后再议纳入 PQ）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse

from scripts.answer import run

_GOLDEN = Path(__file__).resolve().parent.parent / "config" / "answer_golden.json"


def main():
    parser = argparse.ArgumentParser(description="生成层冒烟探针")
    parser.add_argument("--id", help="只跑指定探针")
    args = parser.parse_args()

    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    probes = golden["probes"]
    if args.id:
        probes = [p for p in probes if p["id"] == args.id]
        if not probes:
            print(f"探针 {args.id} 不存在"); sys.exit(1)

    print(f"生成层冒烟探针（模型 {golden['model']}，{len(probes)} 条）")
    print("断言终态路由，不断言文本——LLM 输出不可 diff\n")

    failures = 0
    for p in probes:
        trace = run(p["question"], asker="smoke-test")
        got, want = trace["disposition"], p["expect_disposition"]
        ok = got == want
        failures += 0 if ok else 1
        mark = "✅" if ok else "❌"
        print(f"{mark} {p['id']} expect={want} got={got}")
        print(f"   问题: {p['question']}")
        if not ok:
            print(f"   ⚠️ {p['why']}")
            if "validation" in trace:
                print(f"   校验: {trace['validation']}")
        elif got == "draft_passed":
            print(f"   引用 {len(trace['validation']['citations'])} 处合法，"
                  f"尝试 {trace['llm']['attempts']} 次")

    print(f"\n{'✅ 全部通过' if failures == 0 else f'❌ {failures}/{len(probes)} 未达预期'}")
    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
