"""CSV recording, and the run summary with the instrument's self-validity checks."""

import csv
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from isa.loadgen.client import RequestRecord

# If the generator couldn't dispatch 99% of requests within this of their
# scheduled time, its own timings can't be trusted.
LAG_P99_LIMIT_S = 0.050
MAX_ERROR_RATE = 0.01


def _is_error(r: RequestRecord) -> bool:
    if r.status in (-1, 429):  # cancellations are counted separately
        return False
    return r.status != 200 or bool(r.error)

class CsvRecorder:
    """Writes each record as it completes, so a crash keeps everything finished so far."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("w", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=RequestRecord.columns())
        self._writer.writeheader()
        self.records: list[RequestRecord] = []

    def write(self, record: RequestRecord) -> None:
        self._writer.writerow(asdict(record))
        self.records.append(record)

    def close(self) -> None:
        self._file.close()


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default). q in [0, 100]."""
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _ttft(r: RequestRecord) -> float | None:
    """Measured from intended arrival: the latency a real user would see."""
    if r.status != 200 or r.first_token_ts is None:
        return None
    return r.first_token_ts - r.intended_ts


@dataclass
class Summary:
    profile: str
    requests: int
    by_status: dict[str, int]
    ok: int
    errors: int
    ttft_p50_s: float | None
    ttft_p95_s: float | None
    ttft_p99_s: float | None
    by_phase: dict[str, dict]
    lag_p50_s: float | None
    lag_p99_s: float | None
    lag_max_s: float | None
    peak_inflight: int
    max_connections: int
    cancelled: int
    valid: bool
    invalid_reasons: list[str] = field(default_factory=list)


def summarize(
    records: list[RequestRecord], profile: str, peak_inflight: int, max_connections: int
) -> Summary:
    by_status = Counter(str(r.status) for r in records)
    ttfts = [t for r in records if (t := _ttft(r)) is not None]
    lags = [r.sent_ts - r.intended_ts for r in records if r.sent_ts is not None]

    by_phase: dict[str, dict] = {}
    for phase in sorted({r.phase for r in records}):
        rs = [r for r in records if r.phase == phase]
        ts = [t for r in rs if (t := _ttft(r)) is not None]
        by_phase[str(phase)] = {
            "requests": len(rs),
            "ok": sum(r.status == 200 for r in rs),
            "ttft_p50_s": percentile(ts, 50),
            "ttft_p95_s": percentile(ts, 95),
        }

    cancelled = by_status.get("-1", 0)
    ok = sum(r.status == 200 and not r.error for r in records)
    errors = sum(_is_error(r) for r in records)
    lag_p99 = percentile(lags, 99)
    reasons: list[str] = []
    if records and ok == 0:
        reasons.append("no request succeeded: check that the router and replicas are up")
    elif records and errors / len(records) > MAX_ERROR_RATE:
        reasons.append(
            f"{errors} of {len(records)} requests ({errors / len(records):.1%}) failed with "
            "errors other than 429: the system under test was broken, not just slow"
        )
    if lag_p99 is not None and lag_p99 > LAG_P99_LIMIT_S:
        reasons.append(
            f"dispatch lag p99 {lag_p99 * 1000:.1f} ms > {LAG_P99_LIMIT_S * 1000:.0f} ms: "
            "the load generator itself was saturated"
        )
    if peak_inflight >= max_connections:
        reasons.append(
            f"peak in-flight {peak_inflight} reached the client connection limit: "
            "requests queued inside the load generator"
        )
    if cancelled:
        reasons.append(
            f"{cancelled} requests still in flight at drain timeout: the latency tail is censored"
        )

    return Summary(
        profile=profile,
        requests=len(records),
        by_status=dict(by_status),
        ok=ok,
        errors=errors,
        ttft_p50_s=percentile(ttfts, 50),
        ttft_p95_s=percentile(ttfts, 95),
        ttft_p99_s=percentile(ttfts, 99),
        by_phase=by_phase,
        lag_p50_s=percentile(lags, 50),
        lag_p99_s=lag_p99,
        lag_max_s=max(lags, default=None),
        peak_inflight=peak_inflight,
        max_connections=max_connections,
        cancelled=cancelled,
        valid=not reasons,
        invalid_reasons=reasons,
    )


def write_summary(path: Path, summary: Summary) -> None:
    path.write_text(json.dumps(asdict(summary), indent=2))