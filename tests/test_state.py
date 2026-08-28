from src.state import AgentState


def test_agent_state_only_requires_question():
    assert AgentState.__required_keys__ == {"question"}
    assert "conflict_pairs" in AgentState.__optional_keys__
    assert "failure_type" in AgentState.__optional_keys__