"""Autonomous factor governance V3.

This orchestrator is the single decision loop for factor lifecycle actions.  It
does not write directional signals and it does not bypass DecisionPolicy for
weights.  Every mutation is gated by RiskPolicyService and recorded in the
evolution ledger plus learning/policy audit tables.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from alpha.decision_policy import DecisionPolicy
from alpha.portfolio_compositor import resolve_factor_role
from backend.core.db import get_state_pg_conn
from backend.services.factor_catalog import build_factor_catalog, persist_factor_catalog_snapshot
from backend.services.factor_redundancy import RedundancyDetector
from backend.services.agent_authority import AgentAuthorityRegistryService
from backend.services.mutation_audit import record_api_mutation
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context
from backend.services.runtime_config_mutation import RuntimeConfigMutationService
from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from config import runtime_config
from config.runtime_config import RuntimeConfig
from risk.policy_service import RiskPolicyService, RiskVerdict

logger = logging.getLogger(__name__)


def _p(sql: str) -> str:
    return sql.replace("?", "%s")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


class FactorGovernanceOrchestrator:
    """Conservative autonomous lifecycle and weight governance."""

    _instance: "FactorGovernanceOrchestrator | None" = None

    def __init__(self, risk_policy: RiskPolicyService | None = None):
        self.risk_policy = risk_policy or RiskPolicyService.shared()
        self.overlay = RuntimeConfigOverlayService()

    @classmethod
    def shared(cls) -> "FactorGovernanceOrchestrator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def run_cycle(self, *, trigger_source: str = "scheduled") -> dict[str, Any]:
        cfg = runtime_config.shared()
        if not bool(getattr(cfg, "factor_governance_enabled", True)):
            return {"status": "disabled", "actions": []}

        from backend.services.evolution_ledger import (
            finish_evolution_run,
            start_evolution_run,
        )

        run = start_evolution_run(
            run_type="factor_governance_autonomous",
            trigger_source=trigger_source,
            config=cfg,
            summary={"version": "factor_governance.v3"},
        )
        actions: list[dict[str, Any]] = []
        status = "completed"
        try:
            catalog = build_factor_catalog()
            actions.extend(self._rollback_failed_actions(run))
            catalog_snapshot = persist_factor_catalog_snapshot(
                catalog,
                run_id=str(run.get("run_id") or ""),
                source="factor_governance_cycle",
            )
            actions.extend(self._rollback_canary_regressions(catalog, run))
            if actions:
                catalog = build_factor_catalog()
            posture = self._autonomy_posture()
            if posture in {"shadow_only", "frozen"}:
                summary = {
                    "status": "observation_only",
                    "reason": f"autonomy_posture:{posture}",
                    "catalog_count": len(catalog),
                    "actions": actions,
                    "catalog_snapshot": catalog_snapshot,
                    "redundancy_report": {},
                }
                finish_evolution_run(run["run_id"], status=status, summary=summary)
                return summary
            cfg = runtime_config.shared()
            redundancy_report = RedundancyDetector().build_report(
                catalog,
                min_samples=int(getattr(cfg, "factor_redundancy_min_samples", 200) or 200),
                corr_threshold=float(getattr(cfg, "factor_redundancy_corr_threshold", 0.85) or 0.85),
            )
            actions.extend(self._apply_redundancy_report(catalog, redundancy_report, run))
            if redundancy_report.get("group_count"):
                catalog = build_factor_catalog()
            actions.extend(self._promote_shadow_candidates(catalog, run))
            catalog = build_factor_catalog()
            actions.extend(self._apply_parameter_template_actions(catalog, run))
            catalog = build_factor_catalog()
            actions.extend(self._downweight_weak_alpha(catalog, run))
            catalog = build_factor_catalog()
            actions.extend(self._disable_weak_live_alpha(catalog, run))
            catalog = build_factor_catalog()
            actions.extend(self._retire_quarantined_discovered(catalog, run))
            summary = {
                "status": "ok",
                "catalog_count": len(catalog),
                "actions": actions,
                "catalog_snapshot": catalog_snapshot,
                "redundancy_report": redundancy_report,
            }
            finish_evolution_run(run["run_id"], status=status, summary=summary)
            return summary
        except Exception as exc:
            status = "failed"
            logger.exception("[factor_governance] cycle failed")
            finish_evolution_run(
                run["run_id"],
                status=status,
                summary={"status": status, "error": str(exc), "actions": actions},
            )
            return {"status": status, "error": str(exc), "actions": actions}

    # ── Action selection ────────────────────────────────────────────

    @staticmethod
    def _autonomy_posture() -> str:
        try:
            from backend.services.autonomy_health import AutonomyHealthService

            return str(AutonomyHealthService().latest_snapshot().get("posture") or "unknown")
        except Exception:
            return "unknown"

    @staticmethod
    def _factor_has_pending_effect(factor_id: str) -> bool:
        if not factor_id:
            return False
        try:
            conn = get_state_pg_conn(read_only=True)
            try:
                row = conn.execute(
                    _p("""
                    SELECT l.status AS application_status, e.status AS effect_status
                    FROM learning_application_log l
                    LEFT JOIN learning_application_effect e
                      ON e.application_id=l.application_id
                    WHERE l.scope_type='factor'
                      AND l.scope_key=?
                    ORDER BY l.created_at DESC
                    LIMIT 1
                    """),
                    (factor_id,),
                ).fetchone()
                if row is None:
                    return False
                effect_status = str(row["effect_status"] or "")
                return (
                    str(row["application_status"] or "") == "observing"
                    or effect_status in {"", "observing", "mixed"}
                )
            finally:
                conn.close()
        except Exception:
            return False

    @staticmethod
    def _scoped_factor_rollback_patch(
        factor_id: str,
        rollback_cfg: dict[str, Any],
        current_cfg: dict[str, Any],
    ) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if "factor_signal_config" in rollback_cfg:
            current_signal = dict(current_cfg.get("factor_signal_config") or {})
            rollback_signal = dict(rollback_cfg.get("factor_signal_config") or {})
            if factor_id in rollback_signal:
                current_signal[factor_id] = dict(rollback_signal[factor_id] or {})
            else:
                current_signal.pop(factor_id, None)
            patch["factor_signal_config"] = current_signal
        if "factor_portfolio_weights" in rollback_cfg:
            current_weights = dict(current_cfg.get("factor_portfolio_weights") or {})
            rollback_weights = dict(rollback_cfg.get("factor_portfolio_weights") or {})
            if factor_id in rollback_weights:
                current_weights[factor_id] = float(rollback_weights[factor_id] or 0.0)
            else:
                current_weights.pop(factor_id, None)
            patch["factor_portfolio_weights"] = current_weights
        return patch

    def _apply_runtime_patch(self, patch: dict[str, Any], *, source: str, run_id: str) -> dict[str, Any]:
        return RuntimeConfigMutationService(overlay=self.overlay).apply_patch(
            patch,
            source=source,
            run_id=run_id,
            actor="system:factor_governance",
            action=source,
            audit=False,
        )

    def _rollback_failed_actions(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        cfg = runtime_config.shared()
        min_trades = int(getattr(cfg, "factor_governance_rollback_min_trades", 3) or 3)
        delta_threshold = float(getattr(cfg, "factor_governance_rollback_delta_threshold", -0.15) or -0.15)
        conn = get_state_pg_conn()
        try:
            rows = conn.execute(
                _p("""
                SELECT l.application_id, l.scope_key, l.action, l.suggestion_ids_json,
                       l.details_json, e.observed_trade_count, e.delta_avg_reward
                FROM learning_application_log l
                JOIN learning_application_effect e ON e.application_id = l.application_id
                WHERE l.scope_type='factor'
                  AND l.status IN ('applied','observing','ineffective')
                  AND e.status IN ('observing','applied','ineffective')
                  AND e.observed_trade_count >= ?
                  AND e.delta_avg_reward <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM learning_application_log newer
                      WHERE newer.scope_type='factor'
                        AND newer.scope_key=l.scope_key
                        AND newer.created_at > l.created_at
                        AND newer.status NOT IN ('rolled_back','superseded')
                  )
                ORDER BY e.updated_at DESC, l.created_at DESC
                LIMIT 5
                """),
                (min_trades, delta_threshold),
            ).fetchall()
            for row in rows:
                factor_id = str(row["scope_key"] or "")
                decision_id = self._decision_id_from_application(row)
                rollback_payload = self._rollback_payload_for_decision(decision_id)
                rollback_cfg = rollback_payload.get("runtime_config") if isinstance(rollback_payload, dict) else None
                item = {"factor_id": factor_id, "role": "alpha", "source": "autonomous"}
                evidence = {
                    "application_id": str(row["application_id"] or ""),
                    "decision_id": decision_id,
                    "observed_trade_count": int(row["observed_trade_count"] or 0),
                    "delta_avg_reward": float(row["delta_avg_reward"] or 0.0),
                    "action": str(row["action"] or ""),
                }
                verdict = self._risk("rollback_factor_action", item, evidence)
                if not verdict.allowed or not isinstance(rollback_cfg, dict):
                    actions.append(self._audit_action(
                        run,
                        item,
                        "rollback_factor_action",
                        "blocked_by_risk" if not verdict.allowed else "superseded",
                        evidence,
                        verdict,
                        result={"reason": "risk_blocked" if not verdict.allowed else "missing_rollback_config"},
                    ))
                    continue
                before_cfg = runtime_config.shared().to_dict()
                rollback_patch = self._scoped_factor_rollback_patch(factor_id, rollback_cfg, before_cfg)
                if not rollback_patch:
                    actions.append(self._audit_action(
                        run,
                        item,
                        "rollback_factor_action",
                        "superseded",
                        evidence,
                        verdict,
                        result={"reason": "missing_factor_scoped_rollback_config"},
                    ))
                    continue
                self._apply_runtime_patch(
                    rollback_patch,
                    source="factor_governance_auto_rollback",
                    run_id=str(run.get("run_id") or ""),
                )
                self._mark_application_rolled_back(
                    application_id=str(row["application_id"] or ""),
                    suggestion_ids=self._loads_list(row["suggestion_ids_json"]),
                    decision={"decision_id": decision_id, **evidence},
                )
                actions.append(self._audit_action(
                    run,
                    item,
                    "rollback_factor_action",
                    "rolled_back",
                    evidence,
                    verdict,
                    before={"runtime_config": before_cfg},
                    after={"runtime_config": runtime_config.shared().to_dict()},
                    rollback=rollback_payload,
                    result={"restored": True},
                ))
        except Exception as exc:
            logger.debug("[factor_governance] rollback scan skipped: %s", exc)
        finally:
            conn.close()
        return actions

    def _decision_id_from_application(self, row: Any) -> str:
        details = self._loads_dict(row["details_json"])
        decision_id = str(details.get("decision_id") or "")
        if decision_id:
            return decision_id
        suggestion_ids = self._loads_list(row["suggestion_ids_json"])
        if not suggestion_ids:
            return ""
        conn = get_state_pg_conn(read_only=True)
        try:
            record = conn.execute(
                _p("SELECT evidence_json FROM policy_suggestion WHERE suggestion_id=? LIMIT 1"),
                (suggestion_ids[0],),
            ).fetchone()
            evidence = self._loads_dict(record["evidence_json"] if record else "{}")
            return str(evidence.get("decision_id") or "")
        finally:
            conn.close()

    def _rollback_payload_for_decision(self, decision_id: str) -> dict[str, Any]:
        if not decision_id:
            return {}
        conn = get_state_pg_conn(read_only=True)
        try:
            row = conn.execute(
                _p("SELECT rollback_json FROM evolution_decision WHERE decision_id=? LIMIT 1"),
                (decision_id,),
            ).fetchone()
            return self._loads_dict(row["rollback_json"] if row else "{}")
        finally:
            conn.close()

    def _mark_application_rolled_back(self, *, application_id: str, suggestion_ids: list[str], decision: dict[str, Any]) -> None:
        now = time.time()
        conn = get_state_pg_conn()
        try:
            conn.execute(
                _p("UPDATE learning_application_log SET status='rolled_back' WHERE application_id=?"),
                (application_id,),
            )
            conn.execute(
                _p("""
                UPDATE learning_application_effect
                SET status='rolled_back', decision_json=?, updated_at=?
                WHERE application_id=?
                """),
                (_dumps(decision), now, application_id),
            )
            for suggestion_id in suggestion_ids:
                conn.execute(
                    _p("""
                    UPDATE policy_suggestion
                    SET status='rolled_back', reviewed_at=?, review_note=?
                    WHERE suggestion_id=?
                    """),
                    (now, "auto rollback by factor governance posterior effect", suggestion_id),
                )
            conn.commit()
        finally:
            conn.close()

    def _apply_redundancy_report(
        self,
        catalog: list[dict[str, Any]],
        report: dict[str, Any],
        run: dict[str, Any],
    ) -> list[dict[str, Any]]:
        groups = list(report.get("groups") or [])
        if not groups:
            return []
        before_cfg = runtime_config.shared().to_dict()
        signal_cfg = dict(runtime_config.shared().factor_signal_config or {})
        signal_patch: dict[str, dict[str, Any]] = {}
        for group in groups:
            group_id = str(group.get("group_id") or "")
            leader = str(group.get("leader") or "")
            for member in group.get("members") or []:
                entry = dict(signal_cfg.get(member, {}) or {})
                entry["redundancy_group"] = group_id
                entry["redundancy_leader"] = leader
                signal_cfg[member] = entry
                signal_patch[member] = entry
        result = self._apply_runtime_patch(
            {"factor_signal_config": signal_patch},
            source="factor_governance_redundancy",
            run_id=str(run.get("run_id") or ""),
        )
        item = {"factor_id": "redundancy", "role": "alpha", "source": "catalog"}
        verdict = RiskVerdict(allowed=True, reason="ok", audit_payload={"action": "update_weight"})
        return [self._audit_action(
            run,
            item,
            "update_redundancy_groups",
            "applied",
            report,
            verdict,
            before={"runtime_config": before_cfg},
            after={"runtime_config": runtime_config.shared().to_dict()},
            rollback={"runtime_config": before_cfg},
            result=result,
        )]

    def _apply_parameter_template_actions(self, catalog: list[dict[str, Any]], run: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        try:
            from backend.services.parameter_templates import ParameterTemplateService

            service = ParameterTemplateService()
            recommendations = service.list_recommendations(limit=200)
        except Exception as exc:
            logger.debug("[factor_governance] parameter template recommendations unavailable: %s", exc)
            return actions

        by_factor = {str(item.get("factor_id") or ""): item for item in catalog}
        for rec in recommendations:
            factor_id = str(rec.get("factor_id") or "")
            if not factor_id or factor_id not in by_factor:
                continue
            boundary = rec.get("boundary") or {}
            scope = str(boundary.get("recommended_scope") or "")
            target_template_id = str(rec.get("target_template_id") or "")
            if not target_template_id:
                continue
            evidence = {
                "recommendation_id": rec.get("recommendation_id"),
                "factor_id": factor_id,
                "target_template_id": target_template_id,
                "boundary": boundary,
            }
            item = by_factor[factor_id]
            if scope == "online_light":
                verdict = self._risk("switch_parameter_template", item, evidence)
                if not verdict.allowed:
                    actions.append(self._audit_action(run, item, "switch_parameter_template", "blocked_by_risk", evidence, verdict))
                    continue
                before_cfg = runtime_config.shared().to_dict()
                current = service.get_active_template(
                    factor_id=factor_id,
                    regime_key=str(rec.get("regime_key") or ""),
                ) or {}
                if str(current.get("template_id") or "") == target_template_id:
                    continue
                result = service.activate_template(
                    factor_id=factor_id,
                    template_id=target_template_id,
                    regime_key=str(rec.get("regime_key") or ""),
                    suggestion_id=str(rec.get("suggestion_id") or ""),
                    note="autonomous factor governance online_light switch",
                )
                service.sync_runtime_config()
                after_cfg = runtime_config.shared().to_dict()
                self._apply_runtime_patch(
                    {
                        "factor_signal_config": after_cfg.get("factor_signal_config", {}),
                        "extra": after_cfg.get("extra", {}),
                    },
                    source="factor_governance_parameter_template",
                    run_id=str(run.get("run_id") or ""),
                )
                actions.append(self._audit_action(
                    run,
                    item,
                    "switch_parameter_template",
                    "applied" if not result.get("blocked") else "blocked_by_risk",
                    evidence,
                    verdict,
                    before={"runtime_config": before_cfg},
                    after={"runtime_config": runtime_config.shared().to_dict()},
                    rollback={"runtime_config": before_cfg},
                    result=result,
                ))
            elif scope == "offline_deep":
                result = self._submit_offline_template_validation(rec)
                if result:
                    actions.append(self._audit_action(
                        run,
                        item,
                        "submit_parameter_template_validation",
                        "applied",
                        evidence,
                        RiskVerdict(allowed=True, reason="ok"),
                        result=result,
                    ))
        return actions

    def _submit_offline_template_validation(self, rec: dict[str, Any]) -> dict[str, Any]:
        try:
            from backend.jobs.manager import get_job_manager
            from backend.services.parameter_template_validation import run_parameter_template_offline_validation

            params = {
                "factor_id": str(rec.get("factor_id") or ""),
                "template_id": str(rec.get("target_template_id") or ""),
                "recommendation_context": {
                    "source": "factor_governance_orchestrator",
                    "recommendation_id": str(rec.get("recommendation_id") or ""),
                },
            }
            fn = lambda cb, _params=params: run_parameter_template_offline_validation(_params, cb)
            job = get_job_manager().submit("parameter_template_validation", params, fn)
            return {"job_id": getattr(job, "job_id", "") or getattr(job, "id", ""), "params": params}
        except Exception as exc:
            return {"blocked": True, "reason": f"offline_validation_submit_failed:{exc}"}

    def _promote_shadow_candidates(self, catalog: list[dict[str, Any]], run: dict[str, Any]) -> list[dict[str, Any]]:
        cfg = runtime_config.shared()
        max_actions = int(getattr(cfg, "factor_governance_max_promotions_per_cycle", 1) or 1)
        actions: list[dict[str, Any]] = []
        candidates = [item for item in catalog if item.get("source") == "shadow"]
        candidates.sort(key=lambda item: self._shadow_score(item), reverse=True)
        for item in candidates:
            if len(actions) >= max_actions:
                break
            if self._factor_has_pending_effect(str(item.get("factor_id") or "")):
                continue
            evidence = self._promotion_evidence(item, cfg)
            if not evidence["eligible"]:
                continue
            verdict = self._risk("promote_factor", item, evidence)
            if not verdict.allowed:
                actions.append(self._audit_action(run, item, "promote_factor", "blocked_by_risk", evidence, verdict))
                continue
            before_cfg = runtime_config.shared().to_dict()
            before = {"source": item.get("source"), "runtime_config": before_cfg}
            try:
                from alpha.registry_adapter import RegistryAdapter, SOURCE_DISCOVERED

                adapter = RegistryAdapter.shared()
                promoted = adapter.promote(
                    str(item["factor_id"]),
                    SOURCE_DISCOVERED,
                    reason="autonomous_governance_v3",
                )
                self._ensure_promoted_runtime_config(str(item["factor_id"]), run_id=str(run.get("run_id") or ""))
                after_cfg = runtime_config.shared().to_dict()
                status = "applied" if promoted else "superseded"
                actions.append(self._audit_action(
                    run,
                    item,
                    "promote_factor",
                    status,
                    evidence,
                    verdict,
                    before=before,
                    after={"source": SOURCE_DISCOVERED, "runtime_config": after_cfg},
                    rollback={"runtime_config": before_cfg},
                    result={"promoted": bool(promoted)},
                ))
            except Exception as exc:
                runtime_config.replace(RuntimeConfig.from_dict(before_cfg))
                actions.append(self._audit_action(
                    run,
                    item,
                    "rollback_factor_action",
                    "rolled_back",
                    {**evidence, "error": str(exc)},
                    verdict,
                    before=before,
                    after={"runtime_config": runtime_config.shared().to_dict()},
                    rollback={"runtime_config": before_cfg},
                    result={"error": str(exc)},
                ))
        return actions

    def _rollback_canary_regressions(
        self,
        catalog: list[dict[str, Any]],
        run: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Apply explicit Canary regressions through the sole lifecycle executor.

        Missing canary state is deliberately ignored so legacy discovered
        factors are not mass-demoted.  A persisted, non-ACTIVE stage is an
        explicit safety signal produced by the evolution evidence loop.
        """

        regression_stages = {
            "SHADOW", "CANARY_5", "CANARY_20", "CANARY_50", "PROBATION", "QUARANTINED"
        }
        candidates = [
            item
            for item in catalog
            if item.get("source") == "discovered"
            and str((item.get("canary") or {}).get("stage") or "").upper() in regression_stages
        ]
        actions: list[dict[str, Any]] = []
        for item in candidates:
            factor_id = str(item.get("factor_id") or "")
            stage = str((item.get("canary") or {}).get("stage") or "").upper()
            evidence = {
                "canary_stage": stage,
                "reason": "persisted_canary_regression",
                "source": item.get("source"),
            }
            verdict = self._risk("rollback_factor_action", item, evidence)
            if not verdict.allowed:
                actions.append(self._audit_action(
                    run, item, "rollback_factor_action", "blocked_by_risk", evidence, verdict
                ))
                continue
            before_cfg = runtime_config.shared().to_dict()
            try:
                from alpha.registry_adapter import RegistryAdapter, SOURCE_SHADOW

                rolled_back = RegistryAdapter.shared().promote(
                    factor_id,
                    new_source=SOURCE_SHADOW,
                    reason="canary_regression_via_factor_governance",
                )
                existing = dict(runtime_config.shared().factor_signal_config.get(factor_id, {}) or {})
                existing.update({
                    "enabled": False,
                    "source": "shadow",
                    "lifecycle_status": "QUARANTINE",
                })
                self._apply_runtime_patch(
                    {"factor_signal_config": {factor_id: existing}},
                    source="factor_governance_canary_rollback",
                    run_id=str(run.get("run_id") or ""),
                )
                actions.append(self._audit_action(
                    run,
                    item,
                    "rollback_factor_action",
                    "applied" if rolled_back else "superseded",
                    evidence,
                    verdict,
                    before={"source": "discovered", "runtime_config": before_cfg},
                    after={"source": "shadow", "runtime_config": runtime_config.shared().to_dict()},
                    rollback={"runtime_config": before_cfg},
                    result={"rolled_back": bool(rolled_back)},
                ))
            except Exception as exc:
                logger.exception("[factor_governance] canary rollback failed for %s", factor_id)
                actions.append(self._audit_action(
                    run,
                    item,
                    "rollback_factor_action",
                    "failed",
                    {**evidence, "error": str(exc)},
                    verdict,
                    before={"runtime_config": before_cfg},
                    after={"runtime_config": runtime_config.shared().to_dict()},
                    result={"error": str(exc)},
                ))
        return actions

    def _downweight_weak_alpha(self, catalog: list[dict[str, Any]], run: dict[str, Any]) -> list[dict[str, Any]]:
        cfg = runtime_config.shared()
        watch = float(getattr(cfg, "factor_health_watch_threshold", 40.0) or 40.0)
        max_delta = float(getattr(cfg, "awe_max_single_change", 0.15) or 0.15)
        current_weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
        factor_configs = self._portfolio_configs(cfg)
        patches: dict[str, dict[str, Any]] = {}
        evidence_by_factor: dict[str, dict[str, Any]] = {}
        for item in catalog:
            if not item.get("used_in_score"):
                continue
            score = float(item.get("health_score") or 0.0)
            status = str(item.get("health_status") or "")
            model_evidence = self._model_governance_evidence(item, cfg)
            health_weak = score > 0 and (score < watch or status == "DECAYING")
            model_weak = bool(model_evidence.get("weak_for_downweight"))
            if not health_weak and not model_weak:
                continue
            name = str(item["factor_id"])
            if self._factor_has_pending_effect(name):
                continue
            old_w = float(current_weights.get(name, item.get("weight", 0.0)) or 0.0)
            if old_w <= 0:
                continue
            target = max(0.0, old_w * (1.0 - max_delta))
            reason = (
                "autonomous_model_weakness_downweight"
                if model_weak and not health_weak
                else "autonomous_health_downweight"
            )
            patches[name] = {"weight": target, "reason": reason}
            evidence_by_factor[name] = {
                "health_score": score,
                "health_status": status,
                "health_weak": health_weak,
                "model_governance": model_evidence,
                "old_weight": old_w,
                "target_weight": target,
                "max_single_change": max_delta,
            }
        if not patches:
            return []

        allowed_patches: dict[str, dict[str, Any]] = {}
        actions: list[dict[str, Any]] = []
        verdicts: dict[str, RiskVerdict] = {}
        for name, patch in patches.items():
            item = next(item for item in catalog if item["factor_id"] == name)
            verdict = self._risk("update_weight", item, evidence_by_factor[name])
            verdicts[name] = verdict
            if verdict.allowed:
                allowed_patches[name] = patch
            else:
                actions.append(self._audit_action(run, item, "update_weight", "blocked_by_risk", evidence_by_factor[name], verdict))
        if not allowed_patches:
            return actions

        before_cfg = runtime_config.shared().to_dict()
        dp = DecisionPolicy(
            redundancy_max_group_weight=float(getattr(cfg, "factor_redundancy_max_group_weight", 0.35) or 0.35)
        )
        decisions = dp.fast_decide(
            awe_patches=allowed_patches,
            weight_policy_weights=None,
            factor_configs=factor_configs,
            current_weights=current_weights,
        )
        # DecisionPolicy may clamp an already-minimal weight back to its
        # current value. Treat that as a no-op: writing/auditing it every
        # governance tick creates duplicate proposals and makes effect
        # attribution impossible without changing any runtime behavior.
        decisions = {
            name: decision
            for name, decision in decisions.items()
            if abs(float(decision.new_weight) - float(decision.old_weight)) > 1e-9
        }
        partial = DecisionPolicy.to_weights(decisions)
        if not partial:
            return actions
        self._apply_runtime_patch(
            {"factor_portfolio_weights": partial},
            source="factor_governance_update_weight",
            run_id=str(run.get("run_id") or ""),
        )
        after_cfg = runtime_config.shared().to_dict()
        for name, decision in decisions.items():
            item = next(item for item in catalog if item["factor_id"] == name)
            actions.append(self._audit_action(
                run,
                item,
                "update_weight",
                "applied",
                {**evidence_by_factor.get(name, {}), "decision": decision.to_api()},
                verdicts[name],
                before={"runtime_config": before_cfg, "weight": decision.old_weight},
                after={"runtime_config": after_cfg, "weight": decision.new_weight},
                rollback={"runtime_config": before_cfg},
            ))
        return actions

    def _disable_weak_live_alpha(self, catalog: list[dict[str, Any]], run: dict[str, Any]) -> list[dict[str, Any]]:
        cfg = runtime_config.shared()
        severe = float(getattr(cfg, "retire_severe_threshold", 30.0) or 30.0)
        max_actions = int(getattr(cfg, "factor_governance_max_disables_per_cycle", 1) or 1)
        weak = []
        for item in catalog:
            if not item.get("eligible_for_live") or item.get("role") != "alpha":
                continue
            if self._factor_has_pending_effect(str(item.get("factor_id") or "")):
                continue
            score = float(item.get("health_score") or 0.0)
            model_evidence = self._model_governance_evidence(item, cfg)
            if (score > 0.0 and score < severe) or bool(model_evidence.get("weak_for_disable")):
                weak.append(item)
        weak.sort(key=lambda item: (
            float(item.get("health_score") or 999.0),
            -float((item.get("factor_governance_shadow") or {}).get("avg_weakness_score") or item.get("model_weakness_score") or 0.0),
        ))
        actions: list[dict[str, Any]] = []
        for item in weak[:max_actions]:
            model_evidence = self._model_governance_evidence(item, cfg)
            evidence = {
                "health_score": float(item.get("health_score") or 0.0),
                "health_status": item.get("health_status"),
                "threshold": severe,
                "model_governance": model_evidence,
            }
            verdict = self._risk("disable_factor_live", item, evidence)
            if not verdict.allowed:
                actions.append(self._audit_action(run, item, "disable_factor_live", "blocked_by_risk", evidence, verdict))
                continue
            before_cfg = runtime_config.shared().to_dict()
            name = str(item["factor_id"])
            entry = dict(runtime_config.shared().factor_signal_config.get(name, {}) or {})
            entry["enabled"] = False
            entry["lifecycle_status"] = entry.get("lifecycle_status", "QUARANTINE")
            self._apply_runtime_patch(
                {"factor_signal_config": {name: entry}},
                source="factor_governance_disable_live",
                run_id=str(run.get("run_id") or ""),
            )
            after_cfg = runtime_config.shared().to_dict()
            actions.append(self._audit_action(
                run,
                item,
                "disable_factor_live",
                "applied",
                evidence,
                verdict,
                before={"runtime_config": before_cfg, "enabled": True},
                after={"runtime_config": after_cfg, "enabled": False},
                rollback={"runtime_config": before_cfg},
            ))
        return actions

    def _retire_quarantined_discovered(self, catalog: list[dict[str, Any]], run: dict[str, Any]) -> list[dict[str, Any]]:
        cfg = runtime_config.shared()
        severe = float(getattr(cfg, "retire_severe_threshold", 30.0) or 30.0)
        max_actions = int(getattr(cfg, "factor_governance_max_retires_per_cycle", 1) or 1)
        candidates = []
        for item in catalog:
            if item.get("source") != "discovered" or item.get("enabled"):
                continue
            if self._factor_has_pending_effect(str(item.get("factor_id") or "")):
                continue
            score = float(item.get("health_score") or 0.0)
            model_evidence = self._model_governance_evidence(item, cfg)
            if (score > 0.0 and score < severe) or bool(model_evidence.get("weak_for_disable")):
                candidates.append(item)
        candidates.sort(key=lambda item: (
            float(item.get("health_score") or 999.0),
            -float((item.get("factor_governance_shadow") or {}).get("avg_weakness_score") or item.get("model_weakness_score") or 0.0),
        ))
        actions: list[dict[str, Any]] = []
        for item in candidates[:max_actions]:
            model_evidence = self._model_governance_evidence(item, cfg)
            evidence = {
                "health_score": float(item.get("health_score") or 0.0),
                "health_status": item.get("health_status"),
                "source": item.get("source"),
                "enabled": item.get("enabled"),
                "model_governance": model_evidence,
            }
            verdict = self._risk("retire_factor", item, evidence)
            if not verdict.allowed:
                actions.append(self._audit_action(run, item, "retire_factor", "blocked_by_risk", evidence, verdict))
                continue
            before_cfg = runtime_config.shared().to_dict()
            from alpha.registry_adapter import RegistryAdapter

            retired = RegistryAdapter.shared().retire(str(item["factor_id"]), reason="autonomous_governance_v3")
            actions.append(self._audit_action(
                run,
                item,
                "retire_factor",
                "applied" if retired else "superseded",
                evidence,
                verdict,
                before={"runtime_config": before_cfg, "lifecycle_status": item.get("lifecycle_status")},
                after={"lifecycle_status": "DEAD"},
                rollback={"runtime_config": before_cfg},
                result={"retired": bool(retired)},
            ))
        return actions

    # ── Evidence and mutation helpers ───────────────────────────────

    def _promotion_evidence(self, item: dict[str, Any], cfg: Any) -> dict[str, Any]:
        perf = item.get("shadow_perf") or {}
        oos_bars = int(perf.get("oos_bars") or 0)
        n_valid = int(perf.get("n_valid") or 0)
        cumulative_pnl = float(perf.get("cumulative_pnl") or 0.0)
        hit_rate = float(perf.get("hit_rate") or 0.0)
        max_drawdown = abs(float(perf.get("max_drawdown") or 0.0))
        health_score = float(item.get("health_score") or 0.0)
        health_status = str(item.get("health_status") or "UNKNOWN")
        canary_stage = str((item.get("canary") or {}).get("stage") or "").upper()
        min_oos = int(getattr(cfg, "factor_governance_shadow_min_oos_bars", 100) or 100)
        min_valid = int(getattr(cfg, "factor_governance_shadow_min_valid", 80) or 80)
        min_hit = float(getattr(cfg, "factor_governance_shadow_min_hit_rate", 0.5) or 0.5)
        max_dd = float(getattr(cfg, "factor_governance_shadow_max_drawdown", 0.05) or 0.05)
        watch = float(getattr(cfg, "factor_health_watch_threshold", 40.0) or 40.0)
        health_ok = health_status in {"UNKNOWN", "HEALTHY", "WATCH"} or health_score >= watch
        eligible = (
            canary_stage == "ACTIVE"
            and oos_bars >= min_oos
            and n_valid >= min_valid
            and cumulative_pnl > 0.0
            and hit_rate >= min_hit
            and max_drawdown <= max_dd
            and health_ok
        )
        return {
            "eligible": eligible,
            "oos_bars": oos_bars,
            "n_valid": n_valid,
            "cumulative_pnl": cumulative_pnl,
            "hit_rate": hit_rate,
            "max_drawdown": max_drawdown,
            "health_score": health_score,
            "health_status": health_status,
            "canary_stage": canary_stage,
            "thresholds": {
                "min_oos_bars": min_oos,
                "min_valid": min_valid,
                "min_hit_rate": min_hit,
                "max_drawdown": max_dd,
                "watch_health": watch,
            },
        }

    def _shadow_score(self, item: dict[str, Any]) -> float:
        perf = item.get("shadow_perf") or {}
        return (
            float(perf.get("cumulative_pnl") or 0.0)
            + 0.01 * float(perf.get("hit_rate") or 0.0)
            - abs(float(perf.get("max_drawdown") or 0.0))
        )

    def _model_governance_evidence(self, item: dict[str, Any], cfg: Any) -> dict[str, Any]:
        shadow = item.get("factor_governance_shadow") or {}
        sample_count = int(shadow.get("sample_count") or 0)
        weak_sample_count = int(shadow.get("weak_sample_count") or 0)
        latest_weakness = float(shadow.get("weakness_score") or item.get("model_weakness_score") or 0.0)
        avg_weakness = float(shadow.get("avg_weakness_score") or latest_weakness or 0.0)
        latest_positive = float(shadow.get("positive_score") or item.get("model_positive_score") or 0.0)
        avg_positive = float(shadow.get("avg_positive_score") or latest_positive or 0.0)
        min_samples = int(getattr(cfg, "factor_governance_model_min_samples", 3) or 3)
        down_th = float(getattr(cfg, "factor_governance_model_weakness_threshold", 0.65) or 0.65)
        disable_th = float(getattr(cfg, "factor_governance_model_disable_threshold", 0.85) or 0.85)
        enough = sample_count >= min_samples or weak_sample_count >= min_samples
        weak_for_downweight = enough and max(avg_weakness, latest_weakness) >= down_th
        weak_for_disable = enough and max(avg_weakness, latest_weakness) >= disable_th
        return {
            "sample_count": sample_count,
            "weak_sample_count": weak_sample_count,
            "latest_weakness_score": latest_weakness,
            "avg_weakness_score": avg_weakness,
            "latest_positive_score": latest_positive,
            "avg_positive_score": avg_positive,
            "min_samples": min_samples,
            "downweight_threshold": down_th,
            "disable_threshold": disable_th,
            "weak_for_downweight": weak_for_downweight,
            "weak_for_disable": weak_for_disable,
            "latest_inference_id": str(shadow.get("latest_inference_id") or ""),
            "model_type": str(shadow.get("model_type") or ""),
        }

    def _ensure_promoted_runtime_config(self, factor_id: str, *, run_id: str = "") -> None:
        cfg = runtime_config.shared()
        signal_cfg = dict(cfg.factor_signal_config or {})
        if factor_id not in signal_cfg:
            entry = {
                "enabled": True,
                "mode": "rank_mapping",
                "window": 100,
                "min_samples": 30,
                "direction": 1,
                "role": "alpha",
                "source": "discovered",
                "tags": ["GP发现"],
                "cadence": "bar",
                "history_sample_policy": "every_bar",
            }
        else:
            entry = dict(signal_cfg[factor_id] or {})
            entry["enabled"] = True
            entry.setdefault("role", "alpha")
            entry["source"] = "discovered"
        signal_cfg[factor_id] = entry

        current_weights = dict(cfg.factor_portfolio_weights or {})
        factor_configs = self._portfolio_configs(cfg, signal_cfg=signal_cfg)
        dp = DecisionPolicy(
            redundancy_max_group_weight=float(getattr(cfg, "factor_redundancy_max_group_weight", 0.35) or 0.35)
        )
        target = float(getattr(cfg, "factor_governance_new_factor_weight", 0.3) or 0.3)
        decisions = dp.fast_decide(
            awe_patches=None,
            weight_policy_weights={factor_id: target},
            factor_configs=factor_configs,
            current_weights=current_weights,
        )
        weight_patch = DecisionPolicy.to_weights(decisions)
        self._apply_runtime_patch(
            {
                "factor_signal_config": {factor_id: entry},
                "factor_portfolio_weights": weight_patch,
            },
            source="factor_governance_promote_factor",
            run_id=run_id,
        )

    def _portfolio_configs(self, cfg: Any, *, signal_cfg: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        signal_cfg = dict(signal_cfg if signal_cfg is not None else (getattr(cfg, "factor_signal_config", {}) or {}))
        weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
        merged: dict[str, dict[str, Any]] = {}
        for name in set(signal_cfg) | set(weights):
            entry = dict(signal_cfg.get(name, {}) or {})
            entry["weight"] = float(weights.get(name, entry.get("weight", 0.0)) or 0.0)
            entry.setdefault("role", resolve_factor_role(name, entry))
            merged[name] = entry
        return merged

    def _risk(self, action: str, item: dict[str, Any], evidence: dict[str, Any]) -> RiskVerdict:
        return self.risk_policy.evaluate(
            action,
            {
                "factor": item.get("factor_id"),
                "source": item.get("source"),
                "role": item.get("role"),
                "required_mode": "autonomous_governance",
                "evidence": evidence,
            },
        )

    def _audit_action(
        self,
        run: dict[str, Any],
        item: dict[str, Any],
        action: str,
        status: str,
        evidence: dict[str, Any],
        verdict: RiskVerdict,
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        rollback: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from backend.services.evolution_ledger import persist_runtime_config_snapshot, record_evolution_decision

        snapshot = persist_runtime_config_snapshot(
            runtime_config.shared(),
            source=f"factor_governance:{action}:{status}",
            run_id=str(run.get("run_id") or ""),
        )
        factor_id = str(item.get("factor_id") or "")
        decision_id = record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="factor_governance_autonomous",
            scope_type="factor",
            scope_key=factor_id,
            action=action,
            status=status,
            evidence=evidence,
            risk_verdict=verdict.to_dict(),
            before=before or {},
            after=after or {},
            result=result or {},
            rollback=rollback or {},
            config_version=int(snapshot.get("config_version") or 0),
            config_hash=str(snapshot.get("config_hash") or ""),
        )
        suggestion_id = self._record_policy_suggestion(factor_id, action, status, evidence, decision_id)
        self._record_learning_application(factor_id, action, status, suggestion_id, before, after, result, decision_id=decision_id)
        record_api_mutation(
            user="system:factor_governance",
            endpoint="backend.runtime.factor_governance_orchestrator",
            action=action,
            status=status,
            before=before or {},
            after=after or {},
            result={"decision_id": decision_id, **(result or {})},
            reason=_dumps(evidence),
            required_confirm="autonomous-risk-policy",
            confirm_ok=verdict.allowed,
        )
        return {
            "factor_id": factor_id,
            "action": action,
            "status": status,
            "decision_id": decision_id,
            "suggestion_id": suggestion_id,
            "risk": verdict.to_dict(),
        }

    def _record_policy_suggestion(
        self,
        factor_id: str,
        action: str,
        status: str,
        evidence: dict[str, Any],
        decision_id: str,
    ) -> str:
        suggestion_id = f"fgv3_{uuid.uuid4().hex[:16]}"
        now = time.time()
        evidence_payload = {
            "source_agent": "factor_governance",
            "decision_id": decision_id,
            **evidence,
        }
        evidence_payload.setdefault(
            "authority_verdict",
            AgentAuthorityRegistryService().evaluate_scope_write(
                "factor_governance",
                "factor",
                action,
                requested_writes=["policy_suggestion"],
                status=status,
                impact_level="medium",
            ),
        )
        evidence_payload = attach_policy_suggestion_agent_context(
            evidence_payload,
            source_agent="factor_governance",
            scope_type="factor",
            scope_key=factor_id,
            action=action,
            requested_writes=["policy_suggestion"],
            status=status,
            impact_level="medium",
        )
        conn = get_state_pg_conn()
        try:
            conn.execute(
                _p("""
                INSERT INTO policy_suggestion
                (suggestion_id, scope_type, scope_key, action, confidence, reason,
                 evidence_json, status, reviewed_at, review_note, created_at)
                VALUES (?, 'factor', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """),
                (
                    suggestion_id,
                    factor_id,
                    action,
                    1.0 if status == "applied" else 0.0,
                    "autonomous_factor_governance_v3",
                    _dumps(evidence_payload),
                    status,
                    now if status in {"auto_approved", "applied", "rolled_back", "blocked_by_risk", "superseded"} else 0.0,
                    "autonomous_execution_no_human_approval",
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return suggestion_id

    def _record_learning_application(
        self,
        factor_id: str,
        action: str,
        status: str,
        suggestion_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        result: dict[str, Any] | None,
        decision_id: str = "",
    ) -> None:
        before = before or {}
        after = after or {}
        old_weight = float(before.get("weight") or 0.0)
        new_weight = float(after.get("weight") or old_weight)
        now = time.time()
        details = {
            "source_agent": "factor_governance",
            "decision_id": decision_id,
            "before": before,
            "after": after,
            "result": result or {},
            "authority_verdict": AgentAuthorityRegistryService().evaluate_scope_write(
                "factor_governance",
                "factor",
                action,
                requested_writes=["learning_application_log"],
                status=status,
                impact_level="medium",
            ),
        }
        conn = get_state_pg_conn()
        try:
            conn.execute(
                _p("""
                INSERT INTO learning_application_log
                (application_id, cycle_ts, scope_type, scope_key, action,
                 bias_multiplier, old_weight, new_weight, suggestion_ids_json,
                 status, details_json, created_at)
                VALUES (?, ?, 'factor', ?, ?, 1.0, ?, ?, ?, ?, ?, ?)
                """),
                (
                    f"fgv3_app_{uuid.uuid4().hex[:16]}",
                    now,
                    factor_id,
                    action,
                    old_weight,
                    new_weight,
                    _dumps([suggestion_id] if suggestion_id else []),
                    status,
                    _dumps(details),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _redundancy_report(self, catalog: list[dict[str, Any]]) -> dict[str, Any]:
        groups: dict[str, dict[str, Any]] = {}
        for item in catalog:
            group = str(item.get("redundancy_group") or "").strip()
            if not group or item.get("role") != "alpha":
                continue
            entry = groups.setdefault(group, {"members": [], "total_weight": 0.0, "leader": ""})
            entry["members"].append(item["factor_id"])
            entry["total_weight"] += float(item.get("weight") or 0.0)
            leader = entry.get("leader") or ""
            if not leader or float(item.get("health_score") or 0.0) > float(
                next((x.get("health_score") for x in catalog if x.get("factor_id") == leader), 0.0) or 0.0
            ):
                entry["leader"] = item["factor_id"]
        cap = float(getattr(runtime_config.shared(), "factor_redundancy_max_group_weight", 0.35) or 0.35)
        for entry in groups.values():
            entry["members"] = sorted(entry["members"])
            entry["total_weight"] = round(float(entry["total_weight"]), 6)
            entry["cap"] = cap
            entry["over_cap"] = entry["total_weight"] > cap
        return groups

    @staticmethod
    def _loads_dict(raw: Any) -> dict[str, Any]:
        value = _loads(raw, {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _loads_list(raw: Any) -> list[str]:
        value = _loads(raw, [])
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item or "")]


def run_autonomous_factor_governance_cycle() -> dict[str, Any]:
    return FactorGovernanceOrchestrator.shared().run_cycle(trigger_source="scheduler")
