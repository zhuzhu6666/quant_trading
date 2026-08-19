from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_exists,
)
from backend.core.state_store import (
    is_state_schema_write_sql,
    validate_runtime_state_schema,
)
from backend.core.db_helpers import (
    conn_is_pg as _conn_is_pg,
    load_json as _loads,
    pg_sql as _sql,
)
from backend.services.policy_suggestion_status import count_policy_suggestion_statuses


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _execute(conn, sql: str, params: Any = None):
    rendered = _sql(conn, sql)
    if _conn_is_pg(conn) and is_state_schema_write_sql(rendered):
        return validate_runtime_state_schema(conn, rendered)
    if params is None:
        return conn.execute(rendered)
    return conn.execute(rendered, params)


def _ratio(numer: float, denom: float, *, default: float = 1.0) -> float:
    return default if denom <= 0 else max(0.0, min(1.0, numer / denom))


def _freshness_score(age_seconds: Any, stale_after_seconds: float) -> float:
    age = _safe_float(age_seconds, stale_after_seconds * 2.0)
    if age <= 0:
        return 1.0
    if age >= stale_after_seconds:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (age / stale_after_seconds)))


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def ensure_autonomy_health_snapshot_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS autonomy_health_snapshot (
                snapshot_id TEXT PRIMARY KEY,
                score REAL NOT NULL DEFAULT 0.0,
                posture TEXT DEFAULT '',
                blockers_json TEXT NOT NULL DEFAULT '[]',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                trend_json TEXT NOT NULL DEFAULT '{}',
                source TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_autonomy_health_snapshot_created ON autonomy_health_snapshot(created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_autonomy_health_snapshot_posture ON autonomy_health_snapshot(posture, created_at)")
        conn.commit()
    finally:
        conn.close()


def ensure_autonomy_scope_approval_event_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS autonomy_scope_approval_event (
                event_id TEXT PRIMARY KEY,
                snapshot_id TEXT DEFAULT '',
                posture TEXT DEFAULT '',
                recommendation_json TEXT NOT NULL DEFAULT '{}',
                actor TEXT DEFAULT '',
                decision TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_autonomy_scope_approval_created ON autonomy_scope_approval_event(created_at)",
        )
        _execute(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_autonomy_scope_approval_snapshot ON autonomy_scope_approval_event(snapshot_id, created_at)",
        )
        conn.commit()
    finally:
        conn.close()


def ensure_autonomy_scope_enforcement_event_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS autonomy_scope_enforcement_event (
                event_id TEXT PRIMARY KEY,
                snapshot_id TEXT DEFAULT '',
                posture TEXT DEFAULT '',
                recommendation_json TEXT NOT NULL DEFAULT '{}',
                current_mode TEXT DEFAULT '',
                target_mode TEXT DEFAULT '',
                status TEXT DEFAULT '',
                risk_verdict_json TEXT NOT NULL DEFAULT '{}',
                mutation_json TEXT NOT NULL DEFAULT '{}',
                actor TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_autonomy_scope_enforcement_created ON autonomy_scope_enforcement_event(created_at)",
        )
        _execute(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_autonomy_scope_enforcement_snapshot ON autonomy_scope_enforcement_event(snapshot_id, created_at)",
        )
        _execute(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_autonomy_scope_enforcement_status ON autonomy_scope_enforcement_event(status, created_at)",
        )
        conn.commit()
    finally:
        conn.close()


class AutonomyHealthService:
    """Read-only V15 autonomy health score.

    Health can only tighten future autonomy scope; this v1 service does not
    write runtime config, change risk settings, or alter trading permissions.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    def build(
        self,
        *,
        live_status: dict[str, Any] | None = None,
        system_health: dict[str, Any] | None = None,
        governance: dict[str, Any] | None = None,
        stability: dict[str, Any] | None = None,
        replay_status: dict[str, Any] | None = None,
        governance_freshness: dict[str, Any] | None = None,
        model_status: dict[str, Any] | None = None,
        persist: bool = True,
        persist_min_interval_sec: float = 60.0,
    ) -> dict[str, Any]:
        live_status = dict(live_status or {})
        system_health = dict(system_health or {})
        governance = dict(governance or {})
        stability = dict(stability or {})
        replay_status = dict(replay_status or {})
        governance_freshness = dict(governance_freshness or {})
        model_status = dict(model_status or {})

        action_stats = self._action_stats()
        effect_stats = self._effect_stats()
        evidence_stats = self._evidence_integrity_stats()

        action_success_rate = action_stats["action_success_rate"]
        rollback_rate = action_stats["rollback_rate"]
        blocked_by_risk_rate = action_stats["blocked_by_risk_rate"]
        post_action_reward_delta = effect_stats["post_action_reward_delta"]
        config_restore_success = self._config_restore_success(stability)
        catalog_freshness = self._catalog_freshness(governance)
        replay_freshness = self._replay_freshness(replay_status)
        shadow_freshness = self._shadow_freshness(governance_freshness, model_status)
        evidence_integrity = evidence_stats["evidence_integrity"]
        live_loop_stability = self._live_loop_stability(live_status, system_health)

        positive_delta = max(-1.0, min(1.0, post_action_reward_delta))
        reward_score = max(0.0, min(1.0, 0.5 + positive_delta / 2.0))
        score = (
            0.14 * action_success_rate
            + 0.10 * (1.0 - rollback_rate)
            + 0.10 * (1.0 - blocked_by_risk_rate)
            + 0.10 * reward_score
            + 0.12 * config_restore_success
            + 0.10 * catalog_freshness
            + 0.10 * replay_freshness
            + 0.08 * shadow_freshness
            + 0.12 * evidence_integrity
            + 0.14 * live_loop_stability
        )
        score = round(max(0.0, min(1.0, score)), 6)

        blockers = []
        if config_restore_success < 1.0:
            blockers.append("config_restore_degraded")
        if live_loop_stability < 0.35:
            blockers.append("live_loop_unstable")
        if replay_freshness <= 0.0:
            blockers.append("replay_missing_or_stale")
        if evidence_integrity < 0.35:
            blockers.append("evidence_integrity_low")
        if blocked_by_risk_rate >= 0.50 and action_stats["governance_action_count"] >= 3:
            blockers.append("risk_blocks_many_actions")

        if "config_restore_degraded" in blockers or live_loop_stability <= 0.10:
            posture = "frozen"
        elif score < 0.45 or "replay_missing_or_stale" in blockers:
            posture = "shadow_only"
        elif score < 0.70 or blockers:
            posture = "constrained"
        else:
            posture = "full"

        scope_recommendation = self._scope_recommendation(posture, blockers)
        payload = {
            "schema_version": "autonomy_health.v1",
            "score": score,
            "posture": posture,
            "blockers": blockers,
            "action_success_rate": round(action_success_rate, 6),
            "rollback_rate": round(rollback_rate, 6),
            "blocked_by_risk_rate": round(blocked_by_risk_rate, 6),
            "post_action_reward_delta": round(post_action_reward_delta, 6),
            "config_restore_success": round(config_restore_success, 6),
            "catalog_freshness": round(catalog_freshness, 6),
            "replay_freshness": round(replay_freshness, 6),
            "shadow_freshness": round(shadow_freshness, 6),
            "evidence_integrity": round(evidence_integrity, 6),
            "live_loop_stability": round(live_loop_stability, 6),
            "updated_at": time.time(),
            "read_only": True,
            "scope_effect": "health_persistence_does_not_change_runtime_permissions",
            "scope_recommendation": scope_recommendation,
            "details": {
                "action_stats": action_stats,
                "effect_stats": effect_stats,
                "evidence_stats": evidence_stats,
            },
        }
        trend_before = self.trend()
        payload["trend"] = trend_before
        persistence = {"schema_version": "autonomy_health_persistence.v1", "persisted": False, "reason": "disabled"}
        if persist:
            persistence = self.persist_snapshot(payload, min_interval_sec=persist_min_interval_sec)
            payload["trend"] = self.trend()
        payload["persistence"] = persistence
        return payload

    def persist_snapshot(self, health: dict[str, Any], *, min_interval_sec: float = 60.0, source: str = "backend_readiness") -> dict[str, Any]:
        ensure_autonomy_health_snapshot_table(self.db_path)
        created_at = _safe_float(health.get("updated_at"), time.time())
        conn = _connect(self.db_path)
        try:
            latest = _execute(
                conn,
                """
                SELECT created_at
                FROM autonomy_health_snapshot
                ORDER BY created_at DESC
                LIMIT 1
                """,
            ).fetchone()
            latest_at = _safe_float(latest["created_at"] if latest else 0.0)
            if min_interval_sec > 0 and latest_at > 0 and created_at - latest_at < min_interval_sec:
                return {
                    "schema_version": "autonomy_health_persistence.v1",
                    "persisted": False,
                    "reason": "min_interval_not_elapsed",
                    "latest_age_seconds": round(max(0.0, created_at - latest_at), 3),
                    "min_interval_seconds": float(min_interval_sec),
                }
            snapshot_id = f"ah_{uuid.uuid4().hex[:16]}"
            metrics = {
                key: health.get(key)
                for key in (
                    "action_success_rate",
                    "rollback_rate",
                    "blocked_by_risk_rate",
                    "post_action_reward_delta",
                    "config_restore_success",
                    "catalog_freshness",
                    "replay_freshness",
                    "shadow_freshness",
                    "evidence_integrity",
                    "live_loop_stability",
                    "scope_recommendation",
                )
            }
            _execute(
                conn,
                """
                INSERT INTO autonomy_health_snapshot
                (snapshot_id, score, posture, blockers_json, metrics_json,
                 trend_json, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    _safe_float(health.get("score")),
                    str(health.get("posture") or ""),
                    _dumps(health.get("blockers") or []),
                    _dumps(metrics),
                    _dumps(health.get("trend") or {}),
                    str(source or ""),
                    created_at,
                ),
            )
            conn.commit()
            return {
                "schema_version": "autonomy_health_persistence.v1",
                "persisted": True,
                "snapshot_id": snapshot_id,
                "created_at": created_at,
                "source": source,
            }
        finally:
            conn.close()

    def trend(self, *, lookback_hours: float = 24.0, limit: int = 200) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "autonomy_health_snapshot"):
                return {
                    "schema_version": "autonomy_health_trend.v1",
                    "status": "missing_table",
                    "sample_count": 0,
                    "lookback_hours": float(lookback_hours),
                    "read_only": True,
                }
            since = time.time() - max(0.0, float(lookback_hours)) * 3600.0
            rows = _execute(
                conn,
                """
                SELECT snapshot_id, score, posture, blockers_json, metrics_json, created_at
                FROM autonomy_health_snapshot
                WHERE created_at >= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (since, max(1, int(limit))),
            ).fetchall()
            items = [dict(row) for row in rows]
            if not items:
                return {
                    "schema_version": "autonomy_health_trend.v1",
                    "status": "insufficient_history",
                    "sample_count": 0,
                    "lookback_hours": float(lookback_hours),
                    "read_only": True,
                }
            scores = [_safe_float(item.get("score")) for item in items]
            postures: dict[str, int] = {}
            blockers: dict[str, int] = {}
            for item in items:
                posture = str(item.get("posture") or "unknown")
                postures[posture] = postures.get(posture, 0) + 1
                for blocker in _loads(item.get("blockers_json"), []):
                    name = str(blocker or "")
                    if name:
                        blockers[name] = blockers.get(name, 0) + 1
            latest = items[-1]
            first = items[0]
            score_delta = scores[-1] - scores[0]
            posture_rank = {"full": 0, "constrained": 1, "shadow_only": 2, "frozen": 3}
            latest_rank = posture_rank.get(str(latest.get("posture") or ""), 3)
            worst_rank = max(posture_rank.get(str(item.get("posture") or ""), 3) for item in items)
            status = "stable"
            if latest_rank >= 2 or worst_rank >= 3:
                status = "degraded"
            elif score_delta <= -0.15:
                status = "declining"
            elif score_delta >= 0.15:
                status = "improving"
            return {
                "schema_version": "autonomy_health_trend.v1",
                "status": status,
                "sample_count": len(items),
                "lookback_hours": float(lookback_hours),
                "score_first": round(scores[0], 6),
                "score_latest": round(scores[-1], 6),
                "score_min": round(min(scores), 6),
                "score_avg": round(sum(scores) / len(scores), 6),
                "score_delta": round(score_delta, 6),
                "posture_counts": dict(sorted(postures.items())),
                "top_blockers": dict(sorted(blockers.items(), key=lambda kv: (-kv[1], kv[0]))[:10]),
                "first_snapshot_id": str(first.get("snapshot_id") or ""),
                "latest_snapshot_id": str(latest.get("snapshot_id") or ""),
                "latest_age_seconds": round(max(0.0, time.time() - _safe_float(latest.get("created_at"))), 3),
                "read_only": True,
            }
        finally:
            conn.close()

    def latest_snapshot(self) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "autonomy_health_snapshot"):
                return {"ok": False, "status": "missing_table"}
            row = _execute(
                conn,
                """
                SELECT *
                FROM autonomy_health_snapshot
                ORDER BY created_at DESC
                LIMIT 1
                """,
            ).fetchone()
            if not row:
                return {"ok": False, "status": "missing_snapshot"}
            return self._row_to_snapshot(dict(row))
        finally:
            conn.close()

    def snapshot_by_id(self, snapshot_id: str) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "autonomy_health_snapshot"):
                return {"ok": False, "status": "missing_table", "snapshot_id": str(snapshot_id or "")}
            row = _execute(
                conn,
                """
                SELECT *
                FROM autonomy_health_snapshot
                WHERE snapshot_id = ?
                LIMIT 1
                """,
                (str(snapshot_id or ""),),
            ).fetchone()
            if not row:
                return {"ok": False, "status": "missing_snapshot", "snapshot_id": str(snapshot_id or "")}
            return self._row_to_snapshot(dict(row))
        finally:
            conn.close()

    def record_scope_approval(
        self,
        *,
        health: dict[str, Any] | None = None,
        snapshot_id: str = "",
        actor: str = "system:v15_autonomy_health",
        decision: str = "recorded",
        reason: str = "",
        event_id: str = "",
        created_at: float | None = None,
    ) -> dict[str, Any]:
        health = dict(health or {})
        snapshot = self.snapshot_by_id(snapshot_id) if snapshot_id else self.latest_snapshot()
        effective_snapshot_id = str(snapshot_id or snapshot.get("snapshot_id") or "")
        recommendation = dict(health.get("scope_recommendation") or {})
        if not recommendation and snapshot.get("metrics"):
            recommendation = dict((snapshot.get("metrics") or {}).get("scope_recommendation") or {})
        event = {
            "ok": True,
            "schema_version": "autonomy_scope_approval_event.v1",
            "event_id": str(event_id or f"scope_approval_{uuid.uuid4().hex[:16]}"),
            "snapshot_id": effective_snapshot_id,
            "posture": str(health.get("posture") or snapshot.get("posture") or ""),
            "recommendation": recommendation,
            "actor": str(actor or ""),
            "decision": str(decision or "recorded"),
            "reason": str(reason or ""),
            "boundary": self._scope_approval_boundary(),
            "created_at": _safe_float(created_at, time.time()),
        }
        ensure_autonomy_scope_approval_event_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO autonomy_scope_approval_event
                (event_id, snapshot_id, posture, recommendation_json, actor,
                 decision, reason, boundary_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["snapshot_id"],
                    event["posture"],
                    _dumps(event["recommendation"]),
                    event["actor"],
                    event["decision"],
                    event["reason"],
                    _dumps(event["boundary"]),
                    event["created_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return event

    def latest_scope_approval(self) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "autonomy_scope_approval_event"):
                return {"ok": False, "status": "missing_table"}
            row = _execute(
                conn,
                """
                SELECT *
                FROM autonomy_scope_approval_event
                ORDER BY created_at DESC
                LIMIT 1
                """,
            ).fetchone()
            if not row:
                return {"ok": False, "status": "missing_approval_event"}
            return self._row_to_scope_approval(dict(row))
        finally:
            conn.close()

    def enforce_scope_recommendation(
        self,
        *,
        health: dict[str, Any] | None = None,
        snapshot_id: str = "",
        actor: str = "system:v15_autonomy_health",
        reason: str = "",
        event_id: str = "",
        created_at: float | None = None,
    ) -> dict[str, Any]:
        health = dict(health or {})
        snapshot = self.latest_snapshot() if not snapshot_id else {"snapshot_id": snapshot_id}
        effective_snapshot_id = str(snapshot_id or snapshot.get("snapshot_id") or "")
        recommendation = dict(health.get("scope_recommendation") or {})
        if not recommendation and snapshot.get("metrics"):
            recommendation = dict((snapshot.get("metrics") or {}).get("scope_recommendation") or {})
        posture = str(health.get("posture") or snapshot.get("posture") or recommendation.get("posture") or "")
        current_mode = self._current_incident_mode()
        target_mode = self._target_incident_mode(recommendation)
        status = "no_recommendation"
        risk_verdict: dict[str, Any] = {}
        mutation: dict[str, Any] = {}
        applied = False

        if target_mode:
            from risk.policy_service import INCIDENT_MODE_RANK

            current_rank = INCIDENT_MODE_RANK.get(current_mode, 0)
            target_rank = INCIDENT_MODE_RANK.get(target_mode, 0)
            if target_rank <= current_rank:
                status = "already_at_or_stricter"
            else:
                from backend.services.incident_controls import RuntimeIncidentControlService

                result = RuntimeIncidentControlService(self.db_path).set_mode(
                    target_mode,
                    reason=reason or f"autonomy_health:{recommendation.get('mode') or posture}",
                    actor=actor,
                    confirm_thaw=False,
                )
                status = str(result.get("status") or ("applied" if result.get("ok") else "blocked"))
                risk_verdict = dict(result.get("risk_verdict") or {})
                mutation = dict(result.get("mutation") or {})
                applied = bool(result.get("ok")) and status == "applied"

        event = {
            "ok": status in {"applied", "already_at_or_stricter"},
            "schema_version": "autonomy_scope_enforcement_event.v1",
            "event_id": str(event_id or f"scope_enforcement_{uuid.uuid4().hex[:16]}"),
            "snapshot_id": effective_snapshot_id,
            "posture": posture,
            "recommendation": recommendation,
            "current_mode": current_mode,
            "target_mode": target_mode,
            "status": status,
            "applied": applied,
            "risk_verdict": risk_verdict,
            "mutation": mutation,
            "actor": str(actor or ""),
            "reason": str(reason or ""),
            "boundary": self._scope_enforcement_boundary(),
            "created_at": _safe_float(created_at, time.time()),
        }
        self._persist_scope_enforcement(event)
        return event

    def latest_scope_enforcement(self) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "autonomy_scope_enforcement_event"):
                return {"ok": False, "status": "missing_table"}
            row = _execute(
                conn,
                """
                SELECT *
                FROM autonomy_scope_enforcement_event
                ORDER BY created_at DESC
                LIMIT 1
                """,
            ).fetchone()
            if not row:
                return {"ok": False, "status": "missing_enforcement_event"}
            return self._row_to_scope_enforcement(dict(row))
        finally:
            conn.close()

    @staticmethod
    def _scope_recommendation(posture: str, blockers: list[str]) -> dict[str, Any]:
        if posture == "frozen":
            mode = "freeze_autonomy"
        elif posture == "shadow_only":
            mode = "shadow_only"
        elif posture == "constrained":
            mode = "constrain_high_impact_actions"
        else:
            mode = "no_change"
        return {
            "schema_version": "autonomy_scope_recommendation.v1",
            "mode": mode,
            "posture": posture,
            "blockers": list(blockers or []),
            "can_tighten_only": True,
            "requires_risk_policy_for_actions": True,
            "applied": False,
            "reason": "health_snapshot_is_audit_evidence_only",
        }

    @staticmethod
    def _scope_approval_boundary() -> dict[str, Any]:
        return {
            "schema_version": "autonomy_scope_approval_boundary.v1",
            "audit_only": True,
            "can_tighten_only": True,
            "does_not_change_runtime_permissions": True,
            "does_not_change_runtime_overlay": True,
            "does_not_change_runtime_snapshot": True,
            "does_not_change_factor_weights": True,
            "does_not_change_orders_or_positions": True,
            "risk_policy_service_required_for_actions": True,
            "decision_policy_required_for_weight_writes": True,
            "runtime_overlay_snapshot_required_for_config_changes": True,
            "applied": False,
        }

    @staticmethod
    def _scope_enforcement_boundary() -> dict[str, Any]:
        return {
            "schema_version": "autonomy_scope_enforcement_boundary.v1",
            "can_tighten_only": True,
            "does_not_relax_incident_mode": True,
            "uses_incident_control_service": True,
            "risk_policy_service_required": True,
            "runtime_overlay_snapshot_required_for_applied_changes": True,
            "does_not_change_factor_weights": True,
            "does_not_change_orders_or_positions": True,
            "decision_policy_required_for_weight_writes": True,
        }

    @staticmethod
    def _target_incident_mode(recommendation: dict[str, Any]) -> str:
        mode = str((recommendation or {}).get("mode") or "").strip().lower()
        if mode == "freeze_autonomy":
            return "frozen"
        if mode == "shadow_only":
            return "shadow_only"
        if mode == "constrain_high_impact_actions":
            return "no_new_risk"
        return ""

    def _current_incident_mode(self) -> str:
        try:
            from backend.services.incident_controls import RuntimeIncidentControlService

            return str(
                RuntimeIncidentControlService(self.db_path).status().get("mode")
                or "normal"
            )
        except Exception:
            return "normal"

    def _persist_scope_enforcement(self, event: dict[str, Any]) -> None:
        ensure_autonomy_scope_enforcement_event_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO autonomy_scope_enforcement_event
                (event_id, snapshot_id, posture, recommendation_json, current_mode,
                 target_mode, status, risk_verdict_json, mutation_json, actor,
                 reason, boundary_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["snapshot_id"],
                    event["posture"],
                    _dumps(event["recommendation"]),
                    event["current_mode"],
                    event["target_mode"],
                    event["status"],
                    _dumps(event["risk_verdict"]),
                    _dumps(event["mutation"]),
                    event["actor"],
                    event["reason"],
                    _dumps(event["boundary"]),
                    event["created_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_snapshot(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "schema_version": "autonomy_health_snapshot.v1",
            "snapshot_id": str(row.get("snapshot_id") or ""),
            "score": _safe_float(row.get("score")),
            "posture": str(row.get("posture") or ""),
            "blockers": _loads(row.get("blockers_json"), []),
            "metrics": _loads(row.get("metrics_json"), {}),
            "trend": _loads(row.get("trend_json"), {}),
            "source": str(row.get("source") or ""),
            "created_at": _safe_float(row.get("created_at")),
        }

    @staticmethod
    def _row_to_scope_approval(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "schema_version": "autonomy_scope_approval_event.v1",
            "event_id": str(row.get("event_id") or ""),
            "snapshot_id": str(row.get("snapshot_id") or ""),
            "posture": str(row.get("posture") or ""),
            "recommendation": _loads(row.get("recommendation_json"), {}),
            "actor": str(row.get("actor") or ""),
            "decision": str(row.get("decision") or ""),
            "reason": str(row.get("reason") or ""),
            "boundary": _loads(row.get("boundary_json"), AutonomyHealthService._scope_approval_boundary()),
            "created_at": _safe_float(row.get("created_at")),
        }

    @staticmethod
    def _row_to_scope_enforcement(row: dict[str, Any]) -> dict[str, Any]:
        status = str(row.get("status") or "")
        return {
            "ok": status in {"applied", "already_at_or_stricter"},
            "schema_version": "autonomy_scope_enforcement_event.v1",
            "event_id": str(row.get("event_id") or ""),
            "snapshot_id": str(row.get("snapshot_id") or ""),
            "posture": str(row.get("posture") or ""),
            "recommendation": _loads(row.get("recommendation_json"), {}),
            "current_mode": str(row.get("current_mode") or ""),
            "target_mode": str(row.get("target_mode") or ""),
            "status": status,
            "applied": status == "applied",
            "risk_verdict": _loads(row.get("risk_verdict_json"), {}),
            "mutation": _loads(row.get("mutation_json"), {}),
            "actor": str(row.get("actor") or ""),
            "reason": str(row.get("reason") or ""),
            "boundary": _loads(row.get("boundary_json"), AutonomyHealthService._scope_enforcement_boundary()),
            "created_at": _safe_float(row.get("created_at")),
        }

    def _action_stats(self) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            applied = 0
            rolled_back = 0
            blocked = 0
            total = 0
            try:
                from backend.services.learning_application_store import (
                    LearningApplicationStore,
                )

                counts: dict[str, int] = {}
                for app in LearningApplicationStore(self.db_path).iter_applications():
                    status = str(app.get("status") or "")
                    counts[status] = counts.get(status, 0) + 1
                if counts:
                    applied += sum(counts.get(item, 0) for item in ("applied", "completed", "kept", "reinforced"))
                    rolled_back += sum(counts.get(item, 0) for item in ("rolled_back", "rollback"))
                    blocked += sum(counts.get(item, 0) for item in ("blocked_by_risk", "blocked", "rejected"))
                    total += sum(counts.values())
            except Exception:
                pass
            if state_table_exists(conn, "policy_suggestion"):
                rows = _execute(
                    conn,
                    """
                    SELECT status, action, reason, review_note, evidence_json
                    FROM policy_suggestion
                    """
                ).fetchall()
                normalized = count_policy_suggestion_statuses([dict(row) for row in rows])["normalized"]
                applied += _safe_int(normalized.get("applied")) + _safe_int(normalized.get("auto_approved"))
                rolled_back += _safe_int(normalized.get("rolled_back"))
                blocked += _safe_int(normalized.get("blocked_by_risk"))
                total += sum(_safe_int(v) for v in normalized.values())
            if state_table_exists(conn, "evolution_decision"):
                rows = _execute(
                    conn,
                    """
                    SELECT d.decision_json,
                           p.risk_verdict_json AS risk_verdict_json
                    FROM evolution_decision d
                    LEFT JOIN mutation_payload p ON p.payload_hash=d.payload_hash
                    """
                ).fetchall()
                for row in rows:
                    total += 1
                    meta = _loads(row["decision_json"], {})
                    status = str(meta.get("status") or "")
                    verdict = _loads(row["risk_verdict_json"], {})
                    allowed = verdict.get("allowed")
                    if status in {"applied", "completed", "accepted"}:
                        applied += 1
                    if status in {"rolled_back", "rollback"}:
                        rolled_back += 1
                    if status == "blocked_by_risk" or allowed is False:
                        blocked += 1
            success = _ratio(applied, total, default=1.0)
            return {
                "governance_action_count": total,
                "applied_or_approved_count": applied,
                "rolled_back_count": rolled_back,
                "blocked_by_risk_count": blocked,
                "action_success_rate": success,
                "rollback_rate": _ratio(rolled_back, total, default=0.0),
                "blocked_by_risk_rate": _ratio(blocked, total, default=0.0),
            }
        finally:
            conn.close()

    def _effect_stats(self) -> dict[str, Any]:
        try:
            from backend.services.learning_application_store import (
                LearningApplicationStore,
            )

            count = 0
            delta_sum = 0.0
            for eff in LearningApplicationStore(self.db_path).iter_effects():
                if int(eff.get("observed_trade_count") or 0) > 0:
                    count += 1
                    delta_sum += _safe_float(eff.get("delta_avg_reward"))
            return {
                "effect_count": count,
                "post_action_reward_delta": (delta_sum / count) if count else 0.0,
            }
        except Exception:
            return {"effect_count": 0, "post_action_reward_delta": 0.0}

    def _evidence_integrity_stats(self) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            from backend.services.canonical_v2_reader import iter_training_sample_rows
            rows = iter_training_sample_rows(
                conn, system_contaminated=0, governance_eligible=1,
                min_governance_weight=0.0, order_by_event_ts=True, limit=500,
            )
            if not rows:
                return {"sample_count": 0, "evidence_integrity": 0.5}
            integrity_weight = {"full": 1.0, "recovered": 0.7, "partial": 0.35, "missing": 0.0}
            total = 0.0
            ready = 0
            for row in rows:
                integrity = str(row["integrity"] or "missing")
                label_status = str(row["label_status"] or "")
                evidence_weight = max(
                    0.0,
                    min(1.0, _safe_float(row["governance_effective_weight"])),
                )
                score = 0.65 * integrity_weight.get(integrity, 0.25) + 0.35 * evidence_weight
                total += score
                if label_status == "matured" and evidence_weight > 0:
                    ready += 1
            return {
                "sample_count": len(rows),
                "ready_sample_count": ready,
                "evidence_integrity": total / len(rows),
            }
        finally:
            conn.close()

    @staticmethod
    def _config_restore_success(stability: dict[str, Any]) -> float:
        snapshot = stability.get("runtime_config_snapshot") or {}
        overlay = stability.get("runtime_config_overlay") or {}
        if not snapshot.get("ok"):
            return 0.0
        if overlay.get("suspicious"):
            return 0.0
        if overlay and overlay.get("ok") is False:
            return 0.5
        return 1.0

    @staticmethod
    def _catalog_freshness(governance: dict[str, Any]) -> float:
        runtime = governance.get("factor_governance_runtime") or {}
        if runtime.get("enabled") is False:
            return 1.0
        if not runtime.get("latest_catalog_snapshot"):
            return 0.0
        age = (runtime.get("latest_catalog_snapshot") or {}).get("age_seconds")
        stale_after = _safe_float(runtime.get("stale_after_seconds"), 7200.0)
        return _freshness_score(age, stale_after)

    @staticmethod
    def _replay_freshness(replay_status: dict[str, Any]) -> float:
        if not replay_status.get("latest_report"):
            return 0.0
        if replay_status.get("status") == "missing_report":
            return 0.0
        latest = replay_status.get("latest_report") or {}
        grade = str(latest.get("evidence_grade") or "")
        if grade in {"failed", "missing"}:
            return 0.0
        base = {"A": 1.0, "B": 0.8, "C": 0.45}.get(grade, 0.25)
        age_score = _freshness_score(replay_status.get("age_seconds"), _safe_float(replay_status.get("stale_after_seconds"), 86400.0))
        return min(base, age_score)

    @staticmethod
    def _shadow_freshness(governance_freshness: dict[str, Any], model_status: dict[str, Any]) -> float:
        tables = governance_freshness.get("tables") or {}
        shadow_tables = [
            "factor_governance_shadow_audit",
            "position_quality_shadow_audit",
            "shadow_factor_perf",
        ]
        scores = []
        for name in shadow_tables:
            item = tables.get(name) or {}
            if item.get("status") == "missing_table":
                continue
            scores.append(1.0 if item.get("status") == "fresh" else 0.0)
        return sum(scores) / len(scores) if scores else 0.5

    @staticmethod
    def _live_loop_stability(live_status: dict[str, Any], system_health: dict[str, Any]) -> float:
        score = _safe_float(system_health.get("score"), 0.0)
        if score > 1.0:
            score = score / 100.0
        loop = live_status.get("loop") or {}
        readiness = live_status.get("readiness") or {}
        loop_running = bool(loop.get("running", loop.get("is_running", False)))
        readiness_ok = bool(readiness.get("ok", readiness.get("ready", True)))
        blocking_components = system_health.get("blocking_components") or []
        if blocking_components:
            return min(score, 0.25)
        if loop_running and readiness_ok:
            return max(score, 0.85)
        return max(0.35, min(score or 0.5, 0.65))
