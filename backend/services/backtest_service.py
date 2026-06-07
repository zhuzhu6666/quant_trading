"""Backtest service — wraps main.run_backtest for backend invocation.

v1: re-uses main.py's run_backtest args namespace via lightweight shim,
so we don't have to refactor main.py in Phase 1. Phase 4 will refactor
main.py into a 'service function + CLI main' two-mode form; this shim
will be replaced.
"""
import asyncio
import json
import subprocess  # module-level so tests can patch backend.services.backtest_service.subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from backend.core.paths import CHARTS_DIR
from backend.jobs.progress import ProgressCB


@dataclass
class BacktestParams:
    symbol: str = "XAUUSD+"
    timeframe: str = "M15"
    risk_per_trade_pct: float | None = None
    enable_circuit: bool = False


def run_backtest(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Execute a backtest synchronously; progress emitted via callback.

    For Phase 1 this calls the existing CLI logic in a subprocess (so we
    don't refactor main.py in Phase 1). Phase 4 will replace this with
    direct in-process call.
    """
    import subprocess
    import sys

    progress_cb("starting", 0, f"starting backtest {params.get('symbol')} {params.get('timeframe')}")

    # Build CLI command
    cmd = [
        sys.executable, "main.py",
        "--mode", "backtest",
        "--symbol", params.get("symbol", "XAUUSD+"),
        "--timeframe", params.get("timeframe", "M15"),
    ]
    if params.get("risk_per_trade_pct") is not None:
        cmd += ["--risk-per-trade-pct", str(params["risk_per_trade_pct"])]
    if params.get("enable_circuit"):
        cmd += ["--enable-circuit"]

    progress_cb("running", 10, " ".join(cmd))

    # Run subprocess
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    progress_cb("running", 80, f"backtest exited rc={proc.returncode}")

    if proc.returncode != 0:
        raise RuntimeError(f"backtest failed: {proc.stderr[-500:]}")

    # Try to parse latest backtest report from data/charts/
    report = _find_latest_backtest_report()
    progress_cb("done", 100, f"report: {report}")
    return {
        "returncode": proc.returncode,
        "report_path": str(report) if report else None,
        "stdout_tail": proc.stdout[-1000:],
    }


def _find_latest_backtest_report() -> Path | None:
    """Find most recent backtest_*.txt in data/charts/."""
    if not CHARTS_DIR.exists():
        return None
    candidates = sorted(CHARTS_DIR.glob("backtest_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None
