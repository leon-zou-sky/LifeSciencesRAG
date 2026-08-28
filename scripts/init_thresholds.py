"""
阈值表初始化（BYOM 种子）：从 config/thresholds.yaml 引导入库

建两张表（幂等，可重跑）：
  threshold_config   当前值（运行时事实源）
  threshold_history  变更审计（只增不改：谁、何时、从什么改成什么、为什么）

用法:
    python scripts/init_thresholds.py            # 建表 + 从 yaml 种子（已有值不覆盖）
    python scripts/init_thresholds.py --reseed   # 强制用 yaml 覆盖现值（留痕，慎用）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
from datetime import datetime

import yaml

from src.db import get_conn

_CONFIG = Path(__file__).resolve().parent.parent / "config" / "thresholds.yaml"

DDL = [
    """
    CREATE TABLE IF NOT EXISTS threshold_config (
        tkey VARCHAR(64) PRIMARY KEY COMMENT '阈值键，如 inquiry.duplicate / model',
        tvalue TEXT NOT NULL COMMENT '当前值（数值以字符串存储，读出时转型）',
        updated_by VARCHAR(64) NOT NULL COMMENT '最后变更人',
        updated_at DATETIME NOT NULL COMMENT '最后变更时间',
        reason TEXT COMMENT '最后变更理由（变更控制留痕）'
    ) COMMENT='判重阈值·运行时事实源（BYOM）'
    """,
    """
    CREATE TABLE IF NOT EXISTS threshold_history (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        tkey VARCHAR(64) NOT NULL,
        old_value TEXT COMMENT '变更前值（新建为 NULL）',
        new_value TEXT NOT NULL,
        changed_by VARCHAR(64) NOT NULL,
        changed_at DATETIME NOT NULL,
        reason TEXT NOT NULL,
        INDEX idx_tkey (tkey)
    ) COMMENT='阈值变更审计（只增不改）'
    """,
]


def _flatten(d: dict, prefix: str = "") -> dict[str, str]:
    """{inquiry: {duplicate: 0.85}} → {'inquiry.duplicate': '0.85'}"""
    flat = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flat.update(_flatten(v, key))
        else:
            flat[key] = str(v)
    return flat


def main():
    parser = argparse.ArgumentParser(description="阈值表初始化（从 yaml 种子入库）")
    parser.add_argument("--reseed", action="store_true",
                        help="强制用 yaml 覆盖 DB 现值（每次覆盖都留痕）")
    args = parser.parse_args()

    flat = _flatten(yaml.safe_load(_CONFIG.read_text(encoding="utf-8")))
    conn = get_conn()
    seeded, skipped, overwritten = 0, 0, 0
    try:
        with conn.cursor() as cur:
            for ddl in DDL:
                cur.execute(ddl)
            now = datetime.now()
            for key, value in sorted(flat.items()):
                cur.execute("SELECT tvalue FROM threshold_config WHERE tkey=%s", (key,))
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "INSERT INTO threshold_config (tkey, tvalue, updated_by, updated_at, reason)"
                        " VALUES (%s, %s, 'seed', %s, '从 thresholds.yaml 初始化')",
                        (key, value, now))
                    cur.execute(
                        "INSERT INTO threshold_history"
                        " (tkey, old_value, new_value, changed_by, changed_at, reason)"
                        " VALUES (%s, NULL, %s, 'seed', %s, '从 thresholds.yaml 初始化')",
                        (key, value, now))
                    seeded += 1
                elif args.reseed and row["tvalue"] != value:
                    cur.execute(
                        "UPDATE threshold_config SET tvalue=%s, updated_by='seed',"
                        " updated_at=%s, reason='reseed：yaml 强制覆盖' WHERE tkey=%s",
                        (value, now, key))
                    cur.execute(
                        "INSERT INTO threshold_history"
                        " (tkey, old_value, new_value, changed_by, changed_at, reason)"
                        " VALUES (%s, %s, %s, 'seed', %s, 'reseed：yaml 强制覆盖')",
                        (key, row["tvalue"], value, now))
                    overwritten += 1
                else:
                    skipped += 1
        conn.commit()
    finally:
        conn.close()
    print(f"种子完成：新增 {seeded} 键，跳过 {skipped} 键（已存在），覆盖 {overwritten} 键")
    print("运行时事实源现在是 MySQL threshold_config；调参走 scripts/set_threshold.py")


if __name__ == "__main__":
    main()
