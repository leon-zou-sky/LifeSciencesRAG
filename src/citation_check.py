"""
引用校验器：生成层的确定性断言（机器可判的部分全在这里，见设计文档 8.x 思路）

gp-2.0 起草稿为 JSON 结构契约（src/llm.py parse_draft 的输出），
断言从"自由文本正则"升级为"结构化字段校验"：

  0. 结构合法性：JSON 解析失败/不符合契约 → 硬失败（进有界重试）
  1. 覆盖核对一致性（gp-2.3）：coverage 字段是模型逐药核对的结果——
     任何一个药品标 false 却 refusal=false = 明知缺资料还起草，机器可判的硬失败；
     coverage 为空（非拒答）= 跳过核对步骤，同样硬失败
  2. 拒答判定：refusal=true → 合法拒答（不算失败，算守住了边界），但 reason 必填
     且 statements 必须为空（拒答和起草是互斥终态，两头占 = 契约违反）
  3. 引用存在性：非拒答草稿每条陈述的 citations 必须非空
  4. 引用编号合法性：每个编号必须落在本次提供给模型的资料编号范围内
     （模型编造不存在的编号 = 幻觉的可探测形态，硬失败）
  5. 输出契约完整性："草稿需复核"后缀 gp-2.0 起改由渲染层强制附加（render_draft），
     不再依赖模型输出——边界标记是管道行为，此处无需断言

附：被引块必为现行版这一点由检索层结构性保证（影子只装 status='现行' 的块，
编号又强制映射回本次提供的块列表），故此处无需重复断言。

不能自动判的（语义忠实度：句子是否真的忠于引用原文）留给人复核——
所以签字页和"草稿需复核"后缀永远存在，机器不越这条线。
"""


def validate_draft(struct: dict, chunks: list[dict]) -> dict:
    """
    校验一条结构化草稿（src/llm.py parse_draft 的输出）。返回 {
        disposition: "refusal" | "passed" | "failed",
        citations:   [引用的编号...],
        errors:      [错误描述...],
    }
    disposition 语义：
      refusal —— 模型判证据不足拒答（守住边界，管道按拒答流程走，不算校验失败）
      passed  —— 全部断言通过，可交人复核
      failed  —— 断言未过，进入重试或降级转人工
    """
    if struct.get("_parse_error"):
        return {"disposition": "failed", "citations": [],
                "errors": [struct["_parse_error"]]}

    if struct.get("refusal"):
        errors = []
        if not struct.get("reason"):
            errors.append("拒答但未给原因——拒答理由是人复核时的判断依据，必填")
        if struct.get("statements"):
            errors.append("refusal=true 但 statements 非空——拒答与起草是互斥终态，不得两头占")
        if errors:
            return {"disposition": "failed", "citations": [], "errors": errors}
        return {"disposition": "refusal", "citations": [], "errors": []}

    errors = []

    # 覆盖核对一致性（gp-2.3）：模型自己核对出缺药却不起 refusal = 明知故犯，机器可判
    coverage = struct.get("coverage") or {}
    if not coverage:
        errors.append("缺少 coverage 逐药核对——起草前必须先核对问题涉及的每个药品")
    else:
        uncovered = [d for d, covered in coverage.items() if not covered]
        if uncovered:
            errors.append(f"coverage 显示 {uncovered} 无资料覆盖但未拒答——"
                          "明知缺资料还起草，比漏判更严重")

    statements = struct.get("statements") or []
    if not statements:
        errors.append("非拒答但没有任何陈述——空草稿不得交人复核")

    cited = []
    for i, s in enumerate(statements, 1):
        if not s.get("text"):
            errors.append(f"第 {i} 条陈述 text 为空")
        cites = s.get("citations") or []
        if not cites:
            errors.append(f"第 {i} 条陈述没有任何引用——事实性陈述无法溯源")
        bad = [n for n in cites if n < 1 or n > len(chunks)]
        if bad:
            errors.append(f"第 {i} 条陈述引用了不存在的资料编号 {sorted(set(bad))}"
                          f"（本次仅提供 1~{len(chunks)}）——疑似幻觉引用")
        cited.extend(cites)

    return {
        "disposition": "failed" if errors else "passed",
        "citations": sorted(set(cited)),
        "errors": errors,
    }


# ── 文本模式（中间件用）────────────────────────────────────────────
# scripts/validate_citation.py 是可独立交付的断言中间件，挂在任意 RAG 输出后——
# 它面对的不是本管道的 JSON 契约，而是任意自由文本草稿，故保留文本校验路径。
import re

from src.llm import DRAFT_SUFFIX, REFUSAL_MARKER

_CITATION_RE = re.compile(r"【(\d+)】")


def validate_draft_text(draft: str, chunks: list[dict]) -> dict:
    """自由文本草稿的引用校验（中间件形态）。返回结构同 validate_draft。"""
    text = draft.strip()

    if text.startswith(REFUSAL_MARKER):
        return {"disposition": "refusal", "citations": [], "errors": []}

    errors = []
    cited = [int(m) for m in _CITATION_RE.findall(text)]

    if not cited:
        errors.append("草稿没有任何【n】引用——事实性陈述无法溯源")

    bad = [n for n in cited if n < 1 or n > len(chunks)]
    if bad:
        errors.append(f"引用了不存在的资料编号 {sorted(set(bad))}"
                      f"（本次仅提供 1~{len(chunks)}）——疑似幻觉引用")

    if DRAFT_SUFFIX not in text:
        errors.append("缺少「草稿需复核」后缀——辅助模式边界标记丢失，不得发出")

    return {
        "disposition": "failed" if errors else "passed",
        "citations": sorted(set(cited)),
        "errors": errors,
    }
