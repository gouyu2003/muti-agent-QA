"""Bounded JSON repair with public diagnostics; never store reasoning content."""
from __future__ import annotations

import json
from time import perf_counter
from typing import Callable

from src.agents.llm_retry import response_text
from src.llm_usage import extract_token_usage

ERROR_LABELS = {
    "empty_response": "模型返回空正文",
    "truncated_response": "模型输出被 Token 上限截断",
    "invalid_json": "模型输出不是合法 JSON",
    "invalid_schema": "模型输出缺少必需字段或字段类型错误",
    "invalid_citation": "答案引用无法定位，请原样复制提供的引用标识",
}

class InvalidCitation(ValueError):
    pass


def invoke_json(chat, prompt: str, retries: int, usage=None, *, validate: Callable,
                max_tokens: int | None = None) -> tuple[dict | None, dict]:
    calls = []
    request = prompt
    for attempt in range(max(1, int(retries) + 1)):
        start = perf_counter()
        try:
            response = usage.invoke(chat, request) if usage else chat.invoke(request)
        except Exception:
            if usage and not usage.cancelled.is_set():
                usage.record_attempt({"attempt": attempt + 1, "duration_ms": round((perf_counter() - start) * 1000),
                    "finish_reason": None, "output_chars": 0, "outcome": "request_error"})
            raise
        raw = response_text(response).strip()
        metadata = getattr(response, "response_metadata", None) or {}
        finish = metadata.get("finish_reason")
        token_usage = metadata.get("token_usage") or {}
        details = token_usage.get("completion_tokens_details") or {}
        lc_details = (getattr(response, "usage_metadata", None) or {}).get("output_token_details") or {}
        prompt_tokens, completion_tokens, total_tokens = extract_token_usage(response)
        entry = {"attempt": attempt + 1, "duration_ms": round((perf_counter() - start) * 1000),
                 "finish_reason": finish, "output_chars": len(raw),
                 "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                 "total_tokens": total_tokens,
                 "reasoning_tokens": details.get("reasoning_tokens", lc_details.get("reasoning")),
                 "token_limit_reached": completion_tokens is not None and max_tokens is not None and completion_tokens >= max_tokens}
        entry["request_policy"] = {
            "response_format": (getattr(chat, "model_kwargs", None) or {}).get("response_format"),
            "thinking": (getattr(chat, "extra_body", None) or {}).get("thinking"),
        }
        error = None
        data = None
        if finish == "length":
            error = "truncated_response"
        elif not raw:
            error = "empty_response"
        else:
            # Permit a complete JSON fence, never salvage a truncated object.
            text = raw
            if text.startswith("```") and text.endswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                error = "invalid_json"
            if error is None:
                try:
                    if not isinstance(data, dict):
                        raise ValueError("object required")
                    validate(data)
                except InvalidCitation:
                    error = "invalid_citation"
                    entry["rejected_references"] = [
                        {k: str(v)[:200] for k, v in ref.items() if k in
                         ("sq_id", "evidence_id", "chunk_id", "source_id", "source_title", "source_version", "source_paragraph")}
                        for ref in data.get("supporting_evidence", [])[:8] if isinstance(ref, dict)]
                except (ValueError, TypeError, KeyError):
                    error = "invalid_schema"
        entry["outcome"] = error or "ok"
        calls.append(entry)
        if usage:
            usage.record_attempt(entry)
        diagnostics = {"calls": calls, "retry_count": attempt, "output_error": error}
        if error is None:
            return data, diagnostics
        request = prompt + (
            f"\n\n上一响应无效：{ERROR_LABELS[error]}。重新输出一个完整且简短的 JSON 对象。"
            "只填写要求的字段，保持答案和说明简短，引用只复制标识，不重复原文。"
            "不要输出分析过程、Markdown 或 JSON 之外的文字。"
        )
    return None, diagnostics
