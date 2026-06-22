"""db.store — analytics store with bulk insert helpers.

Thin wrapper around SQLite.  Same patterns as ``data.store.DataStore``
(``@contextmanager`` for connections, ``row_factory = sqlite3.Row``)
but living in a separate file/DB to keep raw market data clean.

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

logger = logging.getLogger(__name__)


class AnalyticsStore:
    """Run/strategy analytics — one SQLite file, multiple tables."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            from backend.core.db import STATE_DB
            db_path = str(STATE_DB)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── connection plumbing ─────────────────────────────────────

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
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

        sql = (
            "INSERT OR REPLACE INTO strategy_perf "
            "(run_id, bar_ts, bar_date, strategy, regime, direction, "
            " hold_bars, unrealized_pnl, cum_pnl, position_open, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with self._conn() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    # ── query helpers (lightweight, used by tests & scripts) ──

    def count_strategy_perf(self, run_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM strategy_perf WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return int(row["c"])

    def fetch_strategy_perf(self, run_id: int,
                            limit: int | None = None
                            ) -> list[sqlite3.Row]:
        sql = ("SELECT * FROM strategy_perf WHERE run_id=? "
               "ORDER BY bar_ts ASC, strategy ASC")
        params: tuple = (run_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (run_id, int(limit))
        with self._conn() as conn:
            return list(conn.execute(sql, params))

    def direction_distribution(self, run_id: int) -> dict:
        """Return {direction: count} for a given run."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT direction, COUNT(*) AS c FROM strategy_perf "
                "WHERE run_id=? GROUP BY direction",
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


class DecisionLogStore:
    """决策审计轨迹 — 独立 DB，记录 signal / risk_check / open / close / circuit_trip / router_select 等关键决策。"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            from backend.core.db import STATE_DB
            db_path = str(STATE_DB)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── connection plumbing ─────────────────────────────────────

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
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
        from .schema import DECISION_LOG_DDL

        with self._conn() as conn:
            for ddl in DECISION_LOG_DDL:
                conn.execute(ddl)

    # ── log helpers ─────────────────────────────────────────────

    def log(self, run_id: int, ts: float, bar_date: str,
            decision_type: str, strategy: str = "", regime: str = "",
            direction: int = 0, confidence: float | None = None,
            factor_scores: str | None = None, decision: str = "",
            meta: str | None = None) -> int:
        """单条插入，返回 log_id。"""
        if factor_scores is not None and not isinstance(factor_scores, str):
            factor_scores = json.dumps(factor_scores, ensure_ascii=False, default=str)
        if meta is not None and not isinstance(meta, str):
            meta = json.dumps(meta, ensure_ascii=False, default=str)

        sql = (
            "INSERT INTO decision_log "
            "(run_id, ts, bar_date, decision_type, strategy, regime, "
            " direction, confidence, factor_scores, decision, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with self._conn() as conn:
            cur = conn.execute(sql, (
                int(run_id), float(ts), str(bar_date), str(decision_type),
                str(strategy), str(regime), int(direction),
                confidence, factor_scores, str(decision),
                str(meta or ""),
            ))
            return int(cur.lastrowid)

    def log_batch(self, records: list[dict]) -> int:
        """批量插入多条决策记录，返回写入行数。"""
        if not records:
            return 0

        rows = []
        for r in records:
            fs = r.get("factor_scores")
            if fs is not None and not isinstance(fs, str):
                fs = json.dumps(fs, ensure_ascii=False, default=str)
            meta = r.get("meta")
            if meta is not None and not isinstance(meta, str):
                meta = json.dumps(meta, ensure_ascii=False, default=str)
            rows.append((
                int(r["run_id"]),
                float(r["ts"]),
                str(r["bar_date"]),
                str(r["decision_type"]),
                str(r.get("strategy", "")),
                str(r.get("regime", "")),
                int(r.get("direction", 0)),
                r.get("confidence"),
                fs,
                str(r.get("decision", "")),
                str(meta or ""),
            ))

        sql = (
            "INSERT INTO decision_log "
            "(run_id, ts, bar_date, decision_type, strategy, regime, "
            " direction, confidence, factor_scores, decision, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with self._conn() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    # ── query helpers ───────────────────────────────────────────

    def query(self, run_id: int | None = None,
              decision_type: str | None = None,
              strategy: str | None = None,
              limit: int = 100) -> "pd.DataFrame":
        """通用查询，返回 DataFrame。"""
        import pandas as pd

        conditions: list[str] = []
        params: list = []

        if run_id is not None:
            conditions.append("run_id=?")
            params.append(int(run_id))
        if decision_type is not None:
            conditions.append("decision_type=?")
            params.append(decision_type)
        if strategy is not None:
            conditions.append("strategy=?")
            params.append(strategy)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM decision_log WHERE {where} ORDER BY ts ASC LIMIT ?"
        params.append(int(limit))

        with self._conn() as conn:
            df = pd.read_sql_query(sql, conn, params=params)
        return df

    def run_summary(self, run_id: int) -> dict:
        """统计指定 run 的决策摘要。"""
        with self._conn() as conn:
            # 各决策类型计数
            type_counts = {
                r["decision_type"]: int(r["c"])
                for r in conn.execute(
                    "SELECT decision_type, COUNT(*) AS c FROM decision_log "
                    "WHERE run_id=? GROUP BY decision_type ORDER BY c DESC",
                    (run_id,),
                ).fetchall()
            }

            # 阻塞率 (decision=block / pass / execute 之类)
            decision_dist = {
                r["decision"]: int(r["c"])
                for r in conn.execute(
                    "SELECT decision, COUNT(*) AS c FROM decision_log "
                    "WHERE run_id=? GROUP BY decision ORDER BY c DESC",
                    (run_id,),
                ).fetchall()
            }

            # 路由选择分布 (仅 router_select)
            router_dist = {}
            for r in conn.execute(
                "SELECT meta FROM decision_log "
                "WHERE run_id=? AND decision_type='router_select'",
                (run_id,),
            ).fetchall():
                try:
                    m = json.loads(r["meta"])
                    sel = m.get("selected", "?")
                    router_dist[sel] = router_dist.get(sel, 0) + 1
                except (json.JSONDecodeError, TypeError):
                    pass

            total = conn.execute(
                "SELECT COUNT(*) AS c FROM decision_log WHERE run_id=?",
                (run_id,),
            ).fetchone()
            total_count = int(total["c"])

            block_count = sum(
                v for k, v in decision_dist.items()
                if k in ("block", "BLOCK", "reject")
            )
            block_rate = round(block_count / total_count, 4) if total_count else 0.0

        return {
            "run_id": run_id,
            "total_logs": total_count,
            "type_counts": type_counts,
            "decision_distribution": decision_dist,
            "block_rate": block_rate,
            "router_selection": router_dist,
        }
