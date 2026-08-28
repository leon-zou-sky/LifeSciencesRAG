"""
归一层：商品名/俗名/错别字 → 药品通用名

关键决策：归一发生在 embedding 编码**之前**——
先把"立普妥"映射成"阿托伐他汀"再算向量，保证商品名和通用名
在向量空间里是同一个东西，而不是指望模型自己猜出来。

对应真实场景：药品通用名/商品名/俗名映射（国内一药多名极其普遍），
生产环境应接入药品本位码/药典词表，PoC 用手维护别名表验证机制。
"""

# 通用名 → [商品名/俗名/常见错别字]
DRUG_ALIASES = {
    "阿托伐他汀": ["立普妥", "阿乐", "阿托伐他汀钙", "阿托伐他汀钙片"],
    "二甲双胍": ["格华止", "二甲双瓜", "二甲双胍缓释片"],
    "辛伐他汀": ["舒降之"],
    "司美格鲁肽": ["诺和泰", "司美"],
    "恩格列净": ["欧唐静"],
    "华法林": ["华法令"],
    "红霉素": ["红霉素肠溶片"],
    "克拉霉素": [],
    "阿奇霉素": [],
    # 库外药（2026-08-19 ③验证后补）：收录只为让 extract_drugs 能识别，
    # 使覆盖缺口检查对"已识别的库外药"能确定性拦截（coverage_gap），
    # 不再单靠 LLM 拒答兜底——词典永远列不全，未收录的库外药仍走模型拒答+人工
    "布洛芬": ["芬必得", "布洛芬缓释胶囊"],
    "奥司他韦": ["达菲", "奥司他韦颗粒"],
    "连花清瘟": ["连花清瘟胶囊", "连花清瘟颗粒"],
    "胺碘酮": ["可达龙"],
    "依折麦布": ["益适纯"],
    "格列美脲": ["亚莫利"],
}

# 反向索引：别名 → 通用名（构建一次）
_ALIAS_TO_GENERIC: dict[str, str] = {}
for _generic, _aliases in DRUG_ALIASES.items():
    _ALIAS_TO_GENERIC[_generic] = _generic
    for _a in _aliases:
        _ALIAS_TO_GENERIC[_a] = _generic

# 按别名长度降序，保证 "阿托伐他汀钙片" 先于 "阿托伐他汀" 命中（最长匹配优先）
_SORTED_ALIASES = sorted(_ALIAS_TO_GENERIC.keys(), key=len, reverse=True)


def extract_drugs(text: str) -> list[str]:
    """从文本中提取涉及的药品通用名（去重，按出现顺序）"""
    found: list[str] = []
    for alias in _SORTED_ALIASES:
        if alias in text:
            generic = _ALIAS_TO_GENERIC[alias]
            if generic not in found:
                found.append(generic)
    return found


def normalize_text(text: str) -> str:
    """把文本中的别名替换为通用名（最长匹配优先，避免部分替换）

    两阶段替换：先全部换成占位符再换回通用名。
    否则短别名是长名的子串时会误伤——如"司美"是"司美格鲁肽"的子串，
    直接 replace 会把"司美格鲁肽"变成"司美格鲁肽格鲁肽"。
    """
    placeholders: dict[str, str] = {}
    result = text
    for i, alias in enumerate(_SORTED_ALIASES):
        generic = _ALIAS_TO_GENERIC[alias]
        if alias == generic and alias not in result:
            continue
        ph = f"\x00{i}\x00"
        if alias in result:
            result = result.replace(alias, ph)
            placeholders[ph] = generic
    for ph, generic in placeholders.items():
        result = result.replace(ph, generic)
    return result
