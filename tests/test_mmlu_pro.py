import json
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
            try:
                with mmlu_pro._lock(mmlu_pro._lock_path(mmlu_pro.jobs_root(), job_id)):
                    return job
            except EvaluationFailure:
                pass
        sleep(0.02)
    pytest.fail("MMLU-Pro job did not finish or pause")


@pytest.mark.parametrize("selection", ["smoke", "development", "comparison-100"])
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
    if selection == "comparison-100":
        with pytest.raises(EvaluationFailure, match="同时选择四种方法"):
            mmlu_pro.create_job("incomplete comparison", ["single"], selection, True)
    created = mmlu_pro.create_job("offline check", methods, selection, True)
    job_id = created["job_id"]
    assert created["planned_calls"] == 75
    assert created["next_stage_calls"] == (75 if selection == "smoke" else 5)
    assert created["scoring_version"] == "mmlu-pro-final-choice-v2"
    mmlu_pro.start_job(job_id, False)
    job = wait_until_idle(job_id)
    if selection != "smoke":
        assert job["state"] == "paused"
        assert job["actual_calls"] == 5
        assert job["next_stage_calls"] == 70
        assert {result["method"] for result in job["results"]} == {"single"}
        mmlu_pro.start_job(job_id, False)
        job = wait_until_idle(job_id)
    assert job["state"] == "completed", job["error"]
    assert job["counts"]["completed"] == len(job["results"]) == 20
    assert job["actual_calls"] == 75
    assert job["next_stage_calls"] == 0
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


def test_resumes_only_pending_tasks_from_the_compatible_previous_runner(tmp_path, monkeypatch):
    assert mmlu_pro._execution_signature() == mmlu_pro.PRE_RECOVERY_EXECUTION_SHA256
    monkeypatch.setattr(mmlu_pro, "jobs_root", lambda: tmp_path)
    monkeypatch.setenv("MAQA_EVALUATIONS_DIR", str(tmp_path / "catalog"))
    monkeypatch.setattr(mmlu_pro, "load_manifest", lambda selection: {
        "sample_count": 3,
        "manifest_sha256": "test-hash",
        "dataset_revision": "test-revision",
        "sampling": {"seed": 42, "quotas": {"math": 3}},
        "records": [
            dict(question_id=index, category="math", question=f"Question {index}",
                 options=["One", "Two", "Three"], answer="A")
            for index in range(3)
        ],
    })
    created = mmlu_pro.create_job("continuation check", list(mmlu_pro.METHODS), "comparison-100", True)
    job_id = created["job_id"]
    mmlu_pro.start_job(job_id, False)
    paused = wait_until_idle(job_id)
    assert paused["state"] == "paused"
    original_generate = mmlu_pro.generate

    def timed_out(client, request, task_index, round_index, agent):
        if task_index == 1 and round_index == 0:
            raise TimeoutError()
        return original_generate(client, request, task_index, round_index, agent)

    with monkeypatch.context() as failing:
        failing.setattr(mmlu_pro, "generate", timed_out)
        mmlu_pro.start_job(job_id, False)
        interrupted = wait_until_idle(job_id)
    assert interrupted["state"] == "interrupted"
    assert interrupted["actual_calls"] == 6 and interrupted["next_stage_calls"] == 38
    assert interrupted["can_resume_pending"]
    unknown = mmlu_pro.trace(job_id, 1, "reflection")
    assert len(unknown) == 1 and unknown[0]["status"] == "unknown"

    with mmlu_pro.connection(tmp_path) as db:
        _, profile = mmlu_pro._job(db, job_id)
        profile.pop("profile_sha256")
        profile["code_sha256"] = mmlu_pro.PRE_RECOVERY_CODE_SHA256
        profile["profile_sha256"] = mmlu_pro.digest(profile)
        frozen = json.dumps(profile)
        db.execute("UPDATE jobs SET profile=? WHERE id=?", (frozen, job_id))
    with pytest.raises(EvaluationFailure, match="只能启动草稿"):
        mmlu_pro.start_job(job_id, False)
    with monkeypatch.context() as changed_code:
        changed_code.setattr(mmlu_pro, "_execution_signature", lambda: "changed")
        with pytest.raises(EvaluationFailure, match="执行代码与冻结实验不同"):
            mmlu_pro.start_job(job_id, False, resume_pending=True)

    mmlu_pro.start_job(job_id, False, resume_pending=True)
    resumed = wait_until_idle(job_id)
    assert resumed["state"] == "interrupted" and not resumed["can_resume_pending"]
    assert resumed["counts"]["completed"] == 11 and resumed["counts"]["interrupted"] == 1
    assert resumed["counts"]["pending"] == 0 and resumed["next_stage_calls"] == 0
    assert resumed["actual_calls"] == 44
    assert {row["qid"]: row for row in resumed["results"] if row["method"] == "single"} == {
        row["qid"]: row for row in paused["results"] if row["method"] == "single"
    }
    assert mmlu_pro.trace(job_id, 1, "reflection") == unknown
    assert resumed["pending_resumes"][0]["scheduled_calls"] == 38
    assert resumed["pending_resumes"][0]["previous_error"] == interrupted["error"]
    with mmlu_pro.connection(tmp_path) as db:
        assert mmlu_pro._job(db, job_id)[0]["profile"] == frozen

    supplemented = mmlu_pro.supplement_reflection_task(job_id, 1)
    assert supplemented["state"] == "completed" and supplemented["counts"]["completed"] == 12
    assert supplemented["actual_calls"] == 46 and supplemented["superseded_calls"] == 1
    assert supplemented["report"]["unknown_requests"] == 1
    trace = mmlu_pro.trace(job_id, 1, "reflection")
    assert trace[0] == unknown[0]
    assert [(call["attempt"], call["round"], call["status"]) for call in trace[1:]] == [
        (2, 0, "completed"), (2, 1, "completed"),
    ]
    assert trace[1]["request"] == trace[0]["request"]
    assert [row for row in supplemented["results"] if (row["qid"], row["method"]) != ("1", "reflection")] == [
        row for row in resumed["results"] if (row["qid"], row["method"]) != ("1", "reflection")
    ]
    with pytest.raises(EvaluationFailure, match="只有指定的 Reflection 题中断"):
        mmlu_pro.supplement_reflection_task(job_id, 1)
