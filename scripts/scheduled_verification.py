"""
持续验证调度器：时间维度的再验证触发器（总纲 3.4 层）

变更时验证（改 prompt/阈值/模型必须重跑探针）只能抓住"有人动了系统"；
时间维度的漂移——容器重启后数据异常、模型文件被动过、阈值被绕开流程改了——
只有定期重跑才能抓到。本脚本就是那个"定期"：

  - 依次执行五个验证套件，以各自 exit code 判定（0=绿 1=红 2=基础设施不可达），
    不解析输出文本——exit code 是唯一契约
  - 前置基础设施检查（MySQL/Milvus/Ollama）：服务级探测（真实执行一次
    查询/列表/HTTP 请求），不是只探 TCP 端口——容器重启窗口里端口转发
    先于服务进程就绪，只探 TCP 会把"重启中"误判为"在线"，套件随后真实
    连接超时被判 fail，infra 过渡态被误报成 red（2026-09-09/09-10 实测）。
    依赖不可达的套件记 skipped，skipped ≠ green——全绿报告必须意味着
    每一项真的跑过
  - 三态总结论：green（全过）/ red（有失败）/ incomplete（无失败但有跳过）
    → exit 0 / 1 / 2（与 reconcile_thresholds.py 语义对齐）
  - 留痕：reports/scheduled/verify_runs.jsonl 追加一行摘要 + 每套件完整输出
    落 <run_id>.log（reports/ 是证据目录，git 排除）
  - 告警：red 或 incomplete 时发 macOS 桌面通知。机器只报告"验证失败，
    需人工处置"，不自动重跑/回滚/修阈值——自动修复动作永远是人发起
    （总纲 4.2 铁律：机器不出批准结论）

定时执行由 launchd 承载（scripts/install_daily_verify.sh 安装），
本脚本本身无任何调度逻辑，手动跑与定时跑行为一致。
"""
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # src/ 可导入（探测函数用 src.db）
_REPORT_DIR = _ROOT / "reports" / "scheduled"

# (名称, 脚本, 依赖的基础设施, 超时秒)
# test_answers 6 条探针每条最多 3 轮 LLM 调用，4b 模型本地推理给足余量
_SUITES = [
    ("reconcile",  "scripts/reconcile_thresholds.py", {"mysql"},          120),
    ("regression", "scripts/regression.py",           {"milvus"},         900),
    ("cases",      "scripts/test_cases.py",           {"milvus"},         600),
    ("pii",        "scripts/test_pii.py",             set(),              120),
    ("answers",    "scripts/test_answers.py",         {"milvus", "ollama"}, 1800),
]

# 基础设施探测：服务级 readiness，超时要短——探测不是压测。
# 只探 TCP 端口不够：容器重启/预热窗口里端口已监听但服务还不能响应，
# 必须真实执行一次最小请求（SELECT 1 / list_collections / HTTP GET）才算在线
def _probe_mysql() -> bool:
    try:
        from src.db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False


def _probe_milvus() -> bool:
    try:
        from pymilvus import MilvusClient
        client = MilvusClient(uri="http://localhost:19531", timeout=5)
        client.list_collections()
        return True
    except Exception:
        return False


def _probe_ollama() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


_INFRA_PROBES = {
    "mysql": _probe_mysql,
    "milvus": _probe_milvus,
    "ollama": _probe_ollama,
}


def probe_infra(name: str) -> bool:
    return _INFRA_PROBES[name]()


def run_suite(name: str, script: str, timeout: int) -> dict:
    """以子进程跑套件，exit code 即结论；完整输出由调用方落盘"""
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(_ROOT / script)],
            capture_output=True, text=True, timeout=timeout, cwd=_ROOT,
        )
        rc, output = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        rc, output = -1, (e.stdout or "") + (e.stderr or "") + f"\n[超时 {timeout}s 被杀]"
    result = {
        "suite": name,
        "exit_code": rc,
        "duration_s": round(time.time() - t0, 1),
        "verdict": "pass" if rc == 0 else ("infra" if rc == 2 else "fail"),
    }
    return result, output


def notify(title: str, body: str) -> None:
    """macOS 桌面通知；通知失败不阻断（告警通道挂了不能拖死验证本身）"""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            timeout=10, capture_output=True,
        )
    except Exception:
        pass


def main() -> None:
    only = set(sys.argv[1:])  # 可选：只跑指定套件，如 python scripts/scheduled_verification.py reconcile pii
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)

    infra = {name: probe_infra(name) for name in _INFRA_PROBES}
    down = [k for k, up in infra.items() if not up]
    print(f"持续验证 {run_id}  基础设施: " +
          ", ".join(f"{k}={'✓' if v else '✗ 不可达'}" for k, v in infra.items()))

    results, logs = [], []
    for name, script, deps, timeout in _SUITES:
        if only and name not in only:
            continue
        missing = deps - {k for k, up in infra.items() if up}
        if missing:
            results.append({"suite": name, "exit_code": None, "duration_s": 0,
                            "verdict": "skipped", "reason": f"依赖不可达: {sorted(missing)}"})
            print(f"  ⏭ {name:10s} skipped（依赖不可达: {', '.join(sorted(missing))}）")
            continue
        result, output = run_suite(name, script, timeout)
        results.append(result)
        logs.append(f"{'=' * 70}\n[{run_id}] suite={name} exit={result['exit_code']}\n{output}")
        mark = {"pass": "✅", "fail": "❌", "infra": "⚠️"}[result["verdict"]]
        print(f"  {mark} {name:10s} {result['verdict']:6s} exit={result['exit_code']} ({result['duration_s']}s)")

    fails = [r for r in results if r["verdict"] == "fail"]
    skips = [r for r in results if r["verdict"] in ("skipped", "infra")]
    overall = "red" if fails else ("incomplete" if skips else "green")
    exit_code = {"green": 0, "red": 1, "incomplete": 2}[overall]

    summary = {
        "run_id": run_id, "overall": overall, "exit_code": exit_code,
        "infra": infra, "suites": results,
        "note": "机器只报告不处置——red/incomplete 需人工按 runbook 排查" if exit_code else "",
    }
    with open(_REPORT_DIR / "verify_runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    if logs:
        (_REPORT_DIR / f"{run_id}.log").write_text("\n\n".join(logs), encoding="utf-8")

    print(f"总结论: {overall}（exit {exit_code}）  留痕: reports/scheduled/{run_id}.log")
    if exit_code:
        bad = "、".join(r["suite"] for r in (fails or skips))
        notify("RAG 持续验证异常", f"{overall}: {bad} 需人工处置")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
