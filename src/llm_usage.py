"""Collect LLM call and token usage without coupling agents to persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from threading import Condition, Event, RLock
from contextlib import contextmanager


@dataclass(frozen=True)
class LLMUsageSnapshot:
    """A truthful aggregate of the usage observed for one run."""

    call_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _first_int(mapping: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _non_negative_int(mapping.get(key))
        if value is not None:
            return value
    return None


def extract_token_usage(response: Any) -> tuple[int | None, int | None, int | None]:
    """Read LangChain and OpenAI-compatible token metadata from a response."""

    candidates: list[Any] = [getattr(response, "usage_metadata", None)]

    response_metadata = _as_mapping(getattr(response, "response_metadata", None))
    if response_metadata is not None:
        candidates.extend(
            [
                response_metadata.get("token_usage"),
                response_metadata.get("usage"),
                response_metadata,
            ]
        )

    additional_kwargs = _as_mapping(getattr(response, "additional_kwargs", None))
    if additional_kwargs is not None:
        candidates.extend(
            [additional_kwargs.get("token_usage"), additional_kwargs.get("usage")]
        )

    for candidate in candidates:
        usage = _as_mapping(candidate)
        if not usage:
            continue
        prompt_tokens = _first_int(usage, "input_tokens", "prompt_tokens")
        completion_tokens = _first_int(usage, "output_tokens", "completion_tokens")
        total_tokens = _first_int(usage, "total_tokens")
        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        if any(value is not None for value in (prompt_tokens, completion_tokens, total_tokens)):
            return prompt_tokens, completion_tokens, total_tokens

    return None, None, None


class LLMUsageCollector:
    """Collect every invocation attempt and aggregate complete token metadata."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._idle = Condition(self._lock)
        self._active_nodes = 0
        self.cancelled = Event()
        self._children: dict[str, LLMUsageCollector] = {}
        self.outputs: dict[str, dict] = {}
        self._attempts: list[dict] = []
        self.call_count = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._prompt_complete = True
        self._completion_complete = True
        self._total_complete = True

    def invoke(self, chat: Any, prompt: str) -> Any:
        """Invoke a chat model and account for the attempt and returned usage."""

        self.check_cancelled()
        with self._lock:
            self.call_count += 1
        try:
            response = chat.invoke(prompt)
        except Exception:
            with self._lock:
                self._mark_missing()
            raise

        prompt_tokens, completion_tokens, total_tokens = extract_token_usage(response)
        with self._lock:
            self._add_usage(prompt_tokens, completion_tokens, total_tokens)
        return response

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise RuntimeError('运行已取消。')

    def for_agent(self, agent_id: str) -> LLMUsageCollector:
        with self._lock:
            if agent_id not in self._children:
                child = LLMUsageCollector()
                child.cancelled = self.cancelled
                self._children[agent_id] = child
            return self._children[agent_id]

    def record_output(self, agent_id: str, output: dict) -> None:
        with self._lock:
            self.outputs[agent_id] = output

    @contextmanager
    def running_node(self):
        with self._idle:
            self.check_cancelled()
            self._active_nodes += 1
        try:
            yield
        finally:
            with self._idle:
                self._active_nodes -= 1
                self._idle.notify_all()

    def wait_for_nodes(self):
        with self._idle:
            self._idle.wait_for(lambda: self._active_nodes == 0)

    def completed_outputs(self):
        with self._lock:
            return dict(self.outputs)

    def agent_usage(self) -> dict[str, dict]:
        from dataclasses import asdict
        with self._lock:
            return {key: {**asdict(child.snapshot()), "calls": child.attempts()}
                    for key, child in self._children.items()}

    def record_attempt(self, entry: dict) -> None:
        with self._lock:
            self._attempts.append(dict(entry))

    def attempts(self) -> list[dict]:
        with self._lock:
            return [dict(entry) for entry in self._attempts]

    def _mark_missing(self) -> None:
        self._prompt_complete = False
        self._completion_complete = False
        self._total_complete = False

    def _add_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
    ) -> None:
        if prompt_tokens is None:
            self._prompt_complete = False
        else:
            self._prompt_tokens += prompt_tokens

        if completion_tokens is None:
            self._completion_complete = False
        else:
            self._completion_tokens += completion_tokens

        if total_tokens is None:
            self._total_complete = False
        else:
            self._total_tokens += total_tokens

    def snapshot(self) -> LLMUsageSnapshot:
        with self._lock:
            own = self._snapshot()
            children = [child.snapshot() for child in self._children.values()]
            used = ([own] if own.call_count else []) + [item for item in children if item.call_count]
            if not used:
                return own
            def total(field):
                values = [getattr(item, field) for item in used]
                return sum(values) if all(v is not None for v in values) else None
            return LLMUsageSnapshot(sum(item.call_count for item in used), total('prompt_tokens'),
                                    total('completion_tokens'), total('total_tokens'))

    def _snapshot(self) -> LLMUsageSnapshot:
        has_calls = self.call_count > 0
        return LLMUsageSnapshot(
            call_count=self.call_count,
            prompt_tokens=(
                self._prompt_tokens if has_calls and self._prompt_complete else None
            ),
            completion_tokens=(
                self._completion_tokens
                if has_calls and self._completion_complete
                else None
            ),
            total_tokens=(
                self._total_tokens if has_calls and self._total_complete else None
            ),
        )
