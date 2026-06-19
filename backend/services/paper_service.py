"""Paper trading service — manages singleton PaperTrader instance.

v1: subprocess to `python main.py --mode paper ...`. Phase 4 will replace
with direct in-process call (same as BacktestService pattern).
"""
import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
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
    strategy_id: str | None = None  # audit 2026-06-08: 当前跑的 strategy 名字 (供 WS 推送 + 总览卡片)


class PaperService:
    """Singleton service holding the current paper subprocess (if any)."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._config: dict | None = None
        self._started_at: str | None = None
        self._strategy_id: str | None = None  # audit 2026-06-08

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
        # B3 fix: 之前只传 use_router / use_event_filter / risk_per_trade_pct,
        # 5 个 enable_* (scheduler/calibrator/factor_monitor/alerter/retrain) 静默丢失
        for flag, key in [
            ("--use-router", "use_router"),
            ("--use-scheduler", "use_scheduler"),
            ("--use-calibrator", "use_calibrator"),
            ("--use-meta-monitor", "use_meta_monitor"),
            ("--use-factor-monitor", "use_factor_monitor"),
            ("--use-alerter", "use_alerter"),
            ("--use-retrain", "use_retrain"),
            ("--use-event-filter", "use_event_filter"),
            ("--include-shadow-factors", "include_shadow_factors"),
        ]:
            if config.get(key):
                cmd.append(flag)
        # 默认启用熔断 (安全)
        if config.get("enable_circuit", True):
            cmd.append("--enable-circuit")
        if config.get("risk_per_trade_pct") is not None:
            cmd += ["--risk-per-trade-pct", str(config["risk_per_trade_pct"])]

        # B2 fix: 之前 stdout/stderr 都 DEVNULL, paper 挂时只看到 "exited rc=1" 一行根本看不到错.
        # 写 logs/paper_<ts>_pid<PID>.log, 启一个 daemon thread drain stdout 到文件.
        log_dir = Path(__file__).resolve().parents[2] / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=Path(__file__).resolve().parents[2],
        )
        log_path = log_dir / f"paper_{datetime.now().strftime('%Y%m%d_%H%M%S')}_pid{self._proc.pid}.log"

        # 用局部引用避免 stop() 设 self._proc = None 时 drain 线程读 None.stdout
        proc = self._proc

        def _drain():
            try:
                with open(log_path, "wb") as f:
                    for line in proc.stdout:
                        f.write(line)
                        f.flush()
            except Exception:
                pass

        threading.Thread(target=_drain, daemon=True).start()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._config = config
        self._strategy_id = config.get("strategy_id", "factor_pipeline_v4")
        progress_cb("started", 100, f"paper pid={self._proc.pid}")
        return PaperStatus(
            status="running", started_at=self._started_at, pid=self._proc.pid,
            config=config, strategy_id=self._strategy_id,
        )

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
            return PaperStatus(status="stopped", strategy_id=self._strategy_id)
        rc = self._proc.poll()
        if rc is None:
            return PaperStatus(
                status="running", started_at=self._started_at, pid=self._proc.pid,
                config=self._config, strategy_id=self._strategy_id,
            )
        self._proc = None
        return PaperStatus(status="stopped", last_error=f"exited rc={rc}", strategy_id=self._strategy_id)


_paper: PaperService | None = None


def get_paper_service() -> PaperService:
    global _paper
    if _paper is None:
        _paper = PaperService()
    return _paper
