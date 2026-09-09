"""LLM 调用的重试封装。

背景：deepseek-v4-flash 是推理模型，reasoning tokens 会计入 max_tokens 预算，
偶发地把预算耗尽，此时 content 返回空字符串（finish_reason=length）。
这类抖动不应让整张图崩溃，因此统一在这里重试，而不是散落到各节点。

约束：
- 纯 Python，不读环境变量、不做网络请求之外的任何副作用；
- chat 由调用方注入，离线测试传 fake 即可，重试对非空输出零影响（只调用一次）。
"""

from __future__ import annotations

from typing import Any

DEFAULT_RETRIES = 2


def response_text(response: Any) -> str:
    """取 AIMessage.content，兼容调用方直接返回字符串的情况。"""
    raw = getattr(response, "content", response)
    return raw if isinstance(raw, str) else str(raw)


def invoke_text(chat: Any, prompt: str, retries: int = DEFAULT_RETRIES) -> str:
    """调用 LLM 并返回非空文本；空输出按 retries 重试，仍为空则返回最后一次内容。

    retries 取自智能体配置的 failure_handling.max_retries（配置驱动）。
    """
    attempts = max(1, int(retries) + 1)
    text = ""
    for _ in range(attempts):
        text = response_text(chat.invoke(prompt)).strip()
        if text:
            return text
    return text
