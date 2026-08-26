"""Typed domain plans for runtime-affecting governance controls.

Domain services remain responsible for evidence and policy judgement.  These
immutable plans only describe the intended before/target control surface and
hand it to the release-mode mutation boundary.  They deliberately expose no
``risk_reduction`` input: risk direction is derived by
``GovernanceMutationCoordinator`` from committed before/after facts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.core.db import STATE_DB
from backend.services.runtime_config_mutation import RuntimeConfigMutationService


TransactionWriter = Callable[[Any, str, Any], Mapping[str, Any] | None]


def governance_coordinator_mode() -> str:
    from backend.core.static_feature_flags import shared_static_feature_flags

    mode = str(
        shared_static_feature_flags().governance_mutation_coordinator_v2_mode or "off"
    ).strip().lower()
    if mode not in {"off", "dual_record", "enforce"}:
        raise ValueError(f"invalid_governance_coordinator_mode:{mode}")
    return mode


@dataclass(frozen=True)
class _TypedControlPlan:
    patch: Mapping[str, Any]
    source: str
    actor: str
    action: str
    run_id: str
    reason: str
    scope_type: str
    scope_key: str
    target_agent: str
    evidence_refs: Mapping[str, Any] = field(default_factory=dict)
    rollback: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    mutation_id: str = ""
    evidence_fingerprint: str = ""
    v16_command_id: str = ""
    v16_claim_token: str = ""
    v16_candidate_id: str = ""
    v16_posterior_fingerprint: str = ""

    def execute(
        self,
        db_path: str | Path = STATE_DB,
        *,
        transaction_writer: TransactionWriter | None = None,
        audit: bool | None = None,
    ) -> dict[str, Any]:
        return RuntimeConfigMutationService(db_path).apply_patch(
            dict(self.patch),
            source=self.source,
            run_id=self.run_id,
            actor=self.actor,
            action=self.action,
            reason=self.reason,
            audit=audit,
            v16_command_id=self.v16_command_id,
            v16_claim_token=self.v16_claim_token,
            v16_target_agent=self.target_agent,
            v16_scope_type=self.scope_type,
            v16_scope_key=self.scope_key,
            v16_action=self.action,
            governance_mutation_id=self.mutation_id,
            governance_idempotency_key=self.idempotency_key,
            governance_evidence_refs=dict(self.evidence_refs),
            governance_evidence_fingerprint=self.evidence_fingerprint,
            governance_rollback=dict(self.rollback),
            governance_transaction_writer=transaction_writer,
        )


@dataclass(frozen=True)
class ParameterTemplateActivationPlan(_TypedControlPlan):
    factor_id: str = ""
    regime_key: str = ""
    target_template_id: str = ""


@dataclass(frozen=True)
class PositionSupervisorTemplatePlan(_TypedControlPlan):
    previous_template_id: str = ""
    target_template_id: str = ""
    suggestion_id: str = ""
    application_id: str = ""
    reservation_id: str = ""


@dataclass(frozen=True)
class ModelPolicyActivationPlan(_TypedControlPlan):
    model_type: str = ""
    target_stage: str = ""


@dataclass(frozen=True)
class IncidentControlPlan(_TypedControlPlan):
    """Typed mutation plan for ``runtime_incident_mode``.

    The plan intentionally carries no caller-supplied risk classification.
    Thawing is expansionary and therefore requires V16 when the coordinator
    surface is active; tightening is derived as exempt by the coordinator.
    """

    current_mode: str = ""
    target_mode: str = ""


@dataclass(frozen=True)
class AutonomyControlPlan(_TypedControlPlan):
    """Typed mutation plan for autonomy mode, unlock and expansion freeze."""

    current_mode: str = ""
    target_mode: str = ""
    unlock_event_id: str = ""


@dataclass(frozen=True)
class OperatorGovernancePausePlan(_TypedControlPlan):
    """Operator-owned all-mode governance expansion kill-switch plan."""

    current_paused: bool = False
    target_paused: bool = False
