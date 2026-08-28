"""
LLM 生成层：Ollama 本地模型客户端 + grounded 起草模板

设计约束（生成层可验证性的来源）：
  - 模型与采样参数钉死：model/temperature=0 写死在 LLM_CONFIG，改动即配置变更，走重验证
  - prompt 是受控配置：PROMPT_VERSION 随每次修改递增，留痕进 trace（复现历史答复用）
  - 输出结构受控：LLM 只做"把检索到的块组织成通顺的话 + 逐句挂【编号】引用"，
    禁止自由发挥——输出结构约束越死，可自动断言的比例越高（见设计文档第 8 章思路）
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

PROMPT_VERSION = "gp-1.1"      # grounded prompt 版本：改模板必须递增，留痕进 trace
                               # gp-1.1（2026-08-18）：ANS-03 负对照翻车修复——明确"主题无关也拒答"+拒答示例

REFUSAL_MARKER = "【证据不足】"  # 模型在资料不足时必须输出的拒答标记（可断言）

SYSTEM_PROMPT = f"""你是药企医学信息（MI）答复起草助手，为医学信息专员起草答复草稿。

铁律：
1. 只能依据下方提供的资料作答，禁止使用资料之外的任何知识——哪怕你知道答案
2. 每个事实性陈述之后必须立即用【编号】标注出处（如【1】【2】），编号对应资料列表
3. 资料不足以回答、或问题超出资料范围时，只回复 {REFUSAL_MARKER} 加一句原因，不要硬答
   ——包括"资料与问题主题无关"的情形：问题涉及的药品/主题在所有资料中都没出现，
   即使你能凭自身知识回答，也必须拒答。把无关资料当依据作答是最严重的违规
4. 语气专业简洁，这是内部草稿，不需要寒暄
5. 结尾固定另起一行输出：（草稿：需医学信息专员复核后发出）

拒答示例：
问题：连花清瘟胶囊可以治疗流感吗
资料：【1】《二甲双胍缓释片》说明书…… 【2】《降脂治疗指南》……
正确回复：{REFUSAL_MARKER}所提供的资料均不涉及连花清瘟，无法依据现有资料回答。"""


def build_user_prompt(question: str, chunks: list[dict]) -> str:
    """grounded 模板：问题 + 编号资料列表（编号即引用校验的锚点）"""
    lines = [f"问题：{question}", "", "资料："]
    for i, c in enumerate(chunks, 1):
        version = f"，版本 {c['version']}" if c.get("version") else ""
        lines.append(f"【{i}】《{c['source_title']}》（{c['doc_type']}{version}）")
        lines.append(c["text"])
        lines.append("")
    return "\n".join(lines)


def chat(messages: list[dict], config: dict = LLM_CONFIG) -> str:
    """调 Ollama /api/chat，返回文本。urllib 实现，不引新依赖"""
    payload = {
        "model": config["model"],
        "messages": messages,
        "stream": False,
        "think": False,  # qwen3：关闭思考链，输出即草稿
        "options": {"temperature": config["temperature"]},
    }
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


def draft_answer(question: str, chunks: list[dict]) -> str:
    """单轮起草（重试逻辑在 answer.py 管道层，这里保持纯函数便于测试）"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(question, chunks)},
    ]
    return chat(messages)


def redraft_with_feedback(question: str, chunks: list[dict],
                          prev_draft: str, errors: list[str]) -> str:
    """带校验反馈的重起草：把上一条草稿和错误清单喂回去"""
    feedback = "你上一版草稿未通过引用校验：\n" + "\n".join(f"- {e}" for e in errors)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(question, chunks)},
        {"role": "assistant", "content": prev_draft},
        {"role": "user", "content": feedback + "\n请修正后重新输出完整草稿。"},
    ]
    return chat(messages)
