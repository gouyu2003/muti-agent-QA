"""Strictly label-free HotpotQA context loader for batch Runtime inputs."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from src.knowledge.hotpotqa_loader import validate_context, build_paragraph_index


def runtime_input(record: dict) -> dict:
    public = deepcopy({key: record[key] for key in ('_id', 'question', 'context')})
    if (not isinstance(public['_id'], str) or not public['_id'].strip()
            or not isinstance(public['question'], str) or not public['question'].strip()):
        raise ValueError('Invalid context ID or question')
    validate_context(public['context'], Path('<batch context>'))
    build_paragraph_index(public['context'])
    return public


def load_context(path: Path) -> dict:
    record = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(record, dict) or set(record) != {'_id', 'question', 'context'}:
        raise ValueError('Runtime context must contain only _id, question and context')
    return runtime_input(record)
