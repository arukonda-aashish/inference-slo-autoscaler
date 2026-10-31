"""Per-run report files, and the cross-run comparison table."""

import json
import statistics
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from isa.analysis.load import load_run
from isa.analysis.metrics import RunMetrics, compute, replica_steps
from isa.analysis.timeline import plot_timeline


def _s(x: float | None) -> str:
    return "—" if x is None else f"{x:.2f}s" if x >= 1 else f"{x * 1000:.0f}ms"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def write_run_report(run_dir: Path) -> RunMetrics:
    run = load_run(run_dir)
    m = compute(run)
    out = run_dir / "analysis"
    out.mkdir(exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(asdict(m), indent=2))
    plot_timeline(run, m, replica_steps(run.events), out / "timeline.png")

    lines = [
        f"# {m.name} ({m.policy})",
        "",
        f"Run `{run_dir.name}`, git `{(m.git_sha or '?')[:8]}`, valid: **{m.valid}**",
        "",
        "| Phase | Rate | Requests | OK | TTFT p50 | TTFT p95 | TTFT p99 | Within SLO |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for p in m.phases:
        lines.append(
            f"| {p.phase} | {p.rate_rps:g} rps | {p.requests} | {p.ok} | {_s(p.ttft_p50_s)} "
            f"| {_s(p.ttft_p95_s)} | {_s(p.ttft_p99_s)} | {_pct(p.within_slo)} |"
        )
    if m.burst:
        b = m.burst
        lines += [
            "",
            "## Burst reaction",
            "",
            f"- Ready at onset: **{b.ready_at_onset}**; scale-ups during baseline: "
            f"{b.scale_ups_before_onset}",
            f"- Detection: {_s(b.detection_s)}; capacity ready: {_s(b.capacity_ready_s)} "
            f"(burst lasted {b.duration_s:g}s)",
            f"- Capacity arrived after the burst ended by: {_s(b.capacity_after_burst_s)}",
        ]
    lines += [
        "",
        "## Cost and activity",
        "",
        f"- Replica-seconds during load: **{m.replica_s_load:.0f}**",
        f"- Peak outstanding: {m.peak_outstanding:.0f}; scale-ups {m.scale_ups}, "
        f"scale-downs {m.scale_downs}",
        "",
        "![timeline](timeline.png)",
        "",
    ]
    (out / "report.md").write_text("\n".join(lines))
    return m


def _summary(values: list[float | None], fmt) -> str:
    vs = [v for v in values if v is not None]
    if not vs:
        return "—"
    if len(vs) == 1:
        return fmt(vs[0])
    return f"{fmt(statistics.median(vs))} [{fmt(min(vs))}–{fmt(max(vs))}]"

def _shed(m: RunMetrics) -> float | None:
    if m.burst is None:
        return None
    p = m.phases[m.burst.phase]
    return p.shed / p.requests if p.requests else None
def compare(metrics: list[RunMetrics]) -> str:
    valid = [m for m in metrics if m.valid]
    invalid = [m for m in metrics if not m.valid]
    groups: dict[str, list[RunMetrics]] = defaultdict(list)
    for m in valid:
        groups[m.name].append(m)

    def phase_p95(m: RunMetrics, offset: int) -> float | None:
        if m.burst is None:
            return None
        idx = m.burst.phase + offset
        return m.phases[idx].ttft_p95_s if 0 <= idx < len(m.phases) else None

    lines = [
        "Median [min–max] across valid runs of each experiment.",
        "",
        "| Experiment | Runs | Burst p95 | Recovery p95 | Burst within SLO | Burst shed "
        "| Ready at onset | Capacity after burst | Replica·s |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name in sorted(groups):
        ms = groups[name]
        lines.append(
            f"| {name} | {len(ms)} "
            f"| {_summary([phase_p95(m, 0) for m in ms], _s)} "
            f"| {_summary([phase_p95(m, 1) for m in ms], _s)} "
            f"| {_summary([m.phases[m.burst.phase].within_slo if m.burst else None for m in ms], _pct)} "
            f"| {_summary([_shed(m) for m in ms], _pct)} "
            f"| {_summary([float(m.burst.ready_at_onset) if m.burst else None for m in ms], lambda x: f'{x:g}')} "
            f"| {_summary([m.burst.capacity_after_burst_s if m.burst else None for m in ms], _s)} "
            f"| {_summary([m.replica_s_load for m in ms], lambda x: f'{x:.0f}')} |"
        )
    if invalid:
        lines += ["", "Excluded (invalid):"]
        lines += [f"- `{Path(m.run_dir).name}`" for m in invalid]
    return "\n".join(lines) + "\n"