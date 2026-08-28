"""
覆盖率对账：业务数据里出现的药品 vs 说明书库已有品种 → 覆盖缺口告警

召回天花板的事前发现机制：排序策略再精妙也救不了库里没有的品种——
不能等用户问了"辛伐他汀"才发现说明书库没有它。

对账口径：
  需求侧 = 问询库 drugs + 病例四要素 drugs + 黄金集探针 expect_drugs
           （全部是归一后的通用名，extract_drugs 的产出物）
  供给侧 = drug_inserts 库（documents 表 doc_type='说明书' 的 drug_name/title）
  判定   = 通用名是某说明书名的子串即视为已覆盖（阿托伐他汀 ∈ 阿托伐他汀钙片）

用法: python scripts/check_coverage.py
生产形态：定时任务跑，差集非空 → 告警/生成补库工单（走 ingest 管道补采）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
from collections import Counter

from src.db import get_conn


def collect_demand(cur) -> Counter:
    """需求侧：业务数据里实际出现的药品通用名 → 出现次数"""
    demand: Counter = Counter()
    # 问询库
    cur.execute("SELECT drugs FROM mi_inquiries WHERE drugs IS NOT NULL AND drugs != ''")
    for row in cur.fetchall():
        for d in row["drugs"].split(","):
            demand[d.strip()] += 1
    # 病例四要素
    cur.execute("SELECT source_meta FROM documents WHERE doc_type='病例报告'")
    for row in cur.fetchall():
        meta = row["source_meta"]
        meta = json.loads(meta) if isinstance(meta, str) else meta
        for d in meta.get("drugs", []):
            demand[d] += 1
    # 黄金集探针（查询侧代表）
    golden = Path(__file__).resolve().parent.parent / "config" / "golden_set.json"
    if golden.exists():
        cfg = json.loads(golden.read_text(encoding="utf-8"))
        for p in cfg.get("inquiry_probes", []):
            for d in p.get("expect_drugs", []):
                demand[d] += 1
    return demand


def collect_supply(cur) -> list[str]:
    """供给侧：说明书库已有品种名"""
    cur.execute("SELECT title, source_meta FROM documents WHERE doc_type='说明书'")
    names = []
    for row in cur.fetchall():
        names.append(row["title"])
        meta = row["source_meta"]
        meta = json.loads(meta) if isinstance(meta, str) else meta
        if meta.get("drug_name"):
            names.append(meta["drug_name"])
    return list(dict.fromkeys(names))  # title 与 drug_name 常相同，去重


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            demand = collect_demand(cur)
            supply = collect_supply(cur)
    finally:
        conn.close()

    print("=" * 60)
    print("说明书库覆盖率对账")
    print("=" * 60)
    print(f"供给侧: {len(supply)} 个品种名 {supply}\n")
    print(f"{'药品(通用名)':<12} {'被引用次数':<8} 覆盖?")
    print("-" * 60)

    gaps = []
    for drug, cnt in demand.most_common():
        covered = any(drug in name for name in supply)
        mark = "✅" if covered else "❌ 缺口"
        print(f"{drug:<12} {cnt:<10} {mark}")
        if not covered:
            gaps.append(drug)

    print("=" * 60)
    if gaps:
        print(f"⚠️ 覆盖缺口 {len(gaps)} 个: {'、'.join(gaps)}")
        print("   → 生成补库工单：样品放入 data/inbox/ 走 ingest 管道，回归后生效")
    else:
        print("✅ 需求侧药品全部有说明书覆盖，无缺口")


if __name__ == "__main__":
    main()
