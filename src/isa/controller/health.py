"""The two gates between STARTING and READY: a health probe, then one warmup request."""

import httpx


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