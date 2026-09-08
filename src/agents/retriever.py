"""Retriever 节点：按依赖顺序检索，构造 EvidenceGroup。

多跳要点：
- 按 depends_on 排序，被依赖的子问题先检索；
- 后继子问题的 query 会拼入从第一跳证据中抽取的英文专名（实体），
  从而把第一跳得到的实体带进第二跳，而不是两条互不相关的 query；
  不要拼接第一跳整句或段落标题：实测会把检索结果拉回第一跳段落，
  使 bridge 实体被淹没，第二跳退化成第一跳；
- 只用本地 BM25 证据；检索不到就写 sufficiency="insufficient"，不编造。
"""

from __future__ import annotations

import re
from typing import Any, Optional, Protocol

from src.knowledge.bm25_retriever import build_retriever
from src.state import AgentState, EvidenceChunk, EvidenceGroup, SubQuestion

AGENT_ID = "retriever_v0"
TOP_K = 3


class RetrieverLike(Protocol):
    def search(self, query: str, top_k: int = 3) -> list[EvidenceChunk]: ...


_ENTITY_RE = re.compile(r"\b(?:[A-Z][a-z]+)(?:\s+[A-Z][a-z]+)+\b")


def extract_entities(text: str, limit: int = 3) -> list[str]:
    """从第一跳证据中确定性抽取英文专名（连续首字母大写的词组）。

    只保留长度 >= 2 的连续专名，单词（如 Kiss / American）噪声过大直接跳过。
    这些专名就是把第一跳实体带进第二跳的桥梁。
    """
    entities: list[str] = []
    for match in _ENTITY_RE.findall(text):
        if match not in entities:
            entities.append(match)
        if len(entities) >= limit:
            break
    return entities


def order_sub_questions(sub_questions: list[SubQuestion]) -> list[SubQuestion]:
    """被依赖的先检索；存在循环依赖时按原顺序兜底，绝不死循环。"""
    known_ids = {sq["id"] for sq in sub_questions}
    remaining = list(sub_questions)
    ordered: list[SubQuestion] = []
    placed: set[str] = set()

    while remaining:
        progressed = False
        for sq in list(remaining):
            deps = [d for d in sq.get("depends_on", []) if d in known_ids]
            if all(dep in placed for dep in deps):
                ordered.append(sq)
                placed.add(sq["id"])
                remaining.remove(sq)
                progressed = True
        if not progressed:
            ordered.extend(remaining)
            break
    return ordered


def build_evidence_groups(
    sub_questions: list[SubQuestion],
    retriever: RetrieverLike,
    supplement: Optional[dict[str, Any]] = None,
    top_k: int = TOP_K,
) -> list[EvidenceGroup]:
    """串行检索全部子问题，返回按输入顺序无关的 EvidenceGroup 列表。"""
    groups: list[EvidenceGroup] = []
    resolved: dict[str, EvidenceChunk] = {}

    for sq in order_sub_questions(sub_questions):
        hints: list[str] = []
        for dep in sq.get("depends_on", []):
            prior = resolved.get(dep)
            if prior is not None:
                # 只取第一跳证据中的专名作为实体提示：
                # 实测 source_title 会让第二跳退回第一跳段落，故不使用。
                for entity in extract_entities(prior["text"]):
                    if entity not in hints:
                        hints.append(entity)

        keywords = [k for k in sq.get("keywords", []) if isinstance(k, str)]
        if supplement and supplement.get("target_sq_id") == sq["id"]:
            keywords += [k for k in supplement.get("additional_keywords", []) if isinstance(k, str)]

        query = " ".join([sq["question"], *keywords, *hints]).strip()
        chunks = retriever.search(query, top_k=top_k)

        if chunks:
            resolved[sq["id"]] = chunks[0]
            groups.append({"sq_id": sq["id"], "chunks": chunks,
                           "sufficiency": "sufficient", "insufficiency_note": ""})
        else:
            groups.append({
                "sq_id": sq["id"],
                "chunks": [],
                "sufficiency": "insufficient",
                "insufficiency_note": f"本地知识库未检索到可回答「{sq['question']}」的证据",
            })
    return groups


def retriever_fn(
    state: AgentState,
    retriever: Optional[RetrieverLike] = None,
) -> dict[str, Any]:
    """LangGraph 节点函数。V0 不调用 LLM。"""
    local = retriever if retriever is not None else build_retriever()
    supplement = state.get("retrieval_supplement_request")

    groups = build_evidence_groups(
        list(state.get("sub_questions", [])), local, supplement
    )
    retry_count = int(state.get("retrieval_retry_count", 0)) + (1 if supplement else 0)

    return {"evidence_chunks": groups, "retrieval_retry_count": retry_count}
