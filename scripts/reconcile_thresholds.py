"""
阈值对账断言：DB 当前值 == 稳态锚定值（config/threshold_anchors.yaml）

2026-08-25 演练教训（演练文档 §2）：
  漏回滚 inquiry.duplicate 造成混合态，回归输出对阈值不敏感（score 列与阈值无关），
  行为测试发现不了配置漂移——配置状态只能直接对账，不能靠行为反推。

用途：
  ① 演练/负对照/客户标定结束后必跑——有键偏离锚定值 exit 1，禁止收工
  ② 日常巡检可接 cron/CI

与 load_thresholds() 的区别：本脚本**只读 DB、不吃 yaml 兜底**——
DB 不可达时无法完成核验，exit 2 大声失败，绝不拿兜底值假装对账通过。

用法:
    python scripts/reconcile_thresholds.py              # 对账全部锚定键，偏离 exit 1
    python scripts/reconcile_thresholds.py --quiet      # 只输出结果不打印明细表
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse

import yaml

from src.thresholds import _load_from_db  # 故意绕过 load_thresholds 的 yaml 兜底

_ANCHORS = Path(__file__).resolve().parent.parent / "config" / "threshold_anchors.yaml"
_EPS = 1e-9


def flatten(d: dict, prefix: str = "") -> dict:
    """anchors 是嵌套还是平铺都接受，统一拍平成 inquiry.duplicate 形式"""
    flat = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flat.update(flatten(v, key))
        else:
            flat[key] = v
    return flat


def main():
    parser = argparse.ArgumentParser(description="阈值稳态对账断言")
    parser.add_argument("--quiet", action="store_true", help="只输出结论")
    args = parser.parse_args()

    anchors = flatten(yaml.safe_load(_ANCHORS.read_text(encoding="utf-8")))

    db = _load_from_db()
    if db is None:
        print("❌ 无法对账：MySQL threshold_config 不可达或为空。"
              "对账只吃 DB 实时值，不可用兜底值代替——请先恢复 DB 再核验。", file=sys.stderr)
        sys.exit(2)
    current = flatten(db)

    rows, mismatches = [], []
    for key, expected in anchors.items():
        actual = current.get(key, "<缺失>")
        if isinstance(expected, float):
            ok = isinstance(actual, float) and abs(actual - expected) < _EPS
        else:
            ok = actual == expected
        rows.append((key, expected, actual, "✅" if ok else "❌"))
        if not ok:
            mismatches.append((key, expected, actual))

    # 反向提示：DB 里有、锚定表没有的键（可能是新键忘了登记锚定值）
    extra = sorted(set(current) - set(anchors))

    if not args.quiet:
        print("键                          锚定值       DB 当前值    状态")
        print("─" * 62)
        for key, expected, actual, mark in rows:
            print(f"{key:<28}{str(expected):<12}{str(actual):<13}{mark}")
        for key in extra:
            print(f"{key:<28}{'<未登记>':<12}{str(current[key]):<13}ℹ️")

    print("=" * 62)
    if mismatches:
        print(f"❌ 对账失败：{len(mismatches)} 个键偏离锚定值")
        for key, expected, actual in mismatches:
            print(f"  - {key}: 锚定 {expected} vs 当前 {actual}")
        print("处置：若是演练残留 → set_threshold.py 回滚；若是合法变更 → 走四级门禁后更新 threshold_anchors.yaml")
        sys.exit(1)
    print(f"✅ 对账通过：{len(anchors)} 个锚定键全部一致"
          + (f"（另有 {len(extra)} 个未登记键，仅提示）" if extra else ""))


if __name__ == "__main__":
    main()
