"""LLM 输出解析：统一处理 Markdown 代码块、非法 JSON 与缺字段。"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _candidates(text: str) -> Iterator[str]:
    yield text
    for block in _FENCE_RE.findall(text):
        yield block.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        yield text[start : end + 1]


def extract_json_object(raw: str) -> dict[str, Any]:
    """提取第一个 JSON 对象；全部尝试失败时抛出带原文片段的 ValueError。"""
    text = (raw or "").strip()
    for candidate in _candidates(text):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError(f"LLM 输出不是合法 JSON 对象：{text[:200]!r}")


def as_str(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    return value if isinstance(value, str) else default


def as_bool(data: dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    return value if isinstance(value, bool) else default


def as_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key, [])
    return value if isinstance(value, list) else []
