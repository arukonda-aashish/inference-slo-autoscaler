from pathlib import Path

from isa.common.config import load_yaml
from isa.controller.backends.vllm import VllmBackendConfig, build_argv
from isa.controller.config import ControllerConfig

ROOT = Path(__file__).resolve().parent.parent


def test_build_argv():
    argv = build_argv(VllmBackendConfig(kind="vllm", executable="vllm"), 8002)
    assert argv[:3] == ["vllm", "serve", "Qwen/Qwen2.5-0.5B-Instruct"]
    assert argv[argv.index("--port") + 1] == "8002"
    assert argv[argv.index("--gpu-memory-utilization") + 1] == "0.28"
    assert "--no-enable-prefix-caching" in argv


def test_gpu_controller_configs_select_the_vllm_backend():
    for name in ("controller_gpu.yaml", "controller_gpu_min2.yaml"):
        assert load_yaml(ROOT / "configs" / name, ControllerConfig).backend.kind == "vllm"


def test_mock_controller_config_still_loads():
    assert load_yaml(ROOT / "configs" / "controller.yaml", ControllerConfig).backend.kind == "mock"