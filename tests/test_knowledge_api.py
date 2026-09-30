from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from backend.app import app
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository

FILES = [("files", ("people.md", b"# People\n\nMara Vale founded Cedar Institute.", "text/markdown")),
         ("files", ("institute.txt", b"Cedar Institute is located in Northbridge.", "text/plain"))]


class PlannerLLM:
    def invoke(self, _prompt):
        return AIMessage(content=json.dumps({"sub_questions": [
            {"id": "sq_1", "question": "What institute did Mara Vale found?", "keywords": ["Mara Vale"], "depends_on": []},
            {"id": "sq_2", "question": "Where is that institute located?", "keywords": ["located"], "depends_on": ["sq_1"]},
        ], "retrieval_strategy": "sequential"}))


class ValidatorLLM:
    def invoke(self, prompt):
        sq_id = ""
        supporting = []
        for line in prompt.splitlines():
            group = re.match(r"\[(sq_\d+)\]", line)
            if group:
                sq_id = group[1]
            chunk = re.search(r"chunk_id=([a-f0-9]+)", line)
            if chunk:
                supporting.append({"sq_id": sq_id, "chunk_id": chunk[1]})
        return AIMessage(content=json.dumps({"answer": "Northbridge", "verified": True,
            "failure_type": "none", "supporting_evidence": supporting, "verification_note": "Test evidence checked"}))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MAQA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setattr("src.agents.planner.default_llm", lambda *_: PlannerLLM())
    monkeypatch.setattr("src.agents.answer_validator.default_llm", lambda *_: ValidatorLLM())
    original = app.state.run_service_factory
    app.state.run_service_factory = lambda: RunService(SQLiteRunRepository(tmp_path / "runs.sqlite3"))
    try:
        with TestClient(app) as result:
            yield result
    finally:
        app.state.run_service_factory = original


def test_upload_run_and_history_preserve_cross_file_provenance(client):
    imported = client.post("/api/v1/knowledge/sources", data={"name": "English notes"}, files=FILES)
    assert imported.status_code == 200
    source = imported.json()
    source_id = source["source_id"]
    assert source["current_version"] == 1 and source["file_count"] == 2
    options = client.get("/api/v1/system/options").json()
    choice = next(item for item in options["sources"] if item["source_id"] == source_id)
    assert choice["record_id"] is None and choice["example_question"] is None
    assert choice["versions"] == [1]
    assert "original_name" not in str(choice)
    preview = client.get(f"/api/v1/knowledge/sources/{source_id}/versions/1/chunks?limit=1&offset=1").json()
    assert preview["total"] == 2 and len(preview["items"]) == 1
    response = client.post("/api/v1/qa/stream", json={"question": "Which city hosts the institute founded by Mara Vale?",
                          "source_id": source_id, "source_version": 1, "workflow_id": "multi_hop_qa_fixed"})
    assert response.status_code == 200
    events = [json.loads(block.split("data: ")[1]) for block in response.text.strip().split("\n\n")]
    run_id = events[0]["run_id"]
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["run"]["final_state"]["answer"] == "Northbridge"
    evidence = detail["run"]["final_state"]["supporting_evidence"]
    assert {item["filename"] for item in evidence} == {"people.md", "institute.txt"}
    assert all(item["source_version"] == 1 and item["chunk_id"] and item["line_start"] for item in evidence)
    assert detail["run"]["context"]["record_id"] is None
    client.post(f"/api/v1/knowledge/sources/{source_id}/versions",
                files=[("files", ("updated.txt", b"Cedar Institute relocated to Southport."))])
    old = client.get(f"/api/v1/runs/{run_id}").json()
    assert old["run"]["context"]["config_snapshot"]["source"]["version"] == 1
    assert old["run"]["final_state"]["supporting_evidence"] == evidence
    old_run = client.post("/api/v1/qa/stream", json={"question": "Where?", "source_id": source_id,
                          "source_version": 1, "workflow_id": "multi_hop_qa_fixed"})
    assert old_run.status_code == 200


def test_failed_version_has_an_id_and_keeps_last_ready_version(client):
    source_id = client.post("/api/v1/knowledge/sources", data={"name": "Notes"}, files=FILES).json()["source_id"]
    failed = client.post(f"/api/v1/knowledge/sources/{source_id}/versions", files=[("files", ("bad.txt", b"\xff"))])
    assert failed.status_code == 422
    assert failed.json()["detail"] == {"source_id": source_id, "version": 2,
            "code": "invalid_encoding", "message": "bad.txt：请使用 UTF-8 编码。"}
    detail = client.get(f"/api/v1/knowledge/sources/{source_id}").json()
    assert detail["status"] == "failed" and detail["current_version"] == 1
    assert client.get(f"/api/v1/knowledge/sources/{source_id}/versions/2/chunks").status_code == 404
    assert client.post("/api/v1/qa/stream", json={"question": "Q", "source_id": source_id, "source_version": 2}).status_code == 422
    assert client.get("/api/v1/knowledge/sources").json()["items"][0]["source_id"] == source_id


def test_failed_first_import_does_not_appear_in_run_options(client):
    response = client.post("/api/v1/knowledge/sources", data={"name": "Bad"}, files=[("files", ("scan.pdf", b"text"))])
    assert response.status_code == 422
    source_id = response.json()["detail"]["source_id"]
    assert all(item["source_id"] != source_id for item in client.get("/api/v1/system/options").json()["sources"])
    assert client.get("/api/v1/knowledge/sources/unknown").status_code == 404


def test_document_citations_reject_unknown_ids_and_backfill_real_locations():
    from src.agents.answer_validator import parse_verification
    chunk = {"text": "Original fact", "source_title": "a.txt", "source_id": "user_a", "chunk_id": "c1",
             "filename": "a.txt", "source_version": 1, "line_start": 3, "line_end": 3}
    state = {"evidence_chunks": [{"sq_id": "sq_1", "chunks": [chunk]}]}
    payload = {"answer": "A", "verified": True, "failure_type": "none",
               "supporting_evidence": [{"sq_id": "sq_1", "chunk_id": "missing", "filename": "forged.txt"}]}
    invalid = parse_verification(json.dumps(payload), state)
    assert invalid["verified"] is False and invalid["supporting_evidence"] == []
    payload["supporting_evidence"][0]["chunk_id"] = "c1"
    valid = parse_verification(json.dumps(payload), state)["supporting_evidence"][0]
    assert valid["filename"] == "a.txt" and valid["text"] == "Original fact" and valid["line_start"] == 3


def test_document_empty_retrieval_cannot_be_marked_verified():
    from src.agents.answer_validator import parse_verification
    result = parse_verification(json.dumps({"answer": "Invented", "verified": True, "failure_type": "none"}),
                                {"evidence_chunks": []}, require_citations=True)
    assert result["verified"] is False and result["failure_type"] == "insufficient"
