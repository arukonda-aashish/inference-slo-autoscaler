"""One chart per run: what users got, what the controller did, and the load, on one time axis."""

import matplotlib

matplotlib.use("Agg")  # file output only, no display

import matplotlib.pyplot as plt  # noqa: E402

from isa.analysis.load import Run  # noqa: E402
from isa.analysis.metrics import RunMetrics, Steps  # noqa: E402


def plot_timeline(run: Run, metrics: RunMetrics, steps: Steps, out) -> None:
    t0 = run.load_start
    fig, (ax_ttft, ax_rep, ax_load) = plt.subplots(
        3, 1, sharex=True, figsize=(11, 8.5), height_ratios=[3, 2, 2]
    )

    burst = metrics.burst
    for ax in (ax_ttft, ax_rep, ax_load):
        for w in run.phases:
            if burst is not None and w["phase"] == burst.phase:
                ax.axvspan(w["start"] - t0, w["end"] - t0, color="tab:red", alpha=0.08)
        ax.grid(alpha=0.3)

    # 1. What users got: TTFT per request, at its arrival time.
    pts = [(r.intended_ts - t0, r.ttft_s) for r in run.requests if r.ttft_s is not None]
    if pts:
        xs, ys = zip(*pts, strict=True)
        ax_ttft.scatter(xs, ys, s=4, alpha=0.5, label="TTFT per request")
    ax_ttft.axhline(run.slo_ttft_s, color="tab:red", ls="--", lw=1, label=f"SLO {run.slo_ttft_s:g}s")
    ax_ttft.set_yscale("log")
    ax_ttft.set_ylabel("TTFT (s)")
    ax_ttft.legend(loc="upper right")

    # 2. What the controller did: live replicas, and what it asked for.
    if steps:
        sx = [t - t0 for t, _ in steps]
        ax_rep.step(sx, [c["ready"] for _, c in steps], where="post", label="ready")
        ax_rep.step(sx, [sum(c.values()) for _, c in steps], where="post", ls="--", label="live")
    tx = [t["t"] - t0 for t in run.ticks]
    ax_rep.plot(tx, [t["decision"]["desired"] for t in run.ticks], alpha=0.4, label="desired (policy)")
    ax_rep.step(tx, [t["action"]["target"] for t in run.ticks], where="post", label="target")
    for t in run.ticks:
        delta = t["action"]["delta"]
        if delta:
            color = "tab:green" if delta > 0 else "tab:orange"
            ax_rep.axvline(t["t"] - t0, color=color, lw=0.8, alpha=0.7)
    ax_rep.set_ylabel("replicas")
    ax_rep.legend(loc="upper right")

    # 3. The load: outstanding requests (controller's view) and arrival rate (client's).
    outstanding = [t["signals"]["running_total"] + t["signals"]["waiting_total"] for t in run.ticks]
    ax_load.plot(tx, outstanding, label="outstanding (running + waiting)")
    ax_load.set_ylabel("requests")
    ax_rate = ax_load.twinx()
    bin_s = 5.0
    bins: dict[int, int] = {}
    for r in run.requests:
        b = int((r.intended_ts - t0) // bin_s)
        bins[b] = bins.get(b, 0) + 1
    if bins:
        keys = sorted(bins)
        ax_rate.step([k * bin_s for k in keys], [bins[k] / bin_s for k in keys],
                     where="post", color="tab:gray", alpha=0.6, label="arrivals (rps)")
    ax_rate.set_ylabel("arrivals / s")
    ax_load.legend(loc="upper left")
    ax_rate.legend(loc="upper right")

    ax_load.set_xlabel("seconds since load start (red band: burst)")
    ax_load.set_xlim(-10, (run.run.get("ended_at", run.load_end) - t0))
    fig.suptitle(f"{run.name} ({run.policy}) — {run.path.name}")
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)