import builtins
import threading
from types import SimpleNamespace

import pytest

from backend.services import live_service
from config import runtime_config


class _LogHandle:
    def __init__(self):
        self.closed = False
        self.lines = []

    def write(self, value):
        self.lines.append(value)

    def flush(self):
        return None

    def close(self):
        self.closed = True


def _install_runtime(monkeypatch, handle):
    monkeypatch.setattr(
        runtime_config,
        "shared",
        lambda: SimpleNamespace(timeframe="M5"),
    )
    monkeypatch.setattr(
        builtins,
        "open",
        lambda *_args, **_kwargs: handle,
    )


def test_loop_log_handle_closes_after_normal_generation_exit(monkeypatch):
    handle = _LogHandle()
    _install_runtime(monkeypatch, handle)
    monkeypatch.setattr(
        live_service,
        "_run_loop_body_active",
        lambda *_args, **_kwargs: None,
    )

    live_service._run_loop_body(
        "ctrader",
        threading.Event(),
        generation_id="generation-1",
    )

    assert handle.closed is True


def test_loop_log_handle_closes_when_generation_body_raises(monkeypatch):
    handle = _LogHandle()
    _install_runtime(monkeypatch, handle)

    def fail(*_args, **_kwargs):
        raise RuntimeError("loop failed")

    monkeypatch.setattr(live_service, "_run_loop_body_active", fail)

    with pytest.raises(RuntimeError, match="loop failed"):
        live_service._run_loop_body(
            "ctrader",
            threading.Event(),
            generation_id="generation-2",
        )

    assert handle.closed is True
