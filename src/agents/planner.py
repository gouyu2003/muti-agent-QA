"""Planner 节点：把复杂问题拆解为可独立检索的子问题。

输入：state["question"]，可选 conflict_flag / conflict_note / conflict_pairs
输出：sub_questions、retrieval_strategy、plan_retry_count，并清掉冲突标记
"""
from __future__ import annotations 
from typing import Any,Optional,Protocol

from src.agents.json_utils import as_list, as_str, extract_json_object
from src.config_loader import load_agent_configs
from src.state import AgentState, SubQuestion
AGENT_ID = "planner_v0"
ALLOWED_STRATEGIES = ("sequential", "parallel")

class ChatLike(Protocol):
    """最小依赖：只需一个 invoke(prompt: str) -> 带 .content 的对象。"""
    def invoke(self,prompt:str) -> Any:...

def default_llm() -> ChatLike:
    from src.llm import build_chat_model  # 延迟导入，离线测试不依赖环境变量
    return build_chat_model(load_agent_configs()[AGENT_ID])

def build_prompt(state:AgentState,system_prompt:str) -> str:
    parts = [system_prompt,"",f"原始问题:{state['question']}",""]
    if state.get("conflict_flag"):
        parts += [
            "上次检索的证据存在冲突，请换一种分解方式重新规划：",
            f"冲突说明:{state.get('conflict_note','')}",
        ]
        for pair in state.get("conflict_pairs", []):
            parts.append(
                f"- {pair.get('chunk_a', '')} vs {pair.get('chunk_b', '')}："
                f"{pair.get('conflict_description', '')}"   
            )
        parts.append("")
    parts += [
        "只输出一个 JSON 对象，不要任何解释文字，格式：",
        '{"sub_questions": [{"id": "sq_1", "question": "...", '
        '"keywords": ["..."], "depends_on": []}], '
        '"retrieval_strategy": "sequential"}',
    ]
    return "\n".join(parts)

def parse_plan(raw: str, fallback_question: str = "") -> dict[str, Any]:
    """纯函数：把 LLM 文本解析为 Planner 输出片段，便于单测覆盖脏输出。

    fallback_question 非空时，即使模型没给出任何子问题也不抛异常，
    而是降级为「把原问题当作唯一子问题」，保证图不会因一次抖动而整体崩溃；
    两者都为空才视为真正的规划失败。
    """
    data = extract_json_object(raw)

    sub_questions: list[SubQuestion] = []
    for index, item in enumerate(as_list(data, "sub_questions"), start=1):
        if not isinstance(item, dict):
            continue
        question = as_str(item, "question").strip()
        if not question:
            continue
        sub_questions.append(
            {
                "id": as_str(item, "id").strip() or f"sq_{index}",
                "question": question,
                "keywords": [k for k in as_list(item, "keywords") if isinstance(k, str)],
                "depends_on": [d for d in as_list(item, "depends_on") if isinstance(d, str)],
            }
        )
    if not sub_questions:
        fallback = (fallback_question or "").strip()
        if not fallback:
            raise ValueError("Planner 输出中没有可用的 sub_questions")
        # 降级：原问题整体作为单一子问题，至少让检索与验证跑完，而不是中断整图
        sub_questions = [
            {"id": "sq_1", "question": fallback, "keywords": [], "depends_on": []}
        ]

    strategy = as_str(data, "retrieval_strategy", "sequential").strip().lower()
    if strategy not in ALLOWED_STRATEGIES:
        strategy = "sequential"

    return {"sub_questions": sub_questions, "retrieval_strategy": strategy}


def planner_fn(state: AgentState, llm: Optional[ChatLike] = None) -> dict[str, Any]:
    """LangGraph 节点函数：只返回增量字段，由框架合并回 AgentState。"""
    config = load_agent_configs()[AGENT_ID]
    chat = llm if llm is not None else default_llm()

    prompt = build_prompt(state, config["model_config"]["system_prompt"])
    response = chat.invoke(prompt)
    raw = getattr(response, "content", response)
    plan = parse_plan(raw if isinstance(raw, str) else str(raw), state.get("question", ""))

    retry_count = int(state.get("plan_retry_count", 0))
    if state.get("conflict_flag"):
        retry_count += 1

    return {
        "sub_questions": plan["sub_questions"],
        "retrieval_strategy": plan["retrieval_strategy"],
        "plan_retry_count": retry_count,
        "conflict_flag": False,
        "conflict_note": "",
        "conflict_pairs": [],
    }