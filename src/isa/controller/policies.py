"""Scaling policies: pure functions from Signals to a desired replica count.

A policy only says how many replicas it wants. Bounds, step limits, and cooldowns
belong to the stabilizer, which applies them identically to every policy so that
comparisons measure the policies, not differences in damping.
"""

import math
from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol

from pydantic import Field

from isa.common.config import StrictModel
from isa.controller.signals import Signals


@dataclass(frozen=True)
class Decision:
    desired: int
    reason: str
    inputs: dict[str, float | None] = field(default_factory=dict)  # logged with every decision


class Policy(Protocol):
    name: str

    def decide(self, s: Signals) -> Decision: ...


# ---- configs -----------------------------------------------------------------


class UtilPolicyConfig(StrictModel):
    kind: Literal["util"]
    target_util_percent: float = Field(gt=0, le=100)
    tolerance: float = Field(default=0.1, ge=0, lt=1)  # HPA's default


class QueuePolicyConfig(StrictModel):
    kind: Literal["queue"]
    target_concurrency: float = Field(gt=0)
    kv_high: float = Field(default=0.9, gt=0, le=1)


class PredictivePolicyConfig(StrictModel):
    kind: Literal["predictive"]
    target_concurrency: float = Field(gt=0)
    kv_high: float = Field(default=0.9, gt=0, le=1)
    mu_rps: float = Field(gt=0)  # per-replica service rate, measured
    rho_target: float = Field(gt=0, le=1)  # target utilization of that rate
    t_cold_s: float = Field(ge=0)  # cold start + detection delay


PolicyConfig = Annotated[
    UtilPolicyConfig | QueuePolicyConfig | PredictivePolicyConfig,
    Field(discriminator="kind"),
]


class PolicyFile(StrictModel):
    """Top-level shape of configs/policies/*.yaml."""

    policy: PolicyConfig


# ---- policies ----------------------------------------------------------------


class UtilPolicy:
    """Baseline: the Kubernetes HPA formula on GPU utilization.

    desired = ceil(current * util / target), held when the ratio is within the
    tolerance band around 1. Implemented faithfully so that its failure is the
    signal's fault, not a strawman's: GPU utilization reads ~97% whenever anything
    is running, so the policy can't tell light load from overload.
    """

    name = "util"

    def __init__(self, cfg: UtilPolicyConfig) -> None:
        self.cfg = cfg

    def decide(self, s: Signals) -> Decision:
        ratio = s.gpu_util_mean / self.cfg.target_util_percent
        inputs = {"gpu_util_mean": s.gpu_util_mean, "ratio": ratio, "current": float(s.current)}
        if abs(ratio - 1.0) <= self.cfg.tolerance:
            return Decision(s.current, "within_tolerance", inputs)
        return Decision(math.ceil(max(s.current, 1) * ratio), "util_ratio", inputs)


def _kv_override(desired: int, s: Signals, kv_high: float) -> tuple[int, bool]:
    """A replica near KV exhaustion will preempt regardless of request count."""
    if s.kv_usage_max >= kv_high and desired <= s.current:
        return s.current + 1, True
    return desired, False


class QueuePolicy:
    """Scale on outstanding requests (running + waiting) per replica.

    desired = ceil(outstanding / target_concurrency): Knative-style concurrency
    targeting. Waiting alone would be wrong: waiting == 0 doesn't mean spare
    capacity, and a waiting-only policy would scale a saturated pool down, then
    back up, and oscillate. Outstanding requests grow with load across the whole
    range, including past saturation, so the signal keeps its meaning exactly
    where it's needed.
    """

    name = "queue"

    def __init__(self, cfg: QueuePolicyConfig) -> None:
        self.cfg = cfg

    def decide(self, s: Signals) -> Decision:
        desired = math.ceil(s.outstanding / self.cfg.target_concurrency)
        desired, kv = _kv_override(desired, s, self.cfg.kv_high)
        inputs = {
            "outstanding": s.outstanding,
            "target_concurrency": self.cfg.target_concurrency,
            "kv_usage_max": s.kv_usage_max,
        }
        return Decision(desired, "kv_pressure" if kv else "concurrency", inputs)


class PredictivePolicy:
    """Size for the load that will exist when new capacity actually arrives.

    Two terms; the larger wins:
      capacity:  ceil(lambda / (mu * rho_target)). Little's-law sizing from the
                 arrival rate. Reacts the moment arrivals rise, before any queue builds.
      projected: ceil((outstanding + max(slope, 0) * t_cold) / target_concurrency).
                 The queue extrapolated to when a replica started now would be ready.
    Falls back to the projected term alone when the arrival rate is unavailable.
    A negative slope is ignored: a shrinking queue is the stabilizer's business.
    """

    name = "predictive"

    def __init__(self, cfg: PredictivePolicyConfig) -> None:
        self.cfg = cfg

    def decide(self, s: Signals) -> Decision:
        c = self.cfg
        projected = s.outstanding + max(s.queue_slope_per_s, 0.0) * c.t_cold_s
        by_projection = math.ceil(projected / c.target_concurrency)
        by_capacity = (
            math.ceil(s.arrival_rate_rps / (c.mu_rps * c.rho_target))
            if s.arrival_rate_rps is not None
            else None
        )

        desired, reason = by_projection, "projected_queue"
        if by_capacity is not None and by_capacity > desired:
            desired, reason = by_capacity, "arrival_capacity"
        desired, kv = _kv_override(desired, s, c.kv_high)
        if kv:
            reason = "kv_pressure"

        inputs = {
            "outstanding": s.outstanding,
            "queue_slope_per_s": s.queue_slope_per_s,
            "projected_outstanding": projected,
            "arrival_rate_rps": s.arrival_rate_rps,
            "by_projection": float(by_projection),
            "by_capacity": None if by_capacity is None else float(by_capacity),
            "kv_usage_max": s.kv_usage_max,
        }
        return Decision(desired, reason, inputs)


def make_policy(cfg: UtilPolicyConfig | QueuePolicyConfig | PredictivePolicyConfig) -> Policy:
    if isinstance(cfg, UtilPolicyConfig):
        return UtilPolicy(cfg)
    if isinstance(cfg, QueuePolicyConfig):
        return QueuePolicy(cfg)
    return PredictivePolicy(cfg)