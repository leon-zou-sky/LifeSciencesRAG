"""
知识图谱数据：药物实体 + 关系

从说明书/论文/病例中抽取的结构化关系
"""

# ============ 节点（实体） ============
DRUGS = [
    {"name": "阿托伐他汀", "type": "他汀类", "category": "降脂药"},
    {"name": "红霉素", "type": "大环内酯类", "category": "抗生素"},
    {"name": "克拉霉素", "type": "大环内酯类", "category": "抗生素"},
    {"name": "二甲双胍", "type": "双胍类", "category": "降糖药"},
    {"name": "格列美脲", "type": "磺脲类", "category": "降糖药"},
    {"name": "司美格鲁肽", "type": "GLP-1受体激动剂", "category": "降糖药/减重药"},
    {"name": "恩格列净", "type": "SGLT2抑制剂", "category": "降糖药"},
    {"name": "依折麦布", "type": "胆固醇吸收抑制剂", "category": "降脂药"},
    {"name": "华法林", "type": "香豆素类", "category": "抗凝药"},
    {"name": "胺碘酮", "type": "Ⅲ类抗心律失常", "category": "心血管药"},
    {"name": "环孢素", "type": "钙调磷酸酶抑制剂", "category": "免疫抑制剂"},
    {"name": "吉非贝齐", "type": "贝特类", "category": "降脂药"},
]

ENZYMES = [
    {"name": "CYP3A4", "type": "细胞色素P450"},
    {"name": "CYP2D6", "type": "细胞色素P450"},
]

REACTIONS = [
    {"name": "横纹肌溶解", "severity": "严重", "description": "肌无力、肌痛、CK显著升高"},
    {"name": "肝毒性", "severity": "严重", "description": "转氨酶持续超过3倍正常上限"},
    {"name": "乳酸酸中毒", "severity": "严重", "description": "乏力、呼吸困难、乳酸>4mmol/L"},
    {"name": "低血糖", "severity": "中等", "description": "血糖<3.9mmol/L"},
    {"name": " INR升高", "severity": "中等", "description": "出血风险增加"},
]

DISEASES = [
    {"name": "高胆固醇血症", "category": "代谢性疾病"},
    {"name": "2型糖尿病", "category": "代谢性疾病"},
    {"name": "肥胖", "category": "代谢性疾病"},
    {"name": "动脉粥样硬化", "category": "心血管疾病"},
]

CLINICAL_TRIALS = [
    {"id": "NCT001", "phase": "III", "title": "GLP-1RA联合SGLT2i在T2DM合并肥胖患者中的疗效", "year": 2024, "enrollment": 320},
]


# ============ 关系（边） ============

# 药物-药物相互作用
DRUG_INTERACTIONS = [
    # (药物A, 药物B, 关系类型, 说明, 证据来源)
    ("阿托伐他汀", "红霉素", "CONTRAINDICATED", "暴露量增加约4倍，应避免合用", "说明书"),
    ("阿托伐他汀", "克拉霉素", "CONTRAINDICATED", "CYP3A4强抑制剂，禁合用", "说明书"),
    ("阿托伐他汀", "胺碘酮", "CAUTION", "横纹肌溶解风险增加", "说明书"),
    ("阿托伐他汀", "环孢素", "CAUTION", "横纹肌溶解风险增加", "说明书"),
    ("阿托伐他汀", "吉非贝齐", "CAUTION", "横纹肌溶解风险增加", "说明书"),
    ("阿托伐他汀", "华法林", "CAUTION", "可增强抗凝效果，需监测INR", "说明书"),
    ("二甲双胍", "格列美脲", "COMBINATION", "可联合使用降糖", "说明书"),
    ("司美格鲁肽", "恩格列净", "SYNERGY", "协同降糖减重，安全性良好", "临床论文"),
]

# 药物-酶 关系
DRUG_ENZYME = [
    ("阿托伐他汀", "CYP3A4", "SUBSTRATE", "经CYP3A4代谢"),
    ("红霉素", "CYP3A4", "INHIBITOR", "强抑制剂"),
    ("克拉霉素", "CYP3A4", "INHIBITOR", "强抑制剂"),
    ("胺碘酮", "CYP3A4", "INHIBITOR", "中等抑制剂"),
]

# 药物-不良反应
DRUG_REACTIONS = [
    ("阿托伐他汀", "横纹肌溶解", "RARE", "罕见但严重，联用CYP3A4抑制剂时风险↑"),
    ("阿托伐他汀", "肝毒性", "UNCOMMON", "转氨酶持续超过3倍正常上限应停药"),
    ("二甲双胍", "乳酸酸中毒", "RARE", "肾功能不全患者风险显著增加"),
    ("格列美脲", "低血糖", "COMMON", "常见不良反应，需注意剂量"),
    ("华法林", " INR升高", "COMMON", "联用他汀可增强抗凝效果"),
]

# 药物-适应症
DRUG_INDICATIONS = [
    ("阿托伐他汀", "高胆固醇血症", "一线治疗"),
    ("阿托伐他汀", "动脉粥样硬化", "降低心血管事件风险"),
    ("二甲双胍", "2型糖尿病", "一线治疗，尤其适合肥胖患者"),
    ("司美格鲁肽", "2型糖尿病", "GLP-1受体激动剂"),
    ("司美格鲁肽", "肥胖", "减重适应症"),
    ("恩格列净", "2型糖尿病", "SGLT2抑制剂"),
    ("恩格列净", "肥胖", "有减重获益"),
]

# 临床试验-药物
TRIAL_DRUGS = [
    ("NCT001", "司美格鲁肽", "TESTED"),
    ("NCT001", "恩格列净", "TESTED"),
]

# 病例-药物
CASE_DRUGS = [
    ("CASE-2024-001", "阿托伐他汀", "PRESCRIBED"),
    ("CASE-2024-001", "红霉素", "PRESCRIBED"),
    ("CASE-2024-002", "二甲双胍", "PRESCRIBED"),
    ("CASE-2024-002", "格列美脲", "PRESCRIBED"),
]
