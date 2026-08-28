"""
引用校验器：生成层的确定性断言（机器可判的部分全在这里，见设计文档 8.x 思路）

LLM 输出是自由文本没法数值比对，验证手段从"比分漂移"换成"断言检查"。
本模块把能自动判的全判掉：

  1. 拒答判定：输出以【证据不足】开头 → 合法拒答（不算失败，算守住了边界）
  2. 引用存在性：非拒答草稿必须至少有一条【n】引用
  3. 引用编号合法性：每个【n】必须落在本次提供给模型的资料编号范围内
     （模型编造不存在的编号 = 幻觉的可探测形态，硬失败）
  4. 输出契约完整性：草稿必须以"需医学信息专员复核"后缀结尾——
     后缀在 = 辅助模式边界在（草稿不会被误当正式答复直接发出）

附：被引块必为现行版这一点由检索层结构性保证（影子只装 status='现行' 的块，
编号又强制映射回本次提供的块列表），故此处无需重复断言。

不能自动判的（语义忠实度：句子是否真的忠于引用原文）留给人复核——
所以签字页和"草稿需复核"后缀永远存在，机器不越这条线。
"""
import re

from src.llm import REFUSAL_MARKER

_CITATION_RE = re.compile(r"【(\d+)】")
_DRAFT_SUFFIX = "（草稿：需医学信息专员复核后发出）"


def validate_draft(draft: str, chunks: list[dict]) -> dict:
    """
    校验一条草稿。返回 {
        disposition: "refusal" | "passed" | "failed",
        citations:   [引用的编号...],
        errors:      [错误描述...],
    }
    disposition 语义：
      refusal —— 模型判证据不足拒答（守住边界，管道按拒答流程走，不算校验失败）
      passed  —— 全部断言通过，可交人复核
      failed  —— 断言未过，进入重试或降级转人工
    """
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

    if _DRAFT_SUFFIX not in text:
        errors.append("缺少「草稿需复核」后缀——辅助模式边界标记丢失，不得发出")

    return {
        "disposition": "failed" if errors else "passed",
        "citations": sorted(set(cited)),
        "errors": errors,
    }
