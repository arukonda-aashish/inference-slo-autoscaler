"""Per-run metrics: latency per phase, the burst reaction, and cost."""

from dataclasses import dataclass
from itertools import pairwise

from isa.analysis.load import Run
from isa.loadgen.report import percentile

LIVE = ("starting", "ready", "draining")

Steps = list[tuple[float, dict[str, int]]]


def replica_steps(events: list[dict]) -> Steps:
    """Replay lifecycle events into (t, live replicas by phase) steps."""
    phase_of: dict[str, str] = {}
    steps: Steps = []
    for e in sorted(events, key=lambda e: e["t"]):
        if e["to"] in ("terminated", "failed"):
            phase_of.pop(e["replica_id"], None)
        else:
            phase_of[e["replica_id"]] = e["to"]
        counts = dict.fromkeys(LIVE, 0)
        for phase in phase_of.values():
            counts[phase] += 1
        steps.append((e["t"], counts))
    return steps


def counts_at(steps: Steps, t: float) -> dict[str, int]:
    current = dict.fromkeys(LIVE, 0)
    for ts, counts in steps:
        if ts > t:
            break
        current = counts
    return current


def replica_seconds(steps: Steps, start: float, end: float) -> float:
    """Integral of live replicas over [start, end].

    Starting and draining replicas count: a process costs GPU memory and money
    from the moment it launches, whether or not it's serving yet.
    """
    total = 0.0
    prev_t, prev_n = start, sum(counts_at(steps, start).values())
    for ts, counts in steps:
        if ts <= start:
            continue
        if ts >= end:
            break
        total += prev_n * (ts - prev_t)
        prev_t, prev_n = ts, sum(counts.values())
    return total + prev_n * (end - prev_t)


@dataclass(frozen=True)
class PhaseMetrics:
    phase: int
    rate_rps: float
    requests: int
    ok: int
    shed: int  # 429s: rejected by admission control
    ttft_p50_s: float | None
    ttft_p95_s: float | None
    ttft_p99_s: float | None
    within_slo: float | None  # share of ALL requests with TTFT <= SLO; failures count against


@dataclass(frozen=True)
class BurstMetrics:
    phase: int
    duration_s: float
    ready_at_onset: int
    scale_ups_before_onset: int  # during the load's baseline: over-reaction, or luck
    detection_s: float | None  # onset -> first scale-up decision at or after onset
    capacity_ready_s: float | None  # onset -> last replica from that decision READY
    capacity_after_burst_s: float | None  # > 0: capacity arrived after the burst ended


@dataclass(frozen=True)
class RunMetrics:
    name: str
    policy: str
    run_dir: str
    git_sha: str | None
    valid: bool
    phases: list[PhaseMetrics]
    burst: BurstMetrics | None
    replica_s_load: float
    peak_outstanding: float
    scale_ups: int
    scale_downs: int


def phase_metrics(run: Run) -> list[PhaseMetrics]:
    out = []
    for w in run.phases:
        rs = [r for r in run.requests if r.phase == w["phase"]]
        ttfts = [t for r in rs if (t := r.ttft_s) is not None]
        within = sum(t <= run.slo_ttft_s for t in ttfts) / len(rs) if rs else None
        out.append(
            PhaseMetrics(
                phase=w["phase"],
                rate_rps=w["rate_rps"],
                requests=len(rs),
                ok=sum(r.ok for r in rs),
                shed=sum(r.status == 429 for r in rs),
                ttft_p50_s=percentile(ttfts, 50),
                ttft_p95_s=percentile(ttfts, 95),
                ttft_p99_s=percentile(ttfts, 99),
                within_slo=within,
            )
        )
    return out


def find_burst(phases: list[dict]) -> dict | None:
    """The first phase whose rate is higher than the one before it."""
    for prev, cur in pairwise(phases):
        if cur["rate_rps"] > prev["rate_rps"]:
            return cur
    return None


def burst_metrics(run: Run, steps: Steps) -> BurstMetrics | None:
    w = find_burst(run.phases)
    if w is None:
        return None
    onset, duration = w["start"], w["end"] - w["start"]
    ups = [t for t in run.ticks if t["action"]["delta"] > 0]
    before = [t for t in ups if run.load_start <= t["t"] < onset]
    after = [t for t in ups if t["t"] >= onset]

    detection = capacity = None
    if after:
        decision = after[0]
        detection = decision["t"] - onset
        # Replicas spawned by that decision: lifecycle 'starting' events within a
        # second of the tick (the spawns happen inside the same tick).
        spawned = {
            e["replica_id"]
            for e in run.events
            if e["to"] == "starting" and 0 <= e["t"] - decision["t"] < 1.0
        }
        readies = [e["t"] for e in run.events if e["to"] == "ready" and e["replica_id"] in spawned]
        if readies:
            capacity = max(readies) - onset

    return BurstMetrics(
        phase=w["phase"],
        duration_s=duration,
        ready_at_onset=counts_at(steps, onset)["ready"],
        scale_ups_before_onset=len(before),
        detection_s=detection,
        capacity_ready_s=capacity,
        capacity_after_burst_s=None if capacity is None else capacity - duration,
    )


def compute(run: Run) -> RunMetrics:
    steps = replica_steps(run.events)
    outstanding = [
        t["signals"]["running_total"] + t["signals"]["waiting_total"] for t in run.ticks
    ]
    return RunMetrics(
        name=run.name,
        policy=run.policy,
        run_dir=str(run.path),
        git_sha=run.meta.get("git_sha"),
        valid=run.valid,
        phases=phase_metrics(run),
        burst=burst_metrics(run, steps),
        replica_s_load=replica_seconds(steps, run.load_start, run.load_end),
        peak_outstanding=max(outstanding, default=0.0),
        scale_ups=sum(t["action"]["delta"] > 0 for t in run.ticks),
        scale_downs=sum(t["action"]["delta"] < 0 for t in run.ticks),
    )