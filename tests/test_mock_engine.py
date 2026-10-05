from pathlib import Path

import pytest
from pydantic import ValidationError

from isa.common.config import load_yaml
from isa.mock_replica.engine import Engine, EngineConfig, FinishedRecord, StepResult

CONFIGS = Path(__file__).resolve().parent.parent / "configs"

BASE = {
    "block_size": 16,
    "num_kv_blocks": 64,  # 1024 tokens
    "watermark_frac": 0.0,
    "max_num_seqs": 4,
    "max_batched_tokens": 256,
    "max_model_len": 512,
    "step_base_ms": 10.0,
    "step_per_seq_ms": 1.0,
    "prefill_per_token_ms": 0.1,
}


def make_cfg(**overrides) -> EngineConfig:
    return EngineConfig(**{**BASE, **overrides})


def run_until_idle(eng: Engine, t: float = 0.0, max_steps: int = 100_000):
    results: list[StepResult] = []
    for _ in range(max_steps):
        if not eng.has_work:
            return t, results
        r = eng.step(t)
        results.append(r)
        t = r.t_end
    raise AssertionError("engine did not go idle")


def finished(results: list[StepResult]) -> dict[str, FinishedRecord]:
    return {rec.req_id: rec for r in results for rec in r.finished}


def assert_no_leak(eng: Engine) -> None:
    assert not eng.has_work
    assert eng.kv_usage == 0.0


# ---- config ------------------------------------------------------------------


def test_repo_engine_config_loads():
    cfg = load_yaml(CONFIGS / "mock_engine.yaml", EngineConfig)
    assert cfg.watermark_blocks == 21  # ceil(2048 * 0.01)


def test_config_rejects_model_len_exceeding_kv():
    with pytest.raises(ValidationError, match="max_model_len needs"):
        make_cfg(max_model_len=2048)  # 128 blocks > 64


def test_config_rejects_seqs_over_batch_budget():
    with pytest.raises(ValidationError, match="max_num_seqs"):
        make_cfg(max_num_seqs=300)


# ---- lifecycle and timing ----------------------------------------------------


def test_single_request_lifecycle():
    eng = Engine(make_cfg())
    eng.submit("a", prompt_tokens=100, max_tokens=5, now=0.0)
    _, results = run_until_idle(eng)
    rec = finished(results)["a"]
    assert rec.output_tokens == 5
    assert rec.ttft_s == pytest.approx(0.020)  # 10ms base + 100 tokens * 0.1ms
    assert rec.tpot_s == pytest.approx(0.011)  # 10ms base + 1 seq * 1ms
    assert_no_leak(eng)


def test_chunked_prefill_delays_first_token():
    eng = Engine(make_cfg())
    eng.submit("a", prompt_tokens=500, max_tokens=2, now=0.0)
    _, results = run_until_idle(eng)
    assert results[0].prefill_tokens == 256
    assert results[0].tokens == []  # prefill incomplete: no token yet
    # step 1: 10 + 25.6 ms, step 2: 10 + 24.4 ms
    assert finished(results)["a"].ttft_s == pytest.approx(0.070)
    assert_no_leak(eng)


def test_decode_step_time_scales_with_batch():
    eng = Engine(make_cfg())
    for i in range(4):
        eng.submit(f"r{i}", prompt_tokens=16, max_tokens=10, now=0.0)
    first = eng.step(0.0)  # all four prefill in one step
    second = eng.step(first.t_end)
    assert second.decode_seqs == 4
    assert second.duration_s == pytest.approx(0.014)  # 10ms + 4 * 1ms


def test_idle_step_is_noop():
    r = Engine(make_cfg()).step(5.0)
    assert r.duration_s == 0.0
    assert r.t_end == 5.0
    assert r.tokens == []


# ---- admission ---------------------------------------------------------------


