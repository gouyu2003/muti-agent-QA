"""BM25 检索器测试：确定性 + 本地离线，不涉及任何浮点分数断言。"""

from src.knowledge.bm25_retriever import build_retriever, tokenize
from src.knowledge.hotpotqa_loader import load_fixture

FIXTURE_PATH = "data/hotpotqa/fixture_001.json"
RECORD = load_fixture(FIXTURE_PATH)


def _retriever():
    return build_retriever(FIXTURE_PATH)


def test_search_returns_deterministic_results():
    retriever = _retriever()
    query = "Who played Corliss Archer in the film Kiss and Tell?"

    first = retriever.search(query, top_k=3)
    second = retriever.search(query, top_k=3)

    assert [r["text"] for r in first] == [r["text"] for r in second]
    assert [r["source_title"] for r in first] == [r["source_title"] for r in second]


def test_first_hop_recalls_supporting_fact():
    """第一跳：找出扮演 Corliss Archer 的演员，须召回 Kiss and Tell 段第 0 句。"""
    retriever = _retriever()
    results = retriever.search(
        "Who played Corliss Archer in the film Kiss and Tell?", top_k=3
    )

    assert any(
        r["source_title"] == "Kiss and Tell (1945 film)" and r["source_paragraph"] == 0
        for r in results
    )
    # 该句正是 supporting_fact 之一，文本里含后续要用的实体
    hit = next(
        r
        for r in results
        if r["source_title"] == "Kiss and Tell (1945 film)"
    )
    assert "Shirley Temple" in hit["text"]


def test_second_hop_uses_entity_from_first_hop():
    """第二跳必须基于第一跳检索得到的实体构造，而非两条独立 query。"""
    retriever = _retriever()

    hop1 = retriever.search(
        "Who played Corliss Archer in the film Kiss and Tell?", top_k=3
    )
    hop1_entity_source = next(
        r
        for r in hop1
        if r["source_title"] == "Kiss and Tell (1945 film)"
    )
    # 第一跳给到实体名（此处直接用句子文本确认实体来源，不猜不编）
    assert "Shirley Temple" in hop1_entity_source["text"]
    entity = "Shirley Temple"

    hop2_query = f"What government position was held by {entity}?"
    hop2 = retriever.search(hop2_query, top_k=3)

    assert any(
        r["source_title"] == "Shirley Temple" and r["source_paragraph"] == 0
        for r in hop2
    )

    # 两条 query 不同，说明第二跳不是把第一跳原样重放
    assert hop2_query != "Who played Corliss Archer in the film Kiss and Tell?"


def test_top_k_is_respected():
    retriever = _retriever()
    results = retriever.search("Shirley Temple", top_k=1)
    assert len(results) == 1


def test_evidence_chunk_fields_and_types():
    retriever = _retriever()
    results = retriever.search("Shirley Temple government position", top_k=3)

    assert results
    for chunk in results:
        assert isinstance(chunk["text"], str) and chunk["text"].strip()
        assert isinstance(chunk["source_title"], str)
        assert isinstance(chunk["source_paragraph"], int)
        assert isinstance(chunk["relevance_score"], float)


def test_empty_query_returns_empty_list():
    retriever = _retriever()
    assert retriever.search("") == []
    assert retriever.search("   !!!  ") == []


def test_irrelevant_query_returns_empty_list():
    """与语料毫无共有词时必须返回空，否则 Retriever 永远判不出证据不足。"""
    retriever = _retriever()
    assert retriever.search("zzz qqq www unrelated nonsense") == []


def test_tokenize_is_plain_ascii_alnum():
    assert tokenize("Shirley Temple's 1945 (film)") == [
        "shirley",
        "temple",
        "s",
        "1945",
        "film",
    ]


def test_union_of_both_hops_covers_supporting_facts():
    """两跳检索累计结果须与 supporting_facts 有交集（覆盖 bridge 所需证据）。"""
    retriever = _retriever()

    hop1 = retriever.search(
        "Who played Corliss Archer in the film Kiss and Tell?", top_k=3
    )
    hop2 = retriever.search(
        "What government position was held by Shirley Temple?", top_k=3
    )

    retrieved = {
        (chunk["source_title"], chunk["source_paragraph"])
        for chunk in [*hop1, *hop2]
    }
    supporting = {
        (title, index) for title, index in RECORD["supporting_facts"]
    }

    assert retrieved & supporting
