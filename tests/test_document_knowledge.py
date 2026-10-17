from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.knowledge.corpus import ImportFailure, parse_document, MAX_FILE_BYTES
from src.knowledge.store import KnowledgeStore, retriever_from_corpus_snapshot
from src.runtime.context import build_v0_run_context
from src.runtime.system import resolve_system, retriever_from_snapshot

FILES = [
    ("people.md", b"# People\n\nMara Vale founded Cedar Institute.\n"),
    ("institute.txt", b"Cedar Institute is located in Northbridge.\n\nIt opened in 2008.\n"),
]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MAQA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    return KnowledgeStore()


def test_parser_is_deterministic_and_locations_match_normalized_original():
    raw = b"\xef\xbb\xbf# Title\r\n\r\nHello English material.\r\nAnother sentence.\r\n\r\n" + b"long word " * 150
    doc = parse_document("sample.md", raw)
    assert doc == parse_document("sample.md", raw)
    assert doc["chunks"][0]["heading_path"] == ["Title"]
    assert doc["chunks"][0]["line_start"] == 3
    for chunk in doc["chunks"]:
        assert doc["text"][chunk["char_start"]:chunk["char_end"]] == chunk["text"]
        assert len(chunk["text"]) <= 600
        assert chunk["line_start"] == doc["text"].count("\n", 0, chunk["char_start"]) + 1


@pytest.mark.parametrize("files,code", [
    ([("a.pdf", b"text")], "unsupported_format"),
    ([("a.txt", b"")], "empty_or_binary"),
    ([("a.txt", b"\xff")], "invalid_encoding"),
    ([("a.md", b"# Heading only")], "no_content"),
    ([("a.txt", b"a"), ("A.TXT", b"b")], "duplicate_filename"),
    ([("../a.txt", b"text")], "invalid_filename"),
    ([("a.txt", b"x" * (MAX_FILE_BYTES + 1))], "file_too_large"),
    ([("a.txt", b"words")] * 11, "file_count"),
])
def test_failed_imports_are_recorded_and_not_runnable(store, files, code):
    result = store.import_files("system", "Name", files)
    assert result["status"] == "failed"
    assert result["current_version"] is None
    assert result["versions"][0]["error"]["code"] == code
    with pytest.raises(ImportFailure):
        store.resolve("system", result["source_id"])


def test_versions_survive_restart_and_old_snapshot_is_immutable(store):
    first = store.import_files("system", "Name", FILES)
    source_id = first["source_id"]
    snapshot = store.resolve("system", source_id)
    evidence = retriever_from_corpus_snapshot(snapshot).search("Cedar Institute located")
    assert evidence and evidence[0]["filename"] == "institute.txt"
    assert evidence[0]["source_version"] == 1
    second = store.import_files("system", None, [("new.txt", b"Cedar Institute relocated to Southport.")], source_id)
    assert second["current_version"] == 2
    assert KnowledgeStore(store.root).resolve("system", source_id)["version"] == 2
    assert retriever_from_corpus_snapshot(snapshot).search("Northbridge")[0]["source_version"] == 1
    failed = store.import_files("system", None, [("bad.txt", b"")], source_id)
    assert failed["current_version"] == 2
    assert store.resolve("system", source_id)["version"] == 2


def test_single_chunk_corpus_is_searchable_and_unrelated_queries_are_empty(store):
    result = store.import_files("system", "Small", [("one.txt", b"Mara Vale lives in Northbridge.")])
    retriever = retriever_from_corpus_snapshot(store.resolve("system", result["source_id"]))
    assert retriever.search("Northbridge")
    assert retriever.search("volcano") == []


def test_hash_validation_rejects_modified_artifacts_even_after_cache_load(store):
    result = store.import_files("system", "Name", FILES)
    snapshot = store.resolve("system", result["source_id"])
    retriever_from_corpus_snapshot(snapshot)
    Path(snapshot["index_path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ImportFailure, match="已改变"):
        retriever_from_corpus_snapshot(snapshot)


def test_interrupted_import_is_recovered(store):
    ready = store.import_files("system", "Name", FILES)
    with store.connection() as db:
        db.execute("INSERT INTO versions (source_id, version, status, created_at) VALUES (?, 2, 'importing', 'now')", (ready["source_id"],))
    store.recover_interrupted()
    result = store.detail("system", ready["source_id"])
    assert result["versions"][0]["error"]["code"] == "import_interrupted"
    assert result["current_version"] == 1


def test_runtime_pins_user_version_without_fake_hotpotqa_fields(store):
    system_id = resolve_system().system_id
    result = store.import_files(system_id, "Name", FILES)
    context = build_v0_run_context("Where?", entrypoint="cli", source_id=result["source_id"], source_version=1)
    assert context.record_id is None
    assert context.config_snapshot["source"]["version"] == 1
    assert context.config_snapshot["source"]["index_sha256"]
    assert retriever_from_snapshot(context.config_snapshot).search("Northbridge")
    assert store.resolve("another-system", result["source_id"])["version"] == 1


def test_markdown_code_does_not_change_heading_path():
    document = parse_document("code.md", b"# Real heading\n\n```python\n# Code comment\n```\n\nEnglish body.\n")
    assert all(chunk["heading_path"] == ["Real heading"] for chunk in document["chunks"])


def test_markdown_keeps_hash_in_heading_name():
    document = parse_document("code.md", b"# C#\n\nEnglish language notes.\n")
    assert document["chunks"][0]["heading_path"] == ["C#"]


def test_punctuation_only_text_is_not_indexed_from_its_filename(store):
    result = store.import_files("system", "Punctuation", [("english.txt", b"!!! ???")])
    assert result["versions"][0]["error"]["code"] == "no_english_tokens"


def test_user_corpus_cli_requires_a_question(capsys):
    import main
    assert main.main(["--source-id", "user_example"]) == 1
    assert "需要明确提供英文问题" in capsys.readouterr().out


def test_total_size_and_chunk_limits_are_reported(store, monkeypatch):
    result = store.import_files("system", "Large", [(f"{i}.txt", b"x" * MAX_FILE_BYTES) for i in range(6)])
    assert result["versions"][0]["error"]["code"] == "total_too_large"
    monkeypatch.setattr("src.knowledge.corpus.MAX_CHUNKS", 1)
    result = store.import_files("system", "Chunks", [("a.txt", b"First fact.\n\nSecond fact.")])
    assert result["versions"][0]["error"]["code"] == "too_many_chunks"


def test_index_failure_keeps_previous_version_usable(store, monkeypatch):
    first = store.import_files("system", "Name", FILES)
    def fail(_files):
        raise RuntimeError("simulated index failure")
    monkeypatch.setattr("src.knowledge.store.prepare_corpus", fail)
    failed = store.import_files("system", None, FILES, first["source_id"])
    assert failed["versions"][0]["error"]["code"] == "index_or_storage_error"
    assert store.resolve("system", first["source_id"])["version"] == 1


def test_cli_process_imports_and_lists_the_same_store(store, tmp_path):
    import os
    import subprocess
    import sys
    path = tmp_path / "notes.txt"
    path.write_text("Mara Vale founded Cedar Institute.", encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "src.knowledge.cli", "create", "--name", "CLI notes", str(path)],
                            env=os.environ.copy(), capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    imported = json.loads(result.stdout)
    listing = subprocess.run([sys.executable, "-m", "src.knowledge.cli", "list"], env=os.environ.copy(),
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert listing.returncode == 0, listing.stderr
    assert json.loads(listing.stdout)[0]["source_id"] == imported["source_id"]
