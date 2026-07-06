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
    state_table_columns,
    state_table_exists,
)


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _status_from_component(component: dict[str, Any], default: str = "unknown") -> str:
    return str(component.get("status") or component.get("overall") or component.get("mode") or default)


def ensure_brain_state_snapshot_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS brain_state_snapshot (
                snapshot_id TEXT PRIMARY KEY,
                schema_version TEXT DEFAULT 'brain_state_snapshot.v1',
                source TEXT DEFAULT '',
                status TEXT DEFAULT 'computed',
                world_model_json TEXT NOT NULL DEFAULT '{}',
                perceptions_json TEXT NOT NULL DEFAULT '{}',
                memory_json TEXT NOT NULL DEFAULT '{}',
                hypotheses_json TEXT NOT NULL DEFAULT '[]',
                critic_json TEXT NOT NULL DEFAULT '{}',
                evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        if "memory_json" not in state_table_columns(conn, "brain_state_snapshot"):
            _execute(conn, "ALTER TABLE brain_state_snapshot ADD COLUMN memory_json TEXT NOT NULL DEFAULT '{}'")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_state_snapshot_created ON brain_state_snapshot(created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_state_snapshot_status ON brain_state_snapshot(status, created_at)")
        conn.commit()
    finally:
        conn.close()


class BrainStateService:
    """V16 Phase 1 read-only brain state builder.

    The service translates existing V15 facts into a world-model snapshot and
    observe-only hypotheses. It never mutates runtime config, weights, orders,
    positions, learning samples, or broker state.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "phase": "v16_phase1_read_only_brain",
            "read_only": True,
            "affects_trading": False,
            "does_not_execute_action_plan": True,
            "does_not_mutate_runtime_overlay": True,
            "does_not_change_factor_weights": True,
            "does_not_write_learning_samples": True,
            "risk_policy_service_required_for_future_actions": True,
            "decision_policy_required_for_future_weight_writes": True,
        }

    def build(
        self,
        *,
        readiness: dict[str, Any],
        persist: bool = True,
        source: str = "brain_state_service",
    ) -> dict[str, Any]:
        now = time.time()
        snapshot_id = f"brain_{uuid.uuid4().hex[:16]}"
        perceptions = self._perceptions(readiness, now=now)
        world_model = self._world_model(perceptions)
        hypotheses = self._hypotheses(perceptions, world_model, now=now)
        memory = self._memory(world_model=world_model, hypotheses=hypotheses)
        hypotheses = self._attach_memory_evidence(hypotheses, memory)
        critic = self._critic(hypotheses, world_model, memory)
        evidence_refs = self._evidence_refs(perceptions, memory)
        snapshot = {
            "ok": True,
            "schema_version": "brain_state_snapshot.v1",
            "snapshot_id": snapshot_id,
            "status": "computed",
            "phase": "v16_phase1_read_only_brain",
            "source": str(source or ""),
            "world_model": world_model,
            "perceptions": perceptions,
            "memory": memory,
            "hypotheses": hypotheses,
            "critic": critic,
            "evidence_refs": evidence_refs,
            "boundary": self.boundary(),
            "created_at": now,
            "read_only": True,
            "affects_trading": False,
        }
        if persist:
            self._persist(snapshot)
        return snapshot

    def latest_snapshot(self) -> dict[str, Any]:
        ensure_brain_state_snapshot_table(self.db_path)
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_state_snapshot"):
                return self._missing_status("missing_table")
            row = _execute(
                conn,
                """
                SELECT snapshot_id, schema_version, source, status, world_model_json,
                       perceptions_json, memory_json, hypotheses_json, critic_json, evidence_refs_json,
                       boundary_json, created_at
                FROM brain_state_snapshot
                ORDER BY created_at DESC
                LIMIT 1
                """,
            ).fetchone()
            if not row:
                return self._missing_status("missing_snapshot")
            return self._row_to_snapshot(row)
        finally:
            conn.close()

    def status(self) -> dict[str, Any]:
        latest = self.latest_snapshot()
        if not latest.get("snapshot_id"):
            return latest
        age_sec = max(0.0, time.time() - _safe_float(latest.get("created_at")))
        posture = latest.get("world_model", {}).get("strategy_posture", "unknown")
        return {
            "ok": True,
            "schema_version": "brain_state_readiness.v1",
            "status": "available",
            "snapshot_id": latest.get("snapshot_id"),
            "age_seconds": round(age_sec, 3),
            "strategy_posture": posture,
            "hypothesis_count": len(latest.get("hypotheses") or []),
            "critic_verdict": latest.get("critic", {}).get("verdict", "unknown"),
            "read_only": True,
            "affects_trading": False,
            "latest_snapshot": latest,
        }

    def _persist(self, snapshot: dict[str, Any]) -> None:
        ensure_brain_state_snapshot_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO brain_state_snapshot
                (snapshot_id, schema_version, source, status, world_model_json,
                 perceptions_json, memory_json, hypotheses_json, critic_json, evidence_refs_json,
                 boundary_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot["snapshot_id"],
                    snapshot["schema_version"],
                    snapshot["source"],
                    snapshot["status"],
                    _dumps(snapshot["world_model"]),
                    _dumps(snapshot["perceptions"]),
                    _dumps(snapshot["memory"]),
                    _dumps(snapshot["hypotheses"]),
                    _dumps(snapshot["critic"]),
                    _dumps(snapshot["evidence_refs"]),
                    _dumps(snapshot["boundary"]),
                    _safe_float(snapshot["created_at"]),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _perceptions(readiness: dict[str, Any], *, now: float) -> dict[str, Any]:
        replay = dict(readiness.get("replay") or {})
        autonomy = dict(readiness.get("autonomy_health") or {})
        incident = dict(readiness.get("incident_control") or {})
        governance = dict(readiness.get("governance") or {})
        freshness = dict(readiness.get("governance_freshness") or {})
        live = dict(readiness.get("live") or {})
        system = dict(readiness.get("system_health") or {})
        release = dict(readiness.get("release") or {})
        return {
            "schema_version": "brain_perception_snapshot.v1",
            "generated_at": now,
            "market": {
                "source": "backend_readiness.market_session",
                "status": _status_from_component(dict(readiness.get("market_session") or {})),
                "freshness": "readiness_snapshot",
            },
            "runtime": {
                "source": "backend_readiness.live",
                "ctrader": dict(live.get("ctrader") or {}),
                "loop": dict(live.get("loop") or {}),
                "system_health": system,
                "freshness": "readiness_snapshot",
            },
            "governance": {
                "source": "backend_readiness.governance",
                "status": _status_from_component(governance, "unknown"),
                "freshness": freshness,
            },
            "replay": {
                "source": "backend_readiness.replay",
                "ok": bool(replay.get("ok")),
                "status": _status_from_component(replay, "unknown"),
                "latest_report": dict(replay.get("latest_report") or replay.get("report") or {}),
            },
            "incident_control": {
                "source": "backend_readiness.incident_control",
                "mode": str(incident.get("mode") or "normal"),
                "readiness_effect": dict(incident.get("readiness_effect") or {}),
            },
            "release": {
                "source": "backend_readiness.release",
                "ok": bool(release.get("ok")),
                "latest_release": dict(release.get("latest_release") or {}),
            },
            "autonomy_health": {
                "source": "backend_readiness.autonomy_health",
                "score": _safe_float(autonomy.get("score")),
                "posture": str(autonomy.get("posture") or "unknown"),
                "blockers": list(autonomy.get("blockers") or []),
            },
            "readiness": {
                "source": "backend_readiness",
                "ready_for_frontend": bool(readiness.get("ready_for_frontend")),
                "blocker_count": len(readiness.get("blockers") or []),
                "known_observation_count": len(readiness.get("known_observations") or []),
            },
        }

    @staticmethod
    def _world_model(perceptions: dict[str, Any]) -> dict[str, Any]:
        incident_mode = str(perceptions.get("incident_control", {}).get("mode") or "normal")
        autonomy_posture = str(perceptions.get("autonomy_health", {}).get("posture") or "unknown")
        replay_ok = bool(perceptions.get("replay", {}).get("ok"))
        runtime_system = perceptions.get("runtime", {}).get("system_health") or {}
        runtime_status = _status_from_component(runtime_system, "unknown")
        blocker_count = int(perceptions.get("readiness", {}).get("blocker_count") or 0)
        strategy_posture = "normal"
        if incident_mode in {"frozen", "only_close"} or autonomy_posture == "frozen":
            strategy_posture = "no_new_risk"
        elif incident_mode in {"shadow_only", "no_new_risk"} or autonomy_posture == "shadow_only":
            strategy_posture = "observation_only"
        elif autonomy_posture == "constrained" or blocker_count > 0 or not replay_ok:
            strategy_posture = "defensive"
        execution_posture = "broker_ok"
        if blocker_count > 0 or runtime_status in {"critical", "degraded"}:
            execution_posture = "degraded"
        if incident_mode in {"frozen", "only_close"}:
            execution_posture = "unsafe"
        governance_freshness = perceptions.get("governance", {}).get("freshness") or {}
        stale_tables = [
            name for name, item in dict(governance_freshness.get("tables") or {}).items()
            if str((item or {}).get("status") or "") not in {"fresh", "ok"}
        ]
        factor_posture = "healthy" if not stale_tables else "unstable"
        learning_posture = "enough_evidence" if replay_ok else "warming_up"
        return {
            "schema_version": "brain_world_model.v1",
            "market_regime": "event_window" if "event" in str(perceptions.get("market", {}).get("status") or "") else "unknown",
            "strategy_posture": strategy_posture,
            "factor_posture": factor_posture,
            "execution_posture": execution_posture,
            "learning_posture": learning_posture,
            "autonomy_posture": autonomy_posture,
            "incident_mode": incident_mode,
            "stale_governance_tables": stale_tables[:10],
            "read_only": True,
        }

    @staticmethod
    def _hypotheses(perceptions: dict[str, Any], world_model: dict[str, Any], *, now: float) -> list[dict[str, Any]]:
        hypotheses: list[dict[str, Any]] = []
        incident_mode = str(world_model.get("incident_mode") or "normal")
        if incident_mode != "normal":
            hypotheses.append(
                BrainStateService._hypothesis(
                    scope="incident",
                    claim=f"runtime incident mode is {incident_mode}; brain should observe only",
                    confidence=0.85,
                    evidence_score=0.78,
                    risk_class="high",
                    evidence_refs={"incident_control": "backend_readiness.incident_control"},
                    now=now,
                )
            )
        if str(world_model.get("autonomy_posture") or "") in {"constrained", "shadow_only", "frozen"}:
            hypotheses.append(
                BrainStateService._hypothesis(
                    scope="autonomy",
                    claim="autonomy health limits current brain action scope",
                    confidence=0.8,
                    evidence_score=max(0.1, _safe_float(perceptions.get("autonomy_health", {}).get("score"))),
                    risk_class="medium",
                    evidence_refs={"autonomy_health": "backend_readiness.autonomy_health"},
                    now=now,
                )
            )
        if not bool(perceptions.get("replay", {}).get("ok")):
            hypotheses.append(
                BrainStateService._hypothesis(
                    scope="simulation",
                    claim="latest replay evidence is missing or unhealthy; high-impact actions must stay blocked",
                    confidence=0.75,
                    evidence_score=0.45,
                    risk_class="medium",
                    evidence_refs={"replay": "backend_readiness.replay"},
                    now=now,
                )
            )
        stale_tables = list(world_model.get("stale_governance_tables") or [])
        if stale_tables:
            hypotheses.append(
                BrainStateService._hypothesis(
                    scope="factor",
                    claim="governance freshness has stale inputs; factor posture should remain cautious",
                    confidence=0.65,
                    evidence_score=0.5,
                    risk_class="low",
                    evidence_refs={"governance_freshness": "backend_readiness.governance_freshness"},
                    now=now,
                )
            )
        if not hypotheses:
            hypotheses.append(
                BrainStateService._hypothesis(
                    scope="runtime",
                    claim="no immediate V16 brain objection found; continue read-only observation",
                    confidence=0.55,
                    evidence_score=0.6,
                    risk_class="low",
                    evidence_refs={"readiness": "backend_readiness"},
                    now=now,
                )
            )
        return hypotheses

    @staticmethod
    def _hypothesis(
        *,
        scope: str,
        claim: str,
        confidence: float,
        evidence_score: float,
        risk_class: str,
        evidence_refs: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        return {
            "hypothesis_id": f"hyp_{uuid.uuid4().hex[:12]}",
            "schema_version": "brain_hypothesis.v1",
            "scope": scope,
            "claim": claim,
            "expected_effect": "read_only_operator_explanation",
            "evidence_refs": evidence_refs,
            "counter_evidence_refs": {},
            "confidence": round(max(0.0, min(float(confidence), 1.0)), 4),
            "evidence_score": round(max(0.0, min(float(evidence_score), 1.0)), 4),
            "risk_class": risk_class,
            "required_validation": ["continue_read_only_observation"],
            "action_scope": "observe_only",
            "expires_at": now + 900.0,
        }

    @staticmethod
    def _attach_memory_evidence(hypotheses: list[dict[str, Any]], memory: dict[str, Any]) -> list[dict[str, Any]]:
        negative_matches = list(memory.get("negative_matches") or [])
        counter_evidence = list(memory.get("counter_evidence") or [])
        source_gaps = list(memory.get("source_gaps") or [])
        enriched = []
        for hypothesis in hypotheses:
            item = dict(hypothesis)
            if counter_evidence:
                refs = dict(item.get("counter_evidence_refs") or {})
                refs["memory"] = [
                    {
                        "memory_id": mem.get("memory_id"),
                        "source_table": mem.get("source_table"),
                        "source_id": mem.get("source_id"),
                        "evidence_score": mem.get("evidence_score"),
                        "similarity_score": mem.get("similarity_score"),
                    }
                    for mem in counter_evidence[:3]
                ]
                item["counter_evidence_refs"] = refs
            if negative_matches:
                refs = dict(item.get("evidence_refs") or {})
                refs["negative_memory"] = [
                    {
                        "memory_id": mem.get("memory_id"),
                        "source_table": mem.get("source_table"),
                        "source_id": mem.get("source_id"),
                        "evidence_score": mem.get("evidence_score"),
                        "similarity_score": mem.get("similarity_score"),
                    }
                    for mem in negative_matches[:3]
                ]
                item["evidence_refs"] = refs
            if source_gaps:
                validation = list(item.get("required_validation") or [])
                validation.append("memory_source_gap_review")
                item["required_validation"] = sorted(set(validation))
            enriched.append(item)
        return enriched

    def _memory(self, *, world_model: dict[str, Any], hypotheses: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            from backend.services.brain_memory import BrainMemoryService

            return BrainMemoryService(self.db_path).retrieve(
                world_model=world_model,
                hypotheses=hypotheses,
                limit=12,
                persist=True,
            )
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "brain_memory_retrieval.v1",
                "items": [],
                "negative_matches": [],
                "counter_evidence": [],
                "source_gaps": ["brain_memory_error"],
                "error": f"{type(exc).__name__}: {exc}",
                "read_only": True,
                "affects_trading": False,
            }

    @staticmethod
    def _critic(hypotheses: list[dict[str, Any]], world_model: dict[str, Any], memory: dict[str, Any]) -> dict[str, Any]:
        objections = []
        verdict = "pass"
        max_scope = "observe_only"
        if str(world_model.get("strategy_posture") or "") in {"defensive", "observation_only", "no_new_risk"}:
            verdict = "shadow_only"
            objections.append("strategy_posture_limits_action_scope")
        if any(float(item.get("evidence_score") or 0.0) < 0.5 for item in hypotheses):
            verdict = "shadow_only"
            objections.append("evidence_score_below_action_threshold")
        if memory.get("negative_matches"):
            verdict = "shadow_only"
            objections.append("negative_memory_match_requires_observation")
        if memory.get("source_gaps"):
            objections.append("memory_sources_incomplete")
        return {
            "schema_version": "brain_critic.v1",
            "verdict": verdict,
            "objections": sorted(set(objections)),
            "missing_evidence": ["v16_counter_evidence_search"] if not memory.get("counter_evidence") else [],
            "required_replay": ["required_before_any_non_observe_action"],
            "max_allowed_action_scope": max_scope,
            "read_only": True,
        }

    @staticmethod
    def _evidence_refs(perceptions: dict[str, Any], memory: dict[str, Any]) -> dict[str, Any]:
        latest_report = perceptions.get("replay", {}).get("latest_report") or {}
        latest_release = perceptions.get("release", {}).get("latest_release") or {}
        return {
            "backend_readiness": {"schema": "backend_readiness.v1"},
            "replay_report": {
                "replay_run_id": str(latest_report.get("replay_run_id") or ""),
                "artifact_hash": str(latest_report.get("artifact_hash") or ""),
            },
            "release_run": {"run_id": str(latest_release.get("run_id") or "")},
            "incident_control": {"mode": str(perceptions.get("incident_control", {}).get("mode") or "normal")},
            "autonomy_health": {
                "posture": str(perceptions.get("autonomy_health", {}).get("posture") or ""),
                "score": _safe_float(perceptions.get("autonomy_health", {}).get("score")),
            },
            "memory": {
                "item_count": len(memory.get("items") or []),
                "negative_match_count": len(memory.get("negative_matches") or []),
                "counter_evidence_count": len(memory.get("counter_evidence") or []),
                "source_gaps": memory.get("source_gaps") or [],
            },
        }

    @staticmethod
    def _missing_status(status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "brain_state_readiness.v1",
            "status": status,
            "read_only": True,
            "affects_trading": False,
            "boundary": BrainStateService.boundary(),
        }

    @staticmethod
    def _row_to_snapshot(row: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "schema_version": str(row["schema_version"] or "brain_state_snapshot.v1"),
            "snapshot_id": str(row["snapshot_id"] or ""),
            "status": str(row["status"] or ""),
            "phase": "v16_phase1_read_only_brain",
            "source": str(row["source"] or ""),
            "world_model": _loads(row["world_model_json"], {}),
            "perceptions": _loads(row["perceptions_json"], {}),
            "memory": _loads(row["memory_json"], {}),
            "hypotheses": _loads(row["hypotheses_json"], []),
            "critic": _loads(row["critic_json"], {}),
            "evidence_refs": _loads(row["evidence_refs_json"], {}),
            "boundary": _loads(row["boundary_json"], BrainStateService.boundary()),
            "created_at": _safe_float(row["created_at"]),
            "read_only": True,
            "affects_trading": False,
        }
