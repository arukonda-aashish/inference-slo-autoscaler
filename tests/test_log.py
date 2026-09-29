import json
import logging
from pathlib import Path

import pytest

from isa.common.log import get_logger, setup_logging


@pytest.fixture(autouse=True)
def _reset_root():
    yield
    logging.getLogger().handlers.clear()


def _json_lines(err: str) -> list[dict]:
    return [json.loads(line) for line in err.strip().splitlines()]


def test_json_event_and_fields(capsys):
    setup_logging("router", level="INFO", fmt="json")
    get_logger("isa.test").info("replica_ready", replica_id="r1", port=8001)
    (rec,) = _json_lines(capsys.readouterr().err)
    assert rec["event"] == "replica_ready"
    assert rec["component"] == "router"
    assert rec["level"] == "info"
    assert rec["replica_id"] == "r1"
    assert rec["port"] == 8001
    assert isinstance(rec["t"], float)


def test_reserved_field_is_renamed(capsys):
    setup_logging("router", level="INFO", fmt="json")
    get_logger("isa.test").info("x", level="bogus", event="bogus")
    (rec,) = _json_lines(capsys.readouterr().err)
    assert rec["level"] == "info"
    assert rec["event"] == "x"
    assert rec["_level"] == "bogus"
    assert rec["_event"] == "bogus"


def test_exception_captured(capsys):
    setup_logging("controller", level="INFO", fmt="json")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        get_logger("isa.test").exception("tick_failed", tick=3)
    (rec,) = _json_lines(capsys.readouterr().err)
    assert rec["level"] == "error"
    assert rec["tick"] == 3
    assert "RuntimeError: boom" in rec["exc"]


def test_level_filtering(capsys):
    setup_logging("router", level="WARNING", fmt="json")
    log = get_logger("isa.test")
    log.info("hidden")
    log.warning("shown")
    assert [r["event"] for r in _json_lines(capsys.readouterr().err)] == ["shown"]


def test_non_serializable_field(capsys):
    setup_logging("router", level="INFO", fmt="json")
    get_logger("isa.test").info("cfg_loaded", path=Path("/tmp/x.yaml"))
    (rec,) = _json_lines(capsys.readouterr().err)
    assert rec["path"] == "/tmp/x.yaml"


def test_console_format(capsys):
    setup_logging("router", level="INFO", fmt="console")
    get_logger("isa.test").info("replica_ready", replica_id="r1")
    err = capsys.readouterr().err
    assert "router" in err
    assert "replica_ready" in err
    assert "replica_id=r1" in err


def test_setup_is_idempotent(capsys):
    setup_logging("router", level="INFO", fmt="json")
    setup_logging("router", level="INFO", fmt="json")
    get_logger("isa.test").info("once")
    assert len(_json_lines(capsys.readouterr().err)) == 1


def test_bad_format_rejected():
    with pytest.raises(ValueError, match="ISA_LOG_FORMAT"):
        setup_logging("router", fmt="xml")


def test_httpx_quieted():
    setup_logging("router", level="DEBUG", fmt="json")
    assert logging.getLogger("httpx").level == logging.WARNING