"""db.store — analytics store with bulk insert helpers.

Thin wrapper around the configured analytics state store.  The default runtime
state path uses PostgreSQL; explicit non-state paths are still supported for
tests and isolated research runs.

Why a separate file from ``data.store``?
  * Different concern: analytics vs market data
  * Different lifecycle: short-lived (one per run) vs long-lived
  * Different schema: wide & denormalised vs narrow & append-only
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .schema import DDL
from backend.core.db import get_state_pg_conn, connect_sqlite, is_state_db_path

logger = logging.getLogger(__name__)


class AnalyticsStore:
    """Run/strategy analytics."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            from backend.core.db import STATE_DB
            db_path = str(STATE_DB)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _p(self) -> str:
        return "%s" if self._use_pg() else "?"

    # ── connection plumbing ─────────────────────────────────────

    @contextmanager
    def _conn(self):
        conn = get_state_pg_conn() if self._use_pg() else connect_sqlite(self.db_path)
        if not self._use_pg():
            conn.row_factory = sqlite3.Row
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            if self._use_pg():
                return
            for ddl in DDL:
                conn.execute(ddl)

    # ── DDL introspection (handy for the backfill report) ──────

    @staticmethod
    def create_table_sql() -> str:
        """Return the DDL statements (for echoing into the report)."""
        return ";\n".join(DDL) + ";"

    # ── strategy_perf ───────────────────────────────────────────

    _STRATEGY_PERF_COLS = (
        "run_id", "bar_ts", "bar_date", "strategy", "regime",
        "direction", "hold_bars", "unrealized_pnl", "cum_pnl",
        "position_open", "meta",
    )

    def insert_strategy_perf(self, records: list[dict]) -> int:
        """Bulk insert strategy_perf rows.

        Each record must contain all columns listed in
        ``_STRATEGY_PERF_COLS`` (extra keys are ignored).  ``meta``
        may be either a dict (serialised to JSON) or a string.

        Returns the number of rows written.
        """
        if not records:
            return 0

        rows = []
        for r in records:
            meta = r.get("meta", "")
            if isinstance(meta, (dict, list)):
                meta = json.dumps(meta, ensure_ascii=False, default=str)
            elif meta is None:
                meta = ""
            rows.append((
                int(r["run_id"]),
                float(r["bar_ts"]),
                str(r["bar_date"]),
                str(r["strategy"]),
                str(r.get("regime", "")),
                int(r.get("direction", 0)),
                int(r.get("hold_bars", 0)),
                float(r.get("unrealized_pnl", 0.0)),
                float(r.get("cum_pnl", 0.0)),
                int(r.get("position_open", 0)),
                str(meta),
            ))

        p = self._p()
        sql = (
            "INSERT INTO strategy_perf "
            "(run_id, bar_ts, bar_date, strategy, regime, direction, "
            " hold_bars, unrealized_pnl, cum_pnl, position_open, meta) "
            f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})"
            " ON CONFLICT(run_id, bar_ts, strategy) DO UPDATE SET "
            "bar_date=excluded.bar_date, regime=excluded.regime, direction=excluded.direction, "
            "hold_bars=excluded.hold_bars, unrealized_pnl=excluded.unrealized_pnl, "
            "cum_pnl=excluded.cum_pnl, position_open=excluded.position_open, meta=excluded.meta"
        )
        with self._conn() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    # ── query helpers (lightweight, used by tests & scripts) ──

    def count_strategy_perf(self, run_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM strategy_perf WHERE run_id={self._p()}",
                (run_id,),
            ).fetchone()
            return int(row["c"])

    def fetch_strategy_perf(self, run_id: int,
                            limit: int | None = None
                            ) -> list[sqlite3.Row]:
        p = self._p()
        sql = (f"SELECT * FROM strategy_perf WHERE run_id={p} "
               "ORDER BY bar_ts ASC, strategy ASC")
        params: tuple = (run_id,)
        if limit is not None:
            sql += f" LIMIT {p}"
            params = (run_id, int(limit))
        with self._conn() as conn:
            return list(conn.execute(sql, params))

    def direction_distribution(self, run_id: int) -> dict:
        """Return {direction: count} for a given run."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT direction, COUNT(*) AS c FROM strategy_perf "
                f"WHERE run_id={self._p()} GROUP BY direction",
                (run_id,),
            ).fetchall()
        return {int(r["direction"]): int(r["c"]) for r in rows}

    def last_run_id(self) -> int:
        """Largest run_id currently in the table (0 if empty)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(run_id), 0) AS m FROM strategy_perf"
            ).fetchone()
        return int(row["m"])
