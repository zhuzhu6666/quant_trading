"""test_structured_log — JsonFormatter 行为 + run_id 注入。"""
from __future__ import annotations

import io
import json
import logging

import pytest

from monitor.structured_log import JsonFormatter, current_run_id, reset_run_id


@pytest.fixture(autouse=True)
def _reset_run_id():
    reset_run_id("test_run_12345")
    yield
    reset_run_id("cleanup_default")


def _make_record(name: str = "test.logger", level: int = logging.INFO, msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_json_formatter_emits_valid_json() -> None:
    fmt = JsonFormatter(lambda: "rid_abc")
    rec = _make_record(msg="payload")
    out = fmt.format(rec)
    data = json.loads(out)
    assert data["level"] == "INFO"
    assert data["logger"] == "test.logger"
    assert data["msg"] == "payload"
    assert data["run_id"] == "rid_abc"
    assert "ts" in data


def test_json_formatter_includes_extra_fields() -> None:
    fmt = JsonFormatter()
    rec = _make_record()
    rec.factor = "rsi_14"
    rec.score = 73.5
    out = fmt.format(rec)
    data = json.loads(out)
    assert data["factor"] == "rsi_14"
    assert data["score"] == 73.5


def test_run_id_reset() -> None:
    rid1 = current_run_id()
    new = reset_run_id("explicit_id")
    assert new == "explicit_id"
    assert current_run_id() == "explicit_id"
    # 还原
    reset_run_id(rid1)


def test_json_formatter_handles_exc_info() -> None:
    fmt = JsonFormatter()
    try:
        raise ValueError("intentional")
    except ValueError:
        import sys

        rec = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="crash",
            args=(),
            exc_info=sys.exc_info(),
        )
        data = json.loads(fmt.format(rec))
        assert "exc" in data
        assert "ValueError" in data["exc"]
