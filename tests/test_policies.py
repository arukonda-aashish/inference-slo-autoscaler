from pathlib import Path

import pytest

from isa.common.config import load_yaml
from isa.controller.policies import (
    PolicyFile,
    PredictivePolicy,
    PredictivePolicyConfig,
    QueuePolicy,
    QueuePolicyConfig,
    UtilPolicy,
    UtilPolicyConfig,
    make_policy,
)
from isa.controller.signals import Signals
from isa.controller.stabilizer import StabilizerConfig

CONFIGS = Path(__file__).resolve().parent.parent / "configs"


def sig(**kw) -> Signals:
    base = dict(
        ts=0.0, ready=1, starting=0, draining=0, waiting_total=0.0, running_total=0.0,
        kv_usage_max=0.1, queue_slope_per_s=0.0, gpu_util_mean=0.0,
    )
    return Signals(**{**base, **kw})


UTIL = UtilPolicy(UtilPolicyConfig(kind="util", target_util_percent=80))
QUEUE = QueuePolicy(QueuePolicyConfig(kind="queue", target_concurrency=12))
PREDICTIVE = PredictivePolicy(
    PredictivePolicyConfig(
        kind="predictive", target_concurrency=12, mu_rps=9.4, rho_target=0.7, t_cold_s=50
    )
)


def test_repo_configs_load():
    for path in sorted((CONFIGS / "policies").glob("*.yaml")):
        policy = make_policy(load_yaml(path, PolicyFile).policy)
        assert policy.name == path.stem
    load_yaml(CONFIGS / "stabilizer.yaml", StabilizerConfig)


# ---- util (baseline) ---------------------------------------------------------


def test_util_hpa_formula():
    d = UTIL.decide(sig(ready=2, gpu_util_mean=97.0))
    assert d.desired == 3  # ceil(2 * 97 / 80)
    assert d.reason == "util_ratio"


def test_util_tolerance_band_holds():
    d = UTIL.decide(sig(ready=2, gpu_util_mean=85.0))  # ratio 1.06, inside 10%
    assert d.desired == 2
    assert d.reason == "within_tolerance"


def test_util_scales_down_when_idle():
    assert UTIL.decide(sig(ready=3, gpu_util_mean=0.0)).desired == 0


def test_util_cannot_tell_light_load_from_overload():
    # The thesis as a unit test: 4 requests in flight vs 184 in flight with a
    # growing queue. Utilization reads ~97% either way, so the decisions match.
    light = sig(running_total=4, gpu_util_mean=97.0)
    overload = sig(running_total=64, waiting_total=120, queue_slope_per_s=4.6, gpu_util_mean=98.0)
    assert UTIL.decide(light).desired == UTIL.decide(overload).desired


# ---- queue -------------------------------------------------------------------


def test_queue_concurrency_formula():
    d = QUEUE.decide(sig(running_total=20, waiting_total=10))
    assert d.desired == 3  # ceil(30 / 12)
    assert d.reason == "concurrency"


def test_queue_distinguishes_light_load_from_overload():
    light = QUEUE.decide(sig(running_total=4, gpu_util_mean=97.0))
    overload = QUEUE.decide(sig(running_total=64, waiting_total=120, gpu_util_mean=98.0))
    assert light.desired == 1
    assert overload.desired == 16  # far above max; the stabilizer bounds it


def test_queue_kv_pressure_override():
    d = QUEUE.decide(sig(running_total=6, kv_usage_max=0.95))  # concurrency alone says 1
    assert d.desired == 2
    assert d.reason == "kv_pressure"


# ---- predictive --------------------------------------------------------------


def test_predictive_capacity_term_leads_at_burst_onset():
    # Arrivals just jumped to 16 rps; the queue hasn't built yet.
    s = sig(running_total=8, arrival_rate_rps=16.0)
    d = PREDICTIVE.decide(s)
    assert d.desired == 3  # ceil(16 / (9.4 * 0.7)) = ceil(2.43)
    assert d.reason == "arrival_capacity"
    assert QUEUE.decide(s).desired == 1  # the queue policy hasn't noticed yet


def test_predictive_projects_queue_over_cold_start():
    d = PREDICTIVE.decide(sig(running_total=12, queue_slope_per_s=0.5))
    assert d.inputs["projected_outstanding"] == pytest.approx(37.0)  # 12 + 0.5 * 50
    assert d.desired == 4  # ceil(37 / 12)
    assert d.reason == "projected_queue"


def test_predictive_ignores_negative_slope():
    shrinking = PREDICTIVE.decide(sig(running_total=12, queue_slope_per_s=-3.0))
    flat = PREDICTIVE.decide(sig(running_total=12))
    assert shrinking.desired == flat.desired == 1


def test_predictive_without_arrival_rate_uses_projection_only():
    d = PREDICTIVE.decide(sig(running_total=30))
    assert d.inputs["by_capacity"] is None
    assert d.desired == 3