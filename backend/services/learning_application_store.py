"""Lean canonical store for learning_application_log / learning_application_effect.

Converged 8-col / 5-col schema (PG == SQLite), single writer + read contract so
the whole learning-application domain stops hand-rolling wide-column SQL:

  learning_application_log(
      application_id TEXT PRIMARY KEY, run_id TEXT, source TEXT,
      status TEXT, details_json TEXT, created_at, updated_at)

  learning_application_effect(
      effect_id TEXT PRIMARY KEY, application_id TEXT, scope TEXT,
      effect_json TEXT, created_at)

details_json carries every field that used to be a wide log column (scope_type,
scope_key, action, bias_multiplier, old_weight, new_weight, suggestion_ids,
mutation_id, governance_eligibility_version, run_id, source) plus caller
details and an ``application_state`` lifecycle sub-dict.

effect_json carries every field that used to be a wide effect column (scope_type,
scope_key, action, status, observed_trade_count, baseline_trade_count,
post_avg_reward, baseline_avg_reward, delta_avg_reward, post_win_rate,
baseline_win_rate, decision, mutation_id, governance_eligibility_version,
last_review_at, updated_at).  ``scope`` holds the governed item's scope_key so
factor/scope filtering stays a plain indexed column (cross-DB, no JSON ops).

All reads parse the JSON and return plain dicts; call sites no longer reference
the removed wide columns.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
)


def _dumps(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def store_for_conn(conn: Any) -> LearningApplicationStore | None:
    """Return a :class:`LearningApplicationStore` bound to the same DB as ``conn``.

    Some call sites only receive a live connection (instead of a db path).
    We recover the underlying database so the store can open its own
    connection against the same file / PostgreSQL runtime State DB.  Returns
    ``None`` when the backing store cannot be determined (e.g. an in-memory
    SQLite database), in which case callers should skip store reads.
    """
    module = str(type(conn).__module__).split(".", 1)[0]
    if module == "psycopg":
        return LearningApplicationStore(STATE_DB)
    # SQLite: recover the backing file from PRAGMA database_list.
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except Exception:
        row = None
    path: str | None = None
    if row is not None:
        try:
            path = row["file"] if hasattr(row, "keys") else row[2]
        except Exception:
            path = None
    if not path or str(path).lower() in (":memory:", ""):
        return None
    return LearningApplicationStore(str(path))


class LearningApplicationStore:
    """CRUD for the converged learning_application_log / _effect pair."""

    def __init__(self, db_path: str | Path = STATE_DB) -> None:
        self.db_path = Path(db_path)

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _conn(self, *, read_only: bool = False):
        if self._use_pg():
            return get_state_pg_conn(read_only=read_only)
        conn = connect_sqlite(self.db_path, read_only=read_only)
        conn.row_factory = __import__("sqlite3").Row
        return conn

    # ── learning_application_log ─────────────────────────────────────────────

    def prepare_application(
        self,
        *,
        scope_type: str,
        scope_key: str,
        action: str,
        status: str = "prepared",
        run_id: str = "",
        source: str = "",
        bias_multiplier: float = 1.0,
        old_weight: float = 0.0,
        new_weight: float = 0.0,
        suggestion_ids: list[str] | None = None,
        mutation_id: str = "",
        governance_eligibility_version: str = "",
        cycle_ts: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        now = float(cycle_ts if cycle_ts is not None else time.time())
        application_id = _new_id("application")
        payload = dict(details or {})
        payload.update(
            {
                "scope_type": str(scope_type or ""),
                "scope_key": str(scope_key or ""),
                "action": str(action or ""),
                "bias_multiplier": float(bias_multiplier or 0.0),
                "old_weight": float(old_weight or 0.0),
                "new_weight": float(new_weight or 0.0),
                "suggestion_ids": list(suggestion_ids or []),
                "mutation_id": str(mutation_id or ""),
                "governance_eligibility_version": str(governance_eligibility_version or ""),
                "run_id": str(run_id or ""),
                "source": str(source or ""),
                "application_state": {
                    "status": str(status or "prepared"),
                    "prepared_at": now,
                    "updated_at": now,
                },
            }
        )
        conn = self._conn()
        try:
            conn.execute(
                self._sql(
                    """INSERT INTO learning_application_log
                       (application_id, run_id, source, status, details_json,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)"""
                ),
                (
                    application_id,
                    str(run_id or ""),
                    str(source or ""),
                    str(status or "prepared"),
                    _dumps(payload),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return application_id

    def transition_application(
        self,
        application_id: str,
        *,
        status: str,
        details_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"applied", "observing", "mutation_failed", "superseded", "rolled_back"}:
            raise ValueError(f"unsupported application transition: {status}")
        now = time.time()
        conn = self._conn()
        try:
            row = conn.execute(
                self._sql(
                    "SELECT details_json, status FROM learning_application_log WHERE application_id=?"
                ),
                (str(application_id),),
            ).fetchone()
            if not row:
                return {"ok": False, "status": "missing", "application_id": str(application_id)}
            details = _loads(row["details_json"], {})
            details.update(dict(details_patch or {}))
            lifecycle = dict(details.get("application_state") or {})
            lifecycle["status"] = status
            lifecycle["updated_at"] = now
            if status == "applied":
                lifecycle.setdefault("applied_at", now)
            elif status == "mutation_failed":
                lifecycle.setdefault("failed_at", now)
            details["application_state"] = lifecycle
            conn.execute(
                self._sql(
                    "UPDATE learning_application_log SET status=?, details_json=? WHERE application_id=?"
                ),
                (
                    status,
                    _dumps(details),
                    str(application_id),
                ),
            )
            conn.commit()
            return {
                "ok": True,
                "status": status,
                "application_id": str(application_id),
                "scope_type": details.get("scope_type", ""),
                "scope_key": details.get("scope_key", ""),
                "action": details.get("action", ""),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_application(self, application_id: str) -> dict[str, Any] | None:
        conn = self._conn(read_only=True)
        try:
            row = conn.execute(
                self._sql(
                    "SELECT application_id, run_id, source, status, details_json, "
                    "created_at, updated_at FROM learning_application_log "
                    "WHERE application_id=?"
                ),
                (str(application_id),),
            ).fetchone()
            if not row:
                return None
            details = _loads(row["details_json"], {})
            return {
                "application_id": row["application_id"],
                "run_id": row["run_id"],
                "source": row["source"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                **details,
            }
        finally:
            conn.close()

    def latest_application(
        self,
        *,
        scope_type: str | None = None,
        scope_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Most recent terminal/active application matching scope (Python-filtered)."""
        for app in self.iter_applications(scope_type=scope_type, scope_key=scope_key):
            return app
        return None

    def iter_applications(
        self,
        *,
        scope_type: str | None = None,
        scope_key: str | None = None,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Iterate applications newest-first, optionally filtered by scope.

        ``limit`` bounds the number of raw rows scanned (mirrors the ORDER BY
        created_at DESC / LIMIT n shape callers previously used).
        """
        conn = self._conn(read_only=True)
        try:
            sql = (
                "SELECT application_id, run_id, source, status, details_json, "
                "created_at, updated_at FROM learning_application_log "
                "ORDER BY created_at DESC, updated_at DESC"
            )
            if limit is not None:
                sql += f" LIMIT {int(limit)}"
            rows = conn.execute(self._sql(sql)).fetchall()
            for row in rows:
                details = _loads(row["details_json"], {})
                if scope_type is not None and str(details.get("scope_type") or "") != scope_type:
                    continue
                if scope_key is not None and str(details.get("scope_key") or "") != scope_key:
                    continue
                yield {
                    "application_id": row["application_id"],
                    "run_id": row["run_id"],
                    "source": row["source"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    **details,
                }
        finally:
            conn.close()

    # ── learning_application_effect ──────────────────────────────────────────

    def write_effect(
        self,
        *,
        application_id: str,
        scope_key: str,
        scope_type: str = "",
        action: str = "",
        status: str = "observing",
        observed_trade_count: int = 0,
        baseline_trade_count: int = 0,
        post_avg_reward: float = 0.0,
        baseline_avg_reward: float = 0.0,
        delta_avg_reward: float | None = None,
        post_win_rate: float = 0.0,
        baseline_win_rate: float = 0.0,
        decision: dict[str, Any] | None = None,
        mutation_id: str = "",
        governance_eligibility_version: str = "",
        last_review_at: float = 0.0,
        updated_at: float | None = None,
    ) -> str:
        now = float(updated_at if updated_at is not None else time.time())
        effect_id = _new_id("effect")
        payload = {
            "scope_type": str(scope_type or ""),
            "scope_key": str(scope_key or ""),
            "action": str(action or ""),
            "status": str(status or "observing"),
            "observed_trade_count": int(observed_trade_count or 0),
            "baseline_trade_count": int(baseline_trade_count or 0),
            "post_avg_reward": float(post_avg_reward or 0.0),
            "baseline_avg_reward": float(baseline_avg_reward or 0.0),
            "delta_avg_reward": (
                float(delta_avg_reward) if delta_avg_reward is not None else None
            ),
            "post_win_rate": float(post_win_rate or 0.0),
            "baseline_win_rate": float(baseline_win_rate or 0.0),
            "decision": dict(decision or {}),
            "mutation_id": str(mutation_id or ""),
            "governance_eligibility_version": str(governance_eligibility_version or ""),
            "last_review_at": float(last_review_at or 0.0),
            "updated_at": now,
        }
        conn = self._conn()
        try:
            conn.execute(
                self._sql(
                    """INSERT INTO learning_application_effect
                       (effect_id, application_id, scope, effect_json, created_at)
                       VALUES (?, ?, ?, ?, ?)"""
                ),
                (effect_id, str(application_id), str(scope_key or ""), _dumps(payload), now),
            )
            conn.commit()
        finally:
            conn.close()
        return effect_id

    def update_effect(
        self,
        application_id: str,
        *,
        patch: dict[str, Any],
    ) -> bool:
        conn = self._conn()
        try:
            row = conn.execute(
                self._sql(
                    "SELECT effect_json FROM learning_application_effect WHERE application_id=?"
                ),
                (str(application_id),),
            ).fetchone()
            if not row:
                return False
            data = _loads(row["effect_json"], {})
            for key, value in patch.items():
                if value is not None:
                    data[key] = value
            data["updated_at"] = time.time()
            conn.execute(
                self._sql(
                    "UPDATE learning_application_effect SET effect_json=?, "
                    "created_at=created_at WHERE application_id=?"
                ),
                (_dumps(data), str(application_id)),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def latest_effect(
        self,
        *,
        scope_key: str,
        scope_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Latest posterior effect for a governed scope_key."""
        conn = self._conn(read_only=True)
        try:
            rows = conn.execute(
                self._sql(
                    "SELECT effect_id, application_id, scope, effect_json, created_at "
                    "FROM learning_application_effect WHERE scope=? "
                    "ORDER BY created_at DESC, effect_id DESC"
                ),
                (str(scope_key),),
            ).fetchall()
            if scope_type is not None:
                for row in rows:
                    data = _loads(row["effect_json"], {})
                    if str(data.get("scope_type") or "") != scope_type:
                        continue
                    return {
                        "effect_id": row["effect_id"],
                        "application_id": row["application_id"],
                        "scope": row["scope"],
                        "created_at": row["created_at"],
                        **data,
                    }
                return None
            if not rows:
                return None
            row = rows[0]
            return {
                "effect_id": row["effect_id"],
                "application_id": row["application_id"],
                "scope": row["scope"],
                "created_at": row["created_at"],
                **_loads(row["effect_json"], {}),
            }
        finally:
            conn.close()

    def iter_effects(
        self,
        *,
        scope_key: str | None = None,
        scope_type: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        conn = self._conn(read_only=True)
        try:
            if scope_key is not None:
                rows = conn.execute(
                    self._sql(
                        "SELECT effect_id, application_id, scope, effect_json, created_at "
                        "FROM learning_application_effect WHERE scope=? "
                        "ORDER BY created_at DESC"
                    ),
                    (str(scope_key),),
                ).fetchall()
            else:
                rows = conn.execute(
                    self._sql(
                        "SELECT effect_id, application_id, scope, effect_json, created_at "
                        "FROM learning_application_effect ORDER BY created_at DESC"
                    )
                ).fetchall()
            for row in rows:
                data = _loads(row["effect_json"], {})
                if scope_type is not None and str(data.get("scope_type") or "") != scope_type:
                    continue
                yield {
                    "effect_id": row["effect_id"],
                    "application_id": row["application_id"],
                    "scope": row["scope"],
                    "created_at": row["created_at"],
                    **data,
                }
        finally:
            conn.close()
