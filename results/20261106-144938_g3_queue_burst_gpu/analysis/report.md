# g3_queue_burst_gpu (queue_gpu)

Run `20261106-144938_g3_queue_burst_gpu`, git `?`, valid: **True**

| Phase | Rate | Requests | OK | TTFT p50 | TTFT p95 | TTFT p99 | Within SLO |
|---|---|---|---|---|---|---|---|
| 0 | 8 rps | 471 | 471 | 43ms | 114ms | 157ms | 100% |
| 1 | 34 rps | 1042 | 1042 | 5.48s | 13.42s | 14.09s | 34% |
| 2 | 8 rps | 968 | 968 | 65ms | 10.31s | 13.23s | 82% |

## Burst reaction

- Ready at onset: **1**; scale-ups during baseline: 0
- Detection: 2.29s; capacity ready: — (burst lasted 30s)
- Capacity arrived after the burst ended by: —

## Cost and activity

- Replica-seconds during load: **318**
- Peak outstanding: 543; scale-ups 3, scale-downs 0

![timeline](timeline.png)
