from typing import TypedDict, List, Optional
# 子问题数据结构（Planner 生成）
class SubQuestion(TypedDict):
    id:str
    question:str
    keywords:List[str]
    depends_on:List[str]

# 单个证据片段（Retriever 检索获得
class EvidenceChunk(TypedDict):
    text:str
    source_title:str
    source_paragraph: int
    relevance_score: float

# 子问题证据组
class EvidenceGroup(TypedDict):
    sq_id: str
    chunks: List[EvidenceChunk]
    sufficiency: str          # "sufficient" | "insufficient"
    insufficiency_note: str

# AgentState 是整个 Agent 图工作流（如 LangGraph）在各节点之间流转和维护的共享状态。
class AgentState(TypedDict):
    # 输入
    question: str
    # Planner 写入
    sub_questions: List[SubQuestion]
    retrieval_strategy: str
    plan_retry_count: int
    # Retriever 写入
    evidence_chunks: List[EvidenceGroup]
    retrieval_retry_count: int
    retrieval_supplement_request: Optional[dict]
    # Answer-Validator 写入
    answer: str
    verified: bool
    failure_type: str         # "none" | "insufficient" | "conflict"
    conflict_flag: bool
    conflict_note: str
    insufficient_sq_ids: List[str]
    verification_note: str
    supporting_evidence: List[dict]
    validation_retry_count: int