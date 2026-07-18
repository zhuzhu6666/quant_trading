"""Compatibility routes for the durable factor lifecycle service."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from backend.core.auth import RequireUser
from pydantic import BaseModel, Field

from backend.services.shadow_service import demote, list_shadows, promote
from backend.services.factor_lifecycle_service import FactorV16Binding
from backend.services.mutation_audit import record_api_mutation

router = APIRouter(prefix="/api/shadow", tags=["shadow"])


class NameRequest(BaseModel):
    name: str


class PromoteRequest(NameRequest):
    expression: str = ""
    artifact_hash: str = ""
    reason: str = "manual shadow promotion preparation"
    evidence_refs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = ""
    v16_command_id: str = ""
    v16_claim_token: str = ""
    v16_target_agent: str = "factor_governance"
    v16_candidate_id: str = ""
    v16_posterior_fingerprint: str = ""
    v16_evidence_fingerprint: str = ""


class DemoteRequest(NameRequest):
    target_stage: Literal["QUARANTINED", "RETIRED"] = "QUARANTINED"
    expression: str = ""
    artifact_hash: str = ""
    reason: str = "manual factor quarantine"
    evidence_refs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = ""


def _actor(user: Any) -> str:
    if isinstance(user, dict):
        identity = user.get("sub") or user.get("username") or user.get("user")
        if identity:
            return f"operator:{identity}"
    return "operator:shadow_api"


@router.get("")
def list_(_user: RequireUser)-> dict:
    return {"shadows": list_shadows()}


@router.post("/promote")
def promote_factor(_user: RequireUser, req: PromoteRequest)-> dict:
    result = promote(
        req.name,
        expression=req.expression,
        artifact_hash=req.artifact_hash,
        actor=_actor(_user),
        reason=req.reason,
        evidence_refs=req.evidence_refs,
        idempotency_key=req.idempotency_key,
        v16=FactorV16Binding(
            command_id=req.v16_command_id,
            claim_token=req.v16_claim_token,
            target_agent=req.v16_target_agent,
            candidate_id=req.v16_candidate_id,
            posterior_fingerprint=req.v16_posterior_fingerprint,
            evidence_fingerprint=req.v16_evidence_fingerprint,
        ),
    )
    record_api_mutation(
        user=_user,
        endpoint="/api/shadow/promote",
        action="promote_shadow_factor",
        status="applied" if result.get("ok", False) else "blocked",
        result={"name": req.name, **result},
    )
    return result


@router.post("/demote")
def demote_factor(_user: RequireUser, req: DemoteRequest)-> dict:
    result = demote(
        req.name,
        target_stage=req.target_stage,
        expression=req.expression,
        artifact_hash=req.artifact_hash,
        actor=_actor(_user),
        reason=req.reason,
        evidence_refs=req.evidence_refs,
        idempotency_key=req.idempotency_key,
    )
    record_api_mutation(
        user=_user,
        endpoint="/api/shadow/demote",
        action="demote_shadow_factor",
        status="applied" if result.get("ok", False) else "blocked",
        result={"name": req.name, **result},
    )
    return result
