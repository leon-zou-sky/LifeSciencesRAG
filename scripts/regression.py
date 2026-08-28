"""
黄金集回归脚本（CI 版质检门禁）

和 test_inquiries.py 的区别：那个是演示版（打印结果人眼看），
这个是断言版——期望写在 config/golden_set.json，机器自动判，失败退出码 1。

三类断言：
  ① 归一断言：探针必须提取出期望的药品
  ② 档位断言：top1 分数必须落在期望的判重档（duplicate/gray/new）
  ③ 漂移断言：分数与基线的偏差不得超过 tolerance（默认 ±0.03）

用法:
    python scripts/regression.py              # 跑全部探针，失败 exit 1
    python scripts/regression.py --record     # 首跑/变更通过后，把实测分数写回基线
    python scripts/regression.py --rebaseline # 换模型重标定后专用：档位断言照旧 enforce，
                                              # 漂移断言降级为提示（新模型分数必然漂移），
                                              # 通过后把新模型的实测分数录为新基线

生产形态：数据入库/模型升级后由 CI 或定时任务自动触发，失败卡住发布。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging

from pymilvus import MilvusClient
from sentence_transformers import SentenceTransformer

from src.normalization import extract_drugs, normalize_text
from src.thresholds import load_thresholds, models_dir
from src.encoding import encode_query
from src.dedup_guard import classify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MILVUS_URI = "http://localhost:19531"
_MODELS_DIR = models_dir()
_CONFIG = Path(__file__).resolve().parent.parent / "config" / "golden_set.json"

AUTHORITY_WEIGHT = {"A": 1.0, "A-": 0.97, "B": 0.95, "C": 0.9}
DOC_COLLECTIONS = ["drug_inserts", "guidelines", "clinical_papers", "case_reports"]


def verdict_of(score: float, dup: float, new: float) -> str:
    if score >= dup:
        return "duplicate"
    if score >= new:
        return "gray"
    return "new"


def check_probe(name: str, score: float, expect_verdict: str, baseline,
                tol: float, dup: float, new: float, failures: list,
                ignore_drift: bool = False, actual_verdict: str | None = None) -> str:
    # actual_verdict 由调用方算好传入（含药品集合护栏）；不传则退化为纯分数三档
    actual_verdict = actual_verdict or verdict_of(score, dup, new)
    status = "✅"
    if actual_verdict != expect_verdict:
        failures.append(f"{name}: 档位错误 期望 {expect_verdict} 实际 {actual_verdict}（{score:.4f}）")
        status = "❌"
    elif baseline is not None and abs(score - baseline) > tol:
        # --rebaseline 模式：换模型后分数必然漂移，降级为提示不判失败
        if not ignore_drift:
            failures.append(f"{name}: 分数漂移 {score:.4f} vs 基线 {baseline}（容差 ±{tol}）")
        status = "⚠️ 漂移"
    return f"  {status} {name[:28]:<30} score={score:.4f} 期望={expect_verdict:<9} 基线={baseline}"


def run_inquiry_probes(cfg, model, client, failures, ignore_drift=False) -> list:
    print("─" * 60)
    print("① 问询库探针（判重行为）")
    print("─" * 60)
    dup, new = cfg["thresholds"]["duplicate"], cfg["thresholds"]["new"]
    lines = []
    for p in cfg["inquiry_probes"]:
        # 断言①：归一
        drugs = extract_drugs(p["input"])
        if drugs != p["expect_drugs"]:
            failures.append(f"归一错误: '{p['input']}' 期望 {p['expect_drugs']} 实际 {drugs}")
            lines.append(f"  ❌ {p['input'][:28]:<30} 归一失败: {drugs}")
            continue
        # 断言②③：检索档位 + 漂移（先归一再编码——和入库管道同一步骤，否则探针测的不是一条路径）
        vec = model.encode([normalize_text(p["input"])], normalize_embeddings=True).tolist()
        hits = client.search(
            collection_name="mi_inquiries", data=vec, limit=1,
            filter='status == "approved"', output_fields=["question_std"],
        )[0]
        score = hits[0]["distance"] if hits else 0.0
        top1 = hits[0]["entity"]["question_std"] if hits else ""
        if p["expect_top1"] and top1 != p["expect_top1"]:
            failures.append(f"top1 易主: '{p['input']}' 期望命中 '{p['expect_top1']}' 实际 '{top1}'")
        # 档位断言走统一 classify（含药品集合护栏）——与 ingest/answer 生产路径同一判定函数，
        # 终审测的就是上线后的真实行为，不是裸分数映射
        c = classify(score, normalize_text(p["input"]), top1, dup, new)
        lines.append(check_probe(p["input"], score, p["expect_verdict"],
                                 p["baseline_score"], cfg["tolerance"], dup, new, failures,
                                 ignore_drift, actual_verdict=c["verdict"]))
        p["_actual_score"] = round(score, 4)
    return lines


def run_document_probes(cfg, model, client, failures, ignore_drift=False) -> list:
    print("─" * 60)
    print("② 文档库探针（检索行为）")
    print("─" * 60)
    lines = []
    for p in cfg["document_probes"]:
        vec = encode_query(model, [p["input"]])  # 文档检索为非对称：查询侧加 BGE 指令前缀
        best = None
        for coll in DOC_COLLECTIONS:
            if not client.has_collection(coll):
                continue
            hits = client.search(
                collection_name=coll, data=vec, limit=3,
                output_fields=["source_title", "authority", "version"],
            )[0]
            for h in hits:
                w = h["distance"] * AUTHORITY_WEIGHT[h["entity"]["authority"]]
                if best is None or w > best[1]:
                    best = (h, w)
        if best is None:
            failures.append(f"文档探针无结果: '{p['input']}'")
            continue
        h, _ = best
        score = h["distance"]
        src = h["entity"]["source_title"]
        if p["expect_top1_source"] not in src:
            failures.append(f"文档探针 top1 错误: '{p['input']}' 期望来源含 '{p['expect_top1_source']}' 实际 '{src}'")
        ver = h["entity"]["version"]
        if p.get("expect_version") and ver != p["expect_version"]:
            failures.append(
                f"文档探针版本错误: '{p['input']}' 期望 v{p['expect_version']} 实际 v{ver}"
                "（命中了作废旧版——过期口径泄漏）"
            )
        line = f"  score={score:.4f} top1=[{h['entity']['authority']}] {src} v{ver}"
        if p["baseline_score"] is not None and abs(score - p["baseline_score"]) > cfg["tolerance"]:
            if not ignore_drift:  # --rebaseline：换模型后漂移是必然，只提示不判失败
                failures.append(f"文档探针漂移: '{p['input']}' {score:.4f} vs 基线 {p['baseline_score']}")
            line = "  ⚠️ 漂移" + line
        elif not any(p["input"] in f for f in failures):
            line = "  ✅ " + p["input"][:28] + line
        lines.append(line)
        p["_actual_score"] = round(score, 4)
    return lines


def main():
    parser = argparse.ArgumentParser(description="黄金集回归")
    parser.add_argument("--record", action="store_true", help="把实测分数写回基线（仅回归通过后使用）")
    parser.add_argument("--rebaseline", action="store_true",
                        help="换模型重标定后专用：漂移降级为提示，通过后录新基线（隐含 --record）")
    args = parser.parse_args()

    cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
    # 阈值与漂移容差的唯一事实源是 config/thresholds.yaml，此处注入 cfg 供探针使用
    th = load_thresholds()
    cfg["tolerance"] = th["tolerance"]
    cfg["thresholds"] = {"duplicate": th["inquiry"]["duplicate"], "new": th["inquiry"]["new"]}
    # 模型版本一致性校验：黄金集基线只在记录它的模型下有可比性（答案可复现性）。
    # 黄金集 model 字段、thresholds.yaml model 字段、实际模型目录三者必须一致，否则基线断言无意义。
    model_name = cfg.get("model")
    if not model_name:
        print("❌ golden_set.json 缺少 model 字段——基线必须标注记录时的模型版本")
        sys.exit(1)
    if th.get("model") and th["model"] != model_name:
        print(f"❌ 模型版本不一致：golden_set={model_name} vs thresholds.yaml={th['model']}\n"
              f"   换模型后必须先走重标定流程（建影子→标定→改阈值→--record），而不是直接回归")
        sys.exit(1)
    _MODEL_PATH = str(_MODELS_DIR / model_name)
    model = SentenceTransformer(_MODEL_PATH)
    client = MilvusClient(uri=MILVUS_URI)

    failures: list = []
    lines = run_inquiry_probes(cfg, model, client, failures, args.rebaseline)
    lines += run_document_probes(cfg, model, client, failures, args.rebaseline)
    print("\n".join(lines))

    print("=" * 60)
    if failures:
        print(f"❌ 回归失败 {len(failures)} 项:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    n = len(cfg["inquiry_probes"]) + len(cfg["document_probes"])
    if args.rebaseline:
        print(f"✅ 重基线通过：{n} 条探针档位全部符合期望（漂移已按新模型豁免）")
    else:
        print(f"✅ 回归通过：{n} 条探针全部符合期望，无漂移")
    if args.record or args.rebaseline:
        if args.rebaseline:
            from datetime import date
            cfg["_baseline_recorded"] = (
                f"{date.today().isoformat()}，重基线（换模型重标定后重录），模型={cfg['model']}"
            )
        for group in ("inquiry_probes", "document_probes"):
            for p in cfg[group]:
                if "_actual_score" in p:
                    p["baseline_score"] = p.pop("_actual_score")
        # 注入的阈值/容差不回写——它们归 thresholds.yaml 管，黄金集只存探针与基线
        out = {k: v for k, v in cfg.items() if k not in ("thresholds", "tolerance")}
        _CONFIG.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  基线已更新（--record）")


if __name__ == "__main__":
    main()
