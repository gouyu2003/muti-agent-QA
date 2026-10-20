"""创建 DeepSeek 兼容的 ChatOpenAI 客户端。

约束：
- 导入本模块不读取任何环境变量，只有调用 build_chat_model() 时才读；
  这样离线单元测试导入节点模块时不会因缺 key 而失败。
- API Key / base URL 必须来自环境变量，且 base_url 显式指向 DeepSeek。
- 异常信息只报变量名，绝不回显其值；不使用 print/log 输出 key。
"""

from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI

ENV_API_KEY = "DEEPSEEK_API_KEY"
ENV_BASE_URL = "DEEPSEEK_BASE_URL"
ENV_MODEL = "DEEPSEEK_MODEL"

def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"缺少环境变量 {name}：请在项目根目录的 .env 中配置（.env 禁止提交）"
        )
    return value

# 按入参配置与环境变量组装并实例化 DeepSeek 兼容的 ChatOpenAI 客户端对象。
def build_chat_model(agent_config: dict[str, Any]) -> ChatOpenAI:
    """按智能体配置创建客户端；模型名以 JSON 配置为准（配置驱动）。"""
    model_config = agent_config["model_config"]

    api_key = require_env(ENV_API_KEY)
    base_url = require_env(ENV_BASE_URL)
    model_name = model_config.get("model_name") or os.environ.get(ENV_MODEL, "").strip()
    if not model_name:
        raise RuntimeError("模型名为空：需配置 model_config.model_name 或环境变量 DEEPSEEK_MODEL")

    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=model_config.get("temperature", 0.0),
        max_tokens=model_config.get("max_tokens"),
        model_kwargs={"response_format": {"type": "json_object"}},
        # These nodes extract short structured answers, not open-ended reasoning.
        extra_body={"thinking": {"type": "disabled"}} if model_name.lower().startswith("deepseek") else {},
        timeout=agent_config.get('_request_timeout', 120),
        max_retries=0,
    )
