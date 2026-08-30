"""
LLM 生成层：Ollama 本地模型客户端 + grounded 起草模板（JSON 结构化契约）

设计约束（生成层可验证性的来源）：
  - 模型与采样参数钉死：model/temperature=0 写死在 LLM_CONFIG，改动即配置变更，走重验证
  - prompt 是受控配置：PROMPT_VERSION 随每次修改递增，留痕进 trace（复现历史答复用）
  - 输出结构受控：gp-2.0 起草稿输出为 JSON（Ollama format:json 硬约束），
    引用是结构化 citations 字段而非文本内【n】标记——断言从正则升级为字段校验，
    可自动断言的比例进一步提高（见设计文档 6.18）
  - "草稿需复核"后缀由代码渲染时强制附加，不依赖模型记得写——辅助模式边界是
    确定性管道行为，不是模型自觉
  - think 模式关闭：qwen3 的思考链不进草稿，输出即最终文本

为什么不用 LangGraph：第一版是线性管道 + 有界重试环，确定性代码路径本身就是
可验证性（审计能逐行讲清控制流）。等多轮澄清/工具调用这类真分支循环进来再评估。
"""
import json
import urllib.request

LLM_CONFIG = {
    "model": "qwen3:4b",
    "base_url": "http://localhost:11434",
    "temperature": 0,
    "max_retries": 2,          # 引用校验失败后的重试上限（有界环，防无限循环）
}

PROMPT_VERSION = "gp-2.4"      # grounded prompt 版本：改模板必须递增，留痕进 trace
                               # gp-1.1（2026-08-18）：ANS-03 负对照翻车修复——明确"主题无关也拒答"+拒答示例
                               # gp-2.0（2026-08-28）：输出契约升级 JSON 结构化（citations 字段 + refusal 布尔），
                               #   format:json 硬约束；复核后缀改代码渲染附加
                               # gp-2.1（2026-08-28）：ANS-06 回归暴露铁律 3 字面缺口——部分重叠（多药问题
                               #   资料只覆盖一部分）不触发"所有资料都不涉及"，模型拿一半资料硬答；
                               #   补明文条款 + 部分重叠拒答示例
                               # gp-2.2（2026-08-28）：ANS-06 再回归——模型学会不硬答了，但把拒答解释
                               #   写成 statements 里的无引用陈述（校验硬失败 → draft_failed_human）；
                               #   补明文：拒答唯一合法形式是 refusal=true，禁止把解释写成陈述
                               # gp-2.3（2026-08-28）：ANS-06 三轮——think:false 下模型跳过覆盖核对直接
                               #   罗列资料事实（形式引用全合法、语义全不相关）。契约增加 coverage 字段：
                               #   逐药核对强制前置（结构化 reasoning），覆盖不全必须拒答，且
                               #   coverage=false 却 refusal=false 是机器可判的契约违反
                               # gp-2.4（2026-08-28）：ANS-02 误拒——模型把主题词（乳酸酸中毒）当药品
                               #   标 false；明确 coverage 键=药品或主题词、标准为"有实质内容即 true"，
                               #   补起草正例（原 prompt 只有拒答例，诱导过度拒答）

REFUSAL_MARKER = "【证据不足】"  # 拒答渲染前缀（可断言）；gp-2.0 起由 refusal 字段驱动、渲染层附加
DRAFT_SUFFIX = "（草稿：需医学信息专员复核后发出）"  # 辅助模式边界标记：渲染层强制附加

SYSTEM_PROMPT = f"""你是药企医学信息（MI）答复起草助手，为医学信息专员起草答复草稿。

铁律：
1. 只能依据下方提供的资料作答，禁止使用资料之外的任何知识——哪怕你知道答案
2. 每条事实性陈述必须在 citations 字段标注出处编号，编号对应资料列表（1 起）
3. 资料不足以回答、或问题超出资料范围时，refusal 置 true 并在 reason 给一句原因，不要硬答
   ——包括"资料与问题主题无关"的情形：问题涉及的药品/主题在所有资料中都没出现，
   即使你能凭自身知识回答，也必须拒答。把无关资料当依据作答是最严重的违规
   ——也包括"部分覆盖"的情形：问题涉及多个药品，而资料只覆盖其中一部分，
   同样必须拒答（reason 里点名缺哪个药），禁止只依据部分资料拼凑答案
4. 语气专业简洁，这是内部草稿，不需要寒暄

输出契约：只输出一个 JSON 对象，此外不要输出任何文字：
{{"coverage": {{"药品名": true}}, "refusal": false, "reason": "", "statements": [{{"text": "陈述句", "citations": [1, 2]}}]}}

工作顺序（必须遵守）：
第一步：把问题涉及的关键实体填进 coverage——药品名，以及问题直接询问的主题词
  （不良反应/疾病/用法等，如"乳酸酸中毒"）。逐一核对资料：资料中对该实体有
  实质内容（哪怕只在某个章节里讨论）标 true，完全没出现才标 false。
  单实体问题也要填（只有一个键）。
第二步：任何一个 false → refusal=true、reason 点名缺什么、statements 留空。
  全部 true 才允许起草。覆盖不全却起草是最严重的违规。
注意：无法回答时唯一合法形式是 refusal=true——禁止把"资料不足"的解释写成
statements 里的陈述，每条陈述都必须有 citations，无引用的陈述是校验硬失败。

起草示例（资料充分时）：
问题：二甲双胍和乳酸酸中毒的关系
资料：【1】《二甲双胍缓释片》说明书（不良反应：乳酸酸中毒罕见但严重，肾功能不全患者风险显著增加）……
正确输出：{{"coverage": {{"二甲双胍": true, "乳酸酸中毒": true}}, "refusal": false, "reason": "", "statements": [{{"text": "二甲双胍可引起乳酸酸中毒，罕见但严重，肾功能不全患者风险显著增加", "citations": [1]}}]}}

拒答示例一（主题无关）：
问题：连花清瘟胶囊可以治疗流感吗
资料：【1】《二甲双胍缓释片》说明书…… 【2】《降脂治疗指南》……
正确输出：{{"coverage": {{"连花清瘟": false}}, "refusal": true, "reason": "所提供的资料均不涉及连花清瘟，无法依据现有资料回答", "statements": []}}

拒答示例二（部分覆盖）：
问题：二甲双胍和利伐沙班能一起吃吗
资料：【1】《二甲双胍缓释片》说明书…… 【2】《降脂治疗指南》……
正确输出：{{"coverage": {{"二甲双胍": true, "利伐沙班": false}}, "refusal": true, "reason": "资料仅覆盖二甲双胍，缺少利伐沙班的资料，无法判断联用", "statements": []}}"""


