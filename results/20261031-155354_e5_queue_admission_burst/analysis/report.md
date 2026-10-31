# e5_queue_admission_burst (queue)

Run `20261031-155354_e5_queue_admission_burst`, git `c5abdf1e`, valid: **True**

| Phase | Rate | Requests | OK | TTFT p50 | TTFT p95 | TTFT p99 | Within SLO |
|---|---|---|---|---|---|---|---|
| 0 | 4 rps | 234 | 234 | 29ms | 102ms | 114ms | 100% |
| 1 | 16 rps | 494 | 494 | 36ms | 144ms | 247ms | 100% |
| 2 | 4 rps | 509 | 509 | 28ms | 100ms | 125ms | 100% |

## Burst reaction

- Ready at onset: **2**; scale-ups during baseline: 1
- Detection: 8.59s; capacity ready: 39.77s (burst lasted 30s)
- Capacity arrived after the burst ended by: 9.77s

## Cost and activity

- Replica-seconds during load: **480**
- Peak outstanding: 45; scale-ups 3, scale-downs 2

![timeline](timeline.png)
