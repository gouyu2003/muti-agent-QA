import json

import pytest
from langchain_core.messages import AIMessage

from src.agents.planner import parse_plan, planner_fn


class FakeLLM:
    """记录 prompt、返回固定内容，全程离线。"""

    def __init__(self, payload):
        self.payload = payload
        self.prompts: list[str] = []

    def invoke(self, prompt: str):
        self.prompts.append(prompt)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return AIMessage(content=text)


NORMAL_PAYLOAD = {
    "sub_questions": [
        {"id": "sq_1", "question": "Who played Corliss Archer in the film Kiss and Tell?",
         "keywords": ["Corliss Archer", "Kiss and Tell"], "depends_on": []},
        {"id": "sq_2", "question": "What government position was held by that actress?",
         "keywords": ["government position"], "depends_on": ["sq_1"]},
    ],
    "retrieval_strategy": "sequential",
}


def test_planner_returns_sub_questions():
    out = planner_fn({"question": "..."}, llm=FakeLLM(NORMAL_PAYLOAD))
    assert [sq["id"] for sq in out["sub_questions"]] == ["sq_1", "sq_2"]
    assert out["retrieval_strategy"] == "sequential"
    assert out["plan_retry_count"] == 0


def test_planner_increments_retry_and_clears_conflict():
    llm = FakeLLM(NORMAL_PAYLOAD)
    out = planner_fn(
        {"question": "...", "conflict_flag": True, "conflict_note": "两段证据矛盾",
         "plan_retry_count": 1},
        llm=llm,
    )
    assert out["plan_retry_count"] == 2
    assert out["conflict_flag"] is False and out["conflict_note"] == ""
    assert "冲突说明" in llm.prompts[0]


def test_parse_plan_accepts_markdown_fence():
    raw = '好的：\n```json\n{"sub_questions": [{"question": "Q?"}], "retrieval_strategy": "parallel"}\n```'
    assert parse_plan(raw)["retrieval_strategy"] == "parallel"


def test_parse_plan_rejects_invalid_json():
    with pytest.raises(ValueError, match="JSON"):
        parse_plan("不是 JSON")


def test_parse_plan_rejects_empty_sub_questions():
    with pytest.raises(ValueError, match="sub_questions"):
        parse_plan('{"sub_questions": []}')


def test_parse_plan_falls_back_on_bad_strategy():
    assert parse_plan('{"sub_questions": [{"question": "Q?"}], "retrieval_strategy": "weird"}')[
        "retrieval_strategy"
    ] == "sequential"
