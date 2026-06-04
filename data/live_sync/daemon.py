"""
data/live_sync/daemon.py -- background sync daemon (T16.5 + auto-recovery, 2026-06-03)

Pulls new MT5 bars -> filters -> writes to db on a fixed interval.

Auto-recovery (added after the 2026-06-03 incident):
- MT5 terminal64 instance guard: refuse to start if 0 or >1 are running,
  poll until exactly 1 is up. Prevents the "auto-spawned hidden instance"
  trap that produced the 9440 + 9448 IPC collision.
- Gap detection: on startup, read the last_sync_utc from data/charts/live_sync_status.json.
  If the gap is larger than gap_threshold_hours, run a one-shot full_sync
  (5000 bars per timeframe, filter dedupes against the db) before entering
  the incremental loop. This makes the daemon self-heal after any downtime.
- Heartbeat: every heartbeat_sec seconds, ping MT5 via symbol_info_tick.
  If it returns None/0/exception, reconnect: shutdown -> check_one ->
  initialize -> ping. This eliminates the "stale handle, copy_rates
  hangs forever" failure mode that cost 8.5h of data on 2026-06-03.
- File logging: setup_logging() attaches a FileHandler to logs/live_sync.log
  with rotation-free append; previously all log lines were lost because the
  daemon ran under pythonw (no console) and never added a file handler.

Usage (CLI):
    python -m data.live_sync.daemon --mode once --type full --timeframes M15,H1,D1
    python -m data.live_sync.daemon --mode daemon --interval 60 --timeframes M15
"""
import argparse
import json
import logging
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.live_sync.orchestrator import SyncOrchestrator, SyncReport, DEFAULT_TIMEFRAMES
from data.live_sync import mt5_guard

logger = logging.getLogger(__name__)


# Where to keep the daemon log file. Created on demand.
LOG_FILE = PROJECT_ROOT / "logs" / "live_sync.log"


def setup_file_logging(level: int = logging.INFO) -> None:
    """
    Attach a FileHandler to the root logger so the daemon's log lines are
    persisted. Safe to call multiple times (checks for an existing handler
    tagged with our filename to avoid double-attach).
    """
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in root.handlers:
        if getattr(h, "_live_sync_file_marker", False):
            return
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    fh._live_sync_file_marker = True
    root.addHandler(fh)
    root.setLevel(min(root.level or logging.WARNING, level))
    logger.info(f"[SyncDaemon] file logging enabled -> {LOG_FILE}")


def _last_sync_utc_from_status() -> Optional[datetime]:
    """
    Read data/charts/live_sync_status.json and return its last_sync_utc as
    a tz-aware datetime. Returns None if the file is missing or malformed.
    """
    p = PROJECT_ROOT / "data" / "charts" / "live_sync_status.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        s = d.get("last_sync_utc")
        if not s:
            return None
        # Format is e.g. "2026-06-03T14:36:55Z"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception as e:
        logger.warning(f"[SyncDaemon] could not parse last_sync_utc: {e}")
        return None