def test_max_num_seqs_caps_admission():
    eng = Engine(make_cfg())
    for i in range(6):
        eng.submit(f"r{i}", prompt_tokens=16, max_tokens=3, now=0.0)
    r = eng.step(0.0)
    assert len(r.admitted) == 4
    assert eng.num_running == 4
    assert eng.num_waiting == 2


def test_head_of_line_blocking():
    eng = Engine(make_cfg())
    eng.submit("a", prompt_tokens=480, max_tokens=10, now=0.0)  # 30 blocks
    eng.submit("b", prompt_tokens=500, max_tokens=10, now=0.0)  # 32 blocks, 2 left free
    eng.submit("c", prompt_tokens=500, max_tokens=10, now=0.0)  # needs 32: blocked
    eng.submit("d", prompt_tokens=16, max_tokens=10, now=0.0)  # would fit, must wait
    r = eng.step(0.0)
    assert r.admitted == ["a", "b"]
    assert eng.num_waiting == 2


def test_watermark_reserved_at_admission():
    eng = Engine(make_cfg(watermark_frac=0.1))  # 7 blocks reserved
    eng.submit("a", prompt_tokens=480, max_tokens=10, now=0.0)  # 30 blocks -> 34 free
    eng.submit("b", prompt_tokens=400, max_tokens=10, now=0.0)  # 25 blocks -> 9 free
    eng.submit("c", prompt_tokens=48, max_tokens=10, now=0.0)  # 3 blocks would leave 6 < 7
    r = eng.step(0.0)
    assert r.admitted == ["a", "b"]


# ---- memory pressure ---------------------------------------------------------


def test_preemption_preserves_token_stream():
    # 34 blocks: two 256-token prompts take 32, so decode growth runs out fast.
    eng = Engine(make_cfg(num_kv_blocks=34))
    eng.submit("a", prompt_tokens=256, max_tokens=40, now=0.0)
    eng.submit("b", prompt_tokens=256, max_tokens=40, now=0.0)
    _, results = run_until_idle(eng)

    assert any(r.preempted for r in results), "expected at least one preemption"
    recs = finished(results)
    assert sum(rec.preemptions for rec in recs.values()) >= 1

    # No token lost or duplicated across preemption and recompute.
    for req_id in ("a", "b"):
        indices = [t.index for r in results for t in r.tokens if t.req_id == req_id]
        assert indices == list(range(1, 41))
        assert recs[req_id].output_tokens == 40
    assert_no_leak(eng)


def test_abort_frees_blocks():
    eng = Engine(make_cfg())
    eng.submit("a", prompt_tokens=100, max_tokens=50, now=0.0)
    eng.step(0.0)
    assert eng.kv_usage > 0
    assert eng.abort("a") is True
    assert eng.abort("a") is False
    eng.submit("b", prompt_tokens=100, max_tokens=50, now=1.0)
    assert eng.abort("b") is True  # aborting while still waiting
    assert_no_leak(eng)


def test_submit_validation():
    eng = Engine(make_cfg())
    eng.submit("a", prompt_tokens=10, max_tokens=10, now=0.0)
    with pytest.raises(ValueError, match="duplicate"):
        eng.submit("a", prompt_tokens=10, max_tokens=10, now=0.0)
    with pytest.raises(ValueError, match="max_model_len"):
        eng.submit("b", prompt_tokens=500, max_tokens=100, now=0.0)
    with pytest.raises(ValueError, match=">= 1"):
        eng.submit("c", prompt_tokens=0, max_tokens=10, now=0.0)


# ---- the behavior the project is about -----------------------------------------


def test_queue_wait_dominates_ttft_under_load():
    eng = Engine(make_cfg())
    for i in range(20):
        eng.submit(f"r{i:02d}", prompt_tokens=64, max_tokens=20, now=0.0)
    _, results = run_until_idle(eng)
    recs = finished(results)
    first, last = recs["r00"].ttft_s, recs["r19"].ttft_s
    # Same prompt, same arrival time: the only difference is time spent queued.
    assert last > 5 * first
    assert_no_leak(eng)