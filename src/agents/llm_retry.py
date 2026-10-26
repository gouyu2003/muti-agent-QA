"""旧文本节点的空正文修正；JSON 节点使用 structured_output。

空正文按节点配置有界重试。请求异常、脱敏诊断和临时故障重试统一交给
LLMUsageCollector，每次实际调用均记账；有效非空响应直接返回。
chat 由调用方注入，离线测试可使用假模型。
"""

from __future__ import annotations

from typing import Any

from src.llm_usage import LLMUsageCollector

DEFAULT_RETRIES = 2


def response_text(response: Any) -> str:
    """取 AIMessage.content，兼容调用方直接返回字符串的情况。"""
    raw = getattr(response, "content", response)
    return raw if isinstance(raw, str) else str(raw)


def invoke_text(
    chat: Any,
    prompt: str,
    retries: int = DEFAULT_RETRIES,
    usage_collector: LLMUsageCollector | None = None,
) -> str:
    """调用 LLM 并返回非空文本；空输出按 retries 重试，仍为空则返回最后一次内容。

    retries 取自智能体配置的 failure_handling.max_retries（配置驱动）。
    """
    attempts = max(1, int(retries) + 1)
    usage_collector = usage_collector if usage_collector is not None else LLMUsageCollector()
    text = ""
    for _ in range(attempts):
        response = usage_collector.invoke(chat, prompt)
        text = response_text(response).strip()
        if text:
            return text
    return text
