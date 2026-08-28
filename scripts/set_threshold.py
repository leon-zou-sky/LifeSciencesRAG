"""
阈值变更工具（BYOM 写路径）：业务侧调参的唯一入口

GxP 变更控制三要素强制齐全：谁改的（--by）、为什么改（--reason）、改成什么（--value）。
每次变更同时写当前值表 + 追加审计表（threshold_history 只增不改）。

用法:
    python scripts/set_threshold.py --key case_report.gray --value 0.80 \\
        --by 张三 --reason "灰色带下限实测调整，见标定记录 XXX"
    python scripts/set_threshold.py --list            # 查看当前全部阈值
    python scripts/set_threshold.py --history case_report.gray   # 查某键变更史

军规：阈值调整后必须重跑回归（scripts/regression.py）确认探针全绿，
      并在标定文档里登记依据——set_threshold 只管改，不管"改得对不对"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
from datetime import datetime

from src.db import get_conn

KNOWN_KEYS = {  # 合法键注册表：防手滑造出孤儿键（脚本读了不存在的键=静默失效）
    "model", "tolerance",
    "inquiry.duplicate", "inquiry.new",
    "case_report.duplicate", "case_report.gray",
    "generation.draft_min_top1",
}
NUMERIC_KEYS = KNOWN_KEYS - {"model"}


def main():
    parser = argparse.ArgumentParser(description="阈值变更（强制留痕）")
    parser.add_argument("--key", help="阈值键，如 case_report.gray")
    parser.add_argument("--value", help="新值")
    parser.add_argument("--by", help="变更人（必填）")
    parser.add_argument("--reason", help="变更理由（必填，建议附标定证据出处）")
    parser.add_argument("--list", action="store_true", help="列出当前全部阈值")
    parser.add_argument("--history", metavar="KEY", help="查看某键的变更史")
    args = parser.parse_args()

    conn = get_conn()
    try:
        if args.list:
            with conn.cursor() as cur:
                cur.execute("SELECT tkey, tvalue, updated_by, updated_at, reason"
                            " FROM threshold_config ORDER BY tkey")
                for r in cur.fetchall():
                    print(f'{r["tkey"]:32s} = {r["tvalue"]:12s}'
                          f'（{r["updated_by"]} @ {r["updated_at"]}）')
            return
        if args.history:
            with conn.cursor() as cur:
                cur.execute("SELECT old_value, new_value, changed_by, changed_at, reason"
                            " FROM threshold_history WHERE tkey=%s ORDER BY id",
                            (args.history,))
                rows = cur.fetchall()
            if not rows:
                print(f"{args.history} 无变更记录")
            for r in rows:
                print(f'{r["changed_at"]}  {r["changed_by"]}:'
                      f' {r["old_value"]} → {r["new_value"]}  ({r["reason"]})')
            return

        if not (args.key and args.value and args.by and args.reason):
            parser.error("变更必须四要素齐全：--key --value --by --reason")
        if args.key not in KNOWN_KEYS:
            parser.error(f"未知阈值键 {args.key}。合法键：{sorted(KNOWN_KEYS)}"
                         f"（新增键请先改注册表并走代码评审）")
        if args.key in NUMERIC_KEYS:
            try:
                float(args.value)
            except ValueError:
                parser.error(f"{args.key} 是数值键，{args.value!r} 不是数字")

        with conn.cursor() as cur:
            cur.execute("SELECT tvalue FROM threshold_config WHERE tkey=%s", (args.key,))
            row = cur.fetchone()
            if row is None:
                parser.error(f"{args.key} 不在 threshold_config 表，请先跑 init_thresholds.py")
            old = row["tvalue"]
            now = datetime.now()
            cur.execute(
                "UPDATE threshold_config SET tvalue=%s, updated_by=%s, updated_at=%s, reason=%s"
                " WHERE tkey=%s", (args.value, args.by, now, args.reason, args.key))
            cur.execute(
                "INSERT INTO threshold_history"
                " (tkey, old_value, new_value, changed_by, changed_at, reason)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (args.key, old, args.value, args.by, now, args.reason))
        conn.commit()
        print(f"✔ {args.key}: {old} → {args.value}（{args.by}：{args.reason}）")
        print("⚠️ 变更已生效并留痕。请重跑回归确认探针全绿：")
        print("   conda run -n py311 python scripts/regression.py")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
