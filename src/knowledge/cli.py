"""python -m src.knowledge.cli: import and inspect local document sources."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

from src.config_loader import load_json
from src.knowledge.corpus import MAX_FILE_BYTES, ImportFailure
from src.knowledge.store import KnowledgeStore
from src.runtime.system import DEFAULT_CATALOG_PATH


def read_files(paths: Sequence[str]) -> list[tuple[str, bytes]]:
    files = []
    for value in paths:
        path = Path(value)
        with path.open("rb") as file:
            files.append((path.name, file.read(MAX_FILE_BYTES + 1)))
    return files


def main(argv: Sequence[str] | None = None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    parser = argparse.ArgumentParser(description="导入英文 UTF-8 TXT/MD 知识源")
    parser.add_argument("--system-config", default=None)
    parser.add_argument("--knowledge-dir", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("files", nargs="+")
    update = commands.add_parser("add-version")
    update.add_argument("source_id")
    update.add_argument("files", nargs="+")
    commands.add_parser("list")
    args = parser.parse_args(argv)
    try:
        catalog = load_json(args.system_config or os.getenv("MAQA_SYSTEM_CONFIG_PATH") or DEFAULT_CATALOG_PATH)
        store = KnowledgeStore(args.knowledge_dir)
        store.recover_interrupted()
        if args.command == "list":
            result = store.list_sources(catalog["system_id"])
        else:
            result = store.import_files(catalog["system_id"],
                                        args.name if args.command == "create" else None,
                                        read_files(args.files),
                                        args.source_id if args.command == "add-version" else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(isinstance(result, dict) and result["status"] == "failed")
    except (ImportFailure, OSError, ValueError) as exc:
        print(json.dumps({"error": {"code": getattr(exc, "code", "import_error"), "message": str(exc)}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
