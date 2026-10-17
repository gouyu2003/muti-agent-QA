"""Local, versioned knowledge imports shared by HTTP and CLI."""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from src.knowledge.corpus import (
    ImportFailure, digest, json_bytes, load_corpus, prepare_corpus,
    corpus_retriever,
)

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "data" / "knowledge"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@lru_cache(maxsize=8)
def _cached_retriever(corpus_path: str, index_path: str, corpus_hash: str,
                      index_hash: str, source_id: str, version: int):
    record = load_corpus(Path(corpus_path))
    record["index"] = load_corpus(Path(index_path))
    record["source_version"] = version
    return corpus_retriever(record, source_id)


class KnowledgeStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or os.getenv("MAQA_KNOWLEDGE_DIR") or DEFAULT_ROOT).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "knowledge.sqlite3"
        # SQLite backup includes committed WAL contents, unlike copying the file.
        if self.database.exists():
            with closing(sqlite3.connect(self.database)) as existing:
                version = existing.execute("PRAGMA user_version").fetchone()[0]
                has_sources = existing.execute("SELECT 1 FROM sqlite_master WHERE name='sources'").fetchone()
                if has_sources and version < 2:
                    backup = self.root / f"knowledge.pre-workspace-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.sqlite3"
                    with closing(sqlite3.connect(backup)) as target:
                        existing.backup(target)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("不支持的知识库数据库版本")
            if version == 2:
                return
            db.execute("BEGIN IMMEDIATE")
            for statement in (
                "CREATE TABLE IF NOT EXISTS sources (source_id TEXT PRIMARY KEY, system_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS versions (source_id TEXT NOT NULL REFERENCES sources(source_id), version INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', error TEXT, PRIMARY KEY (source_id, version))",
                "CREATE TABLE IF NOT EXISTS bindings (system_id TEXT NOT NULL, agent_id TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(source_id), PRIMARY KEY (system_id, agent_id, source_id))",
            ):
                db.execute(statement)
            columns = {row[1] for row in db.execute("PRAGMA table_info(sources)")}
            if "workspace_id" not in columns:
                db.execute("ALTER TABLE sources ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'local'")
            db.execute("PRAGMA user_version = 2")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover_interrupted(self) -> None:
        with self.connection() as db:
            db.execute("UPDATE versions SET status='failed', error=? WHERE status='importing'",
                       (json.dumps({"code": "import_interrupted", "message": "上次导入被中断，请重新导入整套文件。"}),))

    def import_files(self, system_id: str, name: str | None,
                     files: list[tuple[str, bytes]], source_id: str | None = None) -> dict[str, Any]:
        if source_id is None:
            if not name or not name.strip() or len(name.strip()) > 100:
                raise ImportFailure("invalid_name", "知识源名称需为 1 至 100 个字符。")
            source_id = "user_" + uuid4().hex
        else:
            self.detail(system_id, source_id)
            name = None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if name is not None:
                db.execute("INSERT INTO sources (source_id, system_id, name, created_at) VALUES (?, ?, ?, ?)",
                           (source_id, system_id, name.strip(), now()))
            version = db.execute("SELECT COALESCE(MAX(version), 0)+1 FROM versions WHERE source_id=?",
                                 (source_id,)).fetchone()[0]
            db.execute("INSERT INTO versions (source_id, version, status, created_at) VALUES (?, ?, 'importing', ?)",
                       (source_id, version, now()))
        folder = self.root / source_id / str(version)
        try:
            corpus, index = prepare_corpus(files)
            folder.mkdir(parents=True, exist_ok=False)
            originals = folder / "originals"
            originals.mkdir()
            raw_by_name = dict(files)
            file_metadata = []
            for doc in corpus["documents"]:
                original_name = doc["document_id"] + Path(doc["filename"]).suffix.lower()
                (originals / original_name).write_bytes(raw_by_name[doc["filename"]])
                file_metadata.append({k: v for k, v in doc.items() if k != "text"} | {"original_name": original_name})
            corpus_raw, index_raw = json_bytes(corpus), json_bytes(index)
            (folder / "corpus.json").write_bytes(corpus_raw)
            (folder / "index.json").write_bytes(index_raw)
            metadata = {
                "files": file_metadata, "file_count": len(file_metadata),
                "chunk_count": len(corpus["chunks"]), "corpus_sha256": digest(corpus_raw),
                "index_sha256": digest(index_raw), "parser_version": corpus["parser_version"],
                "max_chars": corpus["max_chars"], "index_version": index["index_version"],
                "tokenizer": index["tokenizer"],
            }
            with self.connection() as db:
                db.execute("UPDATE versions SET status='ready', metadata=? WHERE source_id=? AND version=?",
                           (json.dumps(metadata), source_id, version))
                db.execute("INSERT OR IGNORE INTO bindings VALUES (?, 'retriever_v0', ?)", (system_id, source_id))
        except Exception as exc:
            error = {"code": exc.code if isinstance(exc, ImportFailure) else "index_or_storage_error",
                     "message": str(exc) if isinstance(exc, ImportFailure) else "解析、索引或保存失败，请检查材料和本地存储后重新导入。"}
            with self.connection() as db:
                db.execute("UPDATE versions SET status='failed', error=? WHERE source_id=? AND version=?",
                           (json.dumps(error), source_id, version))
        return self.detail(system_id, source_id)

    def detail(self, system_id: str, source_id: str) -> dict[str, Any]:
        with self.connection() as db:
            source = db.execute("SELECT * FROM sources WHERE source_id=? AND workspace_id='local'", (source_id,)).fetchone()
            if source is None:
                raise ImportFailure("source_not_found", f"未知 source_id: {source_id}，未找到当前工作区的用户知识源。")
            rows = db.execute("SELECT * FROM versions WHERE source_id=? ORDER BY version DESC", (source_id,)).fetchall()
        versions = [{"version": row["version"], "status": row["status"], "created_at": row["created_at"],
                     **json.loads(row["metadata"]), "error": json.loads(row["error"]) if row["error"] else None}
                    for row in rows]
        current = next((v for v in versions if v["status"] == "ready"), None)
        return {"source_id": source_id, "name": source["name"], "created_at": source["created_at"],
                "status": versions[0]["status"] if versions else "importing",
                "current_version": current["version"] if current else None,
                "file_count": current.get("file_count", 0) if current else 0,
                "chunk_count": current.get("chunk_count", 0) if current else 0,
                "versions": versions}

    def list_sources(self, system_id: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            ids = db.execute("SELECT source_id FROM sources WHERE workspace_id='local' ORDER BY created_at DESC").fetchall()
        return [self.detail(system_id, row[0]) for row in ids]

    def resolve(self, system_id: str, source_id: str, version: int | None = None) -> dict[str, Any]:
        detail = self.detail(system_id, source_id)
        selected = version if version is not None else detail["current_version"]
        entry = next((v for v in detail["versions"] if v["version"] == selected and v["status"] == "ready"), None)
        if entry is None:
            raise ImportFailure("version_not_ready", "知识源版本不存在或尚未就绪。")
        folder = self.root / source_id / str(selected)
        snapshot = {"source_id": source_id, "name": detail["name"], "version": selected,
                    "resolved_path": str(folder / "corpus.json"), "index_path": str(folder / "index.json"),
                    "sha256": entry["corpus_sha256"], **{k: v for k, v in entry.items()
                    if k not in {"status", "created_at", "error", "version"}}}
        verify_snapshot(snapshot)
        return snapshot

    def chunks(self, system_id: str, source_id: str, version: int, limit: int, offset: int) -> dict[str, Any]:
        snapshot = self.resolve(system_id, source_id, version)
        items = load_corpus(Path(snapshot["resolved_path"]))["chunks"]
        return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}


def verify_snapshot(source: dict[str, Any]) -> None:
    try:
        corpus_path = Path(source["resolved_path"])
        if digest(corpus_path.read_bytes()) != source["sha256"]:
            raise ValueError("corpus hash")
        index_path = Path(source["index_path"])
        if digest(index_path.read_bytes()) != source["index_sha256"]:
            raise ValueError("index hash")
        for doc in source["files"]:
            if digest((corpus_path.parent / "originals" / doc["original_name"]).read_bytes()) != doc["sha256"]:
                raise ValueError("original hash")
    except (OSError, ValueError, KeyError) as exc:
        raise ImportFailure("artifact_changed", "知识源文件或索引缺失、内容已改变，请重新导入材料。") from exc


def retriever_from_corpus_snapshot(source: dict[str, Any]):
    verify_snapshot(source)
    return _cached_retriever(source["resolved_path"], source["index_path"], source["sha256"],
                             source["index_sha256"], source["source_id"], source["version"])
