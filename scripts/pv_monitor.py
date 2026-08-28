"""
① 药物警戒文献监测（PV Literature Surveillance）

对应 Veeva Safety 场景：新发表文献 → LLM 抽取"药物-不良事件"关系
→ 自动写入知识图谱（新增节点/边） → 信号检测规则引擎 → 监测报告

信号规则：
  🔴 新信号    : 图谱中不存在的"药物-反应"关联 + 严重度=严重
  🟠 信号升级  : 已有关联获得新的独立文献来源佐证（来源数≥2）+ 严重度=严重
  🟡 新关联    : 图谱中不存在的关联，严重度中等/轻微，记录观察
  🟢 已知确认  : 已有关联，同一来源重复监测到

用法:
    python scripts/pv_monitor.py            # 监测全部新文献
    python scripts/pv_monitor.py --dry-run  # 只抽取和评估，不写图谱
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import logging
from datetime import date

from neo4j import GraphDatabase

from scripts.query_graphrag import NEO4J_PASS, NEO4J_URI, NEO4J_USER
from src.llm_config import get_ark_client
from src.pv_literature import PV_LITERATURE

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """你是药物警戒(PV)文献监测员。从下面这篇文献中抽取"药物-不良事件"关系。

返回 JSON 数组，每个元素：
{
  "drug": "药品通用名（如：辛伐他汀，不要带剂型规格）",
  "reaction": "不良事件标准名称（如：横纹肌溶解）",
  "severity": "严重/中等/轻微（按不良事件的临床严重度判断）",
  "evidence": "文献中的关键证据（50字以内的原文摘要）",
  "mechanism": "机制说明（如抑制CYP3A4升高血药浓度；没有则为空字符串）"
}

