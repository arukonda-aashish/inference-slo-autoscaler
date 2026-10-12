"""JSONL decision log: one line per tick, one per lifecycle transition.

The primary evidence in the writeup. For every scaling action it records the
signals, the policy's inputs and reason, and the stabilizer's reason; for every
replica, when and why it changed phase.
"""

import json
from pathlib import Path
from typing import Any


class DecisionLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a")

    def write(self, record: dict[str, Any]) -> None:
        self._file.write(json.dumps(record, default=str) + "\n")
        self._file.flush()  # a crash mid-run keeps every line written so far

    def close(self) -> None:
        self._file.close()