import csv
import json
from pathlib import Path

import pytest
import yaml

from isa.analysis.load import load_run
from isa.analysis.metrics import compute, replica_seconds, replica_steps
from isa.analysis.report import compare, write_run_report
from isa.loadgen.client import RequestRecord

PHASES = [
    {"phase": 0, "rate_rps": 4, "start": 1000.0, "end": 1060.0},
    {"phase": 1, "rate_rps": 16, "start": 1060.0, "end": 1090.0},
    {"phase": 2, "rate_rps": 4, "start": 1090.0, "end": 1210.0},
]


def ev(t, rid, frm, to, reason="x"):
    return {"type": "lifecycle", "t": t, "replica_id": rid, "from": frm, "to": to, "reason": reason}


def tick(t, delta, target, desired, running=0.0, waiting=0.0):
    return {
        "type": "tick",
        "t": t,
        "signals": {"running_total": running, "waiting_total": waiting},
        "decision": {"desired": desired},
        "action": {"delta": delta, "target": target, "reason": "r"},
    }


def make_run(root: Path, name: str, burst_ttfts=(0.1, 0.2, 3.0, 5.0), valid=True) -> Path:
    d = root / f"run_{name}_{len(list(root.iterdir()))}"
    (d / "config").mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({
        "valid": valid, "load_started_at": 1000.0, "phases": PHASES, "ended_at": 1360.0,
    }))
    (d / "meta.json").write_text(json.dumps({"git_sha": "abc1234def", "git_dirty": False}))
    (d / "config" / "experiment.yaml").write_text(yaml.safe_dump({
        "name": name, "policy": "configs/policies/queue.yaml", "profile": "p.yaml",
    }))
    (d / "config" / "slo.yaml").write_text(yaml.safe_dump({
        "ttft_p95_s": 2.0, "tpot_p95_s": 0.08, "min_admission_rate": 0.95,
    }))

    rows = []
    def add(phase, intended, ttft, status=200, error=""):
        rec = RequestRecord(req_id=f"q{len(rows)}", phase=phase, intended_ts=intended,
                            prompt_tokens=10, max_tokens=5, sent_ts=intended,
                            first_token_ts=None if ttft is None else intended + ttft,
                            status=status, error=error)
        rows.append({k: ("" if v is None else v) for k, v in rec.__dict__.items()})
    for i in range(4):
        add(0, 1000 + i * 10, 0.03)
    for i, t in enumerate(burst_ttfts):
        add(1, 1060 + i * 5, t)
    for i, t in enumerate((0.05, 0.05, 2.5)):
        add(2, 1095 + i * 10, t)
    add(2, 1150, None, status=502, error="upstream failed")
    with (d / "requests.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RequestRecord.columns())
        w.writeheader()
        w.writerows(rows)

    records = [
        ev(950, "r1", None, "starting"), ev(981, "r1", "starting", "ready"),
        tick(1010, 0, 1, 1, running=4),
        tick(1065, 2, 3, 3, running=10, waiting=5),
        ev(1065.01, "r2", None, "starting"), ev(1065.02, "r3", None, "starting"),
        ev(1096.0, "r2", "starting", "ready"), ev(1096.5, "r3", "starting", "ready"),
        tick(1200, -1, 2, 1), ev(1200, "r2", "ready", "draining"),
        ev(1202, "r2", "draining", "terminated"),
        tick(1230, -1, 1, 1), ev(1230, "r3", "ready", "draining"),
        ev(1232, "r3", "draining", "terminated"),
    ]
    (d / "decisions.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return d


def test_replica_seconds_counts_every_live_replica(tmp_path):
    run = load_run(make_run(tmp_path, "a"))
    steps = replica_steps(run.events)
    # Load window [1000, 1210]: r1 the whole 210s; r2 from 1065.01 to 1202;
    # r3 from 1065.02 to the window end (it drains after 1210).
    expected = 210 + (1202 - 1065.01) + (1210 - 1065.02)
    assert replica_seconds(steps, 1000, 1210) == pytest.approx(expected)


def test_phase_metrics_count_failures_against_the_slo(tmp_path):
    m = compute(load_run(make_run(tmp_path, "a")))
    burst, recovery = m.phases[1], m.phases[2]
    assert burst.ttft_p95_s == pytest.approx(4.7)  # [0.1, 0.2, 3, 5]: 3 + 2 * 0.85
    assert burst.within_slo == pytest.approx(0.5)  # 2 of 4 under 2s
    assert (recovery.requests, recovery.ok) == (4, 3)
    assert recovery.within_slo == pytest.approx(0.5)  # 2 of 4: the 502 counts as a miss


def test_burst_reaction_decomposition(tmp_path):
    m = compute(load_run(make_run(tmp_path, "a")))
    b = m.burst
    assert b.phase == 1
    assert b.ready_at_onset == 1
    assert b.scale_ups_before_onset == 0
    assert b.detection_s == pytest.approx(5.0)  # onset 1060 -> decision 1065
    assert b.capacity_ready_s == pytest.approx(36.5)  # -> r3 ready at 1096.5
    assert b.capacity_after_burst_s == pytest.approx(6.5)  # 30s burst
    assert m.peak_outstanding == 15
    assert (m.scale_ups, m.scale_downs) == (1, 2)


def test_report_writes_all_three_files(tmp_path):
    d = make_run(tmp_path, "a")
    write_run_report(d)
    for name in ("metrics.json", "report.md", "timeline.png"):
        assert (d / "analysis" / name).stat().st_size > 0, name
    assert "Ready at onset: **1**" in (d / "analysis" / "report.md").read_text()


def test_compare_groups_repeats_and_excludes_invalid(tmp_path):
    runs = [
        make_run(tmp_path, "exp_a", burst_ttfts=(0.1, 0.2, 3.0, 5.0)),
        make_run(tmp_path, "exp_a", burst_ttfts=(0.1, 0.1, 0.1, 0.1)),
        make_run(tmp_path, "exp_a", valid=False),
    ]
    table = compare([compute(load_run(d)) for d in runs])
    row = next(line for line in table.splitlines() if line.startswith("| exp_a"))
    assert "| 2 |" in row  # two valid runs grouped, the invalid one left out
    assert "Excluded (invalid)" in table