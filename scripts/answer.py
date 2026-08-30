"""
答复管道：问 → 判重 → 检索 → LLM 起草 → 引用校验 → 留痕（生成层闭环，辅助模式）

用法:
    python scripts/answer.py "二甲双胍和乳酸酸中毒的关系" --asker 张三
    python scripts/answer.py "阿托伐他汀可以和红霉素联用吗"   # 命中 approved 标准答案时 LLM 不出场

流程（五种终态，disposition 写入 trace）：
  ① standard_answer      判重命中 approved 标准答案（≥duplicate 阈值）→ 直接回，LLM 不出场
  ② draft_passed         LLM 草稿通过引用校验 → 交人复核
  ③ insufficient_evidence 模型拒答（【证据不足】）→ 守住边界，附检索结果供人判断
  ④ draft_failed_human   重试耗尽仍未过校验 → 明示降级转人工 + 附检索原文（不静默凑合）
  ⑤ coverage_gap         覆盖缺口（库中无该药 A/A- 原文）→ 不起草，直接明示升级路径

留痕：每次调用追加一条 JSON 到 data/traces/answers.jsonl（只增不改，
含模型 digest、prompt 版本、检索编号列表、校验结果——历史答复可复现到配置级）。
生产化时该 JSONL 下沉 MySQL 审计表，字段已按审计表结构设计。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import logging
from datetime import datetime, timezone

from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from src.citation_check import validate_draft
from src.dedup_guard import classify
from src.encoding import encode_symmetric
from src.llm import LLM_CONFIG, PROMPT_VERSION, draft_answer, model_digest, redraft_with_feedback, render_draft
from src.normalization import extract_drugs
from src.pii_guard import redact as redact_pii
from src.thresholds import load_model_path, load_thresholds

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 复用检索层的现成实现（同一套权威融合/覆盖缺口逻辑，不生二套）
from scripts.query import authority_fusion, search_all_collections  # noqa: E402

_MODEL_PATH = load_model_path()
_TH = load_thresholds()
_TRACE_DIR = Path(__file__).resolve().parent.parent / "data" / "traces"
MILVUS_URI = "http://localhost:19531"  # PoC 实例（与默认 19530 隔离）

TOP_K_CONTEXT = 5  # 提供给 LLM 的资料块数（编号 1~5，即引用校验的合法范围）


def check_inquiry_dedup(client, model, question: str) -> dict | None:
    """① 判重短路：命中 approved 标准答案（对称裸编码，阈值同源 thresholds.yaml）
    药品集合护栏：向量过线但药品集合不一致 → 不短路（guardrail 字段记原因，交检索+起草路径）"""
    if not client.has_collection("mi_inquiries"):
        return None
    vec = encode_symmetric(model, [question])
    hits = client.search(
        collection_name="mi_inquiries", data=vec, limit=1,
        filter='status == "approved"', output_fields=["question_std", "answer"],
    )[0]
    if not hits:
        return None
    score = hits[0]["distance"]
    matched_std = hits[0]["entity"]["question_std"]
    c = classify(score, question, matched_std,
                 _TH["inquiry"]["duplicate"], _TH["inquiry"]["new"])
    if c["verdict"] == "duplicate":
        return {"score": score, "question_std": matched_std,
                "answer": hits[0]["entity"]["answer"]}
    if c["guarded"]:
        return {"score": score, "question_std": matched_std, "answer": None,
                "guardrail": c["reason"]}  # 护栏拦截记录随 trace 留痕，不作为标准答案发出
    return None


def check_coverage(question: str, raw_results: list[dict]) -> list[str]:
    """⑤ 覆盖缺口：问题涉及的药品有没有 A/A- 级原文命中（与 query.py 同口径）"""
    drugs = extract_drugs(question)
    return [d for d in drugs if not any(
        d in r["source_title"] and r["authority"] in ("A", "A-") for r in raw_results
    )]


def write_trace(record: dict):
    """追加式留痕（只增不改）。生产化：下沉 MySQL answer_traces 审计表"""
    _TRACE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_TRACE_DIR / "answers.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(question: str, asker: str) -> dict:
    """主管道。返回 trace 记录（也是五种终态的判定结果）"""
    # PII 前置层：原始问询留审计，脱敏后文本给 LLM 和检索编码
    question_redacted, pii_entities = redact_pii(question)
    trace = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "asker": asker,
        "question": question,                       # 原始问询，审计侧可还原
        "question_redacted": question_redacted,     # 进 LLM / 编码的脱敏版本
        "pii_entities": [
            {"type": e.type, "value": e.value, "placeholder": e.placeholder}
            for e in pii_entities
        ],
        "llm": {"model": LLM_CONFIG["model"], "digest": model_digest(),
                "prompt_version": PROMPT_VERSION, "temperature": LLM_CONFIG["temperature"],
                "attempts": 0},
    }

    model = SentenceTransformer(_MODEL_PATH)
    client = MilvusClient(uri=MILVUS_URI)
    try:
        # ① 判重短路（护栏拦截的记录留痕后继续走检索路径）
        # 使用脱敏后文本判重：医学问询的重复性应由医学内容决定，不应依赖患者身份信息
        hit = check_inquiry_dedup(client, model, question_redacted)
        if hit and hit.get("answer"):
            trace.update(disposition="standard_answer", inquiry_hit=hit)
            return trace
        if hit and hit.get("guardrail"):
            trace["inquiry_guardrail"] = hit

        # 检索（非对称编码 + 权威融合，与 query.py 同路径；用脱敏文本避免 PII 污染编码）
        raw = search_all_collections(client, model, question_redacted)
        chunks = authority_fusion(raw, top_k=TOP_K_CONTEXT, role="general")
        trace["retrieved"] = [{"n": i, "collection": c["collection"],
                               "source_title": c["source_title"], "version": c.get("version", ""),
                               "authority": c["authority"], "score": round(c["score"], 4)}
                              for i, c in enumerate(chunks, 1)]

        # 资格闸（ANS-03 教训）：top-K 最高原始分低于 draft_min_top1 → 管道判拒，LLM 不出场。
        # 让 LLM 自判"资料够不够"不可靠（形式引用全合法仍硬答），守门责任收归确定性管道
        top1 = max((c["score"] for c in chunks), default=0.0)
        floor = _TH["generation"]["draft_min_top1"]
        if top1 < floor:
            trace.update(disposition="insufficient_evidence",
                         gate={"top1": round(top1, 4), "floor": floor},
                         note="检索置信不足，管道判拒（LLM 未出场）")
            return trace

        # ⑤ 覆盖缺口：不起草，明示升级（用脱敏文本抽药品，避免患者姓名被误当药品）
        missing = check_coverage(question_redacted, raw)
        if missing:
            trace.update(disposition="coverage_gap", missing_drugs=missing)
            return trace

        # ②③④ 起草 → 校验 → 有界重试（prompt 内不含 PII；草稿为 JSON 结构契约 gp-2.0）
        struct = draft_answer(question_redacted, chunks)
        result = validate_draft(struct, chunks)
        trace["llm"]["attempts"] = 1
        while result["disposition"] == "failed" and trace["llm"]["attempts"] <= LLM_CONFIG["max_retries"]:
            struct = redraft_with_feedback(question_redacted, chunks, struct["_raw"], result["errors"])
            result = validate_draft(struct, chunks)
            trace["llm"]["attempts"] += 1

        trace["draft"] = render_draft(struct)
        trace["validation"] = result
        trace["disposition"] = {"passed": "draft_passed",
                                "refusal": "insufficient_evidence",
                                "failed": "draft_failed_human"}[result["disposition"]]
        return trace
    finally:
        client.close()


def print_result(trace: dict):
    d = trace["disposition"]
    print("\n" + "=" * 60)
    if trace.get("pii_entities"):
        types = ", ".join(sorted({e["type"] for e in trace["pii_entities"]}))
        print(f"【PII 已脱敏】检测到 {types}，原始问询已留痕，LLM/检索使用脱敏文本")
    if d == "standard_answer":
        hit = trace["inquiry_hit"]
        print(f"【标准答案库命中】相似度 {hit['score']:.4f}（≥判重线，LLM 未出场）")
        print(f"标准问题: {hit['question_std']}")
        print(f"标准答复: {hit['answer']}")
    elif d == "coverage_gap":
        drugs = trace.get("missing_drugs") or []
        print(f"⚠️ 覆盖缺口{'：' + '、'.join(drugs) if drugs else '（检索零命中）'}")
        print("未起草答复。正式答复请升级医学官，并将缺口登记补库。")
    elif d == "insufficient_evidence":
        if "gate" in trace:
            g = trace["gate"]
            print(f"【管道判拒：检索置信不足】最高命中 {g['top1']:.4f} < 资格线 {g['floor']}，LLM 未出场")
            print("问题超出库内资料覆盖范围。请将缺口登记补库；正式答复请升级医学官。")
        else:
            print("【模型拒答：证据不足】守住了边界，不硬答。")
            print(trace["draft"])
            print("检索到的相关资料见 trace，可供人工判断是否补库。")
    else:
        print(trace["draft"])
        if d == "draft_failed_human":
            print("\n⚠️ 草稿经 %d 次尝试仍未通过引用校验：%s"
                  % (trace["llm"]["attempts"], "；".join(trace["validation"]["errors"])))
            print("已降级转人工——请直接基于检索原文起草（见 trace retrieved 段）。")
        else:
            print(f"\n（引用校验通过：{len(trace['validation']['citations'])} 处引用均合法，"
                  f"尝试 {trace['llm']['attempts']} 次）")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="MI 答复起草管道（辅助模式：草稿需人工复核后发出）")
    parser.add_argument("question", type=str, help="问题")
    parser.add_argument("--asker", default="anonymous", help="提问人（留痕用）")
    args = parser.parse_args()

    trace = run(args.question, args.asker)
    write_trace(trace)
    print_result(trace)
    print(f"trace 已留痕 → data/traces/answers.jsonl（disposition={trace['disposition']}）")


if __name__ == "__main__":
    main()
