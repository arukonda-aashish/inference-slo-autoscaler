"""Prometheus file_sd targets, written atomically.

Prometheus watches this file and re-reads it on change, so it must never see a
half-written version: write a temp file, then rename over the old one. A rename
within one filesystem is atomic.
"""

import json
import os
from collections.abc import Iterable
from pathlib import Path

from isa.controller.lifecycle import ManagedReplica


def render_targets(replicas: Iterable[ManagedReplica]) -> list[dict]:
    return [
        {"targets": [f"{r.host}:{r.port}"], "labels": {"replica": r.replica_id}}
        for r in replicas
    ]


def write_targets(path: Path, replicas: Iterable[ManagedReplica]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(render_targets(replicas)))
    os.replace(tmp, path)