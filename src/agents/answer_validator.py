"""Answer-Validator 节点：综合证据作答，并判定充分性 / 冲突。

防幻觉：supporting_evidence 只保留能在已检索证据中匹配到的条目，且写回的
永远是检索到的原文，而不是模型的转述，避免"看起来有证据、实则是编造"。
"""

from __future__ import annotations

import re
from typing import Any, Optional, Protocol

from src.agents.json_utils import as_bool, as_list, as_str, extract_json_object
from src.agents.llm_retry import invoke_text
from src.config_loader import load_agent_configs
from src.llm_usage import LLMUsageCollector
from src.state import AgentState

AGENT_ID = "answer_validator_v0"
FAILURE_TYPES = ("none", "insufficient", "conflict")
MAX_EVIDENCE_CHARS = 600

# 模糊匹配的最短归一化长度：低于此长度的短证据只接受归一化后完全相等，
# 避免"the"/"in" 这类碎片被当成命中
MIN_FUZZY_CHARS = 10

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """归一化：小写、去标点、压缩空白。用于容忍模型转述时的细微差异。"""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", (text or "").lower())).strip()


def match_evidence(text: str, known: dict[str, str]) -> str:
    """把模型给出的证据文本对齐回已检索原文；匹配不上返回空串。

    known 为 {归一化文本: 原始文本}。命中后返回原始文本，
    因此即使模型转述或截取，写回状态里的仍是检索到的原句。
    """
    norm = normalize_text(text)
    if not norm:
        return ""
    if norm in known:
        return known[norm]
    if len(norm) >= MIN_FUZZY_CHARS:
        for key, original in known.items():
            if norm in key or key in norm:
                return original
    return ""


class ChatLike(Protocol):
    def invoke(self, prompt: str) -> Any: ...


def default_llm(agent_config: dict[str, Any] | None = None) -> ChatLike:
    from src.llm import build_chat_model

    return build_chat_model(agent_config or load_agent_configs()[AGENT_ID])


def build_prompt(state: AgentState, system_prompt: str) -> str:
    lines = [system_prompt, "", f"原始问题：{state.get('question', '')}", "", "子问题："]
    for sq in state.get("sub_questions", []):
        lines.append(f"- {sq['id']}: {sq['question']}")

    lines += ["", "证据："]
    for group in state.get("evidence_chunks", []):
        lines.append(f"[{group['sq_id']}] sufficiency={group.get('sufficiency')}")
        for chunk in group.get("chunks", []):
            text = chunk["text"][:MAX_EVIDENCE_CHARS]
            location = (f"chunk_id={chunk['chunk_id']}; lines {chunk['line_start']}-{chunk['line_end']}"
                        if chunk.get("chunk_id") else f"#{chunk.get('source_paragraph', '?')}")
            lines.append(f"  - ({chunk['source_title']} {location}) {text}")
        if not group.get("chunks"):
            lines.append(f"  - 无证据：{group.get('insufficiency_note', '')}")

    lines += [
        "",
        "只输出一个 JSON 对象，字段：answer, verified, failure_type(none/insufficient/conflict),"
        " insufficient_sq_ids, conflict_flag, conflict_note, conflict_pairs, "
        "supporting_evidence[{sq_id,text,source_title,chunk_id}], verification_note。"
        "证据有 chunk_id 时支撑证据可只返回 {sq_id,chunk_id}，必须原样返回该 ID；"
        "只引用提供的证据，材料无答案时 verified=false。",
    ]
    return "\n".join(lines)


def parse_verification(raw: str, state: AgentState, *, require_citations: bool = False) -> dict[str, Any]:
    """纯函数：解析并规整验证结果，处理脏输出与字段缺失。

    raw 为空（推理 token 耗尽导致 content 为空）时不抛异常，
    而是按「证据不足」处理，交给路由触发补充检索，避免整图中断。
    """
    if not (raw or "").strip():
        return {
            "answer": "",
            "verified": False,
            "failure_type": "insufficient",
            "insufficient_sq_ids": [],
            "conflict_flag": False,
            "conflict_note": "",
            "conflict_pairs": [],
            "verification_note": "LLM 返回为空（推理 token 预算耗尽），按证据不足处理",
            "supporting_evidence": [],
        }

    data = extract_json_object(raw)

    known_entries = [
        (group["sq_id"], chunk)
        for group in state.get("evidence_chunks", [])
        for chunk in group.get("chunks", [])
        if isinstance(chunk.get("text"), str)
    ]
    known_texts = {normalize_text(chunk["text"]): chunk["text"] for _, chunk in known_entries}

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

    # 防幻觉：只保留能对齐回已检索证据的支撑证据，且写回原文而非模型转述
    supporting_evidence = []
    for item in as_list(data, "supporting_evidence"):
        if not isinstance(item, dict):
            continue
        if item.get("chunk_id"):
            candidates = [chunk for sq_id, chunk in known_entries
                          if sq_id == as_str(item, "sq_id") and chunk.get("chunk_id") == item["chunk_id"]]
            if len(candidates) != 1:
                continue
            chunk = candidates[0]
            supporting_evidence.append({"sq_id": as_str(item, "sq_id"), "text": chunk["text"],
                                        "source_title": chunk["source_title"], **_provenance(chunk)})
            continue
        matched = match_evidence(as_str(item, "text"), known_texts)
        if matched:
            candidates = [
                chunk for sq_id, chunk in known_entries
                if sq_id == as_str(item, "sq_id")
                and normalize_text(chunk["text"]) == normalize_text(matched)
            ]
            if not candidates:
                continue
            if any(candidate.get("chunk_id") for candidate in candidates) and len(candidates) != 1:
                continue
            chunk = next(
                (candidate for candidate in candidates
                 if candidate["source_title"] == as_str(item, "source_title")),
                candidates[0],
            )
            supporting_evidence.append(
                {"sq_id": as_str(item, "sq_id"), "text": matched,
                 "source_title": chunk["source_title"],
                 **_provenance(chunk)}
            )

    if verified and (require_citations or any(chunk.get("chunk_id") for _, chunk in known_entries)) and not supporting_evidence:
        verified = False
        failure_type = "insufficient"
        conflict_flag = False

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


def _provenance(chunk: dict[str, Any]) -> dict[str, Any]:
    fields = ("source_id", "source_version", "document_id", "chunk_id", "filename",
              "heading_path", "line_start", "line_end", "char_start", "char_end")
    return {key: chunk[key] for key in fields if key in chunk}


def _keywords_for(state: AgentState, sq_id: str) -> list[str]:
    for sq in state.get("sub_questions", []):
        if sq["id"] == sq_id:
            return [k for k in sq.get("keywords", []) if isinstance(k, str)]
    return []


def answer_validator_fn(
    state: AgentState,
    llm: Optional[ChatLike] = None,
    *,
    usage_collector: LLMUsageCollector | None = None,
    agent_config: dict[str, Any] | None = None,
    require_citations: bool = False,
) -> dict[str, Any]:
    config = agent_config or load_agent_configs()[AGENT_ID]
    chat = llm if llm is not None else default_llm(config)

    prompt = build_prompt(state, config["model_config"]["system_prompt"])
    max_retries = config.get("failure_handling", {}).get("max_retries", 2)
    raw = invoke_text(chat, prompt, max_retries, usage_collector)

    result = parse_verification(raw, state, require_citations=require_citations)
    result["validation_retry_count"] = int(state.get("validation_retry_count", 0)) + 1
    return result
