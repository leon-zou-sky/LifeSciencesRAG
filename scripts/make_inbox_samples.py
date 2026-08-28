"""
生成模拟采集样品文件（data/inbox/）——复现真实药企的四种来源格式

  依折麦布片说明书.pdf      → 说明书 PDF（NMPA 核准版最常见的载体）
  hotline_export.csv       → 热线系统问询台账导出（CSV/Excel）
  血脂管理共识.txt          → 指南/共识文本（已数字化的纯文本）
  E2B_case_20240816.xml    → 个例安全性报告（ICH E2B 风格 XML，PV 系统间交换格式）

用法: python scripts/make_inbox_samples.py
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

INBOX = Path(__file__).resolve().parent.parent / "data" / "inbox"

# ---------------- PDF：说明书 ----------------
INSERT_SECTIONS = [
    ("适应症", "用于原发性高胆固醇血症，可单用或与他汀类联合用于降低总胆固醇、低密度脂蛋白胆固醇。"),
    ("用法用量", "口服，推荐剂量为每日一次，每次10mg，可空腹或与食物同服。与他汀联用时按他汀说明书调整。"),
    ("不良反应", "常见：头痛、腹痛、腹泻。偶见：转氨酶升高、肌痛。罕见：横纹肌溶解（与他汀联用时需监测肌酸激酶）。"),
    ("药物相互作用", "与环孢素合用可增加依折麦布血药浓度；与贝特类合用增加胆石症风险；与阿托伐他汀等他汀类联用方案已获批准。"),
]


def _write_insert_pdf(filename: str, title: str, version_line: str, sections: list):
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    path = INBOX / filename
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("STSong-Light", 16)
    c.drawString(72, 780, title)
    c.setFont("STSong-Light", 10)
    c.drawString(72, 762, version_line)
    y = 730
    for sec_title, body in sections:
        c.setFont("STSong-Light", 12)
        c.drawString(72, y, f"【{sec_title}】")
        y -= 20
        c.setFont("STSong-Light", 10)
        while body:
            line, body = body[:45], body[45:]
            c.drawString(72, y, line)
            y -= 16
        y -= 10
    c.save()
    print(f"  生成 {path.name}")


def make_pdf():
    _write_insert_pdf("依折麦布片说明书.pdf", "依折麦布片说明书",
                      "批准文号：国药准字H2024XXXX    版本：2024年修订", INSERT_SECTIONS)


# ---------------- 补库样品：辛伐他汀说明书（覆盖率对账发现的缺口品种） ----------------
SIMVASTATIN_SECTIONS = [
    ("适应症", "用于原发性高胆固醇血症、混合型高脂血症，降低总胆固醇、低密度脂蛋白胆固醇、载脂蛋白B和甘油三酯。"),
    ("用法用量", "口服，晚间一次服用。起始剂量10-20mg每日一次，剂量范围10-40mg。与胺碘酮、维拉帕米或地尔硫卓合用时，本品剂量不得超过20mg每日。"),
    ("不良反应", "常见：头痛、便秘、恶心、腹痛。偶见：肌痛、转氨酶升高。罕见：横纹肌溶解（与CYP3A4抑制剂合用时风险显著增加，表现为肌痛、乏力、酱油色尿）。"),
    ("药物相互作用", "禁与CYP3A4强抑制剂（克拉霉素、伊曲康唑、HIV蛋白酶抑制剂）合用。胺碘酮、维拉帕米、地尔硫卓抑制CYP3A4，合用时辛伐他汀血药浓度升高，剂量须限制在20mg/日以内并监测肌酸激酶。与华法林合用可增强抗凝效果，需监测INR。"),
    ("禁忌", "活动性肝病、妊娠期和哺乳期、对本品过敏者禁用。禁止与CYP3A4强抑制剂联合使用。"),
]


def make_simvastatin_pdf():
    _write_insert_pdf("辛伐他汀片说明书.pdf", "辛伐他汀片说明书",
                      "批准文号：国药准字H2023YYYY    版本：2023年修订", SIMVASTATIN_SECTIONS)


# ---------------- CSV：热线问询导出 ----------------
def make_csv():
    path = INBOX / "hotline_export.csv"
    rows = [
        ["2024-08-12 09:14", "热线", "坐席03", "益适纯和阿乐能放一起吃吗"],
        ["2024-08-12 11:40", "微信", "坐席07", "吃阿托伐他汀腿疼要不要停药"],
        ["2024-08-13 15:22", "热线", "坐席03", "阿托伐他汀钙片能不能跟红霉素一起用"],  # 与底库#1近似重复
        ["2024-08-14 10:05", "代表转达", "-", "欧唐静和格华止联用注意啥"],
        ["2024-08-15 16:47", "热线", "坐席11", "你们这个药根本没用"],  # 噪音：无药品无问题
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["时间", "渠道", "坐席", "问题原文"])
        w.writerows(rows)
    print(f"  生成 {path.name}（{len(rows)} 行）")


# ---------------- TXT：指南共识 ----------------
def make_txt():
    path = INBOX / "血脂管理共识.txt"
    path.write_text(
        "血脂异常基层管理共识（2024）节选\n\n"
        "【联合用药】\n"
        "他汀类药物单药治疗不达标者，推荐联合依折麦布，可进一步降低LDL-C约15%-20%，"
        "且不增加横纹肌溶解发生率。联合方案应从小剂量他汀起始。\n\n"
        "【安全性监测】\n"
        "他汀治疗期间应定期监测肝功能和肌酸激酶。出现肌痛伴CK升高超过正常上限10倍时应停药。"
        "与CYP3A4强抑制剂（红霉素、克拉霉素）合用时应暂停他汀或换用不经CYP3A4代谢的品种。\n",
        encoding="utf-8",
    )
    print(f"  生成 {path.name}")


# ---------------- XML：E2B 风格个例报告 ----------------
def make_xml():
    path = INBOX / "E2B_case_20240816.xml"
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<icsr>
  <case_id>CN-2024-0816</case_id>
  <case_type>个例安全性报告</case_type>
  <patient><age>62</age><sex>男</sex></patient>
  <drugs>
    <drug><name>辛伐他汀</name><dose>40mg qd</dose><role>怀疑用药</role></drug>
    <drug><name>胺碘酮</name><dose>200mg qd</dose><role>合并用药</role></drug>
  </drugs>
  <reactions>
    <reaction><term>横纹肌溶解</term><outcome>住院治疗</outcome><seriousness>严重</seriousness></reaction>
    <reaction><term>肌酸激酶升高</term><outcome>恢复中</outcome><seriousness>非严重</seriousness></reaction>
  </reactions>
  <narrative>患者服用辛伐他汀40mg联合胺碘酮治疗3周后出现双下肢肌痛、乏力，CK 12500 U/L，诊断横纹肌溶解症。停用辛伐他汀后症状缓解。胺碘酮抑制CYP3A4，可增加辛伐他汀血药浓度。</narrative>
</icsr>
""",
        encoding="utf-8",
    )
    print(f"  生成 {path.name}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="生成模拟采集样品")
    parser.add_argument("--simvastatin", action="store_true",
                        help="只生成辛伐他汀说明书（补库缺口样品），不动其他文件")
    args = parser.parse_args()

    INBOX.mkdir(parents=True, exist_ok=True)
    if args.simvastatin:
        make_simvastatin_pdf()
    else:
        make_pdf()
        make_csv()
        make_txt()
        make_xml()
    print(f"\n样品文件就绪: {INBOX}")


if __name__ == "__main__":
    main()
