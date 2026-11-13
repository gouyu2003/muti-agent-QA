"""Small message helpers adapted from Hello Agents / StockSage.

Source: datawhalechina/hello-agents, commit 2a506bc7e0e3e2235435559b7f2825057ce9a09c
Co-creation-projects/lcyting-StockSage-agent/HelloAgents Optimized/hello_agents/core/
message.py and conversation.py. CC BY-NC-SA 4.0; see third_party/hello-agents/.
Changes: dictionary inputs, durable IDs supplied by SQLite, no branching/global state.
"""
from collections.abc import Iterable, Mapping


def message_to_dict(message: Mapping, full: bool = False) -> dict:
    if full:
        return dict(message)
    return {"role": message["role"], "content": message["content"]}


def to_llm_messages(messages: Iterable[Mapping]) -> list[dict[str, str]]:
    return [message_to_dict(message) for message in messages]
