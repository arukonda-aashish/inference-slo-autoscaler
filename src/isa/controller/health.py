"""Health probe, warmup, and in-flight load reads for the replica lifecycle."""

import httpx
from prometheus_client.parser import text_string_to_metric_families

from isa.common.config import MetricsMap


async def probe(client: httpx.AsyncClient, url: str, timeout_s: float = 1.0) -> bool:
    """True only on HTTP 200. Connection refused (still loading) is just False."""
    try:
        r = await client.get(f"{url}/health", timeout=timeout_s)
    except httpx.HTTPError:
        return False
    return r.status_code == 200


async def warmup(client: httpx.AsyncClient, url: str, model: str, timeout_s: float = 60.0) -> bool:
    """One real completion before the replica is marked READY.

    On vLLM the first request triggers CUDA graph capture and compilation; a
    real user shouldn't pay for that. On the mock it's cheap, but it still
    proves the full request path works, not just /health.
    """
    try:
        r = await client.post(
            f"{url}/v1/completions",
            json={"model": model, "prompt": "the", "max_tokens": 4},
            timeout=timeout_s,
        )
    except httpx.HTTPError:
        return False
    return r.status_code == 200
async def replica_load(
    client: httpx.AsyncClient, url: str, names: MetricsMap, timeout_s: float = 1.0
) -> float | None:
    """Running + waiting, read from the replica's own /metrics.

    Authoritative for drain decisions: Prometheus can be up to a scrape interval
    behind, and draining is where "almost zero" isn't good enough. None means the
    load couldn't be read, which callers must not treat as zero.
    """
    try:
        r = await client.get(f"{url}/metrics", timeout=timeout_s)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    total, seen = 0.0, 0
    for family in text_string_to_metric_families(r.text):
        if family.name in (names.waiting, names.running):
            for sample in family.samples:
                total += sample.value
                seen += 1
    return total if seen else None