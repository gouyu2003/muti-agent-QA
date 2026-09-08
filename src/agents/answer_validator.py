"""Answer-Validator 节点：综合证据作答，并判定充分性 / 冲突。

防幻觉：supporting_evidence 只保留文本能在已检索证据中精确匹配到的条目。
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from src.agents.json_utils import as_bool, as_list, as_str, extract_json_object
from src.config_loader import load_agent_configs
from src.state import AgentState

AGENT_ID = "answer_validator_v0"
FAILURE_TYPES = ("none", "insufficient", "conflict")
MAX_EVIDENCE_CHARS = 600


class ChatLike(Protocol):
    def invoke(self, prompt: str) -> Any: ...


def default_llm() -> ChatLike:
    from src.llm import build_chat_model

    return build_chat_model(load_agent_configs()[AGENT_ID])


def build_prompt(state: AgentState, system_prompt: str) -> str:
    lines = [system_prompt, "", f"原始问题：{state.get('question', '')}", "", "子问题："]
    for sq in state.get("sub_questions", []):
        lines.append(f"- {sq['id']}: {sq['question']}")

    lines += ["", "证据："]
    for group in state.get("evidence_chunks", []):
        lines.append(f"[{group['sq_id']}] sufficiency={group.get('sufficiency')}")
        for chunk in group.get("chunks", []):
            text = chunk["text"][:MAX_EVIDENCE_CHARS]
            lines.append(f"  - ({chunk['source_title']} #{chunk['source_paragraph']}) {text}")
        if not group.get("chunks"):
            lines.append(f"  - 无证据：{group.get('insufficiency_note', '')}")

    lines += [
        "",
        "只输出一个 JSON 对象，字段：answer, verified, failure_type(none/insufficient/conflict),"
        " insufficient_sq_ids, conflict_flag, conflict_note, conflict_pairs, "
        "supporting_evidence[{sq_id,text,source_title}], verification_note",
    ]
    return "\n".join(lines)


def parse_verification(raw: str, state: AgentState) -> dict[str, Any]:
    """纯函数：解析并规整验证结果，处理脏输出与字段缺失。"""
    data = extract_json_object(raw)

    known_texts = {
        chunk["text"]
        for group in state.get("evidence_chunks", [])
        for chunk in group.get("chunks", [])
    }

    answer = as_str(data, "answer").strip()
    verified = as_bool(data, "verified", False)

    failure_type = as_str(data, "failure_type", "none").strip().lower()
    if failure_type not in FAILURE_TYPES:
        failure_type = "none"

    insufficient_sq_ids = [i for i in as_list(data, "insufficient_sq_ids") if isinstance(i, str)]
    conflict_pairs = [
        {
            "chunk_a": as_str(p, "chunk_a"),
            "chunk_b": as_str(p, "chunk_b"),
            "conflict_description": as_str(p, "conflict_description"),
        }
        for p in as_list(data, "conflict_pairs")
        if isinstance(p, dict)
    ]

    # 一致性规整：自相矛盾的输出一律按"未通过"处理，
    # 宁可让路由触发重规划/补检索，也不放行可疑答案
    if verified and failure_type != "none":
        verified = False
    if not verified and failure_type == "none":
        failure_type = "conflict" if (as_bool(data, "conflict_flag", False) or conflict_pairs) else "insufficient"

    conflict_flag = failure_type == "conflict"

    # 防幻觉：只保留能在已检索证据中精确匹配的支撑证据
    supporting_evidence = []
    for item in as_list(data, "supporting_evidence"):
        if not isinstance(item, dict):
            continue
        text = as_str(item, "text")
        if text and text in known_texts:
            supporting_evidence.append(
                {"sq_id": as_str(item, "sq_id"), "text": text,
                 "source_title": as_str(item, "source_title")}
            )

    result: dict[str, Any] = {
        "answer": answer,
        "verified": verified and failure_type == "none",
        "failure_type": failure_type,
        "insufficient_sq_ids": insufficient_sq_ids,
        "conflict_flag": conflict_flag,
        "conflict_note": as_str(data, "conflict_note") if conflict_flag else "",
        "conflict_pairs": conflict_pairs if conflict_flag else [],
        "verification_note": as_str(data, "verification_note"),
        "supporting_evidence": supporting_evidence,
    }

    if failure_type == "insufficient":
        target = insufficient_sq_ids[0] if insufficient_sq_ids else ""
        supplied = data.get("retrieval_supplement_request")
        if isinstance(supplied, dict) and as_str(supplied, "target_sq_id"):
            result["retrieval_supplement_request"] = {
                "target_sq_id": as_str(supplied, "target_sq_id"),
                "additional_keywords": [k for k in as_list(supplied, "additional_keywords") if isinstance(k, str)],
                "reason": as_str(supplied, "reason") or result["verification_note"],
            }
        elif target:
            keywords = _keywords_for(state, target)
            result["retrieval_supplement_request"] = {
                "target_sq_id": target,
                "additional_keywords": keywords,
                "reason": result["verification_note"] or f"子问题 {target} 证据不足",
            }
    return result


def _keywords_for(state: AgentState, sq_id: str) -> list[str]:
    for sq in state.get("sub_questions", []):
        if sq["id"] == sq_id:
            return [k for k in sq.get("keywords", []) if isinstance(k, str)]
    return []


def answer_validator_fn(state: AgentState, llm: Optional[ChatLike] = None) -> dict[str, Any]:
    config = load_agent_configs()[AGENT_ID]
    chat = llm if llm is not None else default_llm()

    prompt = build_prompt(state, config["model_config"]["system_prompt"])
    response = chat.invoke(prompt)
    raw = getattr(response, "content", response)

    result = parse_verification(raw if isinstance(raw, str) else str(raw), state)
    result["validation_retry_count"] = int(state.get("validation_retry_count", 0)) + 1
    return result
