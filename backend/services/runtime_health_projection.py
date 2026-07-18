"""Cross-process read model for live runtime health facts."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.core.state_store import validate_runtime_state_schema


PROJECTION_KEY = "runtime_health_projection.v1"


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


class RuntimeHealthProjectionService:
    """Publish live facts once; health/readiness/workers consume the same view."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _conn(self, *, read_only: bool = False):
        if self._use_pg():
            return get_state_pg_conn(read_only=read_only)
        conn = connect_sqlite(self.db_path, read_only=read_only)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure(self, conn: Any) -> None:
        declaration = self._sql(
            """
                CREATE TABLE IF NOT EXISTS runtime_kv (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL DEFAULT 0.0
                )
            """
        )
        if self._use_pg():
            validate_runtime_state_schema(conn, declaration)
        else:
            conn.execute(declaration)

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "runtime_health_projection_boundary.v1",
            "read_model_only": True,
            "does_not_authorize_trading": True,
            "live_process_is_fact_publisher": True,
            "consumers_do_not_recompute_broker_state": True,
        }

    def publish(
        self,
        *,
        market_session: dict[str, Any] | None = None,
        ctrader_connected: bool | None = None,
        live_loop_running: bool | None = None,
        source: str = "live_runtime",
    ) -> dict[str, Any]:
        now = time.time()
        conn = self._conn()
        try:
            self._ensure(conn)
            row = conn.execute(
                self._sql("SELECT value_json FROM runtime_kv WHERE key=?"),
                (PROJECTION_KEY,),
            ).fetchone()
            payload = _loads(row["value_json"] if row else "{}")
            payload.update({
                "schema_version": "runtime_health_projection.v1",
                "source": str(source or "live_runtime"),
                "published_at": now,
            })
            if market_session is not None:
                payload["market_session"] = dict(market_session)
            if ctrader_connected is not None:
                payload["ctrader"] = {
                    "connected": bool(ctrader_connected),
                    "status": "connected" if ctrader_connected else "disconnected",
                    "updated_at": now,
                }
            if live_loop_running is not None:
                payload["live_loop"] = {
                    "running": bool(live_loop_running),
                    "status": "running" if live_loop_running else "stopped",
                    "updated_at": now,
                }
            conn.execute(
                self._sql(
                    """
                    INSERT INTO runtime_kv (key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at
                    """
                ),
                (PROJECTION_KEY, json.dumps(payload, ensure_ascii=False, default=str), now),
            )
            conn.commit()
            return {**payload, "ok": True, "boundary": self.boundary()}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def latest(self, *, max_age_seconds: float = 180.0) -> dict[str, Any]:
        conn = None
        try:
            conn = self._conn(read_only=True)
            row = conn.execute(
                self._sql("SELECT value_json, updated_at FROM runtime_kv WHERE key=?"),
                (PROJECTION_KEY,),
            ).fetchone()
        except Exception as exc:
            return {
                "ok": False,
                "status": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
                "boundary": self.boundary(),
            }
        finally:
            if conn is not None:
                conn.close()
        if not row:
            return {
                "ok": False,
                "status": "missing",
                "age_seconds": None,
                "boundary": self.boundary(),
            }
        payload = _loads(row["value_json"])
        updated_at = float(row["updated_at"] or payload.get("published_at") or 0.0)
        age = max(0.0, time.time() - updated_at) if updated_at else float("inf")
        fresh = age <= max(1.0, float(max_age_seconds))
        return {
            **payload,
            "ok": fresh,
            "status": "fresh" if fresh else "stale",
            "updated_at": updated_at,
            "age_seconds": round(age, 3),
            "boundary": self.boundary(),
        }
