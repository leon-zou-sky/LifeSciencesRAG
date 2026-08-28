"""
Neo4j 知识图谱构建脚本

从 graph_data.py 读取实体和关系，写入 Neo4j
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
import os
from neo4j import GraphDatabase

from src.graph_data import (
    DRUGS, ENZYMES, REACTIONS, DISEASES, CLINICAL_TRIALS,
    DRUG_INTERACTIONS, DRUG_ENZYME, DRUG_REACTIONS,
    DRUG_INDICATIONS, TRIAL_DRUGS, CASE_DRUGS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

NEO4J_URI = os.environ.get("LS_RAG_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("LS_RAG_NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("LS_RAG_NEO4J_PASSWORD", "graphrag2024")  # 本地开发默认值


def clear_graph(tx):
    tx.run("MATCH (n) DETACH DELETE n")


def create_drug(tx, drug):
    tx.run(
        "MERGE (d:Drug {name: $name}) "
        "SET d.type = $type, d.category = $category",
        name=drug["name"], type=drug["type"], category=drug["category"],
    )


def create_enzyme(tx, enzyme):
    tx.run(
        "MERGE (e:Enzyme {name: $name}) SET e.type = $type",
        name=enzyme["name"], type=enzyme["type"],
    )


def create_reaction(tx, reaction):
    tx.run(
        "MERGE (r:AdverseReaction {name: $name}) "
        "SET r.severity = $severity, r.description = $description",
        name=reaction["name"], severity=reaction["severity"],
        description=reaction["description"],
    )


def create_disease(tx, disease):
    tx.run(
        "MERGE (d:Disease {name: $name}) SET d.category = $category",
        name=disease["name"], category=disease["category"],
    )


def create_trial(tx, trial):
    tx.run(
        "MERGE (t:ClinicalTrial {id: $id}) "
        "SET t.phase = $phase, t.title = $title, t.year = $year, t.enrollment = $enrollment",
        id=trial["id"], phase=trial["phase"], title=trial["title"],
        year=trial["year"], enrollment=trial["enrollment"],
    )


def create_drug_interaction(tx, interaction):
    drug_a, drug_b, rel_type, desc, source = interaction
    tx.run(
        f"MATCH (a:Drug {{name: $a}}), (b:Drug {{name: $b}}) "
        f"MERGE (a)-[r:{rel_type}]->(b) "
        f"SET r.description = $desc, r.source = $source",
        a=drug_a, b=drug_b, desc=desc, source=source,
    )


def create_drug_enzyme_rel(tx, rel):
    drug, enzyme, rel_type, desc = rel
    tx.run(
        f"MATCH (d:Drug {{name: $drug}}), (e:Enzyme {{name: $enzyme}}) "
        f"MERGE (d)-[r:{rel_type}]->(e) "
        f"SET r.description = $desc",
        drug=drug, enzyme=enzyme, desc=desc,
    )


def create_drug_reaction_rel(tx, rel):
    drug, reaction, evidence, desc = rel
    tx.run(
        f"MATCH (d:Drug {{name: $drug}}), (r:AdverseReaction {{name: $reaction}}) "
        f"MERGE (d)-[rel:CAUSES {{evidence: $evidence}}]->(r) "
        f"SET rel.description = $desc",
        drug=drug, reaction=reaction, evidence=evidence, desc=desc,
    )


def create_drug_indication_rel(tx, rel):
    drug, disease, role = rel
    tx.run(
        f"MATCH (d:Drug {{name: $drug}}), (dis:Disease {{name: $disease}}) "
        f"MERGE (d)-[r:TREATS {{role: $role}}]->(dis)",
        drug=drug, disease=disease, role=role,
    )


def create_trial_drug_rel(tx, rel):
    trial_id, drug, role = rel
    tx.run(
        f"MATCH (t:ClinicalTrial {{id: $trial_id}}), (d:Drug {{name: $drug}}) "
        f"MERGE (t)-[r:TESTS {{role: $role}}]->(d)",
        trial_id=trial_id, drug=drug, role=role,
    )


def main():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

    with driver.session() as session:
        # 清空旧数据
        session.execute_write(clear_graph)
        logger.info("已清空旧图谱")

        # 创建节点
        for drug in DRUGS:
            session.execute_write(create_drug, drug)
        logger.info(f"创建 Drug 节点: {len(DRUGS)} 个")

        for enzyme in ENZYMES:
            session.execute_write(create_enzyme, enzyme)
        logger.info(f"创建 Enzyme 节点: {len(ENZYMES)} 个")

        for reaction in REACTIONS:
            session.execute_write(create_reaction, reaction)
        logger.info(f"创建 AdverseReaction 节点: {len(REACTIONS)} 个")

        for disease in DISEASES:
            session.execute_write(create_disease, disease)
        logger.info(f"创建 Disease 节点: {len(DISEASES)} 个")

        for trial in CLINICAL_TRIALS:
            session.execute_write(create_trial, trial)
        logger.info(f"创建 ClinicalTrial 节点: {len(CLINICAL_TRIALS)} 个")

        # 创建关系
        for interaction in DRUG_INTERACTIONS:
            session.execute_write(create_drug_interaction, interaction)
        logger.info(f"创建药物相互作用关系: {len(DRUG_INTERACTIONS)} 条")

        for rel in DRUG_ENZYME:
            session.execute_write(create_drug_enzyme_rel, rel)
        logger.info(f"创建药物-酶关系: {len(DRUG_ENZYME)} 条")

        for rel in DRUG_REACTIONS:
            session.execute_write(create_drug_reaction_rel, rel)
        logger.info(f"创建药物-不良反应关系: {len(DRUG_REACTIONS)} 条")

        for rel in DRUG_INDICATIONS:
            session.execute_write(create_drug_indication_rel, rel)
        logger.info(f"创建药物-适应症关系: {len(DRUG_INDICATIONS)} 条")

        for rel in TRIAL_DRUGS:
            session.execute_write(create_trial_drug_rel, rel)
        logger.info(f"创建试验-药物关系: {len(TRIAL_DRUGS)} 条")

        # 统计
        result = session.run("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS cnt ORDER BY cnt DESC")
        logger.info("图谱节点统计:")
        for record in result:
            logger.info(f"  {record['label']}: {record['cnt']}")

        result = session.run("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS cnt ORDER BY cnt DESC")
        logger.info("图谱关系统计:")
        for record in result:
            logger.info(f"  {record['type']}: {record['cnt']}")

    driver.close()
    logger.info("🎉 图谱构建完成")


if __name__ == "__main__":
    main()