def build_user_prompt(question: str, chunks: list[dict]) -> str:
    """grounded 模板：问题 + 编号资料列表（编号即引用校验的锚点）"""
    lines = [f"问题：{question}", "", "资料："]
    for i, c in enumerate(chunks, 1):
        version = f"，版本 {c['version']}" if c.get("version") else ""
        lines.append(f"【{i}】《{c['source_title']}》（{c['doc_type']}{version}）")
        lines.append(c["text"])
        lines.append("")
    return "\n".join(lines)


def chat(messages: list[dict], config: dict = LLM_CONFIG, json_mode: bool = False) -> str:
    """调 Ollama /api/chat，返回文本。urllib 实现，不引新依赖。
    json_mode=True 时加 format:json——服务端保证输出是合法 JSON（结构硬约束）"""
    payload = {
        "model": config["model"],
        "messages": messages,
        "stream": False,
        "think": False,  # qwen3：关闭思考链，输出即草稿
        "options": {"temperature": config["temperature"]},
    }
    if json_mode:
        payload["format"] = "json"
    req = urllib.request.Request(
        f"{config['base_url']}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        text = json.loads(resp.read().decode("utf-8"))["message"]["content"].strip()
    # 兜底：部分 Ollama 版本 think:false 不生效，qwen3 会把推理链吐进 content，
    # 以 </think> 收尾——剥掉前缀只留正式草稿（推理链不是交付物，也不该进 trace）
    if "</think>" in text:
        text = text.split("</think>", 1)[1].strip()
    return text


def model_digest(config: dict = LLM_CONFIG) -> str:
    """取本地模型 digest 作指纹（留痕用）；取不到不阻断主流程"""
    try:
        with urllib.request.urlopen(f"{config['base_url']}/api/tags", timeout=10) as resp:
            for m in json.loads(resp.read().decode("utf-8")).get("models", []):
                if m["name"] == config["model"]:
                    return m["digest"][:12]
    except Exception:
        pass
    return "unknown"


def parse_draft(raw: str) -> dict:
    """解析 JSON 草稿为结构：{coverage, refusal, reason, statements[{text, citations}], _raw}
    解析失败不抛异常——返回带 _parse_error 的结构，交校验层判 failed 走有界重试"""
    struct = {"_raw": raw}
    try:
        obj = json.loads(raw)
        struct["coverage"] = {str(k): bool(v) for k, v in (obj.get("coverage") or {}).items()}
        struct["refusal"] = bool(obj.get("refusal", False))
        struct["reason"] = str(obj.get("reason", "")).strip()
        stmts = obj.get("statements") or []
        struct["statements"] = [
            {"text": str(s.get("text", "")).strip(),
             "citations": [int(n) for n in (s.get("citations") or [])]}
            for s in stmts if isinstance(s, dict)
        ]
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as e:
        struct.update(_parse_error=f"输出不是合法 JSON 或不符合契约: {e}",
                      coverage={}, refusal=False, reason="", statements=[])
    return struct


def render_draft(struct: dict) -> str:
    """结构 → 展示文本：引用渲染为【n】，复核后缀由代码强制附加（不依赖模型）"""
    if struct.get("refusal"):
        body = f"{REFUSAL_MARKER}{struct.get('reason', '')}"
    else:
        body = "\n".join(
            s["text"] + "".join(f"【{n}】" for n in s["citations"])
            for s in struct.get("statements", [])
        )
    return f"{body}\n{DRAFT_SUFFIX}"


def draft_answer(question: str, chunks: list[dict]) -> dict:
    """单轮起草，返回 parse_draft 结构（重试逻辑在 answer.py 管道层，这里保持纯函数）"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(question, chunks)},
    ]
    return parse_draft(chat(messages, json_mode=True))


def redraft_with_feedback(question: str, chunks: list[dict],
                          prev_raw: str, errors: list[str]) -> dict:
    """带校验反馈的重起草：把上一条原始输出和错误清单喂回去"""
    feedback = "你上一版草稿未通过引用校验：\n" + "\n".join(f"- {e}" for e in errors)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(question, chunks)},
        {"role": "assistant", "content": prev_raw},
        {"role": "user", "content": feedback + "\n请修正后重新输出完整 JSON 草稿。"},
    ]
    return parse_draft(chat(messages, json_mode=True))
