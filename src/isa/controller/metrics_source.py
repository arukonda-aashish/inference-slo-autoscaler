"""Reads the controller's signals from Prometheus recording rules.

Staleness is decided here, conservatively. The observation is stale if
Prometheus is unreachable, if any required series is missing, or if the rule
group's latest sample is older than max_age_s. Missing data is never read as
zero load.

Why the age query: an instant query stamps values with the evaluation time, so
a value always looks fresh even if the data behind it stopped updating long
ago. `time() - timestamp(x)` asks for the sample's own age instead.
"""

import asyncio
import math
from dataclasses import dataclass

import httpx

from isa.controller.signals import Signals

# Recording-rule series from deploy/prometheus/rules.yml.
# Required: without these the controller can't reason about load at all.
REQUIRED = {
    "waiting_total": "isa:waiting:sum",
    "running_total": "isa:running:sum",
    "kv_usage_max": "isa:kv_usage:max",
    "queue_slope_per_s": "isa:waiting:slope30s",
    "gpu_util_mean": "isa:gpu_util:avg",
}
# Optional: absence is normal (no first tokens in the window; router not scraped).
OPTIONAL = {
    "ttft_p95_s": "isa:ttft:p95_30s",
    "arrival_rate_rps": "isa:arrival:rps30s",
}
# All rules in the group evaluate together, so one series' age stands for all.
AGE_QUERY = "time() - timestamp(isa:waiting:sum)"


@dataclass(frozen=True)
class Observed:
    values: dict[str, float | None]
    stale: bool
    stale_reason: str = ""
    age_s: float | None = None


def _scalar(result: list[dict]) -> float | None:
    """First sample's value; None if absent, NaN, or infinite.

    histogram_quantile returns NaN when a window has no observations, which
    means "no data", not a number to act on.
    """
    if not result:
        return None
    v = float(result[0]["value"][1])
    return None if math.isnan(v) or math.isinf(v) else v


class MetricsSource:
    def __init__(self, base_url: str, client: httpx.AsyncClient, max_age_s: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._client = client
        self._max_age_s = max_age_s

    async def _query(self, expr: str) -> list[dict]:
        r = await self._client.get(f"{self._base}/api/v1/query", params={"query": expr})
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"query {expr!r} failed: {body.get('error')}")
        return body["data"]["result"]

    async def observe(self) -> Observed:
        names = {**REQUIRED, **OPTIONAL}
        try:
            results = await asyncio.gather(
                *(self._query(expr) for expr in names.values()), self._query(AGE_QUERY)
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            return Observed({}, stale=True, stale_reason=f"prometheus_unavailable: {e!r}"[:200])

        values = {f: _scalar(r) for f, r in zip(names, results[:-1], strict=True)}
        age = _scalar(results[-1])

        missing = [f for f in REQUIRED if values[f] is None]
        if missing:
            return Observed(values, True, f"missing: {','.join(missing)}", age)
        if age is None or age > self._max_age_s:
            return Observed(values, True, f"data_too_old: age_s={age}", age)
        return Observed(values, False, "", age)


def build_signals(obs: Observed, *, ts: float, ready: int, starting: int, draining: int) -> Signals:
    """Merge Prometheus observations with the controller's own replica counts.

    Missing required values become 0.0 only when the observation is stale, and
    the stabilizer never acts on stale signals (beyond restoring bounds, which
    don't depend on them), so those zeros are never read as load.
    """
    v = obs.values

    def required(key: str) -> float:
        x = v.get(key)
        return 0.0 if x is None else x

    return Signals(
        ts=ts,
        ready=ready,
        starting=starting,
        draining=draining,
        waiting_total=required("waiting_total"),
        running_total=required("running_total"),
        kv_usage_max=required("kv_usage_max"),
        queue_slope_per_s=required("queue_slope_per_s"),
        gpu_util_mean=required("gpu_util_mean"),
        ttft_p95_s=v.get("ttft_p95_s"),
        arrival_rate_rps=v.get("arrival_rate_rps"),
        stale=obs.stale,
    )