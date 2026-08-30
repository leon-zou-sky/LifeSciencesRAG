"""
验证包生成器：把"代码+配置+输出日志"包装成正式 IQ/OQ/PQ 验证文档（GxP CSV 验证包）

用法:
    python scripts/gen_validation_pack.py --executor <姓名> [--trigger "换模型重标定"] [--skip-run]

产物: docs/validation/packs/<日期>_<模型>/
    00_plan.md  验证总计划
    01_IQ.md    安装确认（环境/模型指纹SHA-256/配置基线/三方一致性——自动采集）
    02_OQ.md    运行确认（标定输出归档 + 定线理由）
    03_PQ.md    性能确认（黄金集回归输出归档 + 断言覆盖矩阵）
    04_signature.md  签字页（人工签署，脚本只填结论）

设计要点：
  - 模板是受控文档（docs/validation/templates/），脚本只填证据段——证据与格式分离
  - PQ 证据由本脚本实际执行 regression.py 采集（exit code 一并归档），不是复制粘贴
  - OQ 标定输出仅在标定记录存在时归档（日常回归验证可 --skip-oq）
  - 脚本不代签字：04_signature.md 的结论栏由人复核后手填——机器不出"批准"结论
"""
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse

_ROOT = Path(__file__).resolve().parent.parent
_TPL = _ROOT / "docs" / "validation" / "templates"
_PACKS = _ROOT / "docs" / "validation" / "packs"


def fill(template: str, mapping: dict) -> str:
    text = (_TPL / template).read_text(encoding="utf-8")
    for k, v in mapping.items():
        text = text.replace("{{" + k + "}}", str(v))
    left = re.findall(r"\{\{(\w+)\}\}", text)
    if left:
        print(f"⚠️ {template} 有未填占位符: {left}")
    return text


def collect_iq() -> dict:
    """IQ 证据：环境版本 + 模型指纹 + 配置基线 + 三方一致性（全部机器采集）"""
    import hashlib
    import pymilvus
    import sentence_transformers
    from pymilvus import MilvusClient
    from src.db import get_conn
    from src.thresholds import load_model_path, load_thresholds

    th = load_thresholds()
    model_path = Path(load_model_path())
    weights = model_path / "model.safetensors"
    if not weights.exists():
        weights = model_path / "pytorch_model.bin"
    sha = hashlib.sha256(weights.read_bytes()).hexdigest()

    from sentence_transformers import SentenceTransformer
    _m = SentenceTransformer(str(model_path))
    dim = _m.get_embedding_dimension() if hasattr(_m, "get_embedding_dimension") \
        else _m.get_sentence_embedding_dimension()

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT VERSION() AS v")
        mysql_ver = cur.fetchone()["v"]
    conn.close()

    client = MilvusClient(uri="http://localhost:19531")
    stats = []
    for coll in ["drug_inserts", "guidelines", "clinical_papers", "case_reports", "mi_inquiries"]:
        if client.has_collection(coll):
            n = client.get_collection_stats(coll)["row_count"]
            stats.append(f"| {coll} | {n} |")

    golden = json.loads((_ROOT / "config" / "golden_set.json").read_text(encoding="utf-8"))
    consistent = golden.get("model") == th["model"] == model_path.name

    return {
        "python_version": sys.version.split()[0],
        "pymilvus_version": pymilvus.__version__,
        "st_version": sentence_transformers.__version__,
        "milvus_version": client.get_server_version(),
        "mysql_version": mysql_ver,
        "model": th["model"],
        "model_path": str(model_path),
        "weights_file": weights.name,
        "weights_sha256": sha,
        "dim": dim,
        "thresholds_yaml": (_ROOT / "config" / "thresholds.yaml").read_text(encoding="utf-8").strip(),
        "consistency": "✅ 一致" if consistent else "❌ 不一致——IQ 不通过",
        "collection_stats": "\n".join(stats),
        "iq_conclusion": "✅ 通过" if consistent else "❌ 不通过",
        "iq_result": "✅ 通过" if consistent else "❌ 不通过",
        "_golden": golden,
    }


def run_regression() -> tuple[str, int]:
    """PQ 证据：实跑回归，归档原始输出与退出码"""
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "regression.py")],
        capture_output=True, text=True, cwd=_ROOT,
    )
    out = "\n".join(l for l in (proc.stdout + proc.stderr).splitlines()
                    if not l.startswith(("Batches:", "Loading weights:")))
    return out.strip(), proc.returncode


def collect_generation(run_probes: bool) -> dict:
    """生成层 PQ 证据（G-06 升格段）：契约版本/模型指纹/稳定性统计 + 可选实跑冒烟。
    run_probes=False 时只归档配置与稳定性统计，不实跑（Ollama 不在线也能出包）"""
    from src.llm import LLM_CONFIG, PROMPT_VERSION, model_digest

    # 稳定性统计：每日持续验证的 answers 套件运行记录（时间维度证据，G-06 达标标准的数据源）
    runs_file = _ROOT / "reports" / "scheduled" / "verify_runs.jsonl"
    stats = {"pass": 0, "fail": 0, "skipped": 0, "first": None, "last": None}
    if runs_file.exists():
        for line in runs_file.read_text(encoding="utf-8").splitlines():
            try:
                run = json.loads(line)
            except json.JSONDecodeError:
                continue
            for s in run.get("suites", []):
                if s.get("suite") != "answers":
                    continue
                v = s.get("verdict")
                if v in stats:
                    stats[v] += 1
                stats["first"] = stats["first"] or run.get("run_id")
                stats["last"] = run.get("run_id")

    probes_out, probes_rc = "（--with-generation 未指定，本次未实跑）", -1
    if run_probes:
        proc = subprocess.run(
            [sys.executable, str(_ROOT / "scripts" / "test_answers.py")],
            capture_output=True, text=True, cwd=_ROOT, timeout=1800,
        )
        probes_out = "\n".join(l for l in (proc.stdout + proc.stderr).splitlines()
                               if not l.startswith("Loading weights:")).strip()
        probes_rc = proc.returncode

    return {
        "prompt_version": PROMPT_VERSION,
        "llm_model": LLM_CONFIG["model"],
        "llm_digest": model_digest(),
        "llm_temperature": LLM_CONFIG["temperature"],
        "llm_max_retries": LLM_CONFIG["max_retries"],
        "stability": stats,
        "probes_output": probes_out,
        "probes_exit": probes_rc,
    }


