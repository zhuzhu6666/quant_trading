"""
data/live_sync/db_inserter.py -- DB write layer (T16.3, +2026-06-03 auto-recovery fields)

Wraps data.store.DataStore and:
- multi-timeframe insert in one call
- exponential-backoff retry on insert errors
- persistent sync status (data/charts/live_sync_status.json) that now
  distinguishes a successful sync (last_sync_utc / last_status="ok")
  from any attempted sync (last_attempt_utc / last_status="error").
  Previously a "run where every timeframe pull failed" still wrote
  last_sync_utc="ok" with inserted_last=0 -- which looked healthy to
  the watchdog and is exactly what hid the 2026-06-03 incident.
"""
import json
import logging
import time as _time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from data.store import DataStore

logger = logging.getLogger(__name__)


@dataclass
class InsertResult:
    """Result of inserting a single timeframe's bars."""
    symbol: str
    timeframe: str
    inserted: int
    total_db_bars: int
    duration_sec: float
    error: str = ""


@dataclass
class SyncStatus:
    """
    Persisted sync status. Read by SyncDaemon._maybe_recover_from_gap().

    Fields:
      last_sync_utc    -- only updated on a fully successful run
                          (some per_tf actually inserted, no per_tf errors)
      last_attempt_utc -- updated on EVERY call to save_status,
                          success or failure. A health monitor can alert
                          if (now - last_attempt_utc) > some threshold,
                          even if last_status is "error".
      last_status      -- "ok" if all attempted tf succeeded, else "error"
      per_tf           -- per-timeframe breakdown, used for the dashboard
    """
    last_sync_utc: str = ""
    last_attempt_utc: str = ""
    last_status: str = "ok"
    per_tf: dict = field(default_factory=dict)


class DBInserter:
    """
    DB insert wrapper.

    Usage:
        ins = DBInserter()
        results = ins.insert_multi(bars_by_tf, symbol="XAUUSD+", timeframe="M15")
        ins.save_status(results, symbol)
    """

    def __init__(self, db_path: str = "data/market_data.db",
                 status_dir: str = "data/charts"):
        self.store = DataStore(db_path)
        self.status_dir = Path(status_dir)
        self.status_dir.mkdir(parents=True, exist_ok=True)
        self._status_path = self.status_dir / "live_sync_status.json"

    def insert_bars(self, bars: list[dict], symbol: str, timeframe: str,
                    max_retries: int = 3) -> InsertResult:
        """Insert one timeframe's bars with retry on transient errors."""
        t0 = _time.time()
        result = InsertResult(symbol=symbol, timeframe=timeframe, inserted=0,
                              total_db_bars=0, duration_sec=0)
        if not bars:
            result.total_db_bars = self.store.bar_count(symbol, timeframe)
            return result

        for attempt in range(max_retries):
            try:
                self.store.insert_bars(bars, symbol, timeframe)
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        f"[DBInserter] insert failed (attempt {attempt+1}): {e}, "
                        f"retrying in {wait}s"
                    )
                    _time.sleep(wait)
                else:
                    result.error = f"{type(e).__name__}: {e}"
                    return result

        result.inserted = len(bars)
        result.duration_sec = _time.time() - t0
        result.total_db_bars = self.store.bar_count(symbol, timeframe)
        logger.info(
            f"[DBInserter] {symbol} {timeframe}: +{result.inserted} bars "
            f"(total={result.total_db_bars}, {result.duration_sec:.2f}s)"
        )
        return result

    def load_status(self) -> Optional[SyncStatus]:
        """Load the persisted sync status, or None on missing/malformed file."""
        if not self._status_path.exists():
            return None
        try:
            d = json.loads(self._status_path.read_text(encoding="utf-8"))
            return SyncStatus(**d)
        except Exception:
            return None

    def save_status(self, results: list[InsertResult], symbol: str,
                    error: str = "") -> None:
        """
        Persist the outcome of one sync run.

        - last_attempt_utc: now (always)
        - last_status: "ok" iff there were no per-tf errors and no top-level
                       `error` argument. Otherwise "error".
        - last_sync_utc: only updated when last_status == "ok". This way
                        SyncDaemon._maybe_recover_from_gap() only sees a
                        "recent successful sync" once the daemon is actually
                        working again.
        - per_tf: merged with the previous file so other timeframes are
                  preserved across runs.
        """
        now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        status = self.load_status()
        per_tf = (status.per_tf if status else {})

        # If a per-tf insert succeeded (InsertResult with inserted>=0 and no
        # error), record its count. Errors get a separate per_tf entry.
        for r in results:
            per_tf[r.timeframe] = {
                "inserted_last": r.inserted,
                "total_bars": r.total_db_bars,
                "last_sync_utc": now_str,  # audit 2026-06-08: 前端"最近同步"栏需要
                "error": r.error or "",
            }
        # 旧条目没有 last_sync_utc 时回填 (一次全 sync 后所有 tf 都有)
        for tf_name, info in per_tf.items():
            if "last_sync_utc" not in info:
                info["last_sync_utc"] = status.last_sync_utc if status and status.last_sync_utc else ""

        # Status string: error if any InsertResult has an error, or caller
        # passed a top-level error (e.g. "all tf pulls failed").
        any_err = bool(error) or any(r.error for r in results)
        last_status = "error" if any_err else "ok"

        prev_last_sync = (status.last_sync_utc if status else "")
        new_status = SyncStatus(
            last_sync_utc=now_str if last_status == "ok" else prev_last_sync,
            last_attempt_utc=now_str,
            last_status=last_status,
            per_tf=per_tf,
        )

        self._status_path.write_text(
            json.dumps(asdict(new_status), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            f"[DBInserter] sync status saved: last_status={last_status}, "
            f"per_tf={list(per_tf.keys())}"
        )