要求：只返回 JSON 数组，不要任何其他文字；未提及不良事件关系则返回 []。"""


def extract_ae_relations(client, llm_model: str, lit: dict) -> list[dict]:
    """LLM 从文献摘要抽取 药物-不良事件 关系"""
    user_msg = f"文献标题：{lit['title']}\n来源：{lit['journal']} {lit['year']}\n\n摘要：\n{lit['abstract']}"
    resp = client.chat.completions.create(
        model=llm_model,
        messages=[
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.1,
    )
    text = resp.choices[0].message.content.strip()
    # 兼容 ```json ... ``` 包裹
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"LLM 返回无法解析为 JSON: {text[:200]}")
        return []
    return [i for i in items if i.get("drug") and i.get("reaction")]


# ============ 图谱操作 ============

# 实体归一：文献表述 → 图谱标准名（对应真实 PV 场景的 MedDRA 编码映射）
REACTION_ALIASES = {
    "横纹肌溶解症": "横纹肌溶解",
    "INR显著升高": "INR升高",
    "乳酸酸中毒症": "乳酸酸中毒",
}


def normalize_reaction(name: str) -> str:
    name = name.strip()
    return REACTION_ALIASES.get(name, name)


def find_existing_edge(session, drug: str, reaction: str) -> dict | None:
    """查找已存在的 药物-CAUSES-反应 边（反应名忽略首尾空格）"""
    rec = session.run(
        """
        MATCH (d:Drug)-[r:CAUSES]->(ar:AdverseReaction)
        WHERE d.name = $drug AND trim(ar.name) = $reaction
        RETURN ar.name AS reaction_name, coalesce(r.sources, []) AS sources
        """,
        drug=drug, reaction=reaction,
    ).single()
    if rec is None:
        return None
    return {"reaction_name": rec["reaction_name"], "sources": rec["sources"]}


def upsert_ae_edge(session, drug: str, reaction: str, severity: str,
                   evidence: str, lit_id: str, existing: dict | None):
    """写入/更新 药物-CAUSES-反应 边，sources 记录全部独立文献来源"""
    today = date.today().isoformat()

    # 药物节点（可能是图谱外新药物）
    session.run(
        """
        MERGE (d:Drug {name: $drug})
        ON CREATE SET d.detected_via = '文献监测'
        """,
        drug=drug,
    )
    # 反应节点：忽略首尾空格复用已有节点，不存在才新建
    reaction_name = existing["reaction_name"] if existing else reaction
    session.run(
        """
        MERGE (ar:AdverseReaction {name: $reaction})
        ON CREATE SET ar.severity = $severity, ar.detected_via = '文献监测'
        """,
        reaction=reaction_name, severity=severity,
    )
    # 边：sources 数组追加文献来源
    session.run(
        """
        MATCH (d:Drug {name: $drug}), (ar:AdverseReaction {name: $reaction})
        MERGE (d)-[r:CAUSES]->(ar)
        ON CREATE SET r.sources = [$lit_id]
        ON MATCH SET r.sources = CASE
            WHEN $lit_id IN coalesce(r.sources, []) THEN r.sources
            ELSE coalesce(r.sources, []) + $lit_id END
        SET r.evidence = $evidence,
            r.severity = $severity,
            r.detected_via = '文献监测',
            r.detected_date = $today
        """,
        drug=drug, reaction=reaction_name, severity=severity,
        evidence=evidence, lit_id=lit_id, today=today,
    )


# ============ 信号规则引擎 ============

def evaluate_signal(rel: dict, existing: dict | None, lit_id: str) -> tuple[str, str]:
    """返回 (信号级别, 说明)"""
    severe = rel.get("severity") == "严重"

    if existing is None:
        if severe:
            return "🔴 新信号", "图谱中不存在的药物-严重不良事件关联，建议人工评估"
        return "🟡 新关联", "新发现的药物-不良事件关联（非严重），记录观察"

    sources = existing["sources"]
    if lit_id in sources:
        return "🟢 已知确认", "该来源此前已记录，跳过"

    # 已有关联 + 新独立来源（图谱原始构建算 1 个基础来源）
    total_sources = 1 + len(sources) + 1  # 基础来源 + 已记录文献 + 本次
    if severe and total_sources >= 2:
        return "🟠 信号升级", f"严重关联获得第 {total_sources} 个独立来源佐证，信号强度升级"
    return "🟢 已知确认", f"已有关联补充新来源（累计 {total_sources} 个）"


# ============ 主流程 ============

def run_monitor(dry_run: bool = False):
    client, llm_model = get_ark_client()
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

    print(f"\n{'=' * 64}")
    print(f"药物警戒文献监测  |  待监测文献 {len(PV_LITERATURE)} 篇  |  {'DRY-RUN 不写图谱' if dry_run else '写入图谱'}")
    print(f"{'=' * 64}")

    stats = {"🔴 新信号": 0, "🟠 信号升级": 0, "🟡 新关联": 0, "🟢 已知确认": 0}
    alerts = []

    try:
        for lit in PV_LITERATURE:
            print(f"\n▶ {lit['lit_id']} 《{lit['title']}》（{lit['journal']} {lit['year']}）")

            relations = extract_ae_relations(client, llm_model, lit)
            if not relations:
                print("  未抽取到药物-不良事件关系")
                continue

            with driver.session() as session:
                for rel in relations:
                    rel["reaction"] = normalize_reaction(rel["reaction"])
                    existing = find_existing_edge(session, rel["drug"], rel["reaction"])
                    level, reason = evaluate_signal(rel, existing, lit["lit_id"])

                    print(f"  {level}  {rel['drug']} —CAUSES→ {rel['reaction']}"
                          f"（{rel.get('severity', '?')}）")
                    print(f"       {reason}")
                    if rel.get("mechanism"):
                        print(f"       机制: {rel['mechanism']}")

                    stats[level] = stats.get(level, 0) + 1
                    if level in ("🔴 新信号", "🟠 信号升级"):
                        alerts.append((level, rel, lit))

                    if not dry_run:
                        upsert_ae_edge(
                            session, rel["drug"], rel["reaction"],
                            rel.get("severity", "中等"), rel.get("evidence", ""),
                            lit["lit_id"], existing,
                        )

        print(f"\n{'=' * 64}")
        print("监测汇总：" + "  ".join(f"{k}×{v}" for k, v in stats.items() if v))

        if alerts:
            print(f"\n⚠️ 需人工评估的信号 {len(alerts)} 条：")
            for level, rel, lit in alerts:
                print(f"  {level}  {rel['drug']} —CAUSES→ {rel['reaction']}"
                      f"（来源: {lit['lit_id']} 《{lit['title']}》）")

        if not dry_run:
            with driver.session() as session:
                result = session.run(
                    "MATCH ()-[r:CAUSES]->() WHERE r.detected_via = '文献监测' "
                    "RETURN count(r) AS cnt"
                ).single()
            print(f"\n图谱中'文献监测'来源的 CAUSES 边: {result['cnt']} 条")
            print("（可在 Neo4j Browser http://localhost:7474 查看扩图结果）")
    finally:
        driver.close()


def main():
    parser = argparse.ArgumentParser(description="药物警戒文献监测")
    parser.add_argument("--dry-run", action="store_true", help="只抽取和评估，不写图谱")
    args = parser.parse_args()
    run_monitor(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
