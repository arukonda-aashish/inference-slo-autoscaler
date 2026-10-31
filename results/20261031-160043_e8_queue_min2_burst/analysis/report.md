# e8_queue_min2_burst (queue)

Run `20261031-160043_e8_queue_min2_burst`, git `c5abdf1e`, valid: **True**

| Phase | Rate | Requests | OK | TTFT p50 | TTFT p95 | TTFT p99 | Within SLO |
|---|---|---|---|---|---|---|---|
| 0 | 4 rps | 234 | 234 | 27ms | 96ms | 105ms | 100% |
| 1 | 16 rps | 494 | 494 | 36ms | 145ms | 421ms | 100% |
| 2 | 4 rps | 509 | 509 | 27ms | 97ms | 113ms | 100% |

## Burst reaction

- Ready at onset: **2**; scale-ups during baseline: 0
- Detection: 3.54s; capacity ready: 34.70s (burst lasted 30s)
- Capacity arrived after the burst ended by: 4.70s

## Cost and activity

- Replica-seconds during load: **537**
- Peak outstanding: 43; scale-ups 2, scale-downs 1

![timeline](timeline.png)
