"""main.py 的离线测试：使用假 RunService，不调用真实 LLM。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from main import DEFAULT_QUESTION, format_result, parse_args, run
from src.runtime.models import (
    RunContext,
    RunDetail,
    RunError,
    RunMetrics,
    RunRecord,
)


NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)


def _detail(
    *,
    run_id: str = "run-cli-test",
    status: str = "completed",
) -> RunDetail:
    context = RunContext(
        run_id=run_id,
        entrypoint="cli",
        question="Q?",
        source_id="hotpotqa_fixture",
        record_id="fixture_001",
        workflow_id="multi_hop_qa_v0",
        method_id="multi_agent_adaptive_v0",
        model_name="test-model",
        protocol_id="multi_hop_qa_v0",
        code_version="test-version",
        config_snapshot={},
    )
    if status == "failed":
        record = RunRecord(
            context=context,
            status="failed",
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            final_state={"question": "Q?"},
            metrics=RunMetrics(duration_ms=1000),
            error=RunError(
                error_type="execution_error",
                message="公开错误",
            ),
        )
    else:
        record = RunRecord(
            context=context,
            status="completed",
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            final_state={
                "question": "Q?",
                "answer": "Chief of Protocol",
                "verified": True,
                "failure_type": "none",
                "verification_note": "证据充分",
                "supporting_evidence": [],
            },
            metrics=RunMetrics(duration_ms=1000, node_call_count=3),
        )
    return RunDetail(run=record, events=[])


def test_parse_args_joins_words_and_generates_run_id_later():
    args = parse_args(["What", "government", "position", "was", "held?"])
    assert " ".join(args.question) == "What government position was held?"
    assert args.run_id is None


def test_parse_args_defaults_to_builtin_question():
    args = parse_args([])
    assert " ".join(args.question) == "" or not args.question
    assert args.run_id is None
    assert DEFAULT_QUESTION.startswith("What government position")


def test_parse_args_accepts_run_id_and_legacy_thread_id_alias():
    assert parse_args(["q", "--run-id", "r9"]).run_id == "r9"
    assert parse_args(["q", "--thread-id", "legacy"]).run_id == "legacy"


def test_cli_selects_configured_workflow_and_source():
    args = parse_args([
        "Which city?", "--workflow-id", "multi_hop_qa_fixed",
        "--source-id", "demo_fixture_002",
    ])
    assert args.workflow_id == "multi_hop_qa_fixed"
    assert args.source_id == "demo_fixture_002"

    class RecordingService:
        context = None

        def run(self, context):
            self.context = context
            return _detail()

    service = RecordingService()
    run("Which city?", run_service=service,
        workflow_id=args.workflow_id, source_id=args.source_id)
    assert service.context.workflow_id == "multi_hop_qa_fixed"
    assert service.context.source_id == "demo_fixture_002"


def test_run_builds_cli_context_and_uses_injected_service():
    class RecordingService:
        def __init__(self):
            self.context = None

        def run(self, context):
            self.context = context
            return _detail(run_id=context.run_id)

    service = RecordingService()

    detail = run("Q?", run_id="fixed-run", run_service=service)

    assert detail.run.status == "completed"
    assert service.context.run_id == "fixed-run"
    assert service.context.entrypoint == "cli"
    assert service.context.question == "Q?"
    assert service.context.source_id == "hotpotqa_fixture"
    assert service.context.config_snapshot["protocol"]["protocol_id"] == (
        "multi_hop_qa_v0"
    )


def test_format_result_renders_core_fields_and_run_id():
    state = {
        "question": "Q?",
        "answer": "Chief of Protocol",
        "verified": True,
        "failure_type": "none",
        "verification_note": "证据充分",
        "supporting_evidence": [
            {"sq_id": "sq_1", "text": "E1", "source_title": "T1"},
            {"sq_id": "sq_2", "text": "E2", "source_title": "T2"},
        ],
    }
    text = format_result(state, run_id="run-1")

    assert "运行 ID：run-1" in text
    assert "Chief of Protocol" in text
    assert "验证通过：True" in text
    assert "支撑证据（2 条）" in text
    assert "T1" in text and "T2" in text


def test_format_result_tolerates_empty_state():
    text = format_result({"question": "Q?"})

    assert "(无)" in text
    assert "支撑证据（0 条）" in text


def test_main_returns_one_and_reports_exception(monkeypatch, capsys):
    import main as main_module

    def boom(question, run_id=None):
        raise RuntimeError("节点炸了")

    monkeypatch.setattr(main_module, "run", boom)

    assert main_module.main(["Q?"]) == 1
    assert "运行失败" in capsys.readouterr().out


def test_main_returns_one_for_persisted_failed_run(monkeypatch, capsys):
    import main as main_module

    monkeypatch.setattr(
        main_module,
        "run",
        lambda question, run_id=None: _detail(status="failed"),
    )

    assert main_module.main(["Q?"]) == 1
    output = capsys.readouterr().out
    assert "运行失败 [execution_error]：公开错误" in output
    assert "运行 ID：run-cli-test" in output


def test_main_prints_completed_result_and_run_id(monkeypatch, capsys):
    import main as main_module

    monkeypatch.setattr(
        main_module,
        "run",
        lambda question, run_id=None: _detail(),
    )

    assert main_module.main(["Q?"]) == 0
    output = capsys.readouterr().out
    assert "Chief of Protocol" in output
    assert "运行 ID：run-cli-test" in output
