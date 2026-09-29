"""The run code identifier must change when executable source changes."""

from __future__ import annotations

from src.runtime.context import build_v0_run_context
from src.runtime.version import source_fingerprint


def test_source_fingerprint_tracks_runtime_edits(tmp_path):
    source = tmp_path / "src" / "component.py"
    source.parent.mkdir()
    source.write_text("VERSION = 1\n", encoding="utf-8")
    first = source_fingerprint(tmp_path)
    source.write_text("VERSION = 2\n", encoding="utf-8")
    assert source_fingerprint(tmp_path) != first


def test_run_context_uses_code_fingerprint_by_default(monkeypatch):
    monkeypatch.delenv("MAQA_CODE_VERSION", raising=False)
    context = build_v0_run_context("Question?", entrypoint="cli")
    assert "source-sha256:" in context.code_version
    assert len(context.code_version.rsplit("source-sha256:", 1)[1]) == 64
    assert context.code_version != "v0"


def test_run_context_accepts_explicit_build_identifier(monkeypatch):
    monkeypatch.setenv("MAQA_CODE_VERSION", "build-2026-09-29")
    context = build_v0_run_context("Question?", entrypoint="api")
    assert context.code_version.startswith("build:build-2026-09-29+source-sha256:")
    assert len(context.code_version.rsplit("source-sha256:", 1)[1]) == 64
