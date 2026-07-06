from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, state_table_exists
from backend.services.brain_action_planner import _connect, _dumps, _execute, _loads, _safe_float
from backend.services.incident_controls import RuntimeIncidentControlService
from risk.policy_service import INCIDENT_MODE_RANK, INCIDENT_MODES, RiskPolicyService


def ensure_brain_live_ready_guardrail_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS brain_live_ready_guardrail (
                guardrail_id TEXT PRIMARY KEY,
                status TEXT DEFAULT '',
                live_capability_lock_json TEXT NOT NULL DEFAULT '{}',
                broker_local_divergence_json TEXT NOT NULL DEFAULT '{}',
                incident_control_json TEXT NOT NULL DEFAULT '{}',
                incident_memory_json TEXT NOT NULL DEFAULT '{}',
                release_rollback_json TEXT NOT NULL DEFAULT '{}',
                p3_p4_evidence_json TEXT NOT NULL DEFAULT '{}',
                action_recommendation_json TEXT NOT NULL DEFAULT '{}',
                risk_precheck_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_live_ready_guardrail_created ON brain_live_ready_guardrail(created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_live_ready_guardrail_status ON brain_live_ready_guardrail(status, created_at)")
        conn.commit()
    finally:
        conn.close()


class BrainLiveReadyGuardrailService:
    """V16 Phase 5 live-ready guardrail evaluator and tightening entry."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "phase": "v16_phase5_live_ready_guardrails",
            "guardrail_only": True,
            "does_not_submit_orders": True,
            "does_not_apply_policy_suggestions": True,
            "does_not_write_learning_samples": True,
            "does_not_relax_incident_mode": True,
            "tightening_requires_explicit_request": True,
            "tightening_uses_incident_control_service": True,
            "tightening_requires_risk_policy": True,
            "runtime_overlay_snapshot_managed_by_incident_control": True,
        }

    def evaluate(
        self,
        *,
        readiness: dict[str, Any] | None = None,
        persist: bool = True,
        source: str = "system:v16_p5_guardrail",
    ) -> dict[str, Any]:
        ensure_brain_live_ready_guardrail_table(self.db_path)
        readiness = dict(readiness or {})
        now = time.time()
        live_lock = self._live_capability_lock(readiness)
        divergence = self._broker_local_divergence(readiness)
        incident = self._incident_control(readiness)
        incident_memory = self._incident_memory()
        release_rollback = self._release_rollback(readiness)
        p3_p4 = self._p3_p4_evidence(readiness)
        recommendation = self._recommendation(
            live_lock=live_lock,
            divergence=divergence,
            incident=incident,
            incident_memory=incident_memory,
            release_rollback=release_rollback,
            p3_p4=p3_p4,
        )
        risk_precheck = RiskPolicyService.shared().evaluate(
            "set_incident_control",
            {
                "current_mode": incident.get("mode", "normal"),
                "target_mode": recommendation.get("target_mode", "no_new_risk"),
                "reason": "v16_live_ready_guardrail_precheck",
            },
        ).to_dict()
        status = "live_ready_locked" if bool(live_lock.get("locked")) else "guardrail_attention_required"
        payload = {
            "guardrail_id": f"brain_p5_guard_{uuid.uuid4().hex[:16]}",
            "schema_version": "brain_live_ready_guardrail.v1",
            "status": status,
            "source": source,
            "live_capability_lock": live_lock,
            "broker_local_divergence": divergence,
            "incident_control": incident,
            "incident_memory": incident_memory,
            "release_rollback": release_rollback,
            "p3_p4_evidence": p3_p4,
            "action_recommendation": recommendation,
            "risk_precheck": risk_precheck,
            "boundary": self.boundary(),
            "created_at": now,
            "updated_at": now,
        }
        if persist:
            self._persist(payload)
        return payload

    def latest_guardrails(self, *, limit: int = 20) -> dict[str, Any]:
        ensure_brain_live_ready_guardrail_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_live_ready_guardrail"):
                return self._missing_status("missing_table")
            rows = _execute(
                conn,
                """
                SELECT guardrail_id, status, live_capability_lock_json,
                       broker_local_divergence_json, incident_control_json,
                       incident_memory_json, release_rollback_json,
                       p3_p4_evidence_json, action_recommendation_json,
                       risk_precheck_json, boundary_json, created_at, updated_at
                FROM brain_live_ready_guardrail
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return {
                "ok": bool(rows),
                "schema_version": "brain_live_ready_guardrail_list.v1",
                "status": "available" if rows else "missing_guardrail",
                "items": [self._row_to_guardrail(row) for row in rows],
                "boundary": self.boundary(),
            }
        finally:
            conn.close()

    def status(self, *, limit: int = 20) -> dict[str, Any]:
        latest = self.latest_guardrails(limit=limit)
        items = list(latest.get("items") or [])
        if not items:
            return {
                "ok": False,
                "schema_version": "brain_live_ready_guardrail_readiness.v1",
                "status": latest.get("status", "missing_guardrail"),
                "item_count": 0,
                "live_ready_guardrails": True,
            }
        item = items[0]
        return {
            "ok": bool(item.get("live_capability_lock", {}).get("locked")),
            "schema_version": "brain_live_ready_guardrail_readiness.v1",
            "status": str(item.get("status") or "available"),
            "item_count": len(items),
            "latest_created_at": _safe_float(item.get("created_at")),
            "live_capability_locked": bool(item.get("live_capability_lock", {}).get("locked")),
            "recommended_mode": str(item.get("action_recommendation", {}).get("target_mode") or ""),
            "divergence_status": str(item.get("broker_local_divergence", {}).get("status") or ""),
            "live_ready_guardrails": True,
        }

    def tighten(
        self,
        *,
        target_mode: str = "no_new_risk",
        reason: str = "",
        actor: str = "api:ops.brain.live_ready_guardrails",
        readiness: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target_mode = str(target_mode or "no_new_risk").strip().lower()
        if target_mode not in INCIDENT_MODES:
            return {"ok": False, "status": "invalid_target_mode", "target_mode": target_mode, "boundary": self.boundary()}
        evaluation = self.evaluate(readiness=readiness, persist=True, source="system:v16_p5_guardrail.tighten_precheck")
        current_mode = str(evaluation.get("incident_control", {}).get("mode") or "normal")
        if INCIDENT_MODE_RANK.get(target_mode, 0) < INCIDENT_MODE_RANK.get(current_mode, 0):
            return {
                "ok": False,
                "schema_version": "brain_live_ready_guardrail_tighten.v1",
                "status": "refused_to_relax_incident_mode",
                "current_mode": current_mode,
                "target_mode": target_mode,
                "evaluation": evaluation,
                "boundary": self.boundary(),
            }
        result = RuntimeIncidentControlService(self.db_path).set_mode(
            target_mode,
            reason=reason or "v16 live-ready guardrail tightening",
            actor=actor,
            confirm_thaw=False,
        )
        return {
            "ok": bool(result.get("ok")),
            "schema_version": "brain_live_ready_guardrail_tighten.v1",
            "status": "tightened" if result.get("ok") else "tighten_blocked",
            "current_mode": current_mode,
            "target_mode": target_mode,
            "evaluation": evaluation,
            "incident_control_result": result,
            "boundary": self.boundary(),
        }

    def _live_capability_lock(self, readiness: dict[str, Any]) -> dict[str, Any]:
        live = dict(readiness.get("live") or {})
        ctrader = dict(live.get("ctrader") or {})
        loop = dict(live.get("loop") or {})
        execution = dict(readiness.get("execution_semantics") or {})
        incident = dict(readiness.get("incident_control") or {})
        release = dict(readiness.get("release") or {})
        replay = dict(readiness.get("replay") or {})
        autonomy = dict(readiness.get("autonomy_health") or {})
        blockers = []
        if str(ctrader.get("status") or "").lower() not in {"connected", "warming_up"}:
            blockers.append("broker_not_connected")
        if not bool(loop.get("running")):
            blockers.append("live_loop_not_running")
        if not bool(live.get("readiness", {}).get("ok", True)):
            blockers.append("live_readiness_not_ok")
        if not bool(execution.get("effective_send_orders", True)):
            blockers.append("send_orders_disabled_or_unknown")
        if str(incident.get("mode") or "normal") != "normal":
            blockers.append("incident_mode_not_normal")
        if not bool(release.get("ok")):
            blockers.append("missing_release_run")
        latest_release = dict(release.get("latest_release") or {})
        if latest_release and not dict(latest_release.get("rollback_ref") or {}).get("snapshot_hash"):
            blockers.append("release_missing_snapshot_rollback_ref")
        if not bool(replay.get("ok")):
            blockers.append("missing_replay_evidence")
        if str(autonomy.get("posture") or "full") not in {"full", "constrained"}:
            blockers.append("autonomy_posture_not_live_ready")
        return {
            "schema_version": "brain_live_capability_lock.v1",
            "locked": not blockers,
            "blockers": blockers,
            "inputs": {
                "broker_status": str(ctrader.get("status") or ""),
                "loop_running": bool(loop.get("running")),
                "effective_send_orders": bool(execution.get("effective_send_orders", True)),
                "incident_mode": str(incident.get("mode") or "normal"),
                "release_ok": bool(release.get("ok")),
                "replay_ok": bool(replay.get("ok")),
                "autonomy_posture": str(autonomy.get("posture") or ""),
            },
        }

    def _broker_local_divergence(self, readiness: dict[str, Any]) -> dict[str, Any]:
        live = dict(readiness.get("live") or {})
        position_payload = dict(live.get("positions") or readiness.get("positions") or {})
        broker_positions = position_payload.get("broker_positions")
        if broker_positions is None:
            broker_positions = position_payload.get("positions")
        broker_count = len(broker_positions) if isinstance(broker_positions, list) else None
        local_count = self._local_open_position_count()
        if broker_count is None:
            return {
                "schema_version": "broker_local_divergence.v1",
                "status": "missing_broker_position_cache",
                "broker_open_count": None,
                "local_open_count": local_count,
                "divergence_count": None,
                "divergence_detected": False,
                "degraded": True,
            }
        divergence = abs(int(broker_count) - int(local_count))
        return {
            "schema_version": "broker_local_divergence.v1",
            "status": "divergent" if divergence else "aligned",
            "broker_open_count": int(broker_count),
            "local_open_count": int(local_count),
            "divergence_count": divergence,
            "divergence_detected": divergence > 0,
            "degraded": False,
        }

    def _incident_control(self, readiness: dict[str, Any]) -> dict[str, Any]:
        incident = dict(readiness.get("incident_control") or RuntimeIncidentControlService(self.db_path).status())
        mode = str(incident.get("mode") or "normal")
        return {
            "schema_version": "brain_incident_control_guardrail.v1",
            "mode": mode,
            "valid_modes": list(incident.get("valid_modes") or sorted(INCIDENT_MODES)),
            "only_close_available": "only_close" in INCIDENT_MODES,
            "no_new_risk_available": "no_new_risk" in INCIDENT_MODES,
            "autonomy_freeze_available": "frozen" in INCIDENT_MODES,
            "readiness_effect": incident.get("readiness_effect") or {},
        }

    def _incident_memory(self) -> dict[str, Any]:
        rows = self._latest_json_rows(
            "incident_playbook_event",
            "event_id",
            "created_at",
            ["event_type", "status", "evidence_refs_json", "notes"],
            limit=5,
        )
        return {
            "schema_version": "incident_memory_guardrail.v1",
            "available": bool(rows),
            "event_count": len(rows),
            "events": rows,
        }

    def _release_rollback(self, readiness: dict[str, Any]) -> dict[str, Any]:
        release = dict(readiness.get("release") or {})
        latest = dict(release.get("latest_release") or {})
        rollback_ref = dict(latest.get("rollback_ref") or {})
        checklist = dict(latest.get("checklist") or {})
        snapshot_hash = str(rollback_ref.get("snapshot_hash") or latest.get("runtime_config_hash") or "")
        return {
            "schema_version": "release_rollback_guardrail.v1",
            "release_available": bool(release.get("ok")),
            "run_id": str(latest.get("run_id") or ""),
            "release_status": str(latest.get("status") or ""),
            "rollback_ref": rollback_ref,
            "snapshot_hash": snapshot_hash,
            "runtime_snapshot_required": bool((checklist.get("boundary") or {}).get("runtime_snapshot_required_for_rollback", True)),
            "rollback_ready": bool(snapshot_hash),
        }

    def _p3_p4_evidence(self, readiness: dict[str, Any]) -> dict[str, Any]:
        v16 = dict(readiness.get("v16") or {})
        p3 = dict(v16.get("low_impact_executions") or readiness.get("brain_low_impact_executions") or {})
        p4 = dict(v16.get("medium_impact_governance") or readiness.get("brain_medium_impact_governance") or {})
        return {
            "schema_version": "brain_p3_p4_guardrail_evidence.v1",
            "p3_available": bool(p3.get("ok")),
            "p3_status": str(p3.get("status") or ""),
            "p3_count": int(p3.get("execution_count") or p3.get("item_count") or 0),
            "p4_available": bool(p4.get("ok")),
            "p4_status": str(p4.get("status") or ""),
            "p4_count": int(p4.get("item_count") or 0),
        }

    @staticmethod
    def _recommendation(
        *,
        live_lock: dict[str, Any],
        divergence: dict[str, Any],
        incident: dict[str, Any],
        incident_memory: dict[str, Any],
        release_rollback: dict[str, Any],
        p3_p4: dict[str, Any],
    ) -> dict[str, Any]:
        reasons = []
        reasons.extend(live_lock.get("blockers") or [])
        if divergence.get("divergence_detected"):
            reasons.append("broker_local_divergence")
        if divergence.get("degraded"):
            reasons.append("missing_broker_divergence_evidence")
        if not incident_memory.get("available"):
            reasons.append("missing_incident_memory")
        if not release_rollback.get("rollback_ready"):
            reasons.append("missing_release_rollback_ref")
        if not p3_p4.get("p3_available"):
            reasons.append("missing_p3_execution_evidence")
        if not p3_p4.get("p4_available"):
            reasons.append("missing_p4_governance_evidence")
        if live_lock.get("locked") and not reasons:
            target_mode = str(incident.get("mode") or "normal")
            action = "observe"
        elif divergence.get("divergence_detected") or not release_rollback.get("rollback_ready"):
            target_mode = "only_close"
            action = "tighten_to_only_close"
        elif "broker_not_connected" in reasons or "autonomy_posture_not_live_ready" in reasons:
            target_mode = "frozen"
            action = "freeze_autonomy"
        else:
            target_mode = "no_new_risk"
            action = "tighten_to_no_new_risk"
        return {
            "schema_version": "brain_live_ready_action_recommendation.v1",
            "action": action,
            "target_mode": target_mode,
            "reasons": sorted(set(str(item) for item in reasons if item)),
            "requires_operator_or_explicit_api": action != "observe",
        }

    def _local_open_position_count(self) -> int:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "position_lifecycle_event"):
                return 0
            row = _execute(
                conn,
                """
                SELECT COUNT(DISTINCT position_id) AS cnt
                FROM position_lifecycle_event
                WHERE event_type IN ('opened', 'open', 'recovered')
                  AND position_id NOT IN (
                    SELECT position_id
                    FROM position_lifecycle_event
                    WHERE event_type IN ('closed', 'close', 'retired', 'failed')
                  )
                """,
            ).fetchone()
            return int(row["cnt"] if row and row["cnt"] is not None else 0)
        except Exception:
            return 0
        finally:
            conn.close()

    def _latest_json_rows(self, table: str, id_col: str, ts_col: str, cols: list[str], *, limit: int = 5) -> list[dict[str, Any]]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, table):
                return []
            select_cols = ", ".join([id_col, ts_col, *cols])
            rows = _execute(conn, f"SELECT {select_cols} FROM {table} ORDER BY {ts_col} DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for row in rows:
                item = {id_col: str(row[id_col] or ""), ts_col: _safe_float(row[ts_col])}
                for col in cols:
                    value = row[col]
                    item[col.replace("_json", "")] = _loads(value, {}) if col.endswith("_json") else value
                out.append(item)
            return out
        finally:
            conn.close()

    def _persist(self, payload: dict[str, Any]) -> None:
        ensure_brain_live_ready_guardrail_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO brain_live_ready_guardrail
                (guardrail_id, status, live_capability_lock_json,
                 broker_local_divergence_json, incident_control_json,
                 incident_memory_json, release_rollback_json, p3_p4_evidence_json,
                 action_recommendation_json, risk_precheck_json, boundary_json,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guardrail_id) DO UPDATE SET
                    status=excluded.status,
                    live_capability_lock_json=excluded.live_capability_lock_json,
                    broker_local_divergence_json=excluded.broker_local_divergence_json,
                    incident_control_json=excluded.incident_control_json,
                    incident_memory_json=excluded.incident_memory_json,
                    release_rollback_json=excluded.release_rollback_json,
                    p3_p4_evidence_json=excluded.p3_p4_evidence_json,
                    action_recommendation_json=excluded.action_recommendation_json,
                    risk_precheck_json=excluded.risk_precheck_json,
                    boundary_json=excluded.boundary_json,
                    updated_at=excluded.updated_at
                """,
                (
                    str(payload.get("guardrail_id") or ""),
                    str(payload.get("status") or ""),
                    _dumps(payload.get("live_capability_lock") or {}),
                    _dumps(payload.get("broker_local_divergence") or {}),
                    _dumps(payload.get("incident_control") or {}),
                    _dumps(payload.get("incident_memory") or {}),
                    _dumps(payload.get("release_rollback") or {}),
                    _dumps(payload.get("p3_p4_evidence") or {}),
                    _dumps(payload.get("action_recommendation") or {}),
                    _dumps(payload.get("risk_precheck") or {}),
                    _dumps(payload.get("boundary") or {}),
                    _safe_float(payload.get("created_at")),
                    _safe_float(payload.get("updated_at")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_guardrail(row: Any) -> dict[str, Any]:
        return {
            "guardrail_id": str(row["guardrail_id"] or ""),
            "schema_version": "brain_live_ready_guardrail.v1",
            "status": str(row["status"] or ""),
            "live_capability_lock": _loads(row["live_capability_lock_json"], {}),
            "broker_local_divergence": _loads(row["broker_local_divergence_json"], {}),
            "incident_control": _loads(row["incident_control_json"], {}),
            "incident_memory": _loads(row["incident_memory_json"], {}),
            "release_rollback": _loads(row["release_rollback_json"], {}),
            "p3_p4_evidence": _loads(row["p3_p4_evidence_json"], {}),
            "action_recommendation": _loads(row["action_recommendation_json"], {}),
            "risk_precheck": _loads(row["risk_precheck_json"], {}),
            "boundary": _loads(row["boundary_json"], BrainLiveReadyGuardrailService.boundary()),
            "created_at": _safe_float(row["created_at"]),
            "updated_at": _safe_float(row["updated_at"]),
        }

    def _missing_status(self, status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "brain_live_ready_guardrail_list.v1",
            "status": status,
            "items": [],
            "boundary": self.boundary(),
        }
