# e3_queue_burst (queue)

Run `20261031-134657_e3_queue_burst`, git `9fe9b961`, valid: **True**

| Phase | Rate | Requests | OK | TTFT p50 | TTFT p95 | TTFT p99 | Within SLO |
|---|---|---|---|---|---|---|---|
| 0 | 4 rps | 234 | 234 | 30ms | 103ms | 114ms | 100% |
| 1 | 16 rps | 494 | 494 | 6.36s | 15.60s | 16.74s | 20% |
| 2 | 4 rps | 509 | 509 | 28ms | 14.33s | 16.53s | 92% |

## Burst reaction

- Ready at onset: **1**; scale-ups during baseline: 0
- Detection: 8.59s; capacity ready: 39.77s (burst lasted 30s)
- Capacity arrived after the burst ended by: 9.77s

## Cost and activity

- Replica-seconds during load: **490**
- Peak outstanding: 189; scale-ups 2, scale-downs 2

![timeline](timeline.png)
