"""Answer-Validator 节点：综合证据作答，并判定充分性 / 冲突。

防幻觉：supporting_evidence 只保留能在已检索证据中匹配到的条目，且写回的
永远是检索到的原文，而不是模型的转述，避免"看起来有证据、实则是编造"。
"""

from __future__ import annotations

import re
import json
from typing import Any, Optional, Protocol

from src.agents.json_utils import as_bool, as_list, as_str, extract_json_object
from src.agents.structured_output import invoke_json, ERROR_LABELS, InvalidCitation
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


def build_prompt(state: AgentState, system_prompt: str, *, expert_mode: bool = False) -> str:
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
            reference = {'sq_id': group['sq_id'], 'source_title': chunk['source_title']}
            for field in ('evidence_id', 'source_id', 'source_version'):
                if field in chunk:
                    reference[field] = chunk[field]
            if chunk.get('chunk_id'):
                reference['chunk_id'] = chunk['chunk_id']
            elif 'source_paragraph' in chunk:
                reference['source_paragraph'] = chunk['source_paragraph']
            lines.append('    可直接复制的引用：' + json.dumps(reference, ensure_ascii=False))
        if not group.get("chunks"):
            lines.append(f"  - 无证据：{group.get('insufficiency_note', '')}")

    evidence_end = len(lines)
    lines += [
        "",
        "只输出一个 JSON 对象，字段：answer, verified, failure_type(none/insufficient/conflict),"
        " insufficient_sq_ids, retrieval_supplement_request, conflict_flag, conflict_note, conflict_pairs, "
        "supporting_evidence[{sq_id,text,source_title,source_paragraph,chunk_id}], verification_note。"
        "证据有 chunk_id 时支撑证据可只返回 {sq_id,chunk_id}，必须原样返回该 ID；"
        "HotpotQA 证据必须优先返回 {sq_id,source_title,source_paragraph}，source_paragraph 是零起始句子索引。"
        "只引用提供的证据，材料无答案时 verified=false。"
        "首先判断证据是否能回答原始问题，子问题仅是辅助计划；无关子问题缺少证据不能否决已有完整证据的答案。"
        "unknown 仅表示检索命中但语义充分性尚未判断。不要把词语命中当成关系成立；核对同名实体身份、必要的每一跳和与答案有关的事实冲突。"
        "答案只给简短实体、日期、数字或 yes/no，不解释；推理依据写入 verification_note。"
        "insufficient_sq_ids 和 retrieval_supplement_request 只描述阻碍原始问题作答的必要缺口。",
    ]
    if state.get('expert_results'):
        lines += ['', '各专家独立观点（依据证据判断，不按多数投票；不得将观点本身当作事实）：',
                  json.dumps({key: {k: value.get(k) for k in ('answer', 'verified', 'verification_note', 'supporting_evidence')}
                              for key, value in state['expert_results'].items()}, ensure_ascii=False)]
    if any(chunk.get('evidence_id') for group in state.get('evidence_chunks', []) for chunk in group['chunks']):
        lines += ['引用只需 {"sq_id":"原样复制","evidence_id":"原样复制"}；'
                  '从上方“可直接复制的引用”取得这两个字段，不添加 source_paragraph、source_title 等多余字段，不编造或缩写 ID。']
    if expert_mode:
        # Replace full-question verification instructions for a team member.
        lines = lines[:evidence_end]
        lines += [
            '你是团队专家，只负责提供本实例材料支持的事实，完整问题由最终审核者汇总。',
            '只输出简短 JSON：{"answer":"有依据的局部答案","verified":true,'
            '"failure_type":"none","answer_scope":"partial","missing_information":["材料未提供的请求项"],'
            '"supporting_evidence":[{"sq_id":"sq_1","evidence_id":"复制提供的标识"}],"verification_note":"简短依据"}。',
            'answer_scope 取 complete/partial/none。verified 仅核验你实际给出的事实，不要求独立覆盖整个问题。'
            '能回答部分问题时保留局部答案及引用，其他缺口写 missing_information。'
            '无可支持事实时 answer 为空、verified=false、failure_type=insufficient、answer_scope=none；'
            '材料矛盾时 failure_type=conflict、verified=false。不得猜测缺失信息。'
        ]
    return "\n".join(lines)


