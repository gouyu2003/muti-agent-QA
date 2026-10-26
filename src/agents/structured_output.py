"""Bounded JSON repair with public diagnostics; never store reasoning content."""
from __future__ import annotations

import json
from time import perf_counter
from typing import Callable
from pydantic import ValidationError

from src.agents.llm_retry import response_text
from src.llm_usage import LLMUsageCollector, extract_token_usage

ERROR_LABELS = {
    "empty_response": "模型返回空正文",
    "truncated_response": "模型输出被 Token 上限截断",
    "invalid_json": "模型输出不是合法 JSON",
    "invalid_schema": "模型输出不符合字段或业务规则",
    "invalid_citation": "答案引用无法定位，请原样复制提供的引用标识",
}

class InvalidCitation(ValueError):
    pass


class SchemaViolation(ValueError):
    """A public, application-authored rule violation; never include model text."""

    def __init__(self, code: str, message: str, path: str = "$"):
        super().__init__(message)
        self.issue = {"path": path, "code": code, "message": message}


def validation_issues(exc: Exception) -> list[dict]:
    """Keep bounded field diagnostics, excluding rejected values and raw exceptions."""
    if isinstance(exc, SchemaViolation):
        return [dict(exc.issue)]
    if isinstance(exc, ValidationError):
        labels = {
            "missing": "缺少必需字段。", "extra_forbidden": "不允许额外字段，请删除该字段。",
            "string_type": "必须是字符串。", "list_type": "必须是数组。",
            "model_type": "必须是符合 schema 的对象。", "literal_error": "必须使用 schema 中允许的枚举值。",
            "string_pattern_mismatch": "必须符合 schema 中的字符串格式。",
            "string_too_short": "字符串长度低于 schema 的下限。",
            "string_too_long": "字符串长度超过 schema 的上限。",
            "too_long": "元素数量超过 schema 的上限。",
        }
        issues = []
        for error in exc.errors(include_input=False, include_url=False)[:8]:
            rule = (error.get("ctx") or {}).get("error")
            if isinstance(rule, SchemaViolation):
                issues.append(dict(rule.issue))
            else:
                issues.append({"path": ".".join(str(p)[:64] for p in error["loc"][:8]) or "$",
                    "code": error["type"], "message": labels.get(error["type"], "字段不符合 schema 的约束。")})
        return issues
    return [{"path": "$", "code": "invalid_schema", "message": "输出不符合约定结构，请核对 schema。"}]


def invoke_json(chat, prompt: str, retries: int, usage=None, *, validate: Callable,
                max_tokens: int | None = None) -> tuple[dict | None, dict]:
    calls = []
    usage = usage if usage is not None else LLMUsageCollector()
    request = prompt
    for attempt in range(max(1, int(retries) + 1)):
        start = perf_counter()
        previous = len(usage.attempts())
        response = usage.invoke(chat, request)
        calls.extend(usage.attempts()[previous:])
        raw = response_text(response).strip()
        metadata = getattr(response, "response_metadata", None) or {}
        finish = metadata.get("finish_reason")
        token_usage = metadata.get("token_usage") or {}
        details = token_usage.get("completion_tokens_details") or {}
        lc_details = (getattr(response, "usage_metadata", None) or {}).get("output_token_details") or {}
        prompt_tokens, completion_tokens, total_tokens = extract_token_usage(response)
        entry = {"attempt": len(calls) + 1, "output_attempt": attempt + 1,
                 "duration_ms": round((perf_counter() - start) * 1000),
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
                        raise SchemaViolation("object_required", "顶层必须是 JSON 对象。")
                    validate(data)
                except InvalidCitation:
                    error = "invalid_citation"
                    entry["rejected_references"] = [
                        {k: str(v)[:200] for k, v in ref.items() if k in
                         ("sq_id", "evidence_id", "chunk_id", "source_id", "source_title", "source_version", "source_paragraph")}
                        for ref in data.get("supporting_evidence", [])[:8] if isinstance(ref, dict)]
                except (ValueError, TypeError, KeyError) as exc:
                    error = "invalid_schema"
                    entry["validation_errors"] = validation_issues(exc)
        entry["outcome"] = error or "ok"
        calls.append(entry)
        if usage:
            usage.record_attempt(entry)
        diagnostics = {"calls": calls, "retry_count": len(calls) - 1, "output_error": error}
        if error is None:
            return data, diagnostics
        request = prompt + (
            f"\n\n上一响应无效：{ERROR_LABELS[error]}。重新输出一个完整且简短的 JSON 对象。"
            "只填写要求的字段，保持答案和说明简短，引用只复制标识，不重复原文。"
            "不要输出分析过程、Markdown 或 JSON 之外的文字。"
        )
        if entry.get("validation_errors"):
            request += "\n具体校验错误（只修正这些错误，仍须遵守完整 schema）：" + json.dumps(
                entry["validation_errors"], ensure_ascii=False)
    return None, diagnostics
