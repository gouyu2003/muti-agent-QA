"""Strict, credential-free configuration for the opt-in experiment CLI."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.runtime.protocols import Limits

ROOT = Path(__file__).resolve().parents[2]
MethodId = Literal['single_rag', 'pipeline_fixed', 'pipeline_adaptive', 'multi_expert', 'lead', 'debate']
METHODS = list(MethodId.__args__)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8', newline='\n') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ModelConfig(Strict):
    name: str = Field(default='deepseek-v4.1-flash', min_length=1)
    temperature: float = Field(default=0.0, ge=0, le=2, allow_inf_nan=False)
    max_tokens: int = Field(default=4096, ge=1, le=65536)
    output_retries: int = Field(default=2, ge=0, le=5)


class Splits(Strict):
    development: int = Field(default=300, ge=1)
    evaluation: int = Field(default=1000, ge=1)
    smoke: int = Field(default=6, ge=1)

    @model_validator(mode='after')
    def subset(self):
        if self.smoke > self.development:
            raise ValueError('smoke must be a subset of development')
        return self


class PublishedSystem(Strict):
    database: str
    system_id: str
    version: str = Field(pattern=r'^v[1-9][0-9]*$')


class Price(Strict):
    currency: str = Field(min_length=1)
    as_of: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    input_per_million: float = Field(ge=0, allow_inf_nan=False)
    output_per_million: float = Field(ge=0, allow_inf_nan=False)


class ExperimentLimits(Limits):
    max_llm_calls: int = Field(default=40, strict=True, ge=1, le=160)


class ExperimentConfig(Strict):
    schema_version: int = 1
    dataset: str
    dataset_source: str = 'HotpotQA dev distractor; user supplied local file'
    dataset_sha256: str | None = None
    split_directory: str
    output: str
    purpose: Literal['smoke', 'development', 'evaluation'] = 'development'
    splits: Splits = Field(default_factory=Splits)
    seed: int = 42
    sampling: Literal['proportional', 'balanced'] = 'proportional'
    exclude_evaluation_ids: list[str] = Field(default_factory=list)
    exposure_manifests: list[str] = Field(default_factory=list)
    methods: list[MethodId] = Field(default_factory=lambda: METHODS.copy(), min_length=1)
    model: ModelConfig = Field(default_factory=ModelConfig)
    top_k: int = Field(default=3, ge=1, le=50)
    expert_count: int = Field(default=2, ge=2, le=3)
    execution_mode: Literal['sequential', 'parallel'] = 'sequential'
    retrieval_retries: int = Field(default=3, ge=0, le=3)
    plan_retries: int = Field(default=2, ge=0, le=2)
    lead_rounds: int = Field(default=2, ge=1, le=4)
    debate_rounds: int = Field(default=2, ge=1, le=5)
    limits: ExperimentLimits = Field(default_factory=ExperimentLimits)
    repeats: int = Field(default=1, ge=1, le=100)
    rotate_methods: bool = True
    bootstrap_samples: int = Field(default=2000, ge=100, le=10000)
    review_examples_per_category: int = Field(default=5, ge=1, le=100)
    published_systems: dict[MethodId, PublishedSystem] = Field(default_factory=dict)
    price: Price | None = None

    @model_validator(mode='after')
    def contract(self):
        if self.schema_version != 1 or len(set(self.methods)) != len(self.methods):
            raise ValueError('Unsupported schema or duplicate methods')
        if not set(self.published_systems) <= set(self.methods):
            raise ValueError('Published systems must refer to selected methods')
        if not self.model.name.strip():
            raise ValueError('Model name cannot be blank')
        return self


def load_config(path: Path) -> ExperimentConfig:
    config = ExperimentConfig.model_validate(read_json(path))
    def absolute(value):
        return str((path.resolve().parent / value).resolve())
    config.dataset = absolute(config.dataset)
    config.split_directory = absolute(config.split_directory)
    config.output = absolute(config.output)
    config.exposure_manifests = [absolute(p) for p in config.exposure_manifests]
    for system in config.published_systems.values():
        system.database = absolute(system.database)
    # Results must never target the business database directories or source tree.
    output, splits, dataset = Path(config.output), Path(config.split_directory), Path(config.dataset)
    if output == splits or output in splits.parents or splits in output.parents:
        raise ValueError('Output and split directories must be separate siblings')
    if output == dataset or output in dataset.parents or splits == dataset or splits in dataset.parents:
        raise ValueError('Dataset must be outside output and split directories')
    for candidate in (output, splits):
        if candidate == ROOT or candidate in ROOT.parents:
            raise ValueError('Experiment directory cannot contain the project')
        for protected in ('src', 'backend', 'frontend', '.git', 'system-design',
                          'data/runtime', 'data/systems', 'data/knowledge', 'data/protocols'):
            root = ROOT / protected
            if candidate == root or root in candidate.parents or candidate in root.parents:
                raise ValueError('Experiment directory overlaps protected project storage')
    return config