def parse_verification(raw: str, state: AgentState, *, require_citations: bool = True) -> dict[str, Any]:
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
            "retrieval_supplement_request": None,
        }

    data = extract_json_object(raw)

    known_entries = [
        (group["sq_id"], chunk)
        for group in state.get("evidence_chunks", [])
        for chunk in group.get("chunks", [])
        if isinstance(chunk.get("text"), str)
    ]

    answer = as_str(data, "answer").strip()
    verified = as_bool(data, "verified", False)

    failure_type = as_str(data, "failure_type", "none").strip().lower()
    if failure_type not in FAILURE_TYPES:
        failure_type = "none"

    known_ids = {sq['id'] for sq in state.get('sub_questions', [])}
    insufficient_sq_ids = [i for i in as_list(data, "insufficient_sq_ids") if isinstance(i, str) and i in known_ids]
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
    if verified and (as_bool(data, 'conflict_flag', False) or conflict_pairs):
        verified = False
        failure_type = 'conflict'
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
        candidates = [chunk for sq_id, chunk in known_entries if sq_id == as_str(item, 'sq_id')]
        for field in ('evidence_id', 'source_title', 'source_id', 'source_version', 'chunk_id'):
            if item.get(field) is not None and item.get(field) != '':
                candidates = [chunk for chunk in candidates if chunk.get(field) == item[field]]
        if item.get('source_paragraph') is not None:
            index = item['source_paragraph']
            if type(index) is not int or index < 0 or not item.get('source_title'):
                continue
            candidates = [chunk for chunk in candidates if chunk.get('source_paragraph') == index]
        elif not item.get('chunk_id') and not item.get('evidence_id'):
            if item.get('source_title'):
                candidates = [chunk for chunk in candidates if match_evidence(
                    as_str(item, 'text'), {normalize_text(chunk['text']): chunk['text']})]
            else:
                # A legacy text-only citation must identify exactly one retrieved
                # chunk across all sources in this subquestion. No fuzzy fallback.
                norm = normalize_text(as_str(item, 'text'))
                candidates = [chunk for chunk in candidates if norm and normalize_text(chunk['text']) == norm]
        if len(candidates) != 1:
            continue
        chunk = candidates[0]
        evidence = {'sq_id': as_str(item, 'sq_id'), 'text': chunk['text'],
                    'source_title': chunk['source_title'], **_provenance(chunk)}
        if evidence not in supporting_evidence:
            supporting_evidence.append(evidence)

    citation_validation_failed = verified and (not answer or not supporting_evidence)
    if citation_validation_failed:
        verified = False
        failure_type = "insufficient"
        conflict_flag = False

    result: dict[str, Any] = {
        "answer": answer,
        "verified": verified and failure_type == "none",
        "failure_type": failure_type,
        "insufficient_sq_ids": insufficient_sq_ids if not verified else [],
        "conflict_flag": conflict_flag,
        "conflict_note": as_str(data, "conflict_note") if conflict_flag else "",
        "conflict_pairs": conflict_pairs if conflict_flag else [],
        "verification_note": as_str(data, "verification_note"),
        "supporting_evidence": supporting_evidence,
        "retrieval_supplement_request": None,
    }
    if citation_validation_failed:
        result['verification_note'] += ' 服务端校验：答案为空或没有可定位的引用，验证未通过。'

    if failure_type == "insufficient":
        target = insufficient_sq_ids[0] if insufficient_sq_ids else next(
            (sq['id'] for sq in state.get('sub_questions', [])), '')
        supplied = data.get("retrieval_supplement_request")
        if isinstance(supplied, dict) and as_str(supplied, "target_sq_id") in known_ids:
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
    fields = ("evidence_id", "source_paragraph", "source_id", "source_version", "document_id", "chunk_id", "filename",
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
    require_citations: bool = True,
    expert_mode: bool = False,
) -> dict[str, Any]:
    config = agent_config or load_agent_configs()[AGENT_ID]
    chat = llm if llm is not None else default_llm(config)

    prompt = build_prompt(state, config["model_config"]["system_prompt"], expert_mode=expert_mode)
    max_retries = config.get("failure_handling", {}).get("max_retries", 2)
    def validate(data):
        if not isinstance(data.get('answer'), str) or type(data.get('verified')) is not bool:
            raise ValueError('answer and verified required')
        if data.get('failure_type') not in FAILURE_TYPES or not isinstance(data.get('supporting_evidence', []), list):
            raise ValueError('failure_type and supporting_evidence required')
        if data['verified']:
            parsed = parse_verification(json.dumps(data), state, require_citations=require_citations)
            if data['answer'].strip() and not parsed['supporting_evidence']:
                raise InvalidCitation()
    data, diagnostics = invoke_json(chat, prompt, max_retries, usage_collector,
        validate=validate, max_tokens=config['model_config'].get('max_tokens'))
    if data is None:
        result = parse_verification('', state, require_citations=require_citations)
        result['failure_type'] = 'output_error'
        result['verification_note'] = f"{ERROR_LABELS[diagnostics['output_error']]}；已调用 {len(diagnostics['calls'])} 次，未取得有效结构化结果。"
    else:
        result = parse_verification(json.dumps(data), state, require_citations=require_citations)
        if expert_mode:
            scope = data.get('answer_scope')
            result['answer_scope'] = scope if scope in ('complete', 'partial', 'none') else ('partial' if result['answer'] else 'none')
            result['missing_information'] = [s for s in data.get('missing_information', []) if isinstance(s, str)] if isinstance(data.get('missing_information'), list) else []
    result['model_diagnostics'] = diagnostics
    result["validation_retry_count"] = int(state.get("validation_retry_count", 0)) + 1
    return result
