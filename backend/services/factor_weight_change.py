"""Single governed use-case boundary for factor weight mutations."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from alpha.decision_policy import DecisionPolicy
from alpha.portfolio_compositor import resolve_factor_role
from alpha.runtime_factor_selection import select_runtime_factors
from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
)
from backend.services.brain_governance_candidates import sync_candidate_suggestion_lifecycle
from backend.services.experience_prior import ExperiencePriorService
from backend.services.factor_blend_health import FactorBlendHealthService
from backend.services.learning_application_state import LearningApplicationStateService
from backend.services.learning_experiment_admission import LearningExperimentAdmissionService


RiskCheck = Callable[[dict[str, Any]], Any]


class AtomicExperimentAdmissionError(RuntimeError):
    """The coordinator transaction could not admit the complete weight batch."""

    def __init__(self, admission: Mapping[str, Any]):
        self.admission = dict(admission)
        super().__init__(
            "complete_batch_required:"
            + _json(
                {
                    "admissions": self.admission.get("admissions") or {},
                    "reserved_count": self.admission.get("reserved_count") or 0,
                }
            )
        )


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, value: Any, *, length: int = 20) -> str:
    return f"{prefix}_{_fingerprint(value)[:length]}"


def _conn_is_pg(conn: Any) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _execute(conn: Any, sql: str, params: tuple[Any, ...] | list[Any] | None = None):
    statement = sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql
    if params is None:
        return conn.execute(statement)
    return conn.execute(statement, tuple(params))


def _upsert_row(
    conn: Any,
    *,
    table: str,
    primary_key: str,
    values: Mapping[str, Any],
    immutable_columns: set[str] | None = None,
) -> None:
    columns = list(values)
    immutable = set(immutable_columns or ()) | {primary_key}
    updates = [column for column in columns if column not in immutable]
    _execute(
        conn,
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
        f"({', '.join('?' for _ in columns)}) ON CONFLICT({primary_key}) "
        f"DO UPDATE SET {', '.join(f'{column}=excluded.{column}' for column in updates)}",
        [values[column] for column in columns],
    )


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
    def _directional_guard(
        *,
        factor_configs: Mapping[str, Any],
        weights: Mapping[str, Any],
    ) -> dict[str, Any]:
        selection = select_runtime_factors(dict(factor_configs))
        selected: list[str] | None = None
        if selection is not None:
            unavailable_reasons = {
                "factor_admission_unavailable",
                "registry_metadata_unavailable",
            }
            reasons = set((selection.reason_excluded or {}).values())
            if not reasons.intersection(unavailable_reasons):
                selected = list(selection.selected_factor_ids)
        return FactorBlendHealthService.evaluate_directional_portfolio_guard(
            selected_factor_ids=selected,
            factor_configs=factor_configs,
            weights=weights,
        )

    def _activation_canary_expansion_admission(
        self,
        *,
        factor_id: str,
        factor_config: Mapping[str, Any],
        old_weight: float,
        new_weight: float,
    ) -> dict[str, Any]:
        """Require a mature positive real effect before Canary expansion."""
        if new_weight <= old_weight or factor_config.get("activation_canary") is not True:
            return {
                "ok": True,
                "allowed": True,
                "status": "not_activation_canary_expansion",
            }
        if (
            str(factor_config.get("admission_evidence_version") or "")
            != "factor_admission_evidence.v1"
        ):
            return {
                "ok": True,
                "allowed": False,
                "status": "controlled_active_canary_contract_missing",
                "reason": "legacy_evidence_incomplete",
            }
        conn = None
        try:
            conn = (
                get_state_pg_conn(read_only=True)
                if is_state_db_path(self.db_path)
                else connect_sqlite(self.db_path, read_only=True)
            )
            row = _execute(
                conn,
                """SELECT l.application_id,
                          l.status AS application_status,
                          e.status AS effect_status,
                          e.observed_trade_count,
                          e.decision_json,
                          e.updated_at
                   FROM learning_application_log l
                   LEFT JOIN learning_application_effect e
                     ON e.application_id=l.application_id
                   WHERE l.scope_type='factor' AND l.scope_key=?
                   ORDER BY l.cycle_ts DESC, l.created_at DESC
                   LIMIT 1""",
                (factor_id,),
            ).fetchone()
        except Exception as exc:
            return {
                "ok": True,
                "allowed": False,
                "status": "application_effect_unavailable",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        finally:
            if conn is not None:
                conn.close()
        if not row:
            return {
                "ok": True,
                "allowed": False,
                "status": "application_effect_missing",
                "reason": "real_application_effect_required",
            }
        columns = (
            "application_id",
            "application_status",
            "effect_status",
            "observed_trade_count",
            "decision_json",
            "updated_at",
        )
        item = (
            {key: row[key] for key in columns}
            if hasattr(row, "keys")
            else {key: row[index] for index, key in enumerate(columns)}
        )
        try:
            decision = json.loads(str(item.get("decision_json") or "{}"))
        except Exception:
            decision = {}
        quality = dict((decision or {}).get("evidence_quality") or {})
        effect_status = str(item.get("effect_status") or "").lower()
        allowed = bool(
            effect_status == "effective"
            and quality.get("bounded_attribution_allowed") is True
        )
        return {
            "ok": True,
            "allowed": allowed,
            "status": (
                "mature_positive_application_effect"
                if allowed
                else "application_effect_not_mature_positive"
            ),
            "reason": (
                "mature_positive_real_effect"
                if allowed
                else "weight_expansion_requires_effective_bounded_attribution"
            ),
            "application_id": str(item.get("application_id") or ""),
            "application_status": str(item.get("application_status") or ""),
            "effect_status": effect_status,
            "observed_trade_count": int(item.get("observed_trade_count") or 0),
            "bounded_attribution_allowed": quality.get(
                "bounded_attribution_allowed"
            ),
        }

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "factor_weight_change_boundary.v2",
            "single_business_write_path": True,
            "decision_policy_required": True,
            "experience_prior_bounded": True,
            "experiment_admission_required": True,
            "activation_canary_expansion_requires_mature_positive_effect": True,
            "risk_verdict_required": True,
            "coordinator_domain_transaction_required": True,
            "application_effect_reservation_atomic_in_coordinator_modes": True,
            "legacy_prepare_before_mutation_off_mode_only": True,
            "legacy_prepared_recovery_preserved": True,
            "runtime_mutation_required": True,
        }

    @staticmethod
    def _mutation_committed(mutation: Mapping[str, Any]) -> bool:
        return bool(mutation.get("ok")) or str(mutation.get("status") or "") in {
            "applied",
            "committed",
            "committed_projection_degraded",
        }

    def _write_atomic_domain(
        self,
        conn: Any,
        *,
        mutation_id: str,
        decisions: Mapping[str, Any],
        application_ids: Mapping[str, str],
        reservation_ids: Mapping[str, str],
        suggestion_ids_by_factor: Mapping[str, list[str]],
        details_by_factor: Mapping[str, Mapping[str, Any]],
        cycle_ts: float,
        bypass_for_risk_reduction: bool,
    ) -> dict[str, Any]:
        """Commit experiment facts on the coordinator-owned transaction."""
        from backend.services.agent_authority_registry import (
            AgentAuthorityRegistryService,
        )
        from backend.services.governance_eligibility import (
            GOVERNANCE_ELIGIBILITY_VERSION,
        )

        batch_admission = self.admission.reserve_batch_in_transaction(
            conn,
            decisions,
            mutation_id=mutation_id,
            reservation_ids=reservation_ids,
            action="update_weight",
            bypass_for_risk_reduction=bypass_for_risk_reduction,
        )
        admitted = {
            name
            for name, item in (batch_admission.get("admissions") or {}).items()
            if bool((item or {}).get("allowed"))
        }
        expected = {str(name) for name in decisions}
        if admitted != expected:
            raise AtomicExperimentAdmissionError(batch_admission)

        now = time.time()
        app_columns = state_table_columns(conn, "learning_application_log")
        effect_columns = state_table_columns(conn, "learning_application_effect")
        suggestion_columns = state_table_columns(conn, "policy_suggestion")
        reservation_columns = state_table_columns(
            conn, "learning_experiment_reservation"
        )
        missing_mutation_columns = [
            table
            for table, columns in (
                ("learning_application_log", app_columns),
                ("learning_application_effect", effect_columns),
                ("learning_experiment_reservation", reservation_columns),
            )
            if "mutation_id" not in columns
        ]
        if missing_mutation_columns:
            raise RuntimeError(
                "factor_weight_atomic_schema_missing_mutation_id:"
                + ",".join(missing_mutation_columns)
            )
        for name in sorted(expected):
            decision = decisions[name]
            application_id = str(application_ids[name])
            reservation_id = str(reservation_ids[name])
            suggestion_ids = sorted(
                {
                    str(item)
                    for item in (suggestion_ids_by_factor.get(name) or [])
                    if str(item)
                }
            )
            authority_verdict = AgentAuthorityRegistryService().evaluate_scope_write(
                str((details_by_factor.get(name) or {}).get("source_agent") or "factor_governance"),
                "factor",
                "update_weight",
                # The source agent owns the application request.  Effect and
                # reservation rows are coordinator-owned ledger side effects,
                # not extra agent write authority.
                requested_writes=["learning_application_log"],
                status="applied",
                impact_level="medium",
            )
            details = {
                **dict(details_by_factor.get(name) or {}),
                "experiment_admission": (batch_admission.get("admissions") or {}).get(name)
                or {},
                "experiment_reservation_id": reservation_id,
                "mutation_id": mutation_id,
                "commit_boundary": "governance_mutation_coordinator",
                "authority_verdict": authority_verdict,
                "application_state": {
                    "status": "applied",
                    "prepared_at": float(cycle_ts),
                    "applied_at": now,
                    "updated_at": now,
                    "atomic_commit": True,
                },
            }
            old_weight = float(decision.old_weight)
            new_weight = float(decision.new_weight)
            app_values: dict[str, Any] = {
                "application_id": application_id,
                "cycle_ts": float(cycle_ts),
                "scope_type": "factor",
                "scope_key": name,
                "action": "update_weight",
                "bias_multiplier": (new_weight / old_weight) if old_weight else 1.0,
                "old_weight": old_weight,
                "new_weight": new_weight,
                "suggestion_ids_json": _json(suggestion_ids),
                "status": "applied",
                "details_json": _json(details),
                "created_at": now,
            }
            if "mutation_id" in app_columns:
                app_values["mutation_id"] = mutation_id
            if "governance_eligibility_version" in app_columns:
                app_values["governance_eligibility_version"] = (
                    GOVERNANCE_ELIGIBILITY_VERSION
                )
            _upsert_row(
                conn,
                table="learning_application_log",
                primary_key="application_id",
                values=app_values,
                immutable_columns={"created_at"},
            )

            for suggestion_id in suggestion_ids:
                suggestion = _execute(
                    conn,
                    """
                    SELECT status, governance_eligible,
                           governance_eligibility_version,
                           governance_eligibility_fingerprint,
                           applied_mutation_id
                    FROM policy_suggestion
                    WHERE suggestion_id=?
                    LIMIT 1
                    """,
                    (suggestion_id,),
                ).fetchone()
                if suggestion is None:
                    continue
                if not bool(
                    int(suggestion["governance_eligible"] or 0) == 1
                    and str(suggestion["governance_eligibility_version"] or "")
                    == GOVERNANCE_ELIGIBILITY_VERSION
                    and str(suggestion["governance_eligibility_fingerprint"] or "")
                ):
                    raise RuntimeError(
                        f"factor_weight_suggestion_eligibility_invalid:{suggestion_id}"
                    )
                if str(suggestion["status"] or "") not in {"approved", "applied"}:
                    raise RuntimeError(
                        f"factor_weight_suggestion_not_approved:{suggestion_id}"
                    )
                assignments = [
                    "status='applied'",
                    "reviewed_at=?",
                    "review_note=?",
                ]
                params: list[Any] = [
                    now,
                    f"applied by committed factor weight mutation {mutation_id}",
                ]
                if "applied_mutation_id" in suggestion_columns:
                    assignments.append("applied_mutation_id=?")
                    params.append(mutation_id)
                params.append(suggestion_id)
                _execute(
                    conn,
                    "UPDATE policy_suggestion SET "
                    + ", ".join(assignments)
                    + " WHERE suggestion_id=?",
                    params,
                )
                sync_candidate_suggestion_lifecycle(
                    conn,
                    suggestion_id=suggestion_id,
                    suggestion_status="applied",
                    applied_mutation_id=mutation_id,
                    now=now,
                )

            effect_values: dict[str, Any] = {
                "application_id": application_id,
                "scope_type": "factor",
                "scope_key": name,
                "action": "update_weight",
                "status": "observing",
                "decision_json": _json(
                    {
                        "suggestion_ids": suggestion_ids,
                        "bias_multiplier": app_values["bias_multiplier"],
                        "old_weight": old_weight,
                        "new_weight": new_weight,
                        "details": details,
                    }
                ),
                "updated_at": now,
                "created_at": now,
            }
            if "mutation_id" in effect_columns:
                effect_values["mutation_id"] = mutation_id
            if "governance_eligibility_version" in effect_columns:
                effect_values["governance_eligibility_version"] = (
                    GOVERNANCE_ELIGIBILITY_VERSION
                )
            _upsert_row(
                conn,
                table="learning_application_effect",
                primary_key="application_id",
                values=effect_values,
                immutable_columns={"created_at"},
            )

            assignments = ["status='consumed'", "application_id=?", "updated_at=?"]
            params: list[Any] = [application_id, now]
            where = "reservation_id=? AND status='reserved'"
            if "mutation_id" in reservation_columns:
                assignments.append("mutation_id=?")
                params.append(mutation_id)
                where += " AND mutation_id=?"
            params.append(reservation_id)
            if "mutation_id" in reservation_columns:
                params.append(mutation_id)
            updated = _execute(
                conn,
                "UPDATE learning_experiment_reservation SET "
                + ", ".join(assignments)
                + " WHERE "
                + where,
                params,
            )
            if int(updated.rowcount or 0) != 1:
                raise RuntimeError(
                    f"factor_weight_reservation_not_reserved:{reservation_id}"
                )
        return {
            "application_ids": dict(application_ids),
            "reservation_ids": dict(reservation_ids),
            "batch_admission": batch_admission,
            "mutation_id": mutation_id,
            "atomic_domain_commit": True,
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
        exact_committed_rollback: bool = False,
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
        from config import runtime_config

        balanced_demo = runtime_config.bounded_demo_mode_active(
            runtime_config.shared()
        )
        normalizations: dict[str, dict[str, Any]] = {}
        if balanced_demo and not exact_committed_rollback:
            minimum_live_weight = 0.05
            for name, decision in decisions.items():
                raw_entry = factor_configs.get(name, {})
                entry = raw_entry if isinstance(raw_entry, dict) else {}
                lifecycle = str(entry.get("lifecycle_status") or "ACTIVE").upper()
                requested = float(decision.new_weight)
                if (
                    resolve_factor_role(name, entry) == "alpha"
                    and entry.get("enabled", True) is not False
                    and lifecycle == "ACTIVE"
                    and requested < minimum_live_weight
                    and abs(requested - float(decision.old_weight)) > 1e-9
                ):
                    decision.new_weight = minimum_live_weight
                    decision.reason += " | balanced_demo_min_live_weight"
                    decision.source_scores["requested_weight"] = requested
                    decision.source_scores["minimum_live_weight"] = minimum_live_weight
                    normalizations[name] = {
                        "reason": "balanced_demo_min_live_weight",
                        "requested_weight": requested,
                        "effective_weight": minimum_live_weight,
                    }
        decisions = {
            name: decision
            for name, decision in decisions.items()
            if abs(float(decision.new_weight) - float(decision.old_weight)) > 1e-9
        }
        admissions: dict[str, dict[str, Any]] = {}
        admitted: dict[str, Any] = {}
        for name, decision in decisions.items():
            effect_admission = self._activation_canary_expansion_admission(
                factor_id=name,
                factor_config=(
                    factor_configs.get(name, {})
                    if isinstance(factor_configs.get(name), dict)
                    else {}
                ),
                old_weight=float(decision.old_weight),
                new_weight=float(decision.new_weight),
            )
            admission = effect_admission
            if effect_admission.get("allowed"):
                admission = self.admission.evaluate(
                    scope_type="factor",
                    scope_key=name,
                    action="update_weight",
                    old_weight=float(decision.old_weight),
                    new_weight=float(decision.new_weight),
                    bypass_for_risk_reduction=bypass_for_risk_reduction,
                )
                admission = {
                    **admission,
                    "activation_canary_effect": effect_admission,
                }
            admissions[name] = admission
            if admission.get("allowed"):
                admitted[name] = decision
        proposed_weights = {
            **{name: float(value or 0.0) for name, value in current_weights.items()},
            **DecisionPolicy.to_weights(admitted),
        }
        directional_guard_before = self._directional_guard(
            factor_configs=factor_configs,
            weights=current_weights,
        )
        directional_guard_after = self._directional_guard(
            factor_configs=factor_configs,
            weights=proposed_weights,
        )
        guard_allowed = FactorBlendHealthService.guard_allows_transition(
            directional_guard_before,
            directional_guard_after,
        )
        return {
            "ok": True,
            "schema_version": "factor_weight_change_plan.v1",
            "status": (
                "blocked_by_directional_portfolio_guard"
                if admitted and not guard_allowed
                else "planned"
                if admitted
                else "no_admitted_change"
            ),
            "decisions": decisions,
            "admitted_decisions": admitted,
            "admissions": admissions,
            "proposed_weights": DecisionPolicy.to_weights(admitted),
            "weight_normalizations": normalizations,
            "directional_portfolio_guard_before": directional_guard_before,
            "directional_portfolio_guard": directional_guard_after,
            "directional_portfolio_guard_allowed": guard_allowed,
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
        v16_command_id: str = "",
        v16_candidate_id: str = "",
        v16_posterior_fingerprint: str = "",
        v16_evidence_fingerprint: str = "",
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
        try:
            exact_committed_rollback = source in {
                "factor_governance_auto_rollback",
                "factor_governance_auto_rollback_config_only",
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
                exact_committed_rollback=exact_committed_rollback,
            )
        except Exception as exc:
            return self._governance_error(stage="plan", exc=exc)
        if (
            plan.get("status") == "blocked_by_directional_portfolio_guard"
            and not exact_committed_rollback
        ):
            return {
                **plan,
                "ok": False,
                "reason": "directional_portfolio_degraded",
                "applications": {},
            }
        admitted_for_preflight = dict(plan.get("admitted_decisions") or {})
        weight_expansion_requires_v16 = any(
            float(decision.new_weight) > float(decision.old_weight) + 1e-12
            for decision in admitted_for_preflight.values()
        )
        if (
            is_state_db_path(self.db_path)
            and str(actor or "").startswith("system:")
            and weight_expansion_requires_v16
        ):
            try:
                from backend.services.v16_command_gate import V16CommandGate

                v16_authority = V16CommandGate.authorize(
                    self.db_path,
                    target_agent=source_agent,
                    scope_type="factor_weight",
                    scope_key=(
                        next(iter(admitted_for_preflight))
                        if len(admitted_for_preflight) == 1
                        else "alpha_weight_policy"
                    ),
                    action="update_weight",
                    command_id=v16_command_id,
                )
            except Exception as exc:
                return {**plan, **self._governance_error(stage="v16_preflight", exc=exc)}
            if not v16_authority.get("allowed"):
                return {
                    **plan,
                    "ok": True,
                    "status": "blocked_by_admission",
                    "admission_status": "blocked_v16_command_required",
                    "reason": str(v16_authority.get("status") or "v16_command_required"),
                    "v16_authority": v16_authority,
                    "applications": {},
                }
        if not plan.get("admitted_decisions"):
            return {
                **plan,
                "status": (
                    "blocked_by_admission"
                    if plan.get("decisions")
                    else "no_admitted_change"
                ),
                "admission_status": "no_admitted_change",
                "applications": {},
            }
        try:
            from backend.services.governance_control_plans import (
                governance_coordinator_mode,
            )

            coordinator_mode = governance_coordinator_mode()
        except Exception as exc:
            return {
                **plan,
                **self._governance_error(stage="coordinator_mode", exc=exc),
            }
        coordinated = coordinator_mode in {"dual_record", "enforce"}
        reserved_admissions: dict[str, dict[str, Any]] = {}
        reservation_ids_by_factor: dict[str, str] = {}
        reservation_ids: list[str] = []
        if coordinated:
            # The read-only plan is revalidated under the coordinator's scope
            # and global admission locks.  Nothing durable is written here.
            batch_admission = {
                "ok": True,
                "status": "pending_governance_transaction",
                "reserved_count": 0,
                "transaction_owned": True,
            }
            admitted_decisions = dict(plan.get("admitted_decisions") or {})
        else:
            # Compatibility path for the release flag's off mode.  Its
            # short-lived prepared rows remain recoverable by
            # LearningApplicationStateService.recover_prepared().
            try:
                batch_admission = self.admission.reserve_batch(
                    plan.get("admitted_decisions") or {},
                    action="update_weight",
                    bypass_for_risk_reduction=bypass_for_risk_reduction,
                )
            except Exception as exc:
                return {**plan, **self._governance_error(stage="admission", exc=exc)}
            reserved_admissions = dict(batch_admission.get("admissions") or {})
            admitted_decisions = {
                name: decision
                for name, decision in (plan.get("admitted_decisions") or {}).items()
                if bool((reserved_admissions.get(name) or {}).get("allowed"))
            }
            reservation_ids_by_factor = dict(batch_admission.get("reservations") or {})
            reservation_ids = list(reservation_ids_by_factor.values())
            plan["admissions"] = {
                **dict(plan.get("admissions") or {}),
                **reserved_admissions,
            }
        plan["admitted_decisions"] = admitted_decisions
        plan["proposed_weights"] = DecisionPolicy.to_weights(admitted_decisions)
        plan["batch_admission"] = batch_admission
        plan["coordinator_mode"] = coordinator_mode
        proposed_weights = dict(plan["proposed_weights"])
        if not proposed_weights:
            return {
                **plan,
                "status": (
                    "blocked_by_admission"
                    if plan.get("decisions")
                    else "no_admitted_change"
                ),
                "admission_status": str(batch_admission.get("status") or "no_admitted_change"),
                "applications": {},
            }
        try:
            replay_admission = self._replay_admission(plan["admitted_decisions"])
        except Exception as exc:
            self._release_reservations_safely(reservation_ids)
            return {**plan, **self._governance_error(stage="replay", exc=exc)}
        if not replay_admission.get("allowed"):
            self._release_reservations_safely(reservation_ids)
            return {
                **plan,
                "status": "blocked_by_replay",
                "legacy_status": "blocked_by_replay_admission",
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
        try:
            if risk_check is None:
                from risk.policy_service import RiskPolicyService

                verdict = RiskPolicyService.shared().evaluate("update_weight", risk_context)
            else:
                verdict = risk_check({**plan, "risk_context": risk_context})
        except Exception as exc:
            self._release_reservations_safely(reservation_ids)
            return {**plan, **self._governance_error(stage="risk", exc=exc)}
        risk_verdict = _verdict_payload(verdict)
        if not bool(risk_verdict.get("allowed")):
            self._release_reservations_safely(reservation_ids)
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
        details_by_factor: dict[str, dict[str, Any]] = {}
        for name, decision in plan["admitted_decisions"].items():
            suggestion_ids = suggestion_ids_by_factor.get(name) or [f"{run_id}:{name}"]
            suggestion_ids_by_factor[name] = [str(item) for item in suggestion_ids if str(item)]
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
                "v16_command_id": v16_command_id,
                "experiment_reservation_id": str(
                    (reserved_admissions.get(name) or {}).get("reservation_id") or ""
                ),
            }
            details_by_factor[name] = details
            if coordinated:
                identity = {
                    "schema": "factor_weight_application.v2",
                    "source": source,
                    "producer": producer,
                    "run_id": run_id,
                    "factor": name,
                    "old_weight": float(decision.old_weight),
                    "new_weight": float(decision.new_weight),
                    "suggestion_ids": suggestion_ids_by_factor[name],
                    "evidence": evidence_by_factor.get(name) or {},
                }
                application_ids[name] = _stable_id("lapp", identity)
                reservation_ids_by_factor[name] = _stable_id(
                    "learn_resv", {"application_id": application_ids[name]}
                )
            else:
                try:
                    application_ids[name] = self.applications.prepare(
                        scope_key=name,
                        old_weight=float(decision.old_weight),
                        new_weight=float(decision.new_weight),
                        suggestion_ids=suggestion_ids_by_factor[name],
                        cycle_ts=cycle_ts,
                        details=details,
                    )
                    self.admission.finalize_reservation(
                        str(reservation_ids_by_factor.get(name) or ""),
                        application_id=application_ids[name],
                    )
                except Exception as exc:
                    self._release_reservations_safely(reservation_ids)
                    for application_id in application_ids.values():
                        self.applications.transition(
                            application_id,
                            status="mutation_failed",
                            details_patch={
                                "prepare_error": f"{type(exc).__name__}: {exc}"
                            },
                        )
                    return {
                        **plan,
                        **self._governance_error(
                            stage="prepare_application", exc=exc
                        ),
                        "applications": application_ids,
                    }

        atomic_outcome: dict[str, Any] = {}
        governance_evidence_refs = {
            "schema_version": "factor_weight_governance_evidence.v2",
            "source": source,
            "producer": producer,
            "run_id": run_id,
            "factors": {
                name: {
                    "decision": decision.to_api(),
                    "suggestion_ids": suggestion_ids_by_factor.get(name) or [],
                    "evidence": evidence_by_factor.get(name) or {},
                }
                for name, decision in plan["admitted_decisions"].items()
            },
            "risk_verdict": risk_verdict,
            "replay_admission": replay_admission,
            "v16_binding": {
                "command_id": v16_command_id,
                "candidate_id": v16_candidate_id,
                "posterior_fingerprint": v16_posterior_fingerprint,
                "evidence_fingerprint": v16_evidence_fingerprint,
            },
        }

        def transaction_writer(conn: Any, mutation_id: str, _effective_config: Any):
            try:
                domain_result = self._write_atomic_domain(
                    conn,
                    mutation_id=mutation_id,
                    decisions=plan["admitted_decisions"],
                    application_ids=application_ids,
                    reservation_ids=reservation_ids_by_factor,
                    suggestion_ids_by_factor=suggestion_ids_by_factor,
                    details_by_factor=details_by_factor,
                    cycle_ts=cycle_ts,
                    bypass_for_risk_reduction=bypass_for_risk_reduction,
                )
                atomic_outcome.update(domain_result)
                return domain_result
            except AtomicExperimentAdmissionError as exc:
                # reserve_batch_in_transaction completed its verdict before
                # raising.  Recompute-free details remain available only to
                # this process; the database transaction is rolled back.
                atomic_outcome["batch_admission"] = exc.admission
                raise

        mutation: dict[str, Any] = {}
        try:
            patch = {
                **dict(additional_patch or {}),
                "factor_portfolio_weights": proposed_weights,
            }
            mutation_kwargs: dict[str, Any] = {
                "source": source,
                "run_id": run_id,
                "actor": actor,
                "action": "update_weight",
                "reason": reason,
                "require_v16_command": is_state_db_path(self.db_path)
                and str(actor or "").startswith("system:"),
                "v16_command_id": v16_command_id,
                "v16_target_agent": source_agent,
                "v16_scope_type": "factor_weight",
                "v16_scope_key": (
                    next(iter(proposed_weights))
                    if len(proposed_weights) == 1
                    else "alpha_weight_policy"
                ),
                "v16_action": "update_weight",
                "v16_candidate_id": v16_candidate_id,
                "v16_posterior_fingerprint": v16_posterior_fingerprint,
                "risk_reduction": bypass_for_risk_reduction,
            }
            if coordinated:
                mutation_kwargs.update(
                    {
                        "governance_idempotency_key": "factor-weight:v2:"
                        + _fingerprint(
                            {
                                "source": source,
                                "producer": producer,
                                "run_id": run_id,
                                "patch": patch,
                                "evidence": governance_evidence_refs,
                            }
                        ),
                        "governance_evidence_refs": governance_evidence_refs,
                        "governance_evidence_fingerprint": (
                            v16_evidence_fingerprint
                            or _fingerprint(governance_evidence_refs)
                        ),
                        "governance_rollback": {
                            "factor_portfolio_weights": {
                                name: float(decision.old_weight)
                                for name, decision in plan["admitted_decisions"].items()
                            }
                        },
                        "governance_transaction_writer": transaction_writer,
                    }
                )
            mutation = self._mutation_service().apply_patch(patch, **mutation_kwargs)
            if not self._mutation_committed(mutation):
                raise RuntimeError(
                    ":".join(
                        item
                        for item in (
                            str(mutation.get("status") or "runtime_config_mutation_failed"),
                            str(mutation.get("error") or mutation.get("reason") or ""),
                        )
                        if item
                    )
                )
        except Exception as exc:
            if not coordinated:
                for application_id in application_ids.values():
                    self.applications.transition(
                        application_id,
                        status="mutation_failed",
                        details_patch={
                            "mutation_error": f"{type(exc).__name__}: {exc}"
                        },
                    )
            atomic_batch = dict(atomic_outcome.get("batch_admission") or {})
            if coordinated and atomic_batch:
                plan["batch_admission"] = atomic_batch
                plan["admissions"] = {
                    **dict(plan.get("admissions") or {}),
                    **dict(atomic_batch.get("admissions") or {}),
                }
                return {
                    **plan,
                    "ok": True,
                    "status": "blocked_by_admission",
                    "admission_status": str(
                        atomic_batch.get("status") or "no_available_slot"
                    ),
                    "reason": "complete_batch_not_admitted_in_governance_transaction",
                    "mutation": mutation,
                    "applications": {},
                    "atomic_domain_commit": True,
                }
            return {
                **plan,
                **self._governance_error(stage="runtime_mutation", exc=exc),
                "applications": application_ids if not coordinated else {},
                "atomic_domain_commit": coordinated,
            }

        transitions: dict[str, dict[str, Any]] = {}
        if coordinated:
            committed_domain = dict(mutation.get("domain_result") or atomic_outcome)
            committed_batch = dict(committed_domain.get("batch_admission") or {})
            if committed_batch:
                plan["batch_admission"] = committed_batch
                plan["admissions"] = {
                    **dict(plan.get("admissions") or {}),
                    **dict(committed_batch.get("admissions") or {}),
                }
            for name, application_id in application_ids.items():
                transitions[name] = {
                    "ok": True,
                    "status": "applied",
                    "effect_status": "observing",
                    "application_id": application_id,
                    "mutation_id": str(mutation.get("mutation_id") or ""),
                    "atomic_commit": True,
                }
        else:
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
            "atomic_domain_commit": coordinated,
            "projection_ready": bool(mutation.get("ok")),
        }

    def _release_reservations_safely(self, reservation_ids: list[str]) -> None:
        try:
            self.admission.release_reservations(reservation_ids)
        except Exception:
            # The original governance error remains authoritative. Expiry is
            # the final fail-safe for a reservation store outage.
            pass

    @staticmethod
    def _governance_error(*, stage: str, exc: Exception) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "factor_weight_change_result.v1",
            "status": "governance_error",
            "error_stage": str(stage),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "applications": {},
        }
