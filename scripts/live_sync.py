"""T16 live data sync — CLI + importable service.

Two-mode form (per spec §1.3):
- `python scripts/live_sync.py [args]`  → CLI behavior
- `from scripts.live_sync import run_sync_once / start_daemon / stop_daemon / get_status`  → service calls
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.core.paths import CHARTS_DIR  # noqa: E402

BAR_TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]


def _status_path() -> Path:
    return CHARTS_DIR / "live_sync_status.json"


def get_status() -> dict:
    """Read live_sync_status.json. Returns sane defaults if file missing."""
    p = _status_path()
    if not p.exists():
        return {"per_tf": {}, "daemon_running": False, "error": "no_status_file"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"per_tf": {}, "daemon_running": False, "error": f"status_file_corrupt: {e}"}


def run_sync_once(
    timeframes: list[str] | None = None,
    sync_type: str = "incremental",
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
) -> dict:
    """Run a one-shot sync. Service entry point.

    Returns dict with `inserted` (per_tf) and `skipped` (per_tf) plus `total_inserted`.
    """
    if timeframes is None:
        timeframes = BAR_TIMEFRAMES
    cb = progress_cb or (lambda *_: None)
    cb("loading", 5, f"sync {sync_type} {timeframes}")

    # CTraderPuller 替代原 MT5 orchestrator
    try:
        from data.live_sync.ctrader_puller import CTraderPuller
        from data.store import DataStore
        from config.runtime_config import shared as rcc
        cfg = rcc()
        symbol = list(cfg.enabled_symbols)[0] if hasattr(cfg, 'enabled_symbols') and cfg.enabled_symbols else "XAUUSD+"
        store = DataStore()
        results = {}
        per_tf_status = {}
        total = 0
        for tf in timeframes:
            puller = CTraderPuller()
            r = puller.pull_history(symbol=symbol, timeframe=tf, n=100)
            inserted = 0
            if getattr(r, "bars", None):
                inserted = int(store.insert_bars(r.bars, symbol, tf) or 0)
            n = inserted
            results[tf] = n
            per_tf_status[tf] = {
                "inserted": n,
                "fetched": int(getattr(r, "n_bars", 0) or 0),
                "last_time": float(getattr(r, "last_time", 0.0) or 0.0),
                "error": str(getattr(r, "error", "") or ""),
                "updated_at": __import__("time").time(),
            }
            total += n
        result = {"total_inserted": total, "per_tf": results}
        _status_path().write_text(
            json.dumps(
                {
                    "daemon_running": False,
                    "last_sync_type": sync_type,
                    "last_run_at": __import__("time").time(),
                    "symbol": symbol,
                    "per_tf": per_tf_status,
                    "total_inserted": total,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        cb("error", 100, f"sync failed: {e}")
        raise

    cb("done", 100, f"inserted {result.get('total_inserted', 0)} bars")
    return result


def start_daemon(
    interval_seconds: int = 300,
    timeframes: list[str] | None = None,
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
) -> dict:
    """已弃用 — 改用 scheduler"""
    cb = progress_cb or (lambda *_: None)
    cb("error", 100, "daemon 已弃用, 改用 scheduler")
    return {"ok": False, "msg": "daemon 已弃用"}


def stop_daemon(progress_cb: Optional[Callable[[str, float, str], None]] = None) -> dict:
    """已弃用"""
    cb = progress_cb or (lambda *_: None)
    cb("error", 100, "daemon 已弃用")
    return {"ok": False, "msg": "daemon 已弃用"}


# Aliases (Phase 3 backend used these names; preserve compatibility)
run_once = run_sync_once


def main() -> int:
    """CLI entry — preserve original flags from old version."""
    parser = argparse.ArgumentParser(description="T16 live data sync CLI")
    parser.add_argument("--mode", choices=["once", "daemon", "status"], default="status")
    parser.add_argument("--type", choices=["incremental", "full"], default="incremental")
    parser.add_argument("--timeframes", type=str, default="M1,M5,M15,M30,H1,H4,D1", help="CSV of timeframes")
    parser.add_argument("--interval-seconds", type=int, default=300)
    args = parser.parse_args()

    timeframes = [x.strip() for x in args.timeframes.split(",") if x.strip()]

    def _print_progress(step: str, pct: float, msg: str) -> None:
        print(f"[{pct:5.1f}%] {step}: {msg}", flush=True)

    if args.mode == "once":
        result = run_sync_once(timeframes=timeframes, sync_type=args.type, progress_cb=_print_progress)
        print(json.dumps(result, indent=2, default=str))
    elif args.mode == "daemon":
        if "--interval-seconds" in sys.argv or any("interval" in a for a in sys.argv):
            # If a sub-flag is passed, treat as start; else status of existing
            result = start_daemon(interval_seconds=args.interval_seconds, timeframes=timeframes, progress_cb=_print_progress)
            print(json.dumps(result, indent=2, default=str))
        else:
            # Default for --mode daemon: print status (matching old behavior)
            print(json.dumps(get_status(), indent=2, default=str))
    else:  # status
        print(json.dumps(get_status(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
