"""Structured logging: one line per event, an event name plus key=value fields.

Call sites read:   log.info("replica_ready", replica_id="r1", port=8001)

Console mode (default) is for development. JSON mode (ISA_LOG_FORMAT=json) is for
experiment runs, where logs are parsed and joined against the load generator CSV
using the epoch timestamp "t". Every process calls setup_logging() once at startup.
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

# Keys the formatter owns. A caller field with one of these names is renamed to
# "_<name>" instead of silently overwriting it.
_RESERVED = frozenset({"ts", "t", "level", "component", "logger", "event", "exc"})

# httpx logs every request at INFO; at hundreds of req/s the router would drown
# its own logs. Held at WARNING regardless of the root level.
_NOISY = ("httpx", "httpcore", "uvicorn.access")

_component = "isa"


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    f = getattr(record, "fields", None)
    return f if isinstance(f, dict) else {}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "t": round(record.created, 6),
            "level": record.levelname.lower(),
            "component": _component,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for k, v in _fields(record).items():
            out[f"_{k}" if k in _RESERVED else k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        # default=str: a Path or datetime in a field is stringified, never a crash
        return json.dumps(out, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        parts = [ts, f"{record.levelname:<7}", _component, record.getMessage()]
        parts += [f"{k}={v}" for k, v in _fields(record).items()]
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class EventLogger:
    """Wrapper so events take keyword fields.

    The "/" makes the event name positional-only, so a field literally named
    "event" lands in **fields instead of raising a TypeError.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger

    def _emit(self, level: int, event: str, exc_info: bool, fields: dict[str, Any]) -> None:
        if self._log.isEnabledFor(level):
            # stacklevel=3 so lineno points at the caller, not this wrapper
            self._log.log(level, event, exc_info=exc_info, extra={"fields": fields}, stacklevel=3)

    def debug(self, event: str, /, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, False, fields)

    def info(self, event: str, /, **fields: Any) -> None:
        self._emit(logging.INFO, event, False, fields)

    def warning(self, event: str, /, **fields: Any) -> None:
        self._emit(logging.WARNING, event, False, fields)

    def error(self, event: str, /, **fields: Any) -> None:
        self._emit(logging.ERROR, event, False, fields)

    def exception(self, event: str, /, **fields: Any) -> None:
        """Log at ERROR with the current traceback. Call only inside an except block."""
        self._emit(logging.ERROR, event, True, fields)


def setup_logging(component: str, level: str | None = None, fmt: str | None = None) -> None:
    """Configure the root logger. Idempotent: calling twice doesn't duplicate output.

    Arguments override the ISA_LOG_LEVEL / ISA_LOG_FORMAT environment variables.
    """
    global _component
    _component = component
    level = (level or os.environ.get("ISA_LOG_LEVEL", "INFO")).upper()
    fmt = (fmt or os.environ.get("ISA_LOG_FORMAT", "console")).lower()
    if fmt not in ("json", "console"):
        raise ValueError(f"ISA_LOG_FORMAT must be 'json' or 'console', got {fmt!r}")

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for name in _NOISY:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))


def get_logger(name: str) -> EventLogger:
    return EventLogger(logging.getLogger(name))


if __name__ == "__main__":
    setup_logging("demo")
    demo = get_logger("isa.demo")
    demo.info("replica_ready", replica_id="r1", port=8001)
    demo.warning("queue_high", waiting=42, replicas=2)