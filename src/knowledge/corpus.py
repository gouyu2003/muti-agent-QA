"""Deterministic TXT/Markdown parsing and JSON-backed BM25 corpora."""
from __future__ import annotations

import hashlib
from bisect import bisect_left
import json
import re
from pathlib import Path
from typing import Any

from src.knowledge.bm25_retriever import BM25Retriever, tokenize

PARSER_VERSION = "text-v1"
INDEX_VERSION = "bm25-en-v1"
MAX_CHARS = 600
MAX_FILES = 10
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_CHUNKS = 20_000


class ImportFailure(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def parse_document(filename: str, raw: bytes) -> dict[str, Any]:
    if not filename or filename != filename.replace("\\", "/").split("/")[-1]:
        raise ImportFailure("invalid_filename", "文件名必须是普通文件名，不能包含路径。")
    if Path(filename).suffix.lower() not in {".txt", ".md"}:
        raise ImportFailure("unsupported_format", f"{filename}：仅支持 TXT/MD。")
    if len(raw) > MAX_FILE_BYTES:
        raise ImportFailure("file_too_large", f"{filename}：单文件不能超过 2 MiB。")
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise ImportFailure("invalid_encoding", f"{filename}：请使用 UTF-8 编码。") from exc
    if not text.strip() or "\x00" in text:
        raise ImportFailure("empty_or_binary", f"{filename}：文件为空或包含非文本内容。")
    document_id = digest(filename.encode("utf-8") + b"\0" + raw)
    chunks: list[dict[str, Any]] = []
    newlines = [i for i, char in enumerate(text) if char == "\n"]
    headings: list[tuple[int, str]] = []
    start: int | None = None
    offset = 0
    fence: tuple[str, int] | None = None

    def flush(end: int) -> None:
        nonlocal start
        if start is None:
            return
        position = start
        while position < end:
            while position < end and text[position].isspace():
                position += 1
            if position >= end:
                break
            cut = min(end, position + MAX_CHARS)
            if cut < end:
                boundary = max((i for i in range(position + 1, cut + 1) if text[i - 1].isspace()), default=cut)
                cut = boundary
            stop = cut
            while stop > position and text[stop - 1].isspace():
                stop -= 1
            if stop > position:
                chunk_id = digest(f"{document_id}:{position}:{stop}:{PARSER_VERSION}".encode())
                chunks.append({
                    "document_id": document_id, "chunk_id": chunk_id,
                    "filename": filename, "heading_path": [h for _, h in headings],
                    "line_start": bisect_left(newlines, position) + 1,
                    "line_end": bisect_left(newlines, stop - 1) + 1,
                    "char_start": position, "char_end": stop,
                    "text": text[position:stop], "source_title": filename,
                })
            position = cut
        start = None

    for line in text.splitlines(keepends=True):
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})", line) if filename.lower().endswith(".md") else None
        inside_fence = fence is not None
        if fence_match:
            marker = fence_match[1]
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
        heading = (re.match(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?$", line.rstrip("\n"))
                   if filename.lower().endswith(".md") and not inside_fence and not fence_match else None)
        if heading or not line.strip():
            flush(offset)
            if heading:
                level, title = len(heading[1]), heading[2]
                headings[:] = [(depth, name) for depth, name in headings if depth < level]
                headings.append((level, title))
        elif start is None:
            start = offset
        offset += len(line)
    flush(len(text))
    if not chunks:
        raise ImportFailure("no_content", f"{filename}：没有可检索的正文。")
    return {"document_id": document_id, "filename": filename, "sha256": digest(raw),
            "size_bytes": len(raw), "text": text, "chunks": chunks}


def prepare_corpus(files: list[tuple[str, bytes]]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not 1 <= len(files) <= MAX_FILES:
        raise ImportFailure("file_count", "每次请选择 1 至 10 个文件。")
    if sum(len(raw) for _, raw in files) > MAX_TOTAL_BYTES:
        raise ImportFailure("total_too_large", "文件总大小不能超过 10 MiB。")
    if len({name.casefold() for name, _ in files}) != len(files):
        raise ImportFailure("duplicate_filename", "同一版本中不能有重复文件名。")
    documents = [parse_document(name, raw) for name, raw in sorted(files)]
    chunks = [chunk for doc in documents for chunk in doc["chunks"]]
    if len(chunks) > MAX_CHUNKS:
        raise ImportFailure("too_many_chunks", "片段数不能超过 20,000。")
    tokens = [tokenize(c["filename"] + " " + " ".join(c["heading_path"]) + " " + c["text"]) for c in chunks]
    if not any(tokenize(c["text"]) for c in chunks):
        raise ImportFailure("no_english_tokens", "材料没有可索引的英文单词或数字。")
    corpus = {"parser_version": PARSER_VERSION, "max_chars": MAX_CHARS,
              "documents": [{k: v for k, v in doc.items() if k != "chunks"} for doc in documents],
              "chunks": chunks}
    index = {"index_version": INDEX_VERSION, "tokenizer": "lowercase-ascii-alphanumeric",
             "corpus_sha256": digest(json_bytes(corpus)), "tokens": tokens}
    # Validate index construction before publishing any version.
    BM25Retriever.from_documents(chunks, tokens=tokens)
    return corpus, index


def load_corpus(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def corpus_retriever(record: dict[str, Any], source_id: str) -> BM25Retriever:
    chunks = [{**chunk, "source_id": source_id, "source_version": record["source_version"]}
              for chunk in record["chunks"]]
    return BM25Retriever.from_documents(chunks, tokens=record["index"]["tokens"])
