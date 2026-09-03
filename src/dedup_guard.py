"""
判重规则护栏（模型文档 A.5⑦ / RTM REQ-01 / 缺口 G-04、G-10 的关闭实现）

两道集合校验，同一哲学：规则为向量兜底——

第一道【药品集合】（G-04，2026-08-18）：
问题（纯向量判重的物理上限，标定实测）：一字之差的难负样本
  「阿托伐他汀可以和红霉素联用吗」 vs 「阿托伐他汀可以和克拉霉素联用吗」
语义结构几乎同构，余弦相似度 0.9585——任何向量模型的 duplicate 线都压不过它，
否则真重复（同义改写）也会被错杀。这是表征学习的固有极限，不是调参能解决的。

但这两个问题的**药品集合不同**（{阿托伐他汀,红霉素} ≠ {阿托伐他汀,克拉霉素}），
而"标准答案是否相同"恰恰主要取决于药品集合——这是规则能 100% 判定的维度。

第二道【部位/人群】（G-10，2026-09-03）：
药品集合相同也未必同答案——「二甲双胍在肾功能不全/肝功能不全患者中可以使用吗」
一字之差 0.9185，禁忌证完全不同。部位/人群维度同样能被规则确定性判定。

护栏规约（两道一致）：
  - score ≥ duplicate 且任一集合校验不一致 → 强制降灰色带（人工闸），永不自动判重
  - 护栏只拦不抬：不会把低分问题提拔成 duplicate，只否决向量的"自动"资格
  - 任一侧识别不出对应要素 → "unknown"（无法裁决），维持向量判定，不干预
    （泛化问题没有药品名/人群词，护栏无权发言）

架构意义：判重决策从"单模型一票"变成"向量+规则多签"——
确定性机制（词典比对）为概率性机制（向量相似度）的自动动作兜底，
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


# 部位/人群关键词分组表（G-10 处置②）。分组的边界不是拍的——来自 24 对标定对实测：
# 组内改写是答案同源的正样本，必须 match 不拦：
#   肾功能不全 ≈ 肾功能不好 ≈ 慢性肾病 ≈ 透析 ≈ eGFR 偏低（标定对 7/8/9/12，label=1，
#   其中「慢性肾病」实测 0.8569 判 duplicate——若拆成不同组会误伤这条正样本）
# 组间才是护栏要拦的：肾 vs 肝禁忌证完全不同（G-10 负样本 0.9185 会短路复用错误答案）
# 小写匹配仅影响拉丁字母（egfr）；单个"孕"字覆盖 孕妇/怀孕/妊娠/孕期/备孕，医学问询中无误伤面
_POPULATION_GROUPS = {
    "肾功能": ["肾功能", "肾病", "透析", "egfr", "肌酐", "肾衰", "肾不好"],
    "肝功能": ["肝功能", "肝硬化", "肝损伤", "肝脏", "转氨酶", "肝衰", "肝不好"],
    "孕期": ["孕"],
    "哺乳期": ["哺乳", "母乳", "喂奶"],
    "儿童": ["儿童", "小儿", "婴幼儿", "孩子", "宝宝"],
    "老年": ["老年", "老人", "高龄"],
}


def _extract_groups(text: str) -> set[str]:
    """提取问句涉及的部位/人群组（命中任一关键词即入组）"""
    t = text.lower()
    return {group for group, kws in _POPULATION_GROUPS.items() if any(k in t for k in kws)}


def population_set_verdict(question_a: str, question_b: str) -> str:
    """比较两问的部位/人群组集合：'match' | 'mismatch' | 'unknown'（任一侧无人群词，无法裁决）"""
    a = _extract_groups(question_a)
    b = _extract_groups(question_b)
    if not a or not b:
        return "unknown"
    return "match" if (a & b) else "mismatch"


def classify(score: float, question: str, matched_std: str,
             duplicate: float, new: float) -> dict:
    """
    判重三档 + 双集合护栏（所有判重点的唯一入口，禁止各脚本自写阈值分支）

    返回 {
        verdict:          "duplicate" | "gray" | "new",
        guarded:          True = 护栏触发（向量想过线但被规则拦下）
        drug_check:       "match" | "mismatch" | "unknown",
        population_check: "match" | "mismatch" | "unknown",
        reason:           人读理由（留痕/展示用）
    }
    """
    if score >= duplicate:
        drug_check = drug_set_verdict(question, matched_std)
        pop_check = population_set_verdict(question, matched_std)
        if drug_check == "mismatch":
            return {"verdict": "gray", "guarded": True, "drug_check": drug_check,
                    "population_check": pop_check,
                    "reason": f"向量分 {score:.4f} 过判重线，但药品集合不一致"
                              f"（{question} ≠ {matched_std}）——护栏强制降人工闸"}
        if pop_check == "mismatch":
            qa, qb = _extract_groups(question), _extract_groups(matched_std)
            return {"verdict": "gray", "guarded": True, "drug_check": drug_check,
                    "population_check": pop_check,
                    "reason": f"向量分 {score:.4f} 过判重线，但部位/人群不一致"
                              f"（{'/'.join(sorted(qa))} ≠ {'/'.join(sorted(qb))}）"
                              f"——护栏强制降人工闸（G-10）"}
        return {"verdict": "duplicate", "guarded": False, "drug_check": drug_check,
                "population_check": pop_check,
                "reason": f"向量分 {score:.4f} 过线"
                          f"{'，药品集合一致' if drug_check == 'match' else '，药品集合无法裁决（维持向量判定）'}"
                          f"{'，部位/人群一致' if pop_check == 'match' else '，部位/人群无法裁决（维持向量判定）'}"}
    if score >= new:
        return {"verdict": "gray", "guarded": False, "drug_check": "unknown",
                "population_check": "unknown",
                "reason": f"向量分 {score:.4f} 落灰色带"}
    return {"verdict": "new", "guarded": False, "drug_check": "unknown",
            "population_check": "unknown",
            "reason": f"向量分 {score:.4f} 低于新问线"}
