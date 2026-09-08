from src.agents.retriever import build_evidence_groups, order_sub_questions, retriever_fn
from src.knowledge.bm25_retriever import build_retriever

SQ1 = {"id": "sq_1", "question": "Who played Corliss Archer in the film Kiss and Tell?",
       "keywords": ["Corliss Archer"], "depends_on": []}
SQ2 = {"id": "sq_2", "question": "What government position was held by that actress?",
       "keywords": ["government position"], "depends_on": ["sq_1"]}


class RecordingRetriever:
    def __init__(self):
        self.queries: list[str] = []

    def search(self, query: str, top_k: int = 3):
        self.queries.append(query)
        return []


def test_dependent_sub_question_is_retrieved_after_its_dependency():
    assert [sq["id"] for sq in order_sub_questions([SQ2, SQ1])] == ["sq_1", "sq_2"]


def test_second_hop_query_carries_first_hop_entity():
    groups = build_evidence_groups([SQ1, SQ2], build_retriever("data/hotpotqa/fixture_001.json"))
    by_id = {g["sq_id"]: g for g in groups}

    assert by_id["sq_2"]["chunks"]
    assert any(c["source_title"] == "Shirley Temple" for c in by_id["sq_2"]["chunks"]), \
        "第二跳必须带回第一跳的实体，否则只是两条独立检索"


def test_missing_evidence_marks_insufficient_without_fabrication():
    groups = build_evidence_groups([SQ1], RecordingRetriever())
    assert groups[0]["sufficiency"] == "insufficient"
    assert groups[0]["chunks"] == []
    assert groups[0]["insufficiency_note"]


def test_supplement_request_adds_keywords_and_increments_retry():
    rec = RecordingRetriever()
    supplement = {"target_sq_id": "sq_1", "additional_keywords": ["extra"], "reason": "证据不足"}
    out = retriever_fn(
        {"sub_questions": [SQ1], "retrieval_supplement_request": supplement,
         "retrieval_retry_count": 1},
        retriever=rec,
    )
    assert "extra" in rec.queries[0]
    assert out["retrieval_retry_count"] == 2


def test_retriever_fn_uses_real_fixture_by_default():
    out = retriever_fn({"sub_questions": [SQ1, SQ2]})
    assert {g["sq_id"] for g in out["evidence_chunks"]} == {"sq_1", "sq_2"}
    assert out["retrieval_retry_count"] == 0
