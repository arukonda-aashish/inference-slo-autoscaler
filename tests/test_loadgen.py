import csv
import math
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from isa.common.config import MetricsMap, load_yaml
from isa.loadgen.client import RequestRecord
from isa.loadgen.profile import Profile
from isa.loadgen.report import CsvRecorder, percentile, summarize
from isa.loadgen.runner import PreflightError, run
from isa.loadgen.schedule import build_prompt, build_schedule
from isa.mock_replica.engine import EngineConfig
from isa.mock_replica.server import ServerConfig, create_app

CONFIGS = Path(__file__).resolve().parent.parent / "configs"
MODEL = "test-model"
NAMES = load_yaml(CONFIGS / "metrics_map.yaml", MetricsMap)
FAST_ENGINE = EngineConfig(
    block_size=16,
    num_kv_blocks=256,
    watermark_frac=0.0,
    max_num_seqs=8,
    max_batched_tokens=512,
    max_model_len=1024,
    step_base_ms=1.0,
    step_per_seq_ms=0.1,
    prefill_per_token_ms=0.001,
)

BASE_PROFILE = {
    "name": "t",
    "seed": 7,
    "phases": [{"duration_s": 1.0, "rate_rps": 30}],
    "prompt_tokens": {"mixture": [{"weight": 1.0, "range": [5, 20]}]},
    "max_tokens": {"range": [3, 8]},
}


def make_profile(**overrides) -> Profile:
    return Profile.model_validate({**BASE_PROFILE, **overrides})


def read_csv_rows(path: Path) -> list[dict]:
    with path.open() as f:
        return list(csv.DictReader(f))


# ---- profiles ----------------------------------------------------------------


def test_repo_profiles_load():
    paths = sorted((CONFIGS / "profiles").glob("*.yaml"))
    assert paths
    for path in paths:
        profile = load_yaml(path, Profile)
        assert profile.max_request_tokens <= 4096, path.name


def test_profile_rejects_inverted_range():
    with pytest.raises(ValidationError, match="lo <= hi"):
        make_profile(max_tokens={"range": [10, 5]})


# ---- schedule ----------------------------------------------------------------


def test_schedule_is_deterministic():
    a = build_schedule(make_profile())
    assert a == build_schedule(make_profile())
    assert a != build_schedule(make_profile(seed=8))


def test_schedule_rate_and_phase_bounds():
    p = make_profile(phases=[{"duration_s": 200, "rate_rps": 5}])
    s = build_schedule(p)
    assert 900 <= len(s) <= 1100  # expect 1000; Poisson sd ~32
    offsets = [a.offset_s for a in s]
    assert offsets == sorted(offsets)
    assert 0 < offsets[0] and offsets[-1] < 200


def test_interarrivals_are_exponential():
    p = make_profile(phases=[{"duration_s": 1000, "rate_rps": 5}])
    gaps = [b.offset_s - a.offset_s for a, b in pairwise(build_schedule(p))]
    mean = sum(gaps) / len(gaps)
    sd = math.sqrt(sum((g - mean) ** 2 for g in gaps) / (len(gaps) - 1))
    assert mean == pytest.approx(0.2, rel=0.05)  # 1 / rate
       # An exponential's coefficient of variation (sd / mean) is exactly 1.
    assert sd / mean == pytest.approx(1.0, abs=0.08)


def test_zero_rate_phase_has_no_arrivals():
    p = make_profile(phases=[
        {"duration_s": 10, "rate_rps": 5},
        {"duration_s": 10, "rate_rps": 0},
        {"duration_s": 10, "rate_rps": 5},
    ])
    s = build_schedule(p)
    assert not any(10 <= a.offset_s < 20 for a in s)
    assert {a.phase for a in s} == {0, 2}


def test_mixture_weights_respected():
    p = make_profile(
        phases=[{"duration_s": 1000, "rate_rps": 5}],
        prompt_tokens={"mixture": [
            {"weight": 0.7, "range": [1, 10]},
            {"weight": 0.3, "range": [100, 200]},
        ]},
    )
    s = build_schedule(p)
    short = sum(a.prompt_tokens <= 10 for a in s) / len(s)
    assert short == pytest.approx(0.7, abs=0.03)


def test_size_mix_never_moves_arrivals():
    a = build_schedule(make_profile())
    b = build_schedule(make_profile(prompt_tokens={"mixture": [{"weight": 1, "range": [50, 60]}]}))
    assert [x.offset_s for x in a] == [x.offset_s for x in b]


def test_schedule_rejects_requests_over_max_model_len():
    with pytest.raises(ValueError, match="max_model_len"):
        build_schedule(make_profile(), max_model_len=10)


def test_prompt_word_count_and_determinism():
    prompt = build_prompt(seed=1, idx=0, n_tokens=50)
    assert len(prompt.split()) == 50
    assert prompt == build_prompt(seed=1, idx=0, n_tokens=50)
    # Distinct first 16 words = distinct first KV block: no prefix-cache hits.
    assert prompt.split()[:16] != build_prompt(seed=1, idx=1, n_tokens=50).split()[:16]


