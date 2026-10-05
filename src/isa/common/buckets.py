"""Histogram buckets shared by every component that measures latency, so their
histograms are directly comparable. Fine-grained at the low end: if every
observation lands in one bucket, percentiles are fiction."""

TTFT_BUCKETS = (
    0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.15, 0.25, 0.5, 0.75,
    1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0, 60.0,
)
TPOT_BUCKETS = (
    0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.075,
    0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0,
)