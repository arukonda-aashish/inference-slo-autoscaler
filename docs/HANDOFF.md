# HANDOFF

Paste this file into a new chat session to resume.

## Project
Inference SLO Autoscaler: autoscaling LLM serving on queue-derived signals
instead of GPU utilization. Full design in docs/ARCHITECTURE.md.

## Current state
- Last completed step: 10b — controller runtime: loops, drain, decision log, API, router sync
- Next step: 11 — experiment runner (one command per run) and decision-log analysis

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
- D23: Stabilizer checks bounds before staleness. Found while designing
  staleness handling: when every replica dies, metrics vanish, signals go
  stale, and "stale -> hold" would deadlock at zero replicas.
- D24: Staleness = Prometheus unreachable, any required series missing, or the
  rule group's sample age > max_age_s. Instant-query timestamps are evaluation
  times and always look fresh; time() - timestamp(x) gives true sample age.
- D25: Replica ids are never reused (r1, r2, ...); ports are.
- D26: Replicas run in their own process group (start_new_session), so a
  Ctrl-C to the controller doesn't kill them mid-generation. The controller
  owns their shutdown and drains first. Orphan cleanup after a controller
  crash (discover()) is deferred.
- D27: Controller runs two loops (tick 5s, supervise 1s) sharing the pool under
  one asyncio lock. Warmup and terminate run as background tasks outside it.
- D28: Drain completion is read from the replica's own /metrics (running +
  waiting == 0) after a 2s router grace, not from Prometheus.
- D29: A failed replica leaves the pool immediately, but its port is reserved
  until the process exits, so a replacement can't collide on bind.
- D30: Router takes replicas from exactly one source (static list or
  controller). If the controller is unreachable, it keeps the last known set
  (fail static), logging once per outage.
- D31: Controller shutdown terminates every replica it started and clears the
  targets file. Orphans exist only if the controller itself crashes (D26).
  

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
- Queues are sticky: requests already queued on r1 stayed there after r2/r3
  became ready, so the backlog drained at r1's rate (~10/s) while the new
  replicas only took new arrivals. Late binding (router-side queue; dispatch
  only when a replica has room) would let late capacity share the backlog.
  Candidate E7, built on the router's K_max admission mechanism.
- For bursts shorter than T_cold, no reactive policy (predictive included) can
  protect burst-phase TTFT. Compare against the real levers: late binding,
  admission control, and headroom (min_replicas: 2).

## Measurements
- Mock replica, placeholder config (sweep_preview, 30s steps, direct to r1):
  TTFT p95 0.10s @4, 0.16s @6, 0.13s @8, 0.83s @10, 1.9s @12, 10.2s @14 rps.
  Knee between 8 and 10 rps. Service rate mu ~9.4 rps/replica, from in-flight
  growth of ~4.6/s during the 14 rps step (growth = arrival - service).
- First managed run (queue policy, burst_short, mock, 1 -> 3 replicas):
  detection 6.4s + cold start 31.3s = 37.7s reaction vs a 30.6s burst; new
  capacity arrived after the burst ended. TTFT p95 0.10s / 15.5s / 14.2s
  (baseline / burst / recovery). Recovery p50 28ms: bimodal, because ~5.5% of
  recovery arrivals landed on r1 before r2/r3 were ready. Peak in flight 191.
  GPU util read ~97% from the 4 rps baseline on. r1's KV pinned at ~98%.
  Scale-down: 90s window held, one replica at a time, each drain 2.5s.
## Budget
RunPod credits: ~$14. GPU hours used: 0.