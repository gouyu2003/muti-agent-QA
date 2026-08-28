import pytest

from src.config_loader import (
    load_json,
    load_v0_config,
    validate_required_fields,
)


def test_load_v0_config_reads_all_v0_configs():
    config = load_v0_config()

    assert set(config["agents"]) == {
        "planner_v0",
        "retriever_v0",
        "answer_validator_v0",
    }
    assert config["protocol"]["entry_node"] == "planner_v0"


def test_load_json_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="不存在"):
        load_json(tmp_path / "missing.json")


def test_load_json_rejects_invalid_json(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="合法 JSON"):
        load_json(path)


def test_validate_required_fields_reports_all_missing_fields():
    with pytest.raises(ValueError, match="agent_name, version"):
        validate_required_fields(
            {"agent_id": "planner_v0"},
            frozenset({"agent_id", "agent_name", "version"}),
            "智能体 planner_v0 配置",
        )