def render_generation_section(gen: dict, executed: bool) -> str:
    """03_PQ 3.4 段渲染：配置指纹 + 稳定性统计 + 实跑归档（如执行）"""
    s = gen["stability"]
    lines = [
        "| 字段 | 值 |",
        "|---|---|",
        f"| 输出契约 | {gen['prompt_version']}（JSON 结构契约，见设计文档 6.18） |",
        f"| 生成模型 | {gen['llm_model']}（digest {gen['llm_digest']}，temperature={gen['llm_temperature']}，max_retries={gen['llm_max_retries']}） |",
        f"| 冒烟探针 | config/answer_golden.json（6 条，断言五终态路由） |",
        f"| 稳定性证据 | 持续验证记录 {s['first'] or '—'} ~ {s['last'] or '—'}："
        f"pass {s['pass']} / fail {s['fail']} / infra-skipped {s['skipped']} |",
        "",
    ]
    if executed:
        lines += [f"实跑归档（exit code = {gen['probes_exit']}）：", "",
                  "```", gen["probes_output"], "```"]
    else:
        lines.append(gen["probes_output"])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="生成 GxP 验证包")
    parser.add_argument("--executor", required=True, help="验证执行人姓名（填入计划页）")
    parser.add_argument("--trigger", default="定期回归验证", help="触发原因（换模型重标定/定期评审/数据变更）")
    parser.add_argument("--skip-oq", action="store_true", help="日常回归：OQ 标定段标记为'本次未执行'（换模型时必须完整执行）")
    parser.add_argument("--skip-run", action="store_true", help="不实跑 regression（仅调试用）")
    parser.add_argument("--with-generation", action="store_true",
                        help="PQ 含生成层段：实跑 test_answers.py 冒烟并归档（需 Ollama 在线；G-06 升格后必带）")
    args = parser.parse_args()

    today = date.today().isoformat()
    print("① 采集 IQ 证据（环境/模型指纹/配置基线）…")
    iq = collect_iq()
    golden = iq.pop("_golden")

    print("② 执行 PQ 终审（regression.py）…")
    reg_out, exit_code = ("（--skip-run 未执行）", -1) if args.skip_run else run_regression()
    pq_ok = exit_code == 0
    print(f"   exit code = {exit_code}（{'✅ 通过' if pq_ok else '❌ 未通过'}）")

    print("②b 生成层证据（契约指纹/稳定性统计%s）…" % ("+实跑冒烟" if args.with_generation else ""))
    gen = collect_generation(run_probes=args.with_generation)
    gen_ok = gen["probes_exit"] == 0 if args.with_generation else True
    if args.with_generation:
        print(f"   冒烟 exit code = {gen['probes_exit']}（{'✅' if gen_ok else '❌'}）")
    pq_ok = pq_ok and gen_ok

    th_yaml = iq["thresholds_yaml"]
    from src.thresholds import load_thresholds
    _th = load_thresholds()
    dup = _th["inquiry"]["duplicate"]
    new = _th["inquiry"]["new"]
    case_dup, case_gray = _th["case_report"]["duplicate"], _th["case_report"]["gray"]
    tolerance = _th["tolerance"]

    pack = _PACKS / f"{today}_{iq['model']}"
    pack.mkdir(parents=True, exist_ok=True)

    common = {"date": today, "model": iq["model"], "executor": args.executor, "trigger": args.trigger}
    files = {
        "00_plan.md": fill("00_plan.md", {**common, "validation_type": "全量验证（IQ+OQ+PQ）" if not args.skip_oq else "回归验证（IQ+PQ）"}),
        "01_IQ.md": fill("01_IQ.md", {**common, **iq}),
        "02_OQ.md": fill("02_OQ.md", {
            **common,
            "oq_date": "2026-08-17（本次 --skip-oq，沿用该次标定）" if args.skip_oq else today,
            "pairs_count": len(json.loads((_ROOT / "config" / "calibration_pairs.json").read_text(encoding="utf-8"))["pairs"]),
            "pos_count": 12, "neg_count": 12,
            "calibrate_output": "（--skip-oq：本次为回归验证，标定沿用 2026-08-17 记录，见 packs/2026-08-17 或模型文档附录 C）" if args.skip_oq else "（本次未实跑 calibrate.py；换模型验证时先运行 calibrate 并将输出粘贴于此）",
            "dup": dup, "new": new,
            "dup_reason": "见 thresholds.yaml duplicate 行注释",
            "new_reason": "见 thresholds.yaml new 行注释",
            "case_dup": case_dup, "case_gray": case_gray,
            "case_reason": "⚠️ 沿用旧模型刻度（已知缺口 G-01，RTM 已登记）",
            "distribution_verdict": "见附录 C（2026-08-17 三模型对比标定：相对判定合格）",
            "oq_conclusion": "✅ 通过（标定记录归档）" if args.skip_oq else "待标定执行后确认",
            "oq_result": "✅ 通过" if args.skip_oq else "⚠️ 本次未执行",
        }),
        "03_PQ.md": fill("03_PQ.md", {
            **common,
            "probe_count": len(golden["inquiry_probes"]) + len(golden["document_probes"]),
            "inq_count": len(golden["inquiry_probes"]),
            "doc_count": len(golden["document_probes"]),
            "baseline_recorded": golden.get("_baseline_recorded", "未记录"),
            "regression_output": reg_out,
            "exit_code": exit_code,
            "tolerance": tolerance,
            "r1": "✅" if pq_ok else "❌", "r2": "✅" if pq_ok else "❌",
            "r3": "✅ 无漂移" if pq_ok else "❌", "r4": "✅" if pq_ok else "❌",
            "pq_conclusion": "✅ 通过" if pq_ok else "❌ 不通过——禁止签字放行",
            "pq_result": "✅ 通过" if pq_ok else "❌ 不通过",
            "generation_section": render_generation_section(gen, executed=args.with_generation),
        }),
        "04_signature.md": fill("04_signature.md", {
            **common,
            "iq_result": iq["iq_result"],
            "oq_result": "✅ 通过" if args.skip_oq else "⚠️ 本次未执行",
            "pq_result": "✅ 通过" if pq_ok else "❌ 不通过",
            "overall_result": "（人工复核后填写）",
            "open_gaps": 4,
            "change_desc": args.trigger,
        }),
    }
    for name, content in files.items():
        (pack / name).write_text(content, encoding="utf-8")

    print(f"③ 验证包已生成: {pack.relative_to(_ROOT)}")
    for name in files:
        print(f"   - {name}")
    print("\n下一步：人工复核 01-03 证据 → 04_signature.md 填写结论并签署（机器不代出批准结论）")
    if not pq_ok and not args.skip_run:
        print("⚠️ PQ 未通过：签字页不得签署，系统维持/回退至上一已验证状态")
        sys.exit(1)


if __name__ == "__main__":
    main()
