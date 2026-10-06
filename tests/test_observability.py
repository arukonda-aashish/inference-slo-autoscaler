import json
import re
from pathlib import Path

import yaml

from isa.common.config import MetricsMap, load_yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
NAMES = load_yaml(ROOT / "configs" / "metrics_map.yaml", MetricsMap)


def load(path: Path):
    return yaml.safe_load(path.read_text())


def rules() -> list[dict]:
    doc = load(DEPLOY / "prometheus" / "rules.yml")
    return [r for g in doc["groups"] for r in g["rules"]]


def test_prometheus_config_wires_rules_and_replica_discovery():
    cfg = load(DEPLOY / "prometheus" / "prometheus.yml")
    assert cfg["global"]["scrape_interval"] == "2s"
    assert "rules.yml" in cfg["rule_files"]
    jobs = {j["job_name"]: j for j in cfg["scrape_configs"]}
    assert {"router", "replicas"} <= set(jobs)
    assert jobs["replicas"]["file_sd_configs"][0]["files"] == ["targets/replicas.json"]


def test_rules_use_mapped_metric_names():
    exprs = " ".join(r["expr"] for r in rules())
    for name in (NAMES.waiting, NAMES.running, NAMES.kv_usage, NAMES.ttft_bucket, NAMES.tpot_bucket):
        assert name in exprs, f"{name} from metrics_map.yaml is not used by any rule"


def test_dashboard_only_references_recorded_series():
    recorded = {r["record"] for r in rules()}
    dash = json.loads((DEPLOY / "grafana" / "dashboards" / "isa-overview.json").read_text())
    exprs = [t["expr"] for p in dash["panels"] for t in p.get("targets", [])]
    assert exprs
    used = {tok for e in exprs for tok in re.findall(r"isa:[a-z0-9_:]+", e)}
    assert used <= recorded, f"dashboard uses unrecorded series: {used - recorded}"