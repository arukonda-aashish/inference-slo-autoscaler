# HANDOFF

Paste this file into a new chat session to resume.

## Project
Inference SLO Autoscaler: autoscaling LLM serving on queue-derived signals
instead of GPU utilization. Full design in docs/ARCHITECTURE.md.

## Current state
- Phase: 0 (skeleton)
- Last completed step: 1 — tooling, repo, docs
- Next step: 2 — config models and dev tooling

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

## Open questions
- Is co-located scaling near-additive? (E1b)
- Actual T_cold on the pod? (E0)

## Budget
RunPod credits: ~$14. GPU hours used: 0.