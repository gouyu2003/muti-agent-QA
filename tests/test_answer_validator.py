import json

import pytest
from langchain_core.messages import AIMessage

from src.agents.answer_validator import answer_validator_fn, parse_verification


class FakeLLM:
    """记录 prompt、返回固定内容，全程离线、不产生 API 成本。"""

    def __init__(self, payload):
        self.payload = payload
        self.prompts: list[str] = []

    def invoke(self, prompt: str):
        self.prompts.append(prompt)
        text = (
            self.payload
            if isinstance(self.payload, str)
            else json.dumps(self.payload, ensure_ascii=False)
        )
        return AIMessage(content=text)

STATE = {
    "question": "Q?",
    "sub_questions": [{"id": "sq_1", "question": "q1", "keywords": ["k1"], "depends_on": []}],
    "evidence_chunks": [
        {"sq_id": "sq_1", "sufficiency": "sufficient", "insufficiency_note": "",
         "chunks": [{"text": "E1", "source_title": "T1", "source_paragraph": 0, "relevance_score": 1.0}]}
    ],
}

PASS = {"answer": "A", "verified": True, "failure_type": "none",
        "supporting_evidence": [{"sq_id": "sq_1", "text": "E1", "source_title": "T1"}],
        "verification_note": "ok"}
INSUFF = {"answer": "", "verified": False, "failure_type": "insufficient",
          "insufficient_sq_ids": ["sq_1"], "verification_note": "缺少关键证据"}
CONFLICT = {"answer": "", "verified": False, "failure_type": "conflict",
            "conflict_note": "两段证据矛盾",
            "conflict_pairs": [{"chunk_a": "T1#0", "chunk_b": "T2#1", "conflict_description": "年份不一致"}]}


def test_pass_path():
    out = answer_validator_fn(STATE, llm=FakeLLM(PASS))
    assert out["verified"] is True and out["failure_type"] == "none"
    assert out["supporting_evidence"][0]["text"] == "E1"
    assert out["validation_retry_count"] == 1


def test_insufficient_creates_supplement_request():
    out = answer_validator_fn(STATE, llm=FakeLLM(INSUFF))
    assert out["failure_type"] == "insufficient"
    assert out["retrieval_supplement_request"]["target_sq_id"] == "sq_1"
    assert "k1" in out["retrieval_supplement_request"]["additional_keywords"]
    assert out["conflict_flag"] is False


def test_conflict_sets_flag_and_pairs():
    out = answer_validator_fn(STATE, llm=FakeLLM(CONFLICT))
    assert out["conflict_flag"] is True and out["conflict_pairs"]


def test_parses_markdown_fenced_output():
    out = answer_validator_fn(STATE, llm=FakeLLM("```json\n" + json.dumps(PASS) + "\n```"))
    assert out["verified"] is True


def test_parser_rejects_invalid_json_and_node_fails_closed():
    with pytest.raises(ValueError, match="JSON"):
        parse_verification("抱歉，我无法回答", STATE)
    result = answer_validator_fn(STATE, llm=FakeLLM("抱歉，我无法回答"))
    assert not result['verified'] and result['failure_type'] == 'insufficient'


def test_drops_hallucinated_supporting_evidence():
    payload = dict(PASS, supporting_evidence=[{"sq_id": "sq_1", "text": "编造的证据", "source_title": "X"}])
    assert answer_validator_fn(STATE, llm=FakeLLM(payload))["supporting_evidence"] == []


def test_duplicate_evidence_uses_matching_source_title_and_id():
    state = dict(STATE)
    state["evidence_chunks"] = [{
        "sq_id": "sq_1", "sufficiency": "sufficient", "insufficiency_note": "",
        "chunks": [
            {"text": "Same fact", "source_title": "First", "source_paragraph": 0,
             "relevance_score": 1.0, "source_id": "source-a"},
            {"text": "Same fact", "source_title": "Second", "source_paragraph": 0,
             "relevance_score": 0.9, "source_id": "source-b"},
        ],
    }]
    payload = dict(PASS, supporting_evidence=[
        {"sq_id": "sq_1", "text": "Same fact", "source_title": "First"}
    ])
    item = parse_verification(json.dumps(payload), state)["supporting_evidence"][0]
    assert (item["source_title"], item["source_id"]) == ("First", "source-a")


def test_inconsistent_verified_is_normalized():
    out = parse_verification(json.dumps({"answer": "A", "verified": True, "failure_type": "conflict"}), STATE)
    assert out["failure_type"] == "conflict" and out["verified"] is False


LONG_TEXT = "Shirley Temple served as Chief of Protocol of the United States."
STATE_LONG = {
    "question": "Q?",
    "sub_questions": [{"id": "sq_1", "question": "q1", "keywords": [], "depends_on": []}],
    "evidence_chunks": [
        {"sq_id": "sq_1", "sufficiency": "sufficient", "insufficiency_note": "",
         "chunks": [{"text": LONG_TEXT, "source_title": "Shirley Temple",
                     "source_paragraph": 0, "relevance_score": 1.0}]}
    ],
}


def _supporting(payload_text):
    payload = {"answer": "A", "verified": True, "failure_type": "none",
               "supporting_evidence": [{"sq_id": "sq_1", "text": payload_text,
                                        "source_title": "Shirley Temple"}]}
    return parse_verification(json.dumps(payload), STATE_LONG)["supporting_evidence"]


def test_paraphrased_evidence_is_accepted_and_rewritten_to_source():
    """模型转述/截取证据时仍应保留，且写回的是检索原文，不是模型措辞。"""
    items = _supporting("shirley temple served as Chief of Protocol of the United States, "
                        "a post she held in the 1970s")
    assert len(items) == 1
    assert items[0]["text"] == LONG_TEXT


def test_long_hallucinated_evidence_is_still_dropped():
    assert _supporting("Shirley Temple was born in 1928 and never held any office") == []
