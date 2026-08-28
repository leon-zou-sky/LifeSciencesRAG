"""
LLM 配置：火山引擎 Ark（豆包）OpenAI 兼容接口

配置读取优先级（找到即用，不覆盖已有环境变量）：
  1. 环境变量 ARK_API_KEY / ARK_BASE_URL / ARK_MODEL_ENDPOINT
  2. PoC 目录 .env
  3. 同级主项目 .env（本地开发约定：复用主项目配置，免重复维护）
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

_POC_ROOT = Path(__file__).resolve().parent.parent
_SIBLING_ENV = _POC_ROOT.parent / "WeatherAgent" / ".env"

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


def _load_env():
    # PoC 自己的 .env 优先；同级主项目 .env 兜底补齐缺的变量
    load_dotenv(_POC_ROOT / ".env", override=False)
    if _SIBLING_ENV.exists():
        load_dotenv(_SIBLING_ENV, override=False)


def get_ark_client() -> tuple[OpenAI, str]:
    """返回 (client, model_endpoint)。model 为 Ark 推理接入点 ID (ep-xxx)"""
    _load_env()
    api_key = os.getenv("ARK_API_KEY", "")
    model = os.getenv("ARK_MODEL_ENDPOINT", "")
    base_url = os.getenv("ARK_BASE_URL", DEFAULT_BASE_URL)
    if not api_key or not model:
        raise RuntimeError(
            "缺少 ARK_API_KEY / ARK_MODEL_ENDPOINT 配置。\n"
            "请在 PoC 目录创建 .env 写入这两个变量（见 src/llm_config.py 头部说明）。"
        )
    return OpenAI(api_key=api_key, base_url=base_url), model
