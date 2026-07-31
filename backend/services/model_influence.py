"""Bounded demo decision influence for promoted quantitative models.

Models never execute broker/config mutations here.  This service owns the
small, deterministic fusion envelope between a model score and an existing
rule decision, plus an append-only audit trail in ``state_v1``.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.core.state_store import (
    is_state_schema_write_sql,
    validate_runtime_state_schema,
)


MODEL_STAGES = {"shadow", "demo_canary", "demo_active", "quarantined"}
ACTIVE_STAGES = {"demo_canary", "demo_active"}
MODEL_SURFACES = {
    "open_quality_lightgbm": "open_veto",
    "position_quality_lightgbm": "position_supervision",
    "factor_governance_lightgbm": "factor_weight_candidate",
    "meta_model_lightgbm": "global_risk_cap_candidate",
}


def default_model_influence_config() -> dict[str, Any]:
    return {
        "schema_version": "model_influence_config.v1",
        "models": {
            model_type: {
                "stage": "shadow",
                "artifact_path": "",
                "artifact_sha256": "",
                "feature_schema_version": "",
                "allowed_effects": [],
            }
            for model_type in MODEL_SURFACES
        },
    }


def normalized_model_influence_config(value: Any) -> dict[str, Any]:
    base = default_model_influence_config()
    incoming = dict(value or {}) if isinstance(value, dict) else {}
    base.update({key: value for key, value in incoming.items() if key != "models"})
    models = dict(incoming.get("models") or {})
    for model_type, defaults in base["models"].items():
        merged = dict(defaults)
        merged.update(dict(models.get(model_type) or {}))
        if str(merged.get("stage") or "shadow") not in MODEL_STAGES:
            merged["stage"] = "quarantined"
            merged["stage_reason"] = "invalid_model_stage"
        base["models"][model_type] = merged
    base["schema_version"] = "model_influence_config.v1"
    return base


class ModelInfluenceService:
    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path
        self._ensure_tables()

    def _conn(self):
        conn = get_state_pg_conn() if is_state_db_path(self.db_path) else connect_sqlite(self.db_path)
        if not is_state_db_path(self.db_path):
            conn.row_factory = __import__("sqlite3").Row
        return conn

    @staticmethod
    def _sql(conn: Any, sql: str) -> str:
        return sql.replace("?", "%s") if conn.__class__.__module__.split(".", 1)[0] == "psycopg" else sql

    @classmethod
    def _execute(cls, conn: Any, sql: str, params: tuple[Any, ...] = ()):
        rendered = cls._sql(conn, sql)
        if (
            conn.__class__.__module__.split(".", 1)[0] == "psycopg"
            and is_state_schema_write_sql(rendered)
        ):
            return validate_runtime_state_schema(conn, rendered)
        return conn.execute(rendered, params)

    def _ensure_tables(self) -> None:
        conn = self._conn()
        try:
            self._execute(conn, """
                CREATE TABLE IF NOT EXISTS model_influence_decision (
                    influence_id TEXT PRIMARY KEY,
                    model_type TEXT NOT NULL,
                    model_version TEXT DEFAULT '',
                    artifact_sha256 TEXT DEFAULT '',
                    stage TEXT NOT NULL,
                    control_surface TEXT NOT NULL,
                    subject_id TEXT DEFAULT '',
                    rule_decision_json TEXT NOT NULL DEFAULT '{}',
                    model_result_json TEXT NOT NULL DEFAULT '{}',
                    fused_decision_json TEXT NOT NULL DEFAULT '{}',
                    applied INTEGER NOT NULL DEFAULT 0,
                    reason TEXT DEFAULT '',
                    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                )
            """)
            self._execute(conn, """
                CREATE INDEX IF NOT EXISTS idx_model_influence_decision_model_ts
                ON model_influence_decision(model_type, created_at)
            """)
            self._execute(conn, """
                CREATE INDEX IF NOT EXISTS idx_model_influence_decision_subject_ts
                ON model_influence_decision(subject_id, created_at)
            """)
            self._execute(conn, """
                CREATE TABLE IF NOT EXISTS model_influence_effect (
                    effect_id TEXT PRIMARY KEY,
                    influence_id TEXT NOT NULL,
                    model_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    utility_delta DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    outcome_json TEXT NOT NULL DEFAULT '{}',
                    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    matured_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                )
            """)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def policy_for(model_type: str, cfg: Any) -> dict[str, Any]:
        config = normalized_model_influence_config(getattr(cfg, "model_influence_config", {}) or {})
        return dict((config.get("models") or {}).get(model_type) or {})

    @classmethod
    def active_policy(cls, model_type: str, cfg: Any) -> dict[str, Any] | None:
        if not bool(getattr(cfg, "demo_model_influence_enabled", False)):
            return None
        if str(getattr(cfg, "autonomy_mode", "")) not in {"demo_nursery", "demo_autonomous"}:
            return None
        if str(getattr(cfg, "runtime_incident_mode", "normal")) != "normal":
            return None
        policy = cls.policy_for(model_type, cfg)
        if str(policy.get("stage") or "shadow") not in ACTIVE_STAGES:
            return None
        if not str(policy.get("feature_schema_version") or "").startswith("pit.v2"):
            return None
        return policy

    def audit(
        self,
        *,
        model_type: str,
        policy: dict[str, Any] | None,
        subject_id: str,
        rule_decision: dict[str, Any],
        model_result: dict[str, Any],
        fused_decision: dict[str, Any],
        applied: bool,
        reason: str,
    ) -> dict[str, Any]:
        policy = dict(policy or {})
        now = time.time()
        influence_id = f"mi_{uuid.uuid4().hex}"
        conn = self._conn()
        try:
            self._execute(conn, """
                INSERT INTO model_influence_decision
                (influence_id, model_type, model_version, artifact_sha256, stage,
                 control_surface, subject_id, rule_decision_json, model_result_json,
                 fused_decision_json, applied, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                influence_id, model_type, str(model_result.get("model_version") or ""),
                str(policy.get("artifact_sha256") or ""), str(policy.get("stage") or "shadow"),
                MODEL_SURFACES.get(model_type, "unknown"), str(subject_id or ""),
                json.dumps(rule_decision or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(model_result or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(fused_decision or {}, ensure_ascii=False, sort_keys=True),
                1 if applied else 0, str(reason or ""), now,
            ))
            conn.commit()
        finally:
            conn.close()
        return {"influence_id": influence_id, "applied": bool(applied), "reason": reason, "created_at": now}

    def fuse_position(
        self,
        *,
        verdict: dict[str, Any],
        advisory: dict[str, Any],
        position_id: str,
        cfg: Any,
        tighten_controls: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        original = copy.deepcopy(verdict or {})
        fused = copy.deepcopy(verdict or {})
        model_type = "position_quality_lightgbm"
        policy = self.active_policy(model_type, cfg)
        exit_risk = float(advisory.get("exit_risk_score") or 0.0) if advisory.get("ok") else 0.0
        applied = False
        reason = "model_influence_inactive"
        fused["rule_confidence"] = float(original.get("confidence") or 0.0)
        fused["model_exit_risk"] = exit_risk
        fused["fusion_confidence"] = float(original.get("confidence") or 0.0)
        if policy and advisory.get("ok"):
            action = str(original.get("action") or "hold")
            tighten_threshold = float(policy.get("tighten_threshold") or 0.70)
            reduce_threshold = float(policy.get("reduce_threshold") or 0.88)
            effects = set(policy.get("allowed_effects") or [])
            # The position supervisor owns lifecycle evidence readiness.  A
            # model may advise a risk-reducing action, but it must not bypass
            # the supervisor's observation window or act on unknown position
            # components.  Direct callers without this field retain the
            # historical model-service contract; the live supervisor runtime
            # always supplies it.
            model_action_boundary_ready = bool(
                (original.get("evidence") or {}).get(
                    "model_action_boundary_ready",
                    True,
                )
            )
            if not model_action_boundary_ready:
                reason = "position_supervisor_evidence_boundary"
            elif action == "hold" and exit_risk >= reduce_threshold and "reduce" in effects:
                if not self._position_model_reduce_already_applied(position_id, str(policy.get("artifact_sha256") or "")):
                    fused["action"] = "reduce"
                    controls = dict(fused.get("recommended_controls") or {})
                    controls.update({
                        "reduce_fraction": min(0.25, max(0.0, float(policy.get("max_reduce_fraction") or 0.25))),
                        "close_reason": "position_quality_model_reduce",
                        "protection_mode": "model_partial_de_risk",
                        "allow_full_close_fallback": False,
                    })
                    fused["recommended_controls"] = controls
                    applied, reason = True, "model_exit_risk_reduce"
            elif action == "hold" and exit_risk >= tighten_threshold and "tighten" in effects and tighten_controls:
                fused["action"] = "tighten"
                controls = dict(fused.get("recommended_controls") or {})
                controls.update(dict(tighten_controls))
                controls["close_reason"] = "position_quality_model_tighten"
                controls["protection_mode"] = "model_tightened_stop"
                fused["recommended_controls"] = controls
                applied, reason = True, "model_exit_risk_tighten"
            else:
                reason = "model_did_not_tighten_rule"
        fused["model_influence"] = {
            "schema_version": "model_influence_result.v1",
            "model_type": model_type,
            "stage": str((policy or {}).get("stage") or "shadow"),
            "applied": applied,
            "reason": reason,
            "risk_reducing_only": True,
        }
        # Shadow inference already has its own audit table.  Only promoted
        # influence decisions belong here, otherwise every live tick would
        # duplicate an inactive model record.
        if policy:
            audit = self.audit(
                model_type=model_type, policy=policy, subject_id=position_id,
                rule_decision=original, model_result=advisory, fused_decision=fused,
                applied=applied, reason=reason,
            )
            fused["model_influence"]["influence_id"] = audit["influence_id"]
        return fused

    def _position_model_reduce_already_applied(self, position_id: str, artifact_sha256: str) -> bool:
        conn = self._conn()
        try:
            row = self._execute(conn, """
                SELECT 1 FROM model_influence_decision
                WHERE model_type='position_quality_lightgbm' AND subject_id=?
                  AND artifact_sha256=? AND applied=1 AND reason='model_exit_risk_reduce'
                LIMIT 1
            """, (str(position_id), str(artifact_sha256))).fetchone()
            return row is not None
        finally:
            conn.close()

    def evaluate_open_veto(
        self,
        *,
        score: dict[str, Any],
        subject_id: str,
        cfg: Any,
        rule_decision: dict[str, Any],
    ) -> dict[str, Any]:
        model_type = "open_quality_lightgbm"
        policy = self.active_policy(model_type, cfg)
        quality = float(score.get("quality_score") or 0.0) if score.get("ok") else 1.0
        threshold = float((policy or {}).get("veto_threshold") or 0.25)
        applied = bool(policy and score.get("ok") and quality <= threshold and "veto" in set(policy.get("allowed_effects") or []))
        reason = "model_open_quality_veto" if applied else "model_open_veto_not_applied"
        fused = {"passed": not applied, "reason": reason, "quality_score": quality}
        if not policy:
            return fused
        audit = self.audit(
            model_type=model_type, policy=policy, subject_id=subject_id,
            rule_decision=rule_decision, model_result=score, fused_decision=fused,
            applied=applied, reason=reason,
        )
        return {**fused, "influence_id": audit["influence_id"]}

    def apply_meta_risk_cap(
        self,
        *,
        volume: float,
        subject_id: str,
        cfg: Any,
    ) -> dict[str, Any]:
        """Apply the latest promoted meta-model contraction as a sizing cap.

        The cap can only reduce a volume already approved by the ordinary
        sizing/risk chain.  Stale, missing, non-contract, or mismatched model
        evidence is a no-op.
        """
        model_type = "meta_model_lightgbm"
        policy = self.active_policy(model_type, cfg)
        original = max(0.0, float(volume or 0.0))
        if not policy or "risk_budget_cap" not in set(policy.get("allowed_effects") or []):
            return {"volume": original, "applied": False, "reason": "meta_model_influence_inactive"}
        conn = self._conn()
        try:
            row = self._execute(conn, """
                SELECT inference_id, artifact_path, posture, posture_score,
                       contract_score, observe_score, recover_score, created_at
                FROM meta_model_shadow_audit
                WHERE artifact_path=?
                ORDER BY created_at DESC LIMIT 1
            """, (str(policy.get("artifact_path") or ""),)).fetchone()
        finally:
            conn.close()
        if row is None:
            return {"volume": original, "applied": False, "reason": "meta_model_evidence_missing"}

        def value(key: str, index: int, default: Any = None) -> Any:
            try:
                return row[key]
            except (KeyError, TypeError, IndexError):
                try:
                    return row[index]
                except (TypeError, IndexError):
                    return default

        created_at = float(value("created_at", 7, 0.0) or 0.0)
        max_age = max(60.0, float(policy.get("max_evidence_age_seconds") or 1800.0))
        contract_score = float(value("contract_score", 4, 0.0) or 0.0)
        threshold = float(policy.get("contract_threshold") or 0.60)
        posture = str(value("posture", 2, "") or "")
        eligible = time.time() - created_at <= max_age and posture == "contract" and contract_score >= threshold
        multiplier = min(1.0, max(0.50, float(policy.get("risk_budget_multiplier") or 0.80)))
        capped = original * multiplier if eligible else original
        reason = "meta_model_contract_risk_cap" if eligible else "meta_model_did_not_contract"
        model_result = {
            "inference_id": str(value("inference_id", 0, "") or ""),
            "posture": posture,
            "posture_score": float(value("posture_score", 3, 0.0) or 0.0),
            "contract_score": contract_score,
            "observe_score": float(value("observe_score", 5, 0.0) or 0.0),
            "recover_score": float(value("recover_score", 6, 0.0) or 0.0),
            "created_at": created_at,
        }
        audit = self.audit(
            model_type=model_type,
            policy=policy,
            subject_id=subject_id,
            rule_decision={"approved_volume": original},
            model_result=model_result,
            fused_decision={"approved_volume": capped, "multiplier": multiplier if eligible else 1.0},
            applied=eligible,
            reason=reason,
        )
        return {
            "volume": capped,
            "applied": eligible,
            "reason": reason,
            "multiplier": multiplier if eligible else 1.0,
            "influence_id": audit["influence_id"],
            "model_result": model_result,
        }

    def status(self, cfg: Any) -> dict[str, Any]:
        config = normalized_model_influence_config(getattr(cfg, "model_influence_config", {}) or {})
        conn = self._conn()
        try:
            rows = self._execute(conn, """
                SELECT model_type, COUNT(*) AS decisions,
                       SUM(CASE WHEN applied=1 THEN 1 ELSE 0 END) AS applied,
                       MAX(created_at) AS last_decision_at
                FROM model_influence_decision GROUP BY model_type
            """).fetchall()
            def value(row: Any, key: str, index: int, default: Any = None) -> Any:
                try:
                    return row[key]
                except (KeyError, TypeError, IndexError):
                    try:
                        return row[index]
                    except (TypeError, IndexError):
                        return default

            metrics = {
                str(value(row, "model_type", 0, "")): {
                    "decisions": int(value(row, "decisions", 1, 0) or 0),
                    "applied": int(value(row, "applied", 2, 0) or 0),
                    "last_decision_at": float(value(row, "last_decision_at", 3, 0.0) or 0.0),
                }
                for row in rows
            }
        finally:
            conn.close()
        return {
            "schema_version": "model_influence_status.v1",
            "demo_enabled": bool(getattr(cfg, "demo_model_influence_enabled", False)),
            "models": {
                model_type: {**dict(policy or {}), **metrics.get(model_type, {})}
                for model_type, policy in dict(config.get("models") or {}).items()
            },
        }


_SHARED_MODEL_INFLUENCE_SERVICE: ModelInfluenceService | None = None


def shared_model_influence_service() -> ModelInfluenceService:
    global _SHARED_MODEL_INFLUENCE_SERVICE
    if _SHARED_MODEL_INFLUENCE_SERVICE is None:
        _SHARED_MODEL_INFLUENCE_SERVICE = ModelInfluenceService()
    return _SHARED_MODEL_INFLUENCE_SERVICE
