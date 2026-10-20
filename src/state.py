from typing import Annotated, Any, Literal, NotRequired, TypedDict


def merge_expert_results(left: dict | None, right: dict | None) -> dict:
    """Each expert owns one immutable result; replay is idempotent."""
    merged = dict(left or {})
    for key, value in (right or {}).items():
        if key in merged and merged[key] != value:
            raise ValueError(f'专家结果重复且内容不一致：{key}')
        merged[key] = value
    return merged


def merge_state(state: dict, update: dict) -> None:
    for key, value in update.items():
        state[key] = merge_expert_results(state.get(key), value) if key in ('expert_results', 'protocol_calls') else value


class SubQuestion(TypedDict):
    id: str
    question: str
    keywords: list[str]
    depends_on: list[str]


class EvidenceChunk(TypedDict):
    evidence_id: NotRequired[str]
    text: str
    source_title: str
    source_paragraph: NotRequired[int]
    relevance_score: float
    source_id: NotRequired[str]
    source_version: NotRequired[int]
    document_id: NotRequired[str]
    chunk_id: NotRequired[str]
    filename: NotRequired[str]
    heading_path: NotRequired[list[str]]
    line_start: NotRequired[int]
    line_end: NotRequired[int]
    char_start: NotRequired[int]
    char_end: NotRequired[int]


class EvidenceGroup(TypedDict):
    sq_id: str
    chunks: list[EvidenceChunk]
    sufficiency: Literal["sufficient", "insufficient", "unknown"]
    insufficiency_note: str


class RetrievalSupplementRequest(TypedDict):
    target_sq_id: str
    additional_keywords: list[str]
    reason: str


class ConflictPair(TypedDict):
    chunk_a: str
    chunk_b: str
    conflict_description: str


class SupportingEvidence(TypedDict):
    evidence_id: NotRequired[str]
    sq_id: str
    text: str
    source_title: str
    source_paragraph: NotRequired[int]
    source_id: NotRequired[str]
    source_version: NotRequired[int]
    document_id: NotRequired[str]
    chunk_id: NotRequired[str]
    filename: NotRequired[str]
    heading_path: NotRequired[list[str]]
    line_start: NotRequired[int]
    line_end: NotRequired[int]
    char_start: NotRequired[int]
    char_end: NotRequired[int]


class AgentState(TypedDict):
    model_diagnostics: NotRequired[dict[str, Any]]
    # 唯一的图初始输入
    question: str
    expert_results: NotRequired[Annotated[dict[str, Any], merge_expert_results]]

    # Planner 运行期写入
    sub_questions: NotRequired[list[SubQuestion]]
    retrieval_strategy: NotRequired[Literal["sequential", "parallel"]]
    plan_retry_count: NotRequired[int]

    # Retriever 运行期写入
    evidence_chunks: NotRequired[list[EvidenceGroup]]
    retrieval_retry_count: NotRequired[int]

    # Answer-Validator 运行期写入
    retrieval_supplement_request: NotRequired[RetrievalSupplementRequest | None]
    answer: NotRequired[str]
    verified: NotRequired[bool]
    failure_type: NotRequired[Literal["none", "insufficient", "conflict", "output_error"]]
    conflict_flag: NotRequired[bool]
    conflict_note: NotRequired[str]
    conflict_pairs: NotRequired[list[ConflictPair]]
    insufficient_sq_ids: NotRequired[list[str]]
    verification_note: NotRequired[str]
    supporting_evidence: NotRequired[list[SupportingEvidence]]
    validation_retry_count: NotRequired[int]
