"""Identify the exact Python runtime source used for a run."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")


def _source_files(project_root: Path) -> list[Path]:
    files = [project_root / "main.py"]
    for directory in ("src", "backend"):
        files.extend((project_root / directory).rglob("*.py"))
    files.extend(project_root.glob("requirements*.txt"))
    return sorted(path for path in files if path.is_file())


def source_fingerprint(project_root: Path = PROJECT_ROOT) -> str:
    """Hash executable Python sources and dependency declarations, including edits."""

    files = _source_files(project_root)
    if not files:
        raise RuntimeError("无法生成代码版本：未找到运行时代码文件")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(project_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def code_version(project_root: Path = PROJECT_ROOT) -> str:
    """Combine the base commit with a source hash; worktree edits remain identifiable."""

    build_id = os.getenv("MAQA_CODE_VERSION", "").strip()
    if build_id:
        return f"build:{build_id}+source-sha256:{source_fingerprint(project_root)}"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        commit = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        commit = ""
    prefix = f"git:{commit}+" if _COMMIT_RE.fullmatch(commit) else ""
    return f"{prefix}source-sha256:{source_fingerprint(project_root)}"
