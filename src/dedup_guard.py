"""
药品集合规则护栏（模型文档 A.5⑦ / RTM REQ-01 / 缺口 G-04 的关闭实现）

问题（纯向量判重的物理上限，标定实测）：一字之差的难负样本
  「阿托伐他汀可以和红霉素联用吗」 vs 「阿托伐他汀可以和克拉霉素联用吗」
语义结构几乎同构，余弦相似度 0.9585——任何向量模型的 duplicate 线都压不过它，
否则真重复（同义改写）也会被错杀。这是表征学习的固有极限，不是调参能解决的。

但这两个问题的**药品集合不同**（{阿托伐他汀,红霉素} ≠ {阿托伐他汀,克拉霉素}），
而"标准答案是否相同"恰恰主要取决于药品集合——这是规则能 100% 判定的维度。

护栏规约：
  - score ≥ duplicate 且药品集合不一致 → 强制降灰色带（人工闸），永不自动判重
  - 护栏只拦不抬：不会把低分问题提拔成 duplicate，只否决向量的"自动"资格
  - 任一侧识别不出药品 → "unknown"（无法裁决），维持向量判定，不干预
    （泛化问题如"这个药孕妇能吃吗"没有药品名，护栏无权发言）

架构意义：判重决策从"单模型一票"变成"向量+规则双签"——
确定性机制（药品字典比对）为概率性机制（向量相似度）的自动动作兜底，
与资格闸（6.13）、状态机过滤（6.5）同属"规则为向量兜底"这一原则。
"""
from src.normalization import extract_drugs


def drug_set_verdict(question_a: str, question_b: str) -> str:
    """比较两问的药品集合：'match' | 'mismatch' | 'unknown'（任一侧无药品名，无法裁决）"""
    a = set(extract_drugs(question_a))
    b = set(extract_drugs(question_b))
    if not a or not b:
        return "unknown"
    return "match" if a == b else "mismatch"


def classify(score: float, question: str, matched_std: str,
             duplicate: float, new: float) -> dict:
    """
    判重三档 + 药品集合护栏（所有判重点的唯一入口，禁止各脚本自写阈值分支）

    返回 {
        verdict:    "duplicate" | "gray" | "new",
        guarded:    True = 护栏触发（向量想过线但被规则拦下）
        drug_check: "match" | "mismatch" | "unknown",
        reason:     人读理由（留痕/展示用）
    }
    """
    if score >= duplicate:
        check = drug_set_verdict(question, matched_std)
        if check == "mismatch":
            return {"verdict": "gray", "guarded": True, "drug_check": check,
                    "reason": f"向量分 {score:.4f} 过判重线，但药品集合不一致"
                              f"（{question} ≠ {matched_std}）——护栏强制降人工闸"}
        return {"verdict": "duplicate", "guarded": False, "drug_check": check,
                "reason": f"向量分 {score:.4f} 过线"
                          f"{'，药品集合一致' if check == 'match' else '，药品集合无法裁决（维持向量判定）'}"}
    if score >= new:
        return {"verdict": "gray", "guarded": False, "drug_check": "unknown",
                "reason": f"向量分 {score:.4f} 落灰色带"}
    return {"verdict": "new", "guarded": False, "drug_check": "unknown",
            "reason": f"向量分 {score:.4f} 低于新问线"}
