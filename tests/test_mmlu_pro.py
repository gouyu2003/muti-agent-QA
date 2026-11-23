from time import monotonic, sleep

import pytest

from src.evaluation import mmlu_pro
from src.evaluation.platform_store import EvaluationFailure


def test_letter_scoring_preserves_ties_out_of_range_and_truncation():
    record = {"options": ["One", "Two"], "answer": "A"}
    result = mmlu_pro.score_round([
        dict(content="FINAL: a", finish_reason="stop"),
        dict(content="FINAL: B", finish_reason="stop"),
        dict(content="FINAL: J", finish_reason="stop"),
        dict(content="FINAL: A", finish_reason="length"),
        dict(content="FINAL: B\nFINAL: A", finish_reason="stop"),
    ], record)
    assert result["candidates"] == ["A", "B", None, None, None]
    assert result["votes"] == {"A": 1, "B": 1}
    assert result["prediction"] is None
    assert result["tie"] and result["correct"] == 0 and result["truncated"] == 1


@pytest.mark.parametrize(("content", "finish_reason", "choice"), [
    ("Reasoning\n**FINAL: B**\n", "stop", "B"),
    ("**FINAL: B**", "length", None),
    ("**FINAL: J**", "stop", None),
    ("**FINAL: B**\nFINAL: A", "stop", None),
    ("FINAL: A\n**FINAL: B**", "stop", None),
    ("**FINAL: A**\n**FINAL: B**", "stop", None),
    ("**FINAL: B", "stop", None),
    ("**FINAL: B**\nMore text", "stop", None),
])
def test_whole_line_markdown_bold_final_answer(content, finish_reason, choice):
    assert mmlu_pro.parse_choice(dict(content=content, finish_reason=finish_reason), ["One", "Two"]) == choice


def wait_until_idle(job_id):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        job = mmlu_pro.view_job(job_id)
        if job["state"] not in {"running", "stopping"}:
            return job
        sleep(0.02)
    pytest.fail("MMLU-Pro job did not finish or pause")


@pytest.mark.parametrize("selection", ["smoke", "development"])
def test_platform_runs_all_four_methods_and_exposes_the_debate_snapshot(tmp_path, monkeypatch, selection):
    monkeypatch.setattr(mmlu_pro, "jobs_root", lambda: tmp_path)
    monkeypatch.setattr(mmlu_pro, "load_manifest", lambda selection: {
        "sample_count": 5,
        "manifest_sha256": "test-hash",
        "dataset_revision": "test-revision",
        "sampling": {"seed": 42, "quotas": {"math": 5}},
        "records": [
            dict(question_id=index, category="math", question=f"Question {index}",
                 options=["One", "Two", "Three", "Four"], answer="D")
            for index in range(5)
        ],
    })
    methods = list(mmlu_pro.METHODS)
    created = mmlu_pro.create_job("offline check", methods, selection, True)
    job_id = created["job_id"]
    assert created["planned_calls"] == 75
    assert created["scoring_version"] == "mmlu-pro-final-choice-v2"
    mmlu_pro.start_job(job_id, False)
    job = wait_until_idle(job_id)
    if selection == "development":
        assert job["state"] == "paused"
        assert job["actual_calls"] == 5
        assert {result["method"] for result in job["results"]} == {"single"}
        mmlu_pro.start_job(job_id, False)
        job = wait_until_idle(job_id)
    assert job["state"] == "completed", job["error"]
    assert job["counts"]["completed"] == len(job["results"]) == 20
    assert job["actual_calls"] == 75
    assert [job["method_metrics"][method]["calls"] for method in methods] == [5, 10, 30, 30]

    trace = mmlu_pro.trace(job_id, 0, "debate")
    assert len(trace) == 6
    initial = trace[:3]
    for call in trace[3:]:
        index = call["agent"]
        messages = call["request"]["messages"]
        assert messages[1]["content"] == initial[index]["response"]["content"]
        for peer in initial:
            marker = f"Agent {peer['agent'] + 1}:\n{peer['response']['content']}"
            assert (marker in messages[2]["content"]) == (index != peer["agent"])
        assert "FINAL: D" not in str(messages)
    debate = next(result for result in job["results"] if result["qid"] == "0" and result["method"] == "debate")
    assert len(debate["round_scores"]) == 2
    assert debate["round_scores"][0]["votes"] != debate["round_scores"][1]["votes"]

    real = mmlu_pro.create_job("real smoke", methods, "smoke", False)
    with pytest.raises(EvaluationFailure, match="确认真实模型调用"):
        mmlu_pro.start_job(real["job_id"], False)
    assert mmlu_pro.view_job(real["job_id"])["actual_calls"] == 0
