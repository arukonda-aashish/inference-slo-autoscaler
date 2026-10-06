# HANDOFF

Paste this file into a new chat session to resume.

## Project
Inference SLO Autoscaler: autoscaling LLM serving on queue-derived signals
instead of GPU utilization. Full design in docs/ARCHITECTURE.md.

## Current state
- Last completed step: 9 — controller brain: signals, policies, stabilizer (pure)
- Next step: 10 — controller runtime: metrics source with staleness, mock
  backend, lifecycle state machine, control loop, decision log, router sync

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
- D10: Router retries only connect failures, on a different replica, before
  any bytes are sent. Never mid-stream: tokens already reached the client.
- D11: In-flight decrement lives in the streaming response's __call__ finally,
  not the body generator, so it also runs when a client disconnects before
  streaming starts. Decrement happens before any await (cancellation-safe).
- D12: Upstream read timeout is None. Long queue waits under overload are the
  measurement, not an error.
- D13: TTFT is measured from the request's intended arrival time, not its send
  time: the latency a user would experience. Dispatch lag is bounded by the
  validity check, so the two can't silently diverge.
- D14: Arrival times and request sizes use separate seeded random streams, so
  changing the size mix never moves an arrival.
- D15: The load generator self-validates: dispatch lag p99 <= 50 ms, peak
  in-flight below the connection limit, no requests cancelled at drain, at
  least one success, and non-429 error rate <= 1%. Invalid runs exit with
  code 2. A preflight request aborts dead targets before the clock starts
  (exit 3). 429s are excluded from the error rate: shedding is a measurement.
  Origin: a burst run against stopped replicas returned 726/726 502s and was
  reported valid.
- D16: No Docker anywhere, local included. Prometheus and Grafana are Homebrew
  binaries on the Mac, so prometheus.yml is byte-identical on Mac and pod
  (all targets on 127.0.0.1). Supersedes the Compose part of D2.
- D17: Router counts offered load (router_arrivals_total) on receipt, separate
  from responses (router_requests_total), so lambda is measurable under overload.
- D18: tests/test_observability.py enforces metric-name consistency across
  metrics_map.yaml, rules.yml, and the dashboard JSON.
- D19: UtilPolicy is the HPA formula implemented faithfully, incl. the 10%
  tolerance band, so its failure is the signal's fault, not a strawman's.
- D20: QueuePolicy scales on outstanding requests (running + waiting) against
  a per-replica target concurrency (Knative-style), not on waiting alone:
  waiting == 0 doesn't mean spare capacity, and a waiting-only policy would
  scale a saturated pool down and oscillate.
- D21: One stabilizer config (configs/stabilizer.yaml) is shared by every
  policy, so comparisons aren't confounded by differences in damping.
- D22: Scale-down is one replica per action, only when every desired value in
  the window is below current, and never while a replica is starting or
  draining. Every stabilizer outcome has a named reason.
  

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
- Profile rates must be set relative to measured capacity. burst_short is now
  set from the mock's measured mu; re-derive from real E1 before GPU runs.
- E1 steps must be long (minutes, not 30s): at 12 rps the queue was growing
  but p95 still read 1.9s, under the SLO, because the step ended first. Short
  steps also make p95 noisy (~200 samples decide it by the 10th-largest value).
- E2 should run UtilPolicy at two targets (80 and 99): at 80 it should scale
  to max under light load (over-provisioning); at 99 it should never scale
  (SLO violations). Showing both demonstrates no threshold works.
- target_concurrency (12) and mu_rps (9.4) are mock values; re-derive from E1.

## Measurements
- Mock replica, placeholder config (sweep_preview, 30s steps, direct to r1):
  TTFT p95 0.10s @4, 0.16s @6, 0.13s @8, 0.83s @10, 1.9s @12, 10.2s @14 rps.
  Knee between 8 and 10 rps. Service rate mu ~9.4 rps/replica, from in-flight
  growth of ~4.6/s during the 14 rps step (growth = arrival - service).
## Budget
RunPod credits: ~$14. GPU hours used: 0.