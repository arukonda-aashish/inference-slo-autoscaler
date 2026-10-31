# e5_queue_admission_burst (queue)

Run `20261031-161422_e5_queue_admission_burst`, git `c5abdf1e`, valid: **True**

| Phase | Rate | Requests | OK | TTFT p50 | TTFT p95 | TTFT p99 | Within SLO |
|---|---|---|---|---|---|---|---|
| 0 | 4 rps | 234 | 234 | 31ms | 105ms | 118ms | 100% |
| 1 | 16 rps | 494 | 494 | 48ms | 200ms | 407ms | 100% |
| 2 | 4 rps | 509 | 509 | 28ms | 102ms | 133ms | 100% |

## Burst reaction

- Ready at onset: **2**; scale-ups during baseline: 1
- Detection: 8.45s; capacity ready: 39.71s (burst lasted 30s)
- Capacity arrived after the burst ended by: 9.71s

## Cost and activity

- Replica-seconds during load: **480**
- Peak outstanding: 47; scale-ups 3, scale-downs 2

![timeline](timeline.png)