# ---- summary and validity ----------------------------------------------------


def test_percentile():
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 0) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0
    assert percentile([], 50) is None


def rec(i: int, lag: float = 0.001, ttft: float = 0.2, status: int = 200) -> RequestRecord:
    return RequestRecord(
        req_id=f"r{i}", phase=0, intended_ts=float(i), prompt_tokens=10, max_tokens=5,
        sent_ts=i + lag, first_token_ts=i + ttft, status=status,
    )


def test_summary_valid_run():
    s = summarize([rec(i) for i in range(100)], "p", peak_inflight=5, max_connections=100)
    assert s.valid
    assert s.ttft_p50_s == pytest.approx(0.2)
    assert s.by_phase["0"]["ok"] == 100


def test_summary_flags_generator_lag():
    s = summarize([rec(i, lag=0.2) for i in range(100)], "p", peak_inflight=5, max_connections=100)
    assert not s.valid
    assert "dispatch lag" in s.invalid_reasons[0]


def test_summary_flags_connection_limit():
    s = summarize([rec(i) for i in range(10)], "p", peak_inflight=100, max_connections=100)
    assert not s.valid
    assert "connection limit" in s.invalid_reasons[0]


def test_summary_flags_cancelled_requests():
    records = [rec(i) for i in range(10)] + [rec(10, status=-1)]
    s = summarize(records, "p", peak_inflight=5, max_connections=100)
    assert not s.valid
    assert s.cancelled == 1


# ---- end to end against a mock replica -----------------------------------------


async def run_against_mock(
    profile, tmp_path, model=MODEL, drain_timeout_s=120.0, check_target=True
):
    schedule = build_schedule(profile)
    mock = create_app(
        ServerConfig(replica_id="m1", model_name=MODEL, port=9000), FAST_ENGINE, NAMES
    )
    recorder = CsvRecorder(tmp_path / "run.csv")
    async with mock.router.lifespan_context(mock):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock)) as client:
            stats = await run(
                profile, schedule, base_url="http://mock", model=model,
                recorder=recorder, drain_timeout_s=drain_timeout_s, client=client,
                check_target=check_target,
            )
    recorder.close()
    return schedule, recorder, stats


async def test_run_end_to_end(tmp_path):
    schedule, recorder, stats = await run_against_mock(make_profile(), tmp_path)
    assert len(schedule) > 0
    assert len(recorder.records) == len(schedule)
    assert len(read_csv_rows(recorder.path)) == len(schedule)
    assert stats.peak_inflight >= 1
    for r in recorder.records:
        assert r.status == 200, r.error
        assert r.output_tokens == r.max_tokens
        assert r.server_prompt_tokens == r.prompt_tokens
        assert r.sent_ts >= r.intended_ts - 0.005  # never sent meaningfully early
        assert r.first_token_ts is not None and r.first_token_ts >= r.sent_ts


async def test_run_records_http_errors(tmp_path):
    _, recorder, _ = await run_against_mock(
        make_profile(), tmp_path, model="wrong", check_target=False
    )
    assert recorder.records
    assert all(r.status == 404 for r in recorder.records)
    assert "does not exist" in recorder.records[0].error


async def test_drain_timeout_cancels_but_still_records(tmp_path):
    p = make_profile(
        phases=[{"duration_s": 0.5, "rate_rps": 20}],
        prompt_tokens={"mixture": [{"weight": 1, "range": [5, 5]}]},
        max_tokens={"range": [500, 500]},  # far longer than the drain timeout
    )
    schedule, recorder, _ = await run_against_mock(p, tmp_path, drain_timeout_s=0.05)
    assert len(recorder.records) == len(schedule) > 0
    assert all(r.status == -1 for r in recorder.records)
    assert all(r.error == "cancelled_at_run_end" for r in recorder.records)

async def test_preflight_fails_fast(tmp_path):
    with pytest.raises(PreflightError, match="HTTP 404"):
        await run_against_mock(make_profile(), tmp_path, model="wrong")


def test_summary_flags_system_errors():
    records = [rec(i) for i in range(95)] + [rec(i, status=502) for i in range(95, 100)]
    s = summarize(records, "p", peak_inflight=5, max_connections=100)
    assert not s.valid
    assert s.errors == 5
    assert "other than 429" in s.invalid_reasons[0]


def test_summary_treats_429_as_a_measurement():
    records = [rec(i) for i in range(50)] + [rec(i, status=429) for i in range(50, 100)]
    s = summarize(records, "p", peak_inflight=5, max_connections=100)
    assert s.valid
    assert s.ok == 50
    assert s.errors == 0


def test_summary_flags_no_successes():
    records = [rec(i, status=502) for i in range(10)]
    s = summarize(records, "p", peak_inflight=1, max_connections=100)
    assert not s.valid
    assert "no request succeeded" in s.invalid_reasons[0]