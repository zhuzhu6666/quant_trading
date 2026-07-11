"""Single governed use-case boundary for factor weight mutations."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from alpha.decision_policy import DecisionPolicy
from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.services.experience_prior import ExperiencePriorService
from backend.services.learning_application_state import LearningApplicationStateService
from backend.services.learning_experiment_admission import LearningExperimentAdmissionService


RiskCheck = Callable[[dict[str, Any]], Any]


def _verdict_payload(verdict: Any) -> dict[str, Any]:
    if isinstance(verdict, dict):
        return dict(verdict)
    if hasattr(verdict, "to_dict"):
        try:
            return dict(verdict.to_dict())
        except Exception:
            pass
    return {
        "allowed": bool(getattr(verdict, "allowed", verdict is True)),
        "reason": str(getattr(verdict, "reason", "") or ""),
    }


class FactorWeightChangeService:
    """Plan, admit, authorize, persist, and observe factor weight changes."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)
        self.admission = LearningExperimentAdmissionService(self.db_path)
        self.applications = LearningApplicationStateService(self.db_path)

    def _mutation_service(self):
        # Resolve at execution time so deployments/tests can replace the shared
        # mutation boundary without this use-case retaining a stale class alias.
        from backend.services import runtime_config_mutation

        if self.db_path == Path(STATE_DB):
            return runtime_config_mutation.RuntimeConfigMutationService()
        return runtime_config_mutation.RuntimeConfigMutationService(self.db_path)

    def _replay_admission(self, decisions: dict[str, Any]) -> dict[str, Any]:
        max_delta = max(
            (abs(float(item.new_weight) - float(item.old_weight)) for item in decisions.values()),
            default=0.0,
        )
        if max_delta < 0.10:
            return {"required": False, "allowed": True, "max_delta": max_delta}
        conn = get_state_pg_conn(read_only=True) if is_state_db_path(self.db_path) else connect_sqlite(self.db_path, read_only=True)
        try:
            row = conn.execute(
                "SELECT replay_run_id, evidence_grade, status, replay_error, created_at "
                "FROM replay_report ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        except Exception as exc:
            row = None
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = ""
        finally:
            conn.close()
        payload = dict(row) if row is not None else {}
        grade = str(payload.get("evidence_grade") or "")
        allowed = bool(payload) and str(payload.get("status") or "") == "completed" and not payload.get("replay_error") and grade in {"A", "B"}
        return {
            "required": True, "allowed": allowed, "max_delta": max_delta,
            "replay_run_id": str(payload.get("replay_run_id") or ""),
            "evidence_grade": grade, "error": error,
        }

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "factor_weight_change_boundary.v1",
            "single_business_write_path": True,
            "decision_policy_required": True,
            "experience_prior_bounded": True,
            "experiment_admission_required": True,
            "risk_verdict_required": True,
            "application_prepared_before_mutation": True,
            "runtime_mutation_required": True,
        }

    def plan(
        self,
        *,
        factor_configs: dict[str, dict],
        current_weights: dict[str, float],
        awe_patches: dict[str, dict] | None = None,
        weight_policy_weights: dict[str, float] | None = None,
        shadow_perfs: dict[str, Any] | None = None,
        regime: str | None = None,
        fast: bool = False,
        bypass_for_risk_reduction: bool = False,
        decision_policy: DecisionPolicy | None = None,
    ) -> dict[str, Any]:
        policy = decision_policy or DecisionPolicy()
        prior_error = ""
        try:
            priors = ExperiencePriorService(self.db_path).priors()
        except Exception as exc:
            # A fresh isolated store legitimately has no prior history.  The
            # prepared write and runtime mutation remain the fail-closed gates.
            priors = {}
            prior_error = f"{type(exc).__name__}: {exc}"
        if fast:
            decisions = policy.fast_decide(
                awe_patches=awe_patches,
                weight_policy_weights=weight_policy_weights,
                factor_configs=factor_configs,
                current_weights=current_weights,
                experience_priors=priors,
            )
        else:
            decisions = policy.decide(
                awe_patches=awe_patches,
                weight_policy_weights=weight_policy_weights,
                shadow_perfs=shadow_perfs,
                factor_configs=factor_configs,
                current_weights=current_weights,
                regime=regime,
                experience_priors=priors,
            )
        decisions = {
            name: decision
            for name, decision in decisions.items()
            if abs(float(decision.new_weight) - float(decision.old_weight)) > 1e-9
        }
        admissions: dict[str, dict[str, Any]] = {}
        admitted: dict[str, Any] = {}
        for name, decision in decisions.items():
            admission = self.admission.evaluate(
                scope_type="factor",
                scope_key=name,
                action="update_weight",
                old_weight=float(decision.old_weight),
                new_weight=float(decision.new_weight),
                bypass_for_risk_reduction=bypass_for_risk_reduction,
            )
            admissions[name] = admission
            if admission.get("allowed"):
                admitted[name] = decision
        return {
            "ok": True,
            "schema_version": "factor_weight_change_plan.v1",
            "status": "planned" if admitted else "no_admitted_change",
            "decisions": decisions,
            "admitted_decisions": admitted,
            "admissions": admissions,
            "proposed_weights": DecisionPolicy.to_weights(admitted),
            "experience_prior_count": len(priors),
            "experience_prior_status": "available" if not prior_error else "unavailable",
            "experience_prior_error": prior_error,
            "boundary": self.boundary(),
        }

    def execute(
        self,
        *,
        source: str,
        producer: str,
        run_id: str,
        actor: str,
        reason: str,
        factor_configs: dict[str, dict],
        current_weights: dict[str, float],
        awe_patches: dict[str, dict] | None = None,
        weight_policy_weights: dict[str, float] | None = None,
        shadow_perfs: dict[str, Any] | None = None,
        regime: str | None = None,
        fast: bool = False,
        bypass_for_risk_reduction: bool = False,
        decision_policy: DecisionPolicy | None = None,
        risk_check: RiskCheck | None = None,
        evidence_by_factor: dict[str, dict[str, Any]] | None = None,
        suggestion_ids_by_factor: dict[str, list[str]] | None = None,
        source_agent: str = "factor_governance",
        additional_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Fail before preparing application rows when pytest accidentally
        # inherits the production PostgreSQL DSN.  The overlay boundary used
        # to reject only after `learning_application_log` was written, leaving
        # production audit pollution marked as mutation_failed.
        if (
            is_state_db_path(self.db_path)
            and (os.getenv("PYTEST_CURRENT_TEST") or os.getenv("PYTEST_VERSION"))
            and os.getenv("QUANT_ALLOW_PYTEST_STATE_OVERLAY_WRITE", "").strip() != "1"
        ):
            return {
                "ok": False,
                "schema_version": "factor_weight_change_plan.v1",
                "status": "blocked_test_state_isolation",
                "reason": "pytest_must_use_isolated_state_store",
                "applications": {},
                "boundary": self.boundary(),
            }
        plan = self.plan(
            factor_configs=factor_configs,
            current_weights=current_weights,
            awe_patches=awe_patches,
            weight_policy_weights=weight_policy_weights,
            shadow_perfs=shadow_perfs,
            regime=regime,
            fast=fast,
            bypass_for_risk_reduction=bypass_for_risk_reduction,
            decision_policy=decision_policy,
        )
        proposed_weights = dict(plan["proposed_weights"])
        if not proposed_weights:
            return {**plan, "status": "no_admitted_change", "applications": {}}
        replay_admission = self._replay_admission(plan["admitted_decisions"])
        if not replay_admission.get("allowed"):
            return {
                **plan,
                "status": "blocked_by_replay_admission",
                "replay_admission": replay_admission,
                "applications": {},
            }

        risk_context = {
            "schema_version": "factor_weight_change_risk_context.v1",
            "source": source,
            "producer": producer,
            "run_id": run_id,
            "proposed_weights": proposed_weights,
            "decision_count": len(proposed_weights),
            "replay_admission": replay_admission,
        }
        if risk_check is None:
            from risk.policy_service import RiskPolicyService

            verdict = RiskPolicyService.shared().evaluate("update_weight", risk_context)
        else:
            verdict = risk_check({**plan, "risk_context": risk_context})
        risk_verdict = _verdict_payload(verdict)
        if not bool(risk_verdict.get("allowed")):
            return {
                **plan,
                "status": "blocked_by_risk",
                "risk_verdict": risk_verdict,
                "applications": {},
            }

        cycle_ts = time.time()
        application_ids: dict[str, str] = {}
        evidence_by_factor = dict(evidence_by_factor or {})
        suggestion_ids_by_factor = dict(suggestion_ids_by_factor or {})
        try:
            for name, decision in plan["admitted_decisions"].items():
                details = {
                    "source_agent": source_agent,
                    "producer": producer,
                    "run_id": run_id,
                    "mutation_source": source,
                    "prepared_at": cycle_ts,
                    "decision": decision.to_api(),
                    "experiment_admission": plan["admissions"].get(name) or {},
                    "risk_verdict": risk_verdict,
                    "evidence": evidence_by_factor.get(name) or {},
                }
                application_ids[name] = self.applications.prepare(
                    scope_key=name,
                    old_weight=float(decision.old_weight),
                    new_weight=float(decision.new_weight),
                    suggestion_ids=(suggestion_ids_by_factor.get(name) or [f"{run_id}:{name}"]),
                    cycle_ts=cycle_ts,
                    details=details,
                )
        except Exception as exc:
            for application_id in application_ids.values():
                self.applications.transition(
                    application_id,
                    status="mutation_failed",
                    details_patch={"prepare_error": f"{type(exc).__name__}: {exc}"},
                )
            raise

        try:
            mutation = self._mutation_service().apply_patch(
                {
                    **dict(additional_patch or {}),
                    "factor_portfolio_weights": proposed_weights,
                },
                source=source,
                run_id=run_id,
                actor=actor,
                action="update_weight",
                reason=reason,
            )
            if mutation.get("ok") is False:
                raise RuntimeError(str(mutation.get("status") or "runtime_config_mutation_failed"))
        except Exception as exc:
            for application_id in application_ids.values():
                self.applications.transition(
                    application_id,
                    status="mutation_failed",
                    details_patch={"mutation_error": f"{type(exc).__name__}: {exc}"},
                )
            raise

        transitions: dict[str, dict[str, Any]] = {}
        for name, application_id in application_ids.items():
            try:
                transitions[name] = self.applications.transition(
                    application_id,
                    status="applied",
                    details_patch={
                        "applied_at": time.time(),
                        "mutation_snapshot": mutation.get("snapshot") or {},
                        "mutation_status": mutation.get("status") or "applied",
                    },
                )
            except Exception as exc:
                transitions[name] = {
                    "ok": False,
                    "status": "recovery_pending",
                    "error": f"{type(exc).__name__}: {exc}",
                    "application_id": application_id,
                }
        return {
            **plan,
            "status": "applied",
            "risk_verdict": risk_verdict,
            "mutation": mutation,
            "applications": application_ids,
            "transitions": transitions,
        }
