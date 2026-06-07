"""Paper trading service — manages singleton PaperTrader instance.

v1: subprocess to `python main.py --mode paper ...`. Phase 4 will replace
with direct in-process call (same as BacktestService pattern).
"""
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.jobs.progress import ProgressCB


@dataclass
class PaperStatus:
    status: str  # "running" | "stopped" | "starting" | "stopping" | "error"
    started_at: str | None = None
    pid: int | None = None
    last_error: str | None = None
    config: dict | None = None


class PaperService:
    """Singleton service holding the current paper subprocess (if any)."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._config: dict | None = None
        self._started_at: str | None = None

    def start(self, config: dict, progress_cb: ProgressCB | None = None) -> PaperStatus:
        if self._proc is not None and self._proc.poll() is None:
            raise RuntimeError("paper already running")
        progress_cb = progress_cb or (lambda *_: None)
        progress_cb("starting", 0, f"start paper {config.get('symbol')} {config.get('timeframe')}")

        cmd = [sys.executable, "main.py", "--mode", "paper"]
        if config.get("symbol"):
            cmd += ["--symbol", config["symbol"]]
        if config.get("timeframe"):
            cmd += ["--timeframe", config["timeframe"]]
        if config.get("use_router"):
            cmd.append("--use-router")
        if config.get("use_event_filter"):
            cmd.append("--use-event-filter")
        if config.get("risk_per_trade_pct") is not None:
            cmd += ["--risk-per-trade-pct", str(config["risk_per_trade_pct"])]

        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parents[2],
        )
        from datetime import datetime, timezone
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._config = config
        progress_cb("started", 100, f"paper pid={self._proc.pid}")
        return PaperStatus(status="running", started_at=self._started_at, pid=self._proc.pid, config=config)

    def stop(self, close_positions: bool = False) -> PaperStatus:
        if self._proc is None or self._proc.poll() is not None:
            return PaperStatus(status="stopped")
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        return PaperStatus(status="stopped")

    def status(self) -> PaperStatus:
        if self._proc is None:
            return PaperStatus(status="stopped")
        rc = self._proc.poll()
        if rc is None:
            return PaperStatus(status="running", started_at=self._started_at, pid=self._proc.pid, config=self._config)
        self._proc = None
        return PaperStatus(status="stopped", last_error=f"exited rc={rc}")


_paper: PaperService | None = None


def get_paper_service() -> PaperService:
    global _paper
    if _paper is None:
        _paper = PaperService()
    return _paper
