from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agents.llm_retry import invoke_text
from src.llm_usage import LLMUsageCollector, extract_token_usage


class FakeChat:
    def __init__(self, responses):
        self.responses = iter(responses)

    def invoke(self, _prompt: str):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def message(content: str, usage: dict | None = None):
    return SimpleNamespace(content=content, usage_metadata=usage)


def test_extracts_langchain_usage_metadata():
    response = message(
        "ok",
        {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )

    assert extract_token_usage(response) == (11, 7, 18)


def test_extracts_openai_compatible_response_metadata():
    response = SimpleNamespace(
        content="ok",
        usage_metadata=None,
        response_metadata={
            "token_usage": {
                "prompt_tokens": 13,
                "completion_tokens": 5,
                "total_tokens": 18,
            }
        },
    )

    assert extract_token_usage(response) == (13, 5, 18)


def test_empty_response_retry_counts_and_sums_every_llm_call():
    collector = LLMUsageCollector()
    chat = FakeChat(
        [
            message(
                "",
                {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            ),
            message(
                "answer",
                {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            ),
        ]
    )

    assert invoke_text(chat, "prompt", retries=1, usage_collector=collector) == "answer"
    assert collector.snapshot().call_count == 2
    assert collector.snapshot().prompt_tokens == 20
    assert collector.snapshot().completion_tokens == 6
    assert collector.snapshot().total_tokens == 26


def test_missing_usage_keeps_token_totals_unknown_but_counts_call():
    collector = LLMUsageCollector()

    assert invoke_text(
        FakeChat([message("answer")]),
        "prompt",
        usage_collector=collector,
    ) == "answer"

    snapshot = collector.snapshot()
    assert snapshot.call_count == 1
    assert snapshot.prompt_tokens is None
    assert snapshot.completion_tokens is None
    assert snapshot.total_tokens is None


def test_failed_attempt_is_counted_without_inventing_token_usage():
    collector = LLMUsageCollector()

    with pytest.raises(RuntimeError, match="provider failed"):
        invoke_text(
            FakeChat([RuntimeError("provider failed")]),
            "prompt",
            usage_collector=collector,
        )

    snapshot = collector.snapshot()
    assert snapshot.call_count == 1
    assert snapshot.total_tokens is None
