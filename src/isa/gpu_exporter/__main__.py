"""NVML exporter, pod only: python -m isa.gpu_exporter --port 9400

Exposes isa_gpu_utilization_percent{gpu} (the baseline policy's signal, and the
thesis chart's) and isa_gpu_memory_used_bytes{gpu}, read at scrape time.
"""

import argparse
import time

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector


class NvmlCollector(Collector):
    def __init__(self, nvml) -> None:
        self._nvml = nvml
        self._count = nvml.nvmlDeviceGetCount()

    def collect(self):
        util = GaugeMetricFamily(
            "isa_gpu_utilization_percent", "GPU utilization (NVML)", labels=["gpu"]
        )
        mem = GaugeMetricFamily(
            "isa_gpu_memory_used_bytes", "GPU memory used (NVML)", labels=["gpu"]
        )
        for i in range(self._count):
            h = self._nvml.nvmlDeviceGetHandleByIndex(i)
            util.add_metric([str(i)], self._nvml.nvmlDeviceGetUtilizationRates(h).gpu)
            mem.add_metric([str(i)], self._nvml.nvmlDeviceGetMemoryInfo(h).used)
        yield util
        yield mem


def main() -> None:
    p = argparse.ArgumentParser(description="NVML exporter")
    p.add_argument("--port", type=int, default=9400)
    args = p.parse_args()

    import pynvml  # from nvidia-ml-py; imported here so the module loads on a Mac

    pynvml.nvmlInit()
    REGISTRY.register(NvmlCollector(pynvml))
    start_http_server(args.port, addr="127.0.0.1")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()