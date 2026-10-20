import json
from types import SimpleNamespace

import pytest

from src.agents.structured_output import invoke_json
from src.agents.answer_validator import answer_validator_fn
from src.agents.planner import planner_fn
from src.llm_usage import LLMUsageCollector
from src.runtime import RunService, SQLiteRunRepository
from src.router import decide_route
from src.config_loader import load_agent_configs


class Chat:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return next(self.responses)


def message(text, finish="stop"):
    return SimpleNamespace(content=text, usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        response_metadata={"finish_reason": finish, "token_usage": {"completion_tokens_details": {"reasoning_tokens": 0}}},
        additional_kwargs={"reasoning_content": "Must never be persisted"})


@pytest.mark.parametrize("bad,kind", [
    (message(""), "empty_response"),
    (message('{"answer":'), "invalid_json"),
    (message('{"answer":"looks complete"}', "length"), "truncated_response"),
    (message('[]'), "invalid_schema"),
])
def test_bad_output_repaired_with_bounded_calls_and_metadata(bad, kind):
    chat = Chat([bad, message('{"answer":"valid"}')])
    usage = LLMUsageCollector().for_agent("expert")
    data, diagnostics = invoke_json(chat, "JSON required", 1, usage, validate=lambda data: None)
    assert data == {"answer": "valid"}
    assert [call["outcome"] for call in diagnostics["calls"]] == [kind, "ok"]
    assert diagnostics["retry_count"] == 1
    assert "上一响应无效" in chat.prompts[1]
    assert diagnostics["calls"][0]["finish_reason"] == bad.response_metadata["finish_reason"]
    assert usage.snapshot().call_count == 2 and usage.snapshot().total_tokens == 60
    assert "Must never" not in json.dumps(usage.attempts())


def test_exhausted_format_failure_is_not_insufficient_and_does_not_retrieve_again():
    chat = Chat([message("not json")] * 3)
    result = answer_validator_fn({"question": "Q", "evidence_chunks": []}, llm=chat)
    assert result["failure_type"] == "output_error" and not result["verified"]
    assert result["retrieval_supplement_request"] is None
    assert result["model_diagnostics"]["retry_count"] == 2
    assert decide_route(result).reason == "terminal_fallback"


def test_planner_fallback_is_visible_after_invalid_schema():
    result = planner_fn({"question": "Q"}, llm=Chat([message('{"sub_questions":[]}')] * 3))
    assert result["sub_questions"][0]["question"] == "Q"
    assert result["model_diagnostics"]["fallback_used"]
    assert result["model_diagnostics"]["output_error"] == "invalid_schema"


def test_cancel_stops_format_repair_calls():
    usage = LLMUsageCollector()
    class Cancelling:
        def invoke(self, prompt):
            usage.cancelled.set()
            return message("invalid")
    with pytest.raises(RuntimeError, match="取消"):
        invoke_json(Cancelling(), "JSON", 2, usage, validate=lambda data: None)
    assert usage.snapshot().call_count == 1


def test_team_expert_can_verify_local_facts_without_answering_all_parts():
    state = {"question": "Start date and city?", "sub_questions": [{"id": "sq_1", "question": "Q"}],
        "evidence_chunks": [{"sq_id": "sq_1", "chunks": [{"text": "Started on 18 April 2026.",
            "source_title": "launch.txt", "chunk_id": "c1", "evidence_id": "e1", "line_start": 1, "line_end": 1}]}]}
    payload = {"answer": "18 April 2026", "verified": True, "failure_type": "none", "answer_scope": "partial",
        "missing_information": ["pilot city"], "supporting_evidence": [{"sq_id": "sq_1", "evidence_id": "e1"}]}
    chat = Chat([message(json.dumps(payload))])
    result = answer_validator_fn(state, llm=chat, expert_mode=True)
    assert result["verified"] and result["answer_scope"] == "partial"
    assert result["missing_information"] == ["pilot city"]
    assert result["supporting_evidence"][0]["text"] == "Started on 18 April 2026."
    assert "不要求独立覆盖整个问题" in chat.prompts[0]
    assert "首先判断证据是否能回答原始问题" not in chat.prompts[0]

def test_invalid_citation_is_repaired_without_accepting_forged_source():
    state = {"question": "Q", "sub_questions": [{"id": "sq_1", "question": "Q"}],
        "evidence_chunks": [{"sq_id": "sq_1", "chunks": [{"text": "Real fact", "source_title": "real.txt",
            "evidence_id": "real-id", "chunk_id": "c1", "line_start": 1, "line_end": 1}]}]}
    payload = {"answer": "A", "verified": True, "failure_type": "none",
        "supporting_evidence": [{"sq_id": "sq_1", "evidence_id": "forged-id"}]}
    invalid = json.dumps(payload)
    payload["supporting_evidence"][0]["evidence_id"] = "real-id"
    chat = Chat([message(invalid), message(json.dumps(payload))])
    result = answer_validator_fn(state, llm=chat)
    assert result["verified"] and result["supporting_evidence"][0]["source_title"] == "real.txt"
    calls = result["model_diagnostics"]["calls"]
    assert [call["outcome"] for call in calls] == ["invalid_citation", "ok"]
    assert calls[0]["rejected_references"][0]["evidence_id"] == "forged-id"


def test_model_request_has_json_mode_and_disables_deepseek_thinking(monkeypatch):
    from src.llm import build_chat_model
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr("src.llm.ChatOpenAI", lambda **kwargs: kwargs)
    config = load_agent_configs()["planner_v0"]
    config["model_config"]["model_name"] = "deepseek-v4.1-flash"
    request = build_chat_model(config)
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["model_kwargs"]["response_format"] == {"type": "json_object"}
    assert request["timeout"] == 120 and request["max_retries"] == 0
    config["model_config"]["model_name"] = "another-provider-model"
    assert build_chat_model(config)["extra_body"] == {}


def test_repaired_output_diagnostics_survive_run_history(tmp_path, monkeypatch):
    from src.runtime.context import build_v0_run_context
    monkeypatch.setattr("src.agents.planner.default_llm", lambda *_: Chat([
        message('{"sub_questions":[{"id":"sq_1","question":"Where?","keywords":[],"depends_on":[]}]}')]))
    monkeypatch.setattr("src.agents.answer_validator.default_llm", lambda *_: Chat([
        message('{"answer":'),
        message('{"answer":"","verified":false,"failure_type":"insufficient","supporting_evidence":[]}')]))
    repository = SQLiteRunRepository(tmp_path / "runs.sqlite3")
    context = build_v0_run_context("Where?", entrypoint="api", workflow_id="multi_hop_qa_fixed")
    detail = RunService(repository).run(context)
    assert detail.run.status == "completed"
    assert detail.run.final_state["model_diagnostics"]["retry_count"] == 1
    event = next(e for e in detail.events if e.event_type == "node_completed" and e.node == "answer_validator_v0")
    assert [c["outcome"] for c in event.payload["output"]["model_diagnostics"]["calls"]] == ["invalid_json", "ok"]
    assert detail.run.metrics.llm_call_count == 3
