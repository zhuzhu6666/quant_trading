"""Backtest service — in-process via backtest_runner (was subprocess to main.py).

Phase 4.7: dropped the subprocess shim. main.py:run_backtest is preserved
(spec §1.1) for the CLI path; this service uses the parallel importable
runner in backtest_runner.py.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from backend.jobs.progress import ProgressCB


def run_backtest(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Execute a backtest sweep in-process. Progress emitted via callback.

    Returns dict matching the JobState.result schema.
    """
    from backend.services.backtest_runner import run_backtest_sweep

    progress_cb("starting", 0, f"starting backtest {params.get('symbol')} {params.get('timeframe')}")
    result = run_backtest_sweep(
        symbol=params.get("symbol", "XAUUSD+"),
        timeframe=params.get("timeframe", "M15"),
        risk_pct=params.get("risk_per_trade_pct"),
        enable_circuit=bool(params.get("enable_circuit", False)),
        progress_cb=progress_cb,
    )
    return result


def _find_latest_backtest_report():
    """Backwards-compat alias used by some tests."""
    from backend.services.backtest_runner import find_latest_backtest_report

    return find_latest_backtest_report()