class SyncDaemon:
    """
    Background sync daemon.

    Usage:
        daemon = SyncDaemon(symbol="XAUUSD+", timeframes=["M15", "H1", "D1"])
        daemon.run_once()                            # one-shot incremental
        daemon.run_daemon(interval_sec=60)           # loop forever
    """

    def __init__(self, symbol: str = "XAUUSD+",
                 timeframes: list[str] = None,
                 db_path: str = "data/market_data.db",
                 gap_threshold_hours: float = 6.0,
                 heartbeat_sec: int = 300,
                 max_wait_sec: int = 600):
        self.symbol = symbol
        self.timeframes = timeframes or ["M15"]
        self.db_path = db_path
        self.gap_threshold_hours = gap_threshold_hours
        self.heartbeat_sec = heartbeat_sec
        # max_wait_sec caps the initial terminal64-poll loop. Default 600s
        # (10 min) keeps the daemon self-exiting when MT5 is not running
        # instead of flashing the taskbar forever. Set to 0 to wait forever
        # (not recommended).
        self._cli_max_wait_sec = max_wait_sec
        self.orch = SyncOrchestrator(db_path=db_path)
        self._running = False
        self._first_run = True
        # Holds the live mt5 module reference (so heartbeat can talk to it).
        # Imported lazily inside run_daemon so a unit test of SyncDaemon
        # does not require MetaTrader5 to be importable.
        self._mt5 = None

    # ---------------------------------------------------------------------
    # Public entry points
    # ---------------------------------------------------------------------

    def run_once(self, full: bool = False, n_bars: int = 5000) -> SyncReport:
        """Run a single sync (full or incremental)."""
        if not self.orch.connect():
            return SyncReport(run_type="error", symbol=self.symbol, started_utc="",
                              error="MT5 connection failed")
        if full and self._first_run:
            report = self.orch.full_sync(self.symbol, self.timeframes, n_bars=n_bars)
            self._first_run = False
        else:
            report = self.orch.incremental_sync(self.symbol, self.timeframes)
        self.orch.shutdown()
        return report

    def run_daemon(self, interval_sec: int = 60, max_runs: int = 0) -> None:
        """
        Loop forever, syncing every interval_sec.

        Startup protocol:
          1. setup_file_logging() (so the rest of this method is captured)
          2. mt5_guard.check_one()  -- wait until exactly 1 terminal64 exists
          3. acquire the mt5 module (lazy import)
          4. ping it once
          5. read last_sync_utc; if gap > gap_threshold_hours, run_once(full=True)

        Loop body:
          - if N runs since last heartbeat >= heartbeat_sec/interval_sec, ping.
            If ping fails, mt5_guard.reconnect() and try again.
          - run incremental_sync, sleep, repeat.
        """
        setup_file_logging()
        run_count = 0
        runs_since_heartbeat = 0
        self._running = True
        logger.info(
            f"[SyncDaemon] starting daemon, interval={interval_sec}s, "
            f"timeframes={self.timeframes}, gap_threshold={self.gap_threshold_hours}h, "
            f"heartbeat={self.heartbeat_sec}s"
        )

        # --- 1. Wait for exactly one terminal64 (bail out after max_wait_sec
        # so the daemon doesn't sit in a 5s poll loop -- which spawns a
        # powershell.exe per poll and flashes the taskbar -- when MT5 is
        # just not running. See commit 2fa5695 incident writeup.) ---
        try:
            pids = mt5_guard.check_one(
                poll_sec=5.0, max_wait_sec=self._cli_max_wait_sec
            )
        except RuntimeError as e:
            logger.error(f"[SyncDaemon] {e}")
            return
        logger.info(f"[SyncDaemon] terminal64 OK, PID={pids[0]}")

        # --- 2. Acquire mt5 module + initial ping ---
        import MetaTrader5 as mt5
        self._mt5 = mt5
        if not mt5.initialize():
            logger.error(f"[SyncDaemon] mt5.initialize() failed: {mt5.last_error()}")
            return
        if not mt5_guard.ping(mt5, self.symbol):
            logger.error("[SyncDaemon] initial ping failed; cannot proceed")
            mt5.shutdown()
            return

        # --- 3. Gap detection: recover from a long downtime ---
        gap_hours = self._maybe_recover_from_gap()
        if gap_hours is None:
            # Could not run; bail out cleanly.
            mt5.shutdown()
            return

        # --- 4. Main loop ---
        heartbeat_every = max(1, self.heartbeat_sec // max(1, interval_sec))
        while self._running:
            run_count += 1
            runs_since_heartbeat += 1
            t0 = _time.time()
            logger.info(
                f"[SyncDaemon] run #{run_count} @ "
                f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
            )

            # Periodic heartbeat -- catch dead handles early
            if runs_since_heartbeat >= heartbeat_every:
                runs_since_heartbeat = 0
                if not mt5_guard.ping(mt5, self.symbol):
                    logger.warning(
                        f"[SyncDaemon] heartbeat ping failed on run #{run_count}, "
                        f"reconnecting"
                    )
                    try:
                        mt5_guard.reconnect(mt5, poll_sec=5.0, max_wait_sec=300)
                    except RuntimeError as e:
                        logger.error(f"[SyncDaemon] reconnect failed: {e}")
                        _time.sleep(interval_sec)
                        continue

            try:
                report = self.orch.incremental_sync(self.symbol, self.timeframes)
                if report.total_inserted > 0:
                    logger.info(
                        f"[SyncDaemon] run #{run_count}: +{report.total_inserted} bars "
                        f"({report.elapsed_sec:.1f}s)"
                    )
                else:
                    logger.info(
                        f"[SyncDaemon] run #{run_count}: no new bars "
                        f"({report.elapsed_sec:.1f}s)"
                    )
            except Exception as e:
                logger.error(
                    f"[SyncDaemon] run #{run_count} exception: "
                    f"{type(e).__name__}: {e}"
                )

            if max_runs > 0 and run_count >= max_runs:
                logger.info(f"[SyncDaemon] reached max_runs={max_runs}, exiting")
                break

            elapsed = _time.time() - t0
            wait = max(0, interval_sec - elapsed)
            _time.sleep(wait)

        try:
            mt5.shutdown()
        except Exception:
            pass
        logger.info("[SyncDaemon] daemon stopped")

    def stop(self) -> None:
        self._running = False

    # ---------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------

    def _maybe_recover_from_gap(self) -> Optional[float]:
        """
        Inspect last_sync_utc. If the gap is bigger than
        gap_threshold_hours, run a one-shot full_sync to fill in everything
        the incremental window (200 bars) cannot cover.

        Returns the gap in hours (or 0.0 if there's no prior status), or
        None if recovery itself failed (caller should bail out).
        """
        last = _last_sync_utc_from_status()
        if last is None:
            logger.info("[SyncDaemon] no prior status; entering incremental loop")
            return 0.0
        now = datetime.now(timezone.utc)
        gap_hours = (now - last).total_seconds() / 3600.0
        logger.info(
            f"[SyncDaemon] last_sync_utc={last.isoformat()}, "
            f"gap={gap_hours:.2f}h, threshold={self.gap_threshold_hours:.2f}h"
        )
        if gap_hours < self.gap_threshold_hours:
            return gap_hours
        logger.warning(
            f"[SyncDaemon] gap {gap_hours:.2f}h exceeds threshold "
            f"{self.gap_threshold_hours:.2f}h, running full_sync to recover"
        )
        try:
            report = self.orch.full_sync(self.symbol, self.timeframes, n_bars=5000)
            logger.info(
                f"[SyncDaemon] recovery full_sync inserted "
                f"{report.total_inserted} bars ({report.elapsed_sec:.1f}s)"
            )
        except Exception as e:
            logger.error(f"[SyncDaemon] recovery full_sync failed: {e}")
            return None
        return gap_hours


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="MT5 live-sync daemon (with auto-recovery)"
    )
    p.add_argument("--mode", choices=["once", "daemon"], default="once",
                   help="once: one-shot; daemon: loop forever")
    p.add_argument("--type", choices=["full", "incremental"], default="incremental",
                   help="which sync to run in --mode once")
    p.add_argument("--symbol", default="XAUUSD+")
    p.add_argument("--timeframes", default="M15,H1,D1",
                   help="comma-separated, e.g. M15,H1,D1 or M5,M15,M30,H1,H4,D1")
    p.add_argument("--interval", type=int, default=60,
                   help="sync interval (seconds) for --mode daemon")
    p.add_argument("--n-bars", type=int, default=5000,
                   help="bars per timeframe for full sync")
    p.add_argument("--gap-threshold-hours", type=float, default=6.0,
                   help="if last sync is older than this, run full_sync first")
    p.add_argument("--heartbeat-sec", type=int, default=300,
                   help="how often to ping MT5 to detect dead handles")
    p.add_argument("--max-wait-sec", type=int, default=600,
                   help="seconds to wait for terminal64.exe at startup "
                        "before giving up (default 600 = 10 min). Set to "
                        "0 to wait forever (not recommended).")
    p.add_argument("--max-runs", type=int, default=0,
                   help="stop after N runs (0 = forever), debug only")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _build_arg_parser().parse_args(argv)
    setup_file_logging()

    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    d = SyncDaemon(
        symbol=args.symbol,
        timeframes=tfs,
        gap_threshold_hours=args.gap_threshold_hours,
        heartbeat_sec=args.heartbeat_sec,
        max_wait_sec=args.max_wait_sec,
    )

    if args.mode == "once":
        full = (args.type == "full")
        report = d.run_once(full=full, n_bars=args.n_bars)
        d.orch.print_report(report)
        return 0 if not report.error else 1

    # mode == daemon
    d.run_daemon(interval_sec=args.interval, max_runs=args.max_runs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
