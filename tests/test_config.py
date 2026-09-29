from pathlib import Path

import pytest
from pydantic import ValidationError

from isa.common.config import MetricsMap, SLOConfig, load_yaml

CONFIGS = Path(__file__).resolve().parent.parent / "configs"


def test_repo_slo_config_loads():
    slo = load_yaml(CONFIGS / "slo.yaml", SLOConfig)
    assert slo.ttft_p95_s == 2.0
    assert slo.min_admission_rate == 0.95


def test_repo_metrics_map_loads():
    m = load_yaml(CONFIGS / "metrics_map.yaml", MetricsMap)
    assert m.ttft_bucket == "vllm:time_to_first_token_seconds_bucket"


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "slo.yaml"
    p.write_text("ttft_p95_s: 2.0\ntpot_p95_s: 0.08\nmin_admission_rate: 0.95\nttft_p99_s: 5\n")
    with pytest.raises(ValidationError, match="ttft_p99_s"):
        load_yaml(p, SLOConfig)


def test_out_of_range_rejected(tmp_path):
    p = tmp_path / "slo.yaml"
    p.write_text("ttft_p95_s: -1\ntpot_p95_s: 0.08\nmin_admission_rate: 1.5\n")
    with pytest.raises(ValidationError) as exc:
        load_yaml(p, SLOConfig)
    assert exc.value.error_count() == 2


def test_non_mapping_rejected(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="expected a mapping"):
        load_yaml(p, SLOConfig)


def test_missing_file_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_yaml(tmp_path / "nope.yaml", SLOConfig)


def test_config_is_frozen():
    slo = load_yaml(CONFIGS / "slo.yaml", SLOConfig)
    with pytest.raises(ValidationError):
        slo.ttft_p95_s = 99.0