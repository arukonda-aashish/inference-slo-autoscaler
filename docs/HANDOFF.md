# HANDOFF

Paste this file into a new chat session to resume.

## Project
Inference SLO Autoscaler: autoscaling LLM serving on queue-derived signals
instead of GPU utilization. Full design in docs/ARCHITECTURE.md.

## Current state
- Last completed step: 5 — mock replica HTTP server
- Next step: 6 — router: registry, least-outstanding balancing, streaming proxy

## Decisions
- D1: Custom controller with pluggable backends, not KEDA. RunPod pods are
  unprivileged containers; Docker/k3s can't run inside them. KEDA is a
  stretch goal against the local mock.
- D2: No Docker in the run path. Every service is a Python process;
  Prometheus is a standalone binary. Compose is for local dev only.
- D3: Default model Qwen2.5-0.5B-Instruct. Revisit 1.5B after E1 if GPU
  util tracks load (model too small to saturate the card).
- D4: Three co-located replicas on one 3090 at --gpu-memory-utilization
  0.28. Unvalidated; E1b tests scaling efficiency. Fallback: 2×3090 pod for
  the live comparison only.
- D5: Open-loop load generator with scheduling-lag self-check, to avoid
  coordinated omission.
- D6: Metric names come from configs/metrics_map.yaml, never hardcoded;
  vLLM renames metrics across versions.
- D7: Stdlib logging with a thin EventLogger wrapper, not structlog. No extra
  dependency; uvicorn and httpx already use stdlib logging. JSON lines carry
  an epoch "t" for joining logs against the load generator CSV.
- D8: Mock cold start = port not bound (connection refused), not /health 503.
  Real vLLM binds its HTTP port only after model load.
- D9: Mock counts prompt tokens as whitespace words. The load generator builds
  prompts from single-token words so counts match vLLM's tokenizer.

## Open questions
- Is co-located scaling near-additive? (E1b)
- Actual T_cold on the pod? (E0)
- Does KV memory bind on the real server? At 0.5B (~12 KB KV/token) and
  gpu-memory-utilization 0.28, likely not: sequence slots and step time will.
  If E1 shows no KV pressure, constrain it with --num-gpu-blocks-override.
  The mock config is deliberately KV-tight so the mechanism is tested either way.
- Verify in Phase 5: prompt word count == vLLM usage.prompt_tokens for loadgen prompts.
- Verify in Phase 5: does vLLM observe TPOT per token or per request? The mock
  observes each request's mean once; match vLLM's semantics before calibrating.

## Budget
RunPod credits: ~$14. GPU hours used: 0.