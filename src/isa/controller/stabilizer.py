"""Stabilizer: turns a policy's raw desired count into a safe action.

Order of checks each tick:
  1. Stale signals -> hold. Never scale on missing data, and never read
     "Prometheus is down" as "load is zero".
  2. Bounds restored immediately, cooldowns notwithstanding: below min means a
     replica failed, and that's not a decision to deliberate over.
  3. Scale up: compared against ready + starting (pending accounting), at most
     max_step_up per action, then an up-cooldown.
  4. Scale down: only if every desired value over the last down_window_s was
     below current (so a brief lull never drains a replica), one replica per
     action, and never while a replica is still starting or draining.
"""

from collections import deque
from dataclasses import dataclass

from pydantic import Field, model_validator

from isa.common.config import StrictModel
from isa.controller.signals import Signals


class StabilizerConfig(StrictModel):
    min_replicas: int = Field(ge=1)
    max_replicas: int = Field(ge=1)
    max_step_up: int = Field(default=2, ge=1)
    up_cooldown_s: float = Field(ge=0)
    down_window_s: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_bounds(self) -> "StabilizerConfig":
        if self.min_replicas > self.max_replicas:
            raise ValueError(
                f"min_replicas ({self.min_replicas}) > max_replicas ({self.max_replicas})"
            )
        return self


@dataclass(frozen=True)
class Action:
    target: int
    current: int
    desired_raw: int
    desired_bounded: int
    reason: str

    @property
    def delta(self) -> int:
        return self.target - self.current


class Stabilizer:
    def __init__(self, cfg: StabilizerConfig) -> None:
        self.cfg = cfg
        self._history: deque[tuple[float, int]] = deque()  # (ts, bounded desired)
        self._last_up_ts: float | None = None

    def apply(self, desired: int, s: Signals, now: float) -> Action:
        cfg = self.cfg
        current = s.current
        bounded = min(max(desired, cfg.min_replicas), cfg.max_replicas)

        def act(target: int, reason: str) -> Action:
            return Action(target, current, desired, bounded, reason)

        if s.stale:
            return act(current, "hold_stale_signals")  # also: not recorded in history

        if current < cfg.min_replicas:
            self._last_up_ts = now
            return act(cfg.min_replicas, "restore_min")
        if current > cfg.max_replicas:
            return act(cfg.max_replicas, "enforce_max")

        self._history.append((now, bounded))
        while self._history and self._history[0][0] < now - cfg.down_window_s:
            self._history.popleft()

        if bounded > current:
            if self._last_up_ts is not None and now - self._last_up_ts < cfg.up_cooldown_s:
                return act(current, "hold_up_cooldown")
            self._last_up_ts = now
            return act(min(bounded, current + cfg.max_step_up), "scale_up")

        if bounded < current:
            if s.starting or s.draining:
                return act(current, "hold_transition_in_progress")
            if max(d for _, d in self._history) >= current:
                return act(current, "hold_down_stabilization")
            return act(current - 1, "scale_down")

        return act(current, "steady")