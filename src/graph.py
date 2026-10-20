"""Build the supported LangGraph nodes and edges from a validated workflow."""

from __future__ import annotations

from functools import partial
from typing import Any, Callable, Optional

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from src.agents.answer_validator import answer_validator_fn
from src.agents.planner import planner_fn
from src.agents.retriever import retriever_fn
from src.llm_usage import LLMUsageCollector
from src.router import PLANNER_NODE, RETRIEVER_NODE, route_after_validator
from src.runtime.specs import WorkflowSpec
from src.state import AgentState

ANSWER_VALIDATOR_NODE = "answer_validator_v0"

NodeFn = Callable[[AgentState], dict[str, Any]]


def default_nodes(
    usage_collector: LLMUsageCollector | None = None,
    agent_configs: dict[str, dict[str, Any]] | None = None,
    retriever: Any | None = None,
) -> dict[str, NodeFn]:
    """真实节点实现：三个 LangGraph 节点函数。"""
    return {
        PLANNER_NODE: partial(
            planner_fn, usage_collector=usage_collector,
            agent_config=agent_configs[PLANNER_NODE] if agent_configs else None,
        ),
        RETRIEVER_NODE: partial(retriever_fn, retriever=retriever,
                               agent_config=agent_configs[RETRIEVER_NODE] if agent_configs else None),
        ANSWER_VALIDATOR_NODE: partial(
            answer_validator_fn, usage_collector=usage_collector,
            require_citations=True,
            agent_config=agent_configs[ANSWER_VALIDATOR_NODE] if agent_configs else None,
        ),
    }


def build_graph(
    nodes: Optional[dict[str, NodeFn]] = None,
    checkpointer: Optional[Any] = None,
    workflow: WorkflowSpec | None = None,
):
    """Compile the selected workflow; fake nodes keep graph tests offline."""
    if workflow is None:
        from src.config_loader import load_protocol_config
        from src.runtime.system import _workflow

        workflow = _workflow(load_protocol_config())
    impl = nodes if nodes is not None else default_nodes()
    missing = set(workflow.agents) - set(impl)
    if missing:
        raise ValueError(f"缺少已注册节点: {', '.join(sorted(missing))}")

    graph = StateGraph(AgentState)

    for node in workflow.agents:
        graph.add_node(node, impl[node])
    if workflow.start_nodes:
        for node in workflow.start_nodes:
            graph.add_edge(START, node)
    else:
        graph.set_entry_point(workflow.entry_node)
    if workflow.join_sources:
        graph.add_edge(workflow.join_sources, workflow.join_target)
    for node in workflow.agents:
        edges = [edge for edge in workflow.edges if edge.source == node]
        if node in workflow.join_sources:
            continue
        if len(edges) > 1 and all(edge.route_name == 'always' for edge in edges):
            for edge in edges:
                graph.add_edge(node, edge.target)
            continue
        if len(edges) == 1 and edges[0].route_name == "always":
            graph.add_edge(node, END if edges[0].target == "END" else edges[0].target)
        else:
            targets = {END if edge.target == "END" else edge.target for edge in edges}
            graph.add_conditional_edges(
                node,
                partial(route_after_validator, workflow=workflow, source=node),
                {target: target for target in targets},
            )

    compiled = graph.compile(
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver()
    )
    # Wake the synchronous consumer for task events while other branches run.
    compiled.stream_eager = True
    return compiled
