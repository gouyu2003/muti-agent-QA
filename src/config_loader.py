"""加载并校验 Multi-Agent QA V0 的静态 JSON 配置。"""

import json
from pathlib import Path
from typing import Any

# 路径常量与文件映射定义
PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = PROJECT_ROOT / "system-design" / "agents"
PROTOCOLS_DIR = PROJECT_ROOT / "system-design" / "protocols"

AGENT_FILES = {
    "planner_v0": "planner.json",
    "retriever_v0": "retriever.json",
    "answer_validator_v0": "answer_validator.json",
}
PROTOCOL_FILES = {
    "multi_hop_qa_v0": "multi_hop_qa.json",
}

# 必填字段集合
REQUIRED_AGENT_FIELDS = frozenset(
    {
        "agent_id",
        "agent_name",
        "version",
        "role",
        "domain",
        "reasoning_method",
        "knowledge_sources",
        "model_config",
        "neighbor_agents",
        "input_schema",
        "output_schema",
        "failure_handling",
    }
)
REQUIRED_PROTOCOL_FIELDS = frozenset(
    {
        "protocol_id",
        "agents",
        "entry_node",
        "shared_state_fields",
        "edges",
        "routing_rules",
        "message_schemas",
    }
)

# 通用 JSON 读取与字段校验函数
def load_json(path: Path) -> dict[str, Any]:
    """读取一个 JSON 对象，并将常见配置错误转换为清晰异常。"""
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件不是合法 JSON：{path}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"配置根节点必须是 JSON 对象：{path}")
    return data

def validate_required_fields(
    data: dict[str, Any], required_fields: frozenset[str], label: str
) -> None:
    missing_fields = sorted(required_fields - data.keys())
    if missing_fields:
        raise ValueError(f"{label}缺少必填字段：{', '.join(missing_fields)}")

# 智能体配置加载与校验
def load_agent_configs() -> dict[str, dict[str, Any]]:
    """读取并校验 V0 的三份智能体配置。"""
    configs: dict[str, dict[str, Any]] = {}
    for agent_id, filename in AGENT_FILES.items():
        config = load_json(AGENTS_DIR / filename) 
        validate_required_fields(config, REQUIRED_AGENT_FIELDS, f"智能体 {agent_id} 配置")

        if config["agent_id"] != agent_id:
            raise ValueError(
                f"智能体 {agent_id} 的配置 agent_id 不一致：{config['agent_id']!r}"
            )
        configs[agent_id] = config   #存入config字典
    return configs  #循环结束后返回包含全部 3 个智能体配置的字典。

# 加载并校验协议配置
def load_protocol_config() -> dict[str, Any]:
    """读取并校验 V0 协同协议配置。"""
    protocol_id = "multi_hop_qa_v0"
    config = load_json(PROTOCOLS_DIR / PROTOCOL_FILES[protocol_id])
    validate_required_fields(config, REQUIRED_PROTOCOL_FIELDS, f"协议 {protocol_id} 配置")

    if config["protocol_id"] != protocol_id:
        raise ValueError(
            f"协议 {protocol_id} 的配置 protocol_id 不一致：{config['protocol_id']!r}"
        )

    declared_agents = config["agents"]
    if not isinstance(declared_agents, list) or not all(
        isinstance(agent_id, str) for agent_id in declared_agents
    ):
        raise ValueError("协议 multi_hop_qa_v0 的 agents 必须是字符串列表")

    if set(declared_agents) != set(AGENT_FILES):
        raise ValueError(
            "协议 multi_hop_qa_v0 的 agents 与 V0 智能体定义不一致："
            f"{declared_agents!r}"
        )

    entry_node = config["entry_node"]
    if not isinstance(entry_node, str) or entry_node not in AGENT_FILES:
        raise ValueError(
            "协议 multi_hop_qa_v0 的 entry_node 必须是已定义的 V0 智能体 ID"
        )
    return config  #返回校验完毕的协议配置字典。

# 提取模型配置
def get_model_config(agent_config: dict[str, Any]) -> dict[str, Any]:
    """提取已校验智能体配置的模型配置，缺失或类型错误时立即失败。"""
    agent_id = agent_config.get("agent_id", "<unknown>")
    try:
        model_config = agent_config["model_config"]
    except KeyError as exc:
        raise ValueError(f"智能体配置 {agent_id!r} 缺少 model_config") from exc

    if not isinstance(model_config, dict):
        raise ValueError(f"智能体配置 {agent_id!r} 的 model_config 必须是对象")
    return model_config

#总加载入口
def load_v0_config() -> dict[str, Any]:
    """加载 V0 的三份智能体配置和协同协议，并校验它们彼此一致。"""
    # 一键调用 load_agent_configs() 和 load_protocol_config()，打包返回包含全部智能体和协议的完整系统配置树。
    return {
        "agents": load_agent_configs(),
        "protocol": load_protocol_config(),
    }
