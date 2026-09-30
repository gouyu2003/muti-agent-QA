"""Explicit real-model smoke for the current API, CLI, and persisted history."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from backend.app import app
from src.runtime.system import PROJECT_ROOT

pytestmark = pytest.mark.integration

V0_QUESTION = (
    "What government position was held by the woman who portrayed "
    "Corliss Archer in the film Kiss and Tell?"
)
DEMO_QUESTION = "Which city contains the university where Ada Example studied?"


def _env_ready() -> bool:
    load_dotenv()
    return bool(
        os.environ.get("DEEPSEEK_API_KEY", "").strip()
        and os.environ.get("DEEPSEEK_BASE_URL", "").strip()
    )


def _assert_history(client: TestClient, run_id: str, *, workflow: str, source: str,
                    expected_answer: str) -> dict:
    response = client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200
    detail = response.json()
    run = detail["run"]
    events = detail["events"]
    assert run["context"]["run_id"] == run_id
    assert run["status"] == "completed", run.get("error")
    assert expected_answer.lower() in (run["final_state"]["answer"] or "").lower()
    assert run["context"]["workflow_id"] == workflow
    assert run["context"]["source_id"] == source
    assert "source-sha256:" in run["context"]["code_version"]
    snapshot = run["context"]["config_snapshot"]
    assert snapshot["workflow_spec"]["protocol_id"] == workflow
    assert snapshot["source"]["source_id"] == source
    assert len(snapshot["source"]["sha256"]) == 64
    assert len(snapshot["config_sha256"]) == 64
    assert events and all(event["run_id"] == run_id for event in events)
    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert events[0]["event_type"] == "run_started"
    assert events[-1]["event_type"] == "run_completed"
    assert run["final_state"]["evidence_chunks"]
    assert all(
        chunk["source_id"] == source
        for group in run["final_state"]["evidence_chunks"]
        for chunk in group["chunks"]
    )
    return detail


@pytest.mark.skipif(not _env_ready(), reason="缺少真实模型连接配置")
def test_real_api_and_cli_share_runtime_and_persist_matching_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One real run per entrypoint also covers both stage-two templates and sources."""

    db_path = tmp_path / "smoke-runs.sqlite3"
    monkeypatch.setenv("MAQA_RUN_DB_PATH", str(db_path))
    cli_run_id = "cli-" + uuid4().hex

    with TestClient(app) as client:
        # The API exercises the fixed workflow and the second local source.
        response = client.post(
            "/api/v1/qa/stream",
            json={"question": DEMO_QUESTION, "workflow_id": "multi_hop_qa_fixed",
                  "source_id": "demo_fixture_002"},
        )
        assert response.status_code == 200
        event_names = [line.removeprefix("event: ") for line in response.text.splitlines()
                       if line.startswith("event: ")]
        envelopes = [json.loads(line.removeprefix("data: "))
                     for line in response.text.splitlines() if line.startswith("data: ")]
        assert event_names[0] == "run_started"
        assert event_names[-1] == "run_completed", event_names
        assert envelopes and len({item["run_id"] for item in envelopes}) == 1
        api_run_id = envelopes[0]["run_id"]
        api_detail = _assert_history(
            client, api_run_id, workflow="multi_hop_qa_fixed",
            source="demo_fixture_002", expected_answer="Bristol",
        )
        assert api_detail["run"]["context"]["entrypoint"] == "api"

        # The actual command-line process exercises the original feedback flow.
        command = [sys.executable, "main.py", "--run-id", cli_run_id,
                   "--workflow-id", "multi_hop_qa_v0",
                   "--source-id", "hotpotqa_fixture", V0_QUESTION]
        result = subprocess.run(
            command, cwd=PROJECT_ROOT, env=os.environ.copy(),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=240, check=False,
        )
        assert result.returncode == 0, result.stdout[-1000:] + result.stderr[-1000:]
        assert f"运行 ID：{cli_run_id}" in result.stdout
        cli_detail = _assert_history(
            client, cli_run_id, workflow="multi_hop_qa_v0",
            source="hotpotqa_fixture", expected_answer="Chief of Protocol",
        )
        assert cli_detail["run"]["context"]["entrypoint"] == "cli"
        assert (api_detail["run"]["context"]["code_version"]
                == cli_detail["run"]["context"]["code_version"])
        assert (api_detail["run"]["context"]["config_snapshot"]["config_sha256"]
                != cli_detail["run"]["context"]["config_snapshot"]["config_sha256"])


@pytest.mark.skipif(not _env_ready(), reason="缺少真实模型连接配置")
def test_real_document_source_single_multi_hop_and_missing_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("MAQA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setenv("MAQA_RUN_DB_PATH", str(tmp_path / "runs.sqlite3"))
    example = PROJECT_ROOT / "data" / "examples" / "stage3"
    files = [("files", (path.name, path.read_bytes())) for path in sorted(example.iterdir())]
    reports = []
    with TestClient(app) as client:
        imported = client.post("/api/v1/knowledge/sources", data={"name": "Stage 3 live smoke"}, files=files)
        assert imported.status_code == 200
        source_id = imported.json()["source_id"]
        for question, expected in [
            ("In what year did Cedar Institute open?", "2008"),
            ("Which city hosts the institute founded by Mara Vale?", "Northbridge"),
            ("Who is Mara Vale's spouse?", None),
        ]:
            response = client.post("/api/v1/qa/stream", json={"question": question,
                                   "source_id": source_id, "source_version": 1, "workflow_id": "multi_hop_qa_fixed"})
            assert response.status_code == 200
            envelope = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
            detail = client.get(f"/api/v1/runs/{envelope['run_id']}").json()
            run = detail["run"]
            reports.append({"question": question, "run_id": envelope["run_id"], "status": run["status"],
                            "result": run["final_state"], "error": run["error"], "metrics": run["metrics"]})
            (tmp_path / "stage3-live-results.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
            assert run["status"] == "completed", run["error"]
            result = run["final_state"]
            if expected is None:
                assert result["verified"] is False
            else:
                assert expected.lower() in result["answer"].lower()
                assert result["verified"] is True
                assert result["supporting_evidence"]
            assert all(item["source_version"] == 1 and item["filename"] and item["chunk_id"]
                       for item in result.get("supporting_evidence", []))
