"""
引用合法性断言中间件（可独立交付形态）

不依赖完整 answer.py 管道，可挂在任意 RAG 的输出后做最后一道形式校验：
  - 拒答标记检查
  - 引用存在性
  - 引用编号越界（幻觉引用可探测形态）
  - 草稿后缀完整性

用法：
    # 单条文本校验
    python scripts/validate_citation.py --draft "二甲双胍可导致乳酸酸中毒【1】（草稿：需医学信息专员复核后发出）" --chunks 5

    # 批量校验 JSONL（每行 {"draft": "...", "chunks": [...]}）
    python scripts/validate_citation.py --batch data/traces/drafts_to_check.jsonl

    # 作为过滤器：只输出通过的记录
    python scripts/validate_citation.py --batch drafts.jsonl --passed-only

退出码：
    0 —— 全部通过或仅拒答
    1 —— 存在 failed 记录
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.citation_check import validate_draft_text as validate_draft


def _fmt(result: dict, draft: str = "") -> str:
    disp = result["disposition"]
    icon = {"passed": "✔", "refusal": "⊘", "failed": "✘"}.get(disp, "?")
    lines = [f"{icon} {disp}"]
    if draft:
        preview = draft.replace("\n", " ")[:80] + ("..." if len(draft) > 80 else "")
        lines.append(f"   draft: {preview}")
    if result["citations"]:
        lines.append(f"   citations: {result['citations']}")
    if result["errors"]:
        lines.append(f"   errors: {result['errors']}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="引用合法性断言中间件")
    parser.add_argument("--draft", help="待校验的单条草稿")
    parser.add_argument("--chunks", type=int, default=5,
                        help="本次提供给模型的资料块数量（编号合法范围 1~N）")
    parser.add_argument("--batch", help="批量校验 JSONL 文件路径")
    parser.add_argument("--passed-only", action="store_true",
                        help="批量模式下只输出 passed 的记录")
    args = parser.parse_args()

    if args.batch:
        path = Path(args.batch)
        if not path.exists():
            print(f"文件不存在: {path}", file=sys.stderr)
            sys.exit(1)
        total = passed = refusal = failed = 0
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            rec = json.loads(line)
            draft = rec.get("draft", "")
            chunks = rec.get("chunks", [])
            result = validate_draft(draft, chunks)
            total += 1
            if result["disposition"] == "passed":
                passed += 1
            elif result["disposition"] == "refusal":
                refusal += 1
            else:
                failed += 1
            if not args.passed_only or result["disposition"] == "passed":
                print(_fmt(result, draft))
        print(f"\n汇总: total={total} passed={passed} refusal={refusal} failed={failed}")
        sys.exit(1 if failed else 0)

    if args.draft is None:
        parser.error("--draft 或 --batch 至少指定一个")
    result = validate_draft(args.draft, [{}] * args.chunks)
    print(_fmt(result, args.draft))
    sys.exit(0 if result["disposition"] in ("passed", "refusal") else 1)


if __name__ == "__main__":
    main()
