"""
客户标定对导入工具：把 CSV/JSON 标定数据转成本项目标准格式（config/calibration_pairs.json）

CSV 格式（UTF-8，无 BOM）：
    q1,q2,label,rationale
    "二甲双胍怎么吃","二甲双胍用法",1,"同问题不同表述"
    "二甲双胍怎么吃","阿托伐他汀怎么吃",0,"不同药品"

JSON 格式：
    [{"q1":"...","q2":"...","label":1,"rationale":"..."}, ...]

label: 1=同问题（应判重复），0=异问题（应判新）

用法：
    python scripts/import_calibration_pairs.py data/customer_pairs.csv
    python scripts/import_calibration_pairs.py data/customer_pairs.json --out config/calibration_pairs.json

注意：
  - 默认追加到现有标定集（badcase 只增不改原则）
  - 用 --replace 可全量替换（谨慎）
  - 导入后必须跑 scripts/calibrate.py 或 scripts/calibration_report.py 重新定线
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "config" / "calibration_pairs.json"


def _load_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "pairs" in data:
        return data["pairs"]
    return data


def _load_csv(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            label = int(row["label"].strip())
            rec = {"q1": row["q1"].strip(), "q2": row["q2"].strip(), "label": label}
            if row.get("rationale"):
                rec["rationale"] = row["rationale"].strip()
            rows.append(rec)
    return rows


def _load(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        return _load_csv(path)
    return _load_json(path)


def main():
    parser = argparse.ArgumentParser(description="导入客户标定对")
    parser.add_argument("input", help="输入 CSV/JSON 文件路径")
    parser.add_argument("--out", default=str(_DEFAULT_OUT), help="输出标定对文件路径")
    parser.add_argument("--replace", action="store_true", help="全量替换现有标定集（默认追加）")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.out)
    if not in_path.exists():
        print(f"输入文件不存在: {in_path}", file=sys.stderr)
        sys.exit(1)

    new_pairs = _load(in_path)
    if args.replace:
        pairs = new_pairs
    else:
        if out_path.exists():
            existing = _load_json(out_path)
        else:
            existing = []
        pairs = existing + new_pairs

    out_path.write_text(json.dumps({"pairs": pairs}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"已写入 {out_path}：共 {len(pairs)} 对（新增 {len(new_pairs)}，"
          f"{'全量替换' if args.replace else '追加'}）")
    print("下一步：跑 scripts/calibrate.py 或 scripts/calibration_report.py 重新定线")


if __name__ == "__main__":
    main()
