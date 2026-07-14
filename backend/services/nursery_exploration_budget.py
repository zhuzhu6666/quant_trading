from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path


def build_setup_fingerprint(*, symbol: str, direction: int, regime: str, session: str,
                            event_state: str, signal_score: float, alpha_family: list[str]) -> str:
    payload = {
        "symbol": str(symbol or ""),
        "direction": 1 if int(direction or 0) > 0 else -1,
        "regime": str(regime or "unknown"),
        "session": str(session or "unknown"),
        "event_state": str(event_state or "none"),
        "signal_bucket": round(abs(float(signal_score or 0.0)) * 10.0) / 10.0,
        "alpha_family": sorted({str(item) for item in alpha_family if item})[:8],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class NurseryExplorationBudgetService:
    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    def _connect(self):
        if is_state_db_path(self.db_path):
            return get_state_pg_conn()
        return connect_sqlite(self.db_path)

    @staticmethod
    def _sql(conn, sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s") if conn.__class__.__module__.startswith("psycopg") else sql

    def reserve(self, *, reasons: list[str], setup_fingerprint: str, per_reason_limit: int,
                global_limit: int, setup_limit: int, ttl_seconds: int, now: float | None = None) -> dict[str, Any]:
        reasons = sorted({str(reason) for reason in reasons if reason})
        if not reasons:
            return {"allowed": True, "status": "not_exploration", "reservation_id": ""}
        now = float(now or time.time())
        trade_date = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()
        conn = self._connect()
        try:
            if conn.__class__.__module__.startswith("psycopg"):
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"nursery:{trade_date}",))
            else:
                conn.execute("BEGIN IMMEDIATE")
            conn.execute(self._sql(conn, "UPDATE nursery_exploration_reservation SET status='expired', updated_at=? WHERE status='reserved' AND expires_at<=?"), (now, now))
            active = ("reserved", "consumed")
            global_count = conn.execute(self._sql(conn, "SELECT COUNT(DISTINCT reservation_id) FROM nursery_exploration_reservation WHERE trade_date=? AND status IN (?, ?)"), (trade_date, *active)).fetchone()[0]
            if int(global_count) >= max(0, int(global_limit)):
                conn.rollback()
                return {"allowed": False, "status": "global_budget_exhausted", "global_count": int(global_count)}
            setup_count = conn.execute(self._sql(conn, "SELECT COUNT(DISTINCT reservation_id) FROM nursery_exploration_reservation WHERE trade_date=? AND setup_fingerprint=? AND status IN (?, ?)"), (trade_date, setup_fingerprint, *active)).fetchone()[0]
            if int(setup_count) >= max(0, int(setup_limit)):
                conn.rollback()
                return {"allowed": False, "status": "setup_budget_exhausted", "setup_count": int(setup_count)}
            for reason in reasons:
                count = conn.execute(self._sql(conn, "SELECT COUNT(*) FROM nursery_exploration_reservation WHERE trade_date=? AND reason=? AND status IN (?, ?)"), (trade_date, reason, *active)).fetchone()[0]
                if int(count) >= max(0, int(per_reason_limit)):
                    conn.rollback()
                    return {"allowed": False, "status": "reason_budget_exhausted", "reason": reason, "reason_count": int(count)}
            reservation_id = f"nursery_resv_{uuid.uuid4().hex[:16]}"
            primary_reason = reasons[0]
            for reason in reasons:
                conn.execute(self._sql(conn, "INSERT INTO nursery_exploration_reservation (reservation_id, trade_date, reason, setup_fingerprint, status, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)"), (reservation_id, trade_date, reason, setup_fingerprint, now + max(30, int(ttl_seconds)), now, now))
            conn.commit()
            return {"allowed": True, "status": "reserved", "reservation_id": reservation_id, "reason": primary_reason, "reasons": reasons, "trade_date": trade_date}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finalize(self, reservation_id: str, *, consumed: bool) -> None:
        if not reservation_id:
            return
        conn = self._connect()
        try:
            conn.execute(self._sql(conn, "UPDATE nursery_exploration_reservation SET status=?, updated_at=? WHERE reservation_id=? AND status='reserved'"), ("consumed" if consumed else "released", time.time(), reservation_id))
            conn.commit()
        finally:
            conn.close()
