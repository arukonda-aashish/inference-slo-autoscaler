# MANIFEST

## Environment
- Local: macOS, Apple Silicon, zsh
- Python: 3.12 (managed by uv)
- Package manager: uv

## Files
| Path | Purpose |
|---|---|
| pyproject.toml | Project metadata and dependencies |
| .python-version | Pins Python 3.12 for uv |
| .gitignore | Ignores venv, caches, secrets, runtime state |
| src/isa/__init__.py | Package root (uv placeholder, replaced later) |
| docs/ARCHITECTURE.md | System design |
| docs/HANDOFF.md | Session-resume context and decision log |
| docs/MANIFEST.md | This file |
| uv.lock | Exact resolved dependency versions; pod installs with uv sync --frozen |
| pytest.ini | Test discovery and asyncio mode |
| ruff.toml | Lint rules (incl. ASYNC for blocking-call detection) |
| configs/slo.yaml | The SLO contract |
| configs/metrics_map.yaml | vLLM metric names (unverified until Phase 5) |
| src/isa/common/config.py | StrictModel, SLOConfig, MetricsMap, load_yaml |
| tests/test_config.py | Config loading and validation tests |
| src/isa/common/log.py | setup_logging, get_logger, JSON/console formatters |
| tests/test_log.py | Logging format, filtering, and safety tests |
| configs/mock_engine.yaml | Mock engine parameters (placeholders until Phase 6 calibration) |
| src/isa/mock_replica/engine.py | Continuous-batching simulator: admission, chunked prefill, KV blocks, preemption |
| tests/test_mock_engine.py | Engine timing, admission, preemption, and queueing tests |
| src/isa/mock_replica/driver.py | Async loop running the engine in real time; token delivery |
| src/isa/mock_replica/metrics.py | Prometheus metrics named per metrics_map.yaml |
| src/isa/mock_replica/server.py | OpenAI-compatible /v1/completions, /health, /metrics |
| src/isa/mock_replica/__main__.py | CLI: python -m isa.mock_replica |
| tests/test_mock_server.py | HTTP API, SSE format, metrics, and abort tests |

## Pinned dependencies
isa v0.1.0
├── pydantic v2.13.5
│   ├── annotated-types v0.8.0
│   ├── pydantic-core v2.46.5
│   │   └── typing-extensions v4.16.0
│   ├── typing-extensions v4.16.0
│   └── typing-inspection v0.4.4
│       └── typing-extensions v4.16.0
├── pyyaml v6.0.3
├── pytest v9.1.1 (group: dev)
│   ├── iniconfig v2.3.0
│   ├── packaging v26.3
│   ├── pluggy v1.6.0
│   └── pygments v2.21.0
├── pytest-asyncio v1.4.0 (group: dev)
│   ├── pytest v9.1.1 (*)
│   └── typing-extensions v4.16.0
└── ruff v0.16.9 (group: dev)