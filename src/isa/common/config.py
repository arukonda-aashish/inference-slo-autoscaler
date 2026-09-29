"""Typed config loading. Every config file in the project goes through load_yaml."""

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base for all config models.

    extra="forbid": an unknown key (usually a typo) is an error, not silently ignored.
    frozen=True: config can't be mutated after load, so every component sees the same values.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class SLOConfig(StrictModel):
    ttft_p95_s: float = Field(gt=0)
    tpot_p95_s: float = Field(gt=0)
    min_admission_rate: float = Field(gt=0, le=1)


class MetricsMap(StrictModel):
    vllm_version: str
    waiting: str
    running: str
    kv_usage: str
    ttft_hist: str
    tpot_hist: str

    @property
    def ttft_bucket(self) -> str:
        """Prometheus exposes histogram buckets as <name>_bucket."""
        return f"{self.ttft_hist}_bucket"

    @property
    def tpot_bucket(self) -> str:
        return f"{self.tpot_hist}_bucket"


M = TypeVar("M", bound=BaseModel)


def load_yaml(path: str | Path, model: type[M]) -> M:
    """Load a YAML file and validate it against a pydantic model.

    Raises FileNotFoundError if the file is missing, ValueError if the top level
    isn't a mapping, and pydantic.ValidationError if any field is wrong.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"config not found: {p}")
    with p.open() as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: expected a mapping at top level, got {type(raw).__name__}")
    return model.model_validate(raw)