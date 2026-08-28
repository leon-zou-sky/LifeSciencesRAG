"""
PII 筛查层：输入侧敏感信息检测与脱敏（国内生命科学场景，贴合《个人信息保护法》）

设计原则：
  1. 离线可运行：PoC 阶段以规则+轻量词典为主，不强制挂载云端 NER 服务。
  2. 可插拔：Detector 接口统一，后面可无缝替换为 Presidio / 自研 NER / 厂商 SDK。
  3. 双版本留痕：原始问询进审计日志（人要复核），脱敏后文本给 LLM 和检索编码。
  4. 业务定制：除通用 PII（姓名/电话/身份证）外，重点覆盖医疗场景（病历号、医院名、病史细节）。

使用：
    from src.pii_guard import PiiGuard
    guard = PiiGuard()
    redacted, entities = guard.redact("张三电话13800138000，吃二甲双胍后腹泻...")
    # redacted = "[PATIENT_NAME]电话[PHONE]，吃二甲双胍后腹泻..."
    # entities = [{"type":"name", "value":"张三", ...}, {"type":"phone", ...}]
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PiiEntity:
    type: str
    value: str
    start: int
    end: int
    placeholder: str


class Detector(Protocol):
    def detect(self, text: str) -> list[PiiEntity]: ...


class _RegexDetector(Detector):
    """规则检测器：确定性模式先捞一轮（手机号、身份证、邮箱、病历号、地址）。"""

    # 占位符命名与 HIPAA/国内个保法审计习惯对齐，便于下游日志识别
    _RULES = [
        ("phone", r"(?<![\d])1[3-9]\d{9}(?![\d])", "[PHONE]"),
        ("id_card", r"(?<![\d])[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![\d])", "[ID_CARD]"),
        ("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]"),
        # 病历号/就诊号：常见 6-20 位字母数字组合，前面带"病历号/门诊号/住院号/就诊号"等线索
        ("medical_record_id", r"(?:病历号|门诊号|住院号|就诊号|病案号|ID|编号)[:：\s]*([A-Za-z0-9]{6,20})", "[MRN]", 1),
        # 地址：省市县区 + 街道/路/巷/号/室，至少 8 字
        ("address", r"(?:[^，。,\n]{2,10}(?:省|自治区|直辖市|市|区|县|旗|镇|乡|街道))"
                    r"(?:[^，。,\n]{2,30}(?:路|街|巷|弄|号|栋|单元|室|楼|层))", "[ADDRESS]"),
    ]

    def detect(self, text: str) -> list[PiiEntity]:
        out: list[PiiEntity] = []
        for rule in self._RULES:
            if len(rule) == 3:
                pii_type, pattern, placeholder = rule
                group = 0
            else:
                pii_type, pattern, placeholder, group = rule
            for m in re.finditer(pattern, text):
                value = m.group(group)
                start = m.start(group)
                end = m.end(group)
                out.append(PiiEntity(pii_type, value, start, end, placeholder))
        return out


class _NameDetector(Detector):
    """姓名检测器：常见姓氏 + 2~3 字名。规则粗但轻量，后续可换 NER。"""

    # 常见百家姓前百 + 少量复姓
    _SURNAMES = set(
        "王李张刘陈杨黄赵周吴徐孙马朱胡郭何林罗高郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘"
        "于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏付方白邹孟熊秦邱江"
        "尹薛闫段雷侯龙史黎贺顾毛郝龚邵万钱严覃武戴莫孔白汤向常"
        "欧阳司马上官诸葛东方皇甫慕容公孙"
    )
    # 避免把常见药品/医学词当成人名
    _STOPWORDS = {"二甲双胍", "格列美脲", "阿托伐他汀", "胰岛素", "葡萄糖", "维生素", "阿司匹林",
                  "说明书", "不良反应", "副作用", "适应症", "禁忌症", "临床表现", "诊断标准",
                  "患者", "医生", "医师", "护士", "药师", "医学", "医院", "门诊", "住院"}

    def detect(self, text: str) -> list[PiiEntity]:
        out: list[PiiEntity] = []
        # ① 强上下文（患者/孩子等词义明确）：不限后接字符，靠姓氏表+停用词过滤
        for m in re.finditer(
                r"(?:患者|病人|姓名|名字叫|名为|叫|孩子|儿子|女儿)\s*([一-龥]{2,3})", text):
            value = m.group(1)
            if self._likely_name(value):
                out.append(PiiEntity("name", value, m.start(1), m.end(1), "[PATIENT_NAME]"))
        # ② 弱上下文（是/冒号等泛化词）：要求名字后紧跟标点/联系方式，防"是二甲双胍"式误伤
        for m in re.finditer(
                r"(?:是|[:：])\s*([一-龥]{2,3})(?=[，。,、；\n（(]|$|电话|手机)", text):
            value = m.group(1)
            if self._likely_name(value):
                out.append(PiiEntity("name", value, m.start(1), m.end(1), "[PATIENT_NAME]"))
        # ③ 姓名紧邻联系方式："张三电话138..."/"王五 138..."（正向先行断言，不吃"电"字）
        for m in re.finditer(r"([一-龥]{2,3})(?=电话|手机|微信号)", text):
            value = m.group(1)
            if self._likely_name(value):
                out.append(PiiEntity("name", value, m.start(1), m.end(1), "[PATIENT_NAME]"))
        for m in re.finditer(r"([一-龥]{2,3})(?=\s*1[3-9]\d{9})", text):
            value = m.group(1)
            if self._likely_name(value):
                out.append(PiiEntity("name", value, m.start(1), m.end(1), "[PATIENT_NAME]"))
        # ④ 称谓后缀：张女士/李先生/王大爷
        for m in re.finditer(r"([一-龥]{1,2}(?:女士|先生|大爷|大妈))", text):
            value = m.group(1)
            if value[0] in self._SURNAMES:
                out.append(PiiEntity("name", value, m.start(1), m.end(1), "[PATIENT_NAME]"))
        # ⑤ 单姓自报："我姓王"
        for m in re.finditer(r"我姓([一-龥])", text):
            value = m.group(1)
            if value in self._SURNAMES:
                out.append(PiiEntity("name", value, m.start(1), m.end(1), "[PATIENT_NAME]"))
        # 同值去重（多规则命中同一名字时只留一条）
        deduped: list[PiiEntity] = []
        for e in out:
            if not any(x.start == e.start and x.end == e.end for x in deduped):
                deduped.append(e)
        return deduped

    def _likely_name(self, value: str) -> bool:
        if value in self._STOPWORDS:
            return False
        # 复姓
        if value[:2] in self._SURNAMES:
            return True
        # 单姓 + 名
        if value[0] in self._SURNAMES and len(value) in (2, 3):
            return True
        return False


class PiiGuard:
    """PII 守门器：组合多个 detector，输出脱敏文本与实体清单。"""

    def __init__(self, detectors: list[Detector] | None = None):
        self.detectors = detectors or [_RegexDetector(), _NameDetector()]

    def detect(self, text: str) -> list[PiiEntity]:
        """检测全部 PII 实体，按出现位置排序，并处理重叠（长实体优先）。"""
        all_entities = []
        for d in self.detectors:
            all_entities.extend(d.detect(text))
        # 去重：按 (start, end) 去重，长区间吞短区间
        all_entities.sort(key=lambda e: (e.start, -e.end))
        merged: list[PiiEntity] = []
        for e in all_entities:
            if not merged:
                merged.append(e)
                continue
            last = merged[-1]
            if e.start < last.end:  # 重叠
                if e.end - e.start > last.end - last.start:
                    merged[-1] = e
            else:
                merged.append(e)
        return merged

    def redact(self, text: str) -> tuple[str, list[PiiEntity]]:
        """返回 (脱敏后文本, 实体清单)。原始文本不动，用于审计侧单独留存。"""
        entities = self.detect(text)
        if not entities:
            return text, []
        chars = list(text)
        for e in reversed(entities):  # 从后往前替换，避免位置偏移
            chars[e.start:e.end] = list(e.placeholder)
        return "".join(chars), entities

    def has_pii(self, text: str) -> bool:
        return bool(self.detect(text))


# 默认单例，便于调用方直接 import 使用
_default_guard = PiiGuard()


def redact(text: str) -> tuple[str, list[PiiEntity]]:
    return _default_guard.redact(text)


def detect(text: str) -> list[PiiEntity]:
    return _default_guard.detect(text)
