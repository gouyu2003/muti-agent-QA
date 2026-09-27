from typing import Literal, NotRequired, TypedDict


class SubQuestion(TypedDict):
    id: str
    question: str
    keywords: list[str]
    depends_on: list[str]


class EvidenceChunk(TypedDict):
    text: str
    source_title: str
    source_paragraph: int
    relevance_score: float
    source_id: NotRequired[str]


class EvidenceGroup(TypedDict):
    sq_id: str
    chunks: list[EvidenceChunk]
    sufficiency: Literal["sufficient", "insufficient"]
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
    sq_id: str
    text: str
    source_title: str
    source_id: NotRequired[str]


class AgentState(TypedDict):
    # 唯一的图初始输入
    question: str

    # Planner 运行期写入
    sub_questions: NotRequired[list[SubQuestion]]
    retrieval_strategy: NotRequired[Literal["sequential", "parallel"]]
    plan_retry_count: NotRequired[int]

    # Retriever 运行期写入
    evidence_chunks: NotRequired[list[EvidenceGroup]]
    retrieval_retry_count: NotRequired[int]

    # Answer-Validator 运行期写入
    retrieval_supplement_request: NotRequired[RetrievalSupplementRequest]
    answer: NotRequired[str]
    verified: NotRequired[bool]
    failure_type: NotRequired[Literal["none", "insufficient", "conflict"]]
    conflict_flag: NotRequired[bool]
    conflict_note: NotRequired[str]
    conflict_pairs: NotRequired[list[ConflictPair]]
    insufficient_sq_ids: NotRequired[list[str]]
    verification_note: NotRequired[str]
    supporting_evidence: NotRequired[list[SupportingEvidence]]
    validation_retry_count: NotRequired[int]
