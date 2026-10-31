# e8_queue_min2_burst (queue)

Run `20261031-162112_e8_queue_min2_burst`, git `c5abdf1e`, valid: **True**

| Phase | Rate | Requests | OK | TTFT p50 | TTFT p95 | TTFT p99 | Within SLO |
|---|---|---|---|---|---|---|---|
| 0 | 4 rps | 234 | 234 | 28ms | 101ms | 113ms | 100% |
| 1 | 16 rps | 494 | 494 | 38ms | 173ms | 496ms | 100% |
| 2 | 4 rps | 509 | 509 | 27ms | 100ms | 113ms | 100% |

## Burst reaction

- Ready at onset: **2**; scale-ups during baseline: 0
- Detection: 8.47s; capacity ready: 39.79s (burst lasted 30s)
- Capacity arrived after the burst ended by: 9.79s

## Cost and activity

- Replica-seconds during load: **533**
- Peak outstanding: 46; scale-ups 2, scale-downs 1

![timeline](timeline.png)
