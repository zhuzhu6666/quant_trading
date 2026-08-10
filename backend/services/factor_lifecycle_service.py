"""Durable factor lifecycle and runtime-projection contract.

PostgreSQL owns lifecycle truth.  RuntimeConfig and ``RegistryAdapter`` are
post-commit projections only; a failed projection leaves the committed fact in
place and is marked for recovery instead of being reported as an activation.

The production schema is migration-owned.  The SQLite DDL in this module is
only for isolated tests and offline fixtures.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from alpha.registry import factor_registry
from alpha.registry_adapter import (
    RegistryAdapter,
    SOURCE_BUILTIN,
    SOURCE_DISCOVERED,
    SOURCE_REMOVED,
    SOURCE_SHADOW,
)
from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_exists,
)
from backend.services.factor_identity import (
    canonical_factor_id,
    factor_definition_fingerprint,
)
from backend.services.governance_mutation_coordinator import (
    GovernanceMutationCoordinator,
    GovernanceMutationPlan,
)
from config import runtime_config
from config.runtime_config import RuntimeConfig, canonical_runtime_config_payload


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROJECTION_OK = frozenset({"loaded", "current", "healthy"})
_LIVE_PROJECTION_ROLES = frozenset({"live_alpha", "backend", "live_loop"})
_COORDINATOR_PROJECTION_PROCESS_ID = "factor_lifecycle_service"
_COORDINATOR_PROJECTION_BOOT_ID = "canonical"


class FactorLifecycleStage(str, Enum):
    SHADOW = "SHADOW"
    PROMOTION_PREPARED = "PROMOTION_PREPARED"
    ACTIVE = "ACTIVE"
    QUARANTINED = "QUARANTINED"
    RETIRED = "RETIRED"


TERMINAL_STAGES = frozenset(
    {FactorLifecycleStage.QUARANTINED, FactorLifecycleStage.RETIRED}
)

# Promotion is deliberately linear.  A restrictive terminal transition is
# also allowed from a pre-active stage so an operator can cancel a candidate
# without first expanding risk.
ALLOWED_TRANSITIONS: Mapping[FactorLifecycleStage, frozenset[FactorLifecycleStage]] = {
    FactorLifecycleStage.SHADOW: frozenset(
        {
            FactorLifecycleStage.PROMOTION_PREPARED,
            FactorLifecycleStage.QUARANTINED,
            FactorLifecycleStage.RETIRED,
        }
    ),
    FactorLifecycleStage.PROMOTION_PREPARED: frozenset(
        {
            FactorLifecycleStage.ACTIVE,
            FactorLifecycleStage.QUARANTINED,
            FactorLifecycleStage.RETIRED,
        }
    ),
    FactorLifecycleStage.ACTIVE: TERMINAL_STAGES,
    FactorLifecycleStage.QUARANTINED: frozenset(),
    FactorLifecycleStage.RETIRED: frozenset(),
}


class FactorLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class FactorV16Binding:
    command_id: str = ""
    claim_token: str = ""
    target_agent: str = "factor_governance"
    candidate_id: str = ""
    posterior_fingerprint: str = ""
    evidence_fingerprint: str = ""


@dataclass(frozen=True)
class FactorDefinition:
    name: str
    expression: str
    factor_id: str
    definition_fingerprint: str
    artifact_hash: str
    origin: str = "dsl"


@dataclass(frozen=True)
class FactorLifecycleMutation:
    definition: FactorDefinition
    target_stage: FactorLifecycleStage
    actor: str
    reason: str
    source: str
    evidence_refs: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    v16: FactorV16Binding = field(default_factory=FactorV16Binding)
    weight: float | None = None
    new_generation: bool = False


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _p(db_path: str | Path, sql: str) -> str:
    return sql.replace("?", "%s") if is_state_db_path(db_path) else sql


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    return dict(row)


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _artifact_hash(value: str, *, fallback: str = "") -> str:
    normalized = str(value or fallback or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise FactorLifecycleError("stable_artifact_hash_required")
    return normalized


def _builtin_artifact_hash(name: str) -> str:
    """Bind a native factor lifecycle to its executable implementation."""

    func = factor_registry.get(str(name or ""))
    if func is None:
        raise FactorLifecycleError("builtin_factor_callable_missing")
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        code = getattr(func, "__code__", None)
        source = repr(
            (
                getattr(func, "__module__", ""),
                getattr(func, "__qualname__", ""),
                getattr(code, "co_code", b"").hex(),
                getattr(code, "co_consts", ()),
            )
        )
    payload = {
        "schema_version": "builtin_factor_artifact.v1",
        "name": str(name),
        "module": str(getattr(func, "__module__", "")),
        "qualname": str(getattr(func, "__qualname__", "")),
        "source": source,
    }
    return _hash(payload)


class FactorLifecycleService:
    """State machine plus post-commit Registry projection."""

    def __init__(
        self,
        db_path: str | Path = STATE_DB,
        *,
        adapter: RegistryAdapter | None = None,
        projection_stale_after_sec: float = 75.0,
        health_stale_after_sec: float = 180.0,
    ) -> None:
        self.db_path = db_path
        self.adapter = adapter or RegistryAdapter.shared()
        self.projection_stale_after_sec = max(1.0, float(projection_stale_after_sec))
        self.health_stale_after_sec = max(1.0, float(health_stale_after_sec))
        self.coordinator = GovernanceMutationCoordinator(
            db_path,
            publisher=self._publish_committed,
        )

    @property
    def production_state(self) -> bool:
        return is_state_db_path(self.db_path)

    def prepare_promotion(
        self,
        *,
        name: str,
        expression: str = "",
        artifact_hash: str = "",
        actor: str = "operator:shadow_api",
        reason: str = "manual shadow promotion preparation",
        evidence_refs: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
        v16: FactorV16Binding | None = None,
    ) -> dict[str, Any]:
        """Commit SHADOW -> PROMOTION_PREPARED; never activates Registry."""
        try:
            definition = self._definition(name, expression, artifact_hash)
            current = self.get_state(factor_id=definition.factor_id, factor_name=name)
            if not current:
                registered = self.register_shadow(
                    name=name,
                    expression=definition.expression,
                    artifact_hash=definition.artifact_hash,
                    actor=actor,
                    reason="bootstrap durable shadow fact before promotion preparation",
                    evidence_refs=evidence_refs,
                    idempotency_key=f"{idempotency_key}:shadow" if idempotency_key else "",
                )
                if not registered.get("ok"):
                    return registered
                current = self.get_state(factor_id=definition.factor_id, factor_name=name)
            if current and current.get("lifecycle_stage") == FactorLifecycleStage.PROMOTION_PREPARED.value:
                if self._same_definition(current, definition):
                    if str(current.get("runtime_admission") or "") == "degraded":
                        recovered = self.recover_projection(str(current.get("mutation_id") or ""))
                        return {
                            **recovered,
                            "factor_id": definition.factor_id,
                            "lifecycle_stage": FactorLifecycleStage.PROMOTION_PREPARED.value,
                        }
                    return {
                        "ok": True,
                        "status": "already_prepared",
                        "factor_id": definition.factor_id,
                        "lifecycle_stage": FactorLifecycleStage.PROMOTION_PREPARED.value,
                        "mutation_id": str(current.get("mutation_id") or ""),
                    }
            self._require_transition(current, FactorLifecycleStage.PROMOTION_PREPARED)
            mutation = FactorLifecycleMutation(
                definition=definition,
                target_stage=FactorLifecycleStage.PROMOTION_PREPARED,
                actor=actor,
                reason=reason,
                source="factor_lifecycle.prepare_promotion",
                evidence_refs=dict(evidence_refs or {}),
                idempotency_key=idempotency_key,
                v16=v16 or FactorV16Binding(),
            )
            return self._execute(mutation, current=current)
        except Exception as exc:
            return self._failure(exc, name=name)

    def register_shadow(
        self,
        *,
        name: str,
        expression: str = "",
        artifact_hash: str = "",
        actor: str = "system:factor_research",
        reason: str = "register durable shadow candidate",
        evidence_refs: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
        v16: FactorV16Binding | None = None,
    ) -> dict[str, Any]:
        """Persist an observation-only SHADOW fact without live admission."""
        try:
            definition = self._definition(name, expression, artifact_hash)
            current = self.get_state(factor_id=definition.factor_id, factor_name=name)
            if current:
                if (
                    current.get("lifecycle_stage") == FactorLifecycleStage.SHADOW.value
                    and self._same_definition(current, definition)
                ):
                    if str(current.get("runtime_admission") or "") == "degraded":
                        recovered = self.recover_projection(str(current.get("mutation_id") or ""))
                        return {
                            **recovered,
                            "factor_id": definition.factor_id,
                            "lifecycle_stage": FactorLifecycleStage.SHADOW.value,
                        }
                    return {
                        "ok": True,
                        "status": "already_shadow",
                        "factor_id": definition.factor_id,
                        "lifecycle_stage": FactorLifecycleStage.SHADOW.value,
                        "mutation_id": str(current.get("mutation_id") or ""),
                    }
                raise FactorLifecycleError("factor_lifecycle_state_already_exists")
            mutation = FactorLifecycleMutation(
                definition=definition,
                target_stage=FactorLifecycleStage.SHADOW,
                actor=actor,
                reason=reason,
                source="factor_lifecycle.register_shadow",
                evidence_refs=dict(evidence_refs or {}),
                idempotency_key=idempotency_key,
                v16=v16 or FactorV16Binding(),
            )
            return self._execute(mutation, current=None)
        except Exception as exc:
            return self._failure(exc, name=name)

    def reenroll_quarantined_builtin(
        self,
        *,
        name: str,
        actor: str,
        reason: str,
        evidence_refs: Mapping[str, Any] | None,
        idempotency_key: str,
        v16: FactorV16Binding | None = None,
    ) -> dict[str, Any]:
        """Start a new SHADOW generation without reviving a terminal generation."""

        try:
            definition = self._definition(name, name, "")
            if definition.origin != SOURCE_BUILTIN:
                raise FactorLifecycleError("reenroll_builtin_required")
            current = self.get_state(
                factor_id=definition.factor_id,
                factor_name=name,
            )
            if not current:
                raise FactorLifecycleError("factor_lifecycle_state_missing")
            if (
                str(current.get("lifecycle_stage") or "")
                != FactorLifecycleStage.QUARANTINED.value
            ):
                raise FactorLifecycleError("quarantined_generation_required")
            if not self._same_definition(current, definition):
                raise FactorLifecycleError("factor_definition_changed_since_quarantine")
            mutation = FactorLifecycleMutation(
                definition=definition,
                target_stage=FactorLifecycleStage.SHADOW,
                actor=actor,
                reason=reason,
                source="factor_lifecycle.reenroll_quarantined_builtin",
                evidence_refs={
                    **dict(evidence_refs or {}),
                    "previous_generation": int(current.get("generation") or 1),
                    "previous_terminal_mutation_id": str(
                        current.get("mutation_id") or ""
                    ),
                },
                idempotency_key=idempotency_key,
                v16=v16 or FactorV16Binding(),
                new_generation=True,
            )
            return self._execute(mutation, current=current)
        except Exception as exc:
            return self._failure(exc, name=name)

    def activate(
        self,
        *,
        name: str,
        weight: float | None,
        actor: str = "system:factor_governance",
        reason: str = "factor activation",
        evidence_refs: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
        v16: FactorV16Binding | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Activate only after durable, fresh projection and health proofs."""
        try:
            current = self.get_state(factor_name=name)
            if not current:
                raise FactorLifecycleError("factor_lifecycle_state_missing")
            if current.get("lifecycle_stage") == FactorLifecycleStage.ACTIVE.value:
                if str(current.get("runtime_admission") or "") == "degraded":
                    recovered = self.recover_projection(str(current.get("mutation_id") or ""))
                    return {
                        **recovered,
                        "factor_id": str(current.get("factor_id") or ""),
                        "lifecycle_stage": FactorLifecycleStage.ACTIVE.value,
                    }
                return {
                    "ok": True,
                    "status": "already_active",
                    "factor_id": str(current.get("factor_id") or ""),
                    "lifecycle_stage": FactorLifecycleStage.ACTIVE.value,
                    "mutation_id": str(current.get("mutation_id") or ""),
                }
            self._require_transition(current, FactorLifecycleStage.ACTIVE)
            definition = self._definition_from_state(current)
            if weight is None:
                raise FactorLifecycleError("explicit_positive_weight_required")
            explicit_weight = float(weight)
            if explicit_weight <= 0.0:
                raise FactorLifecycleError("explicit_positive_weight_required")
            checked_at = float(now or time.time())
            projection = self._require_loaded_projection(current, now=checked_at)
            health = self._require_fresh_health(current, now=checked_at)
            mutation = FactorLifecycleMutation(
                definition=definition,
                target_stage=FactorLifecycleStage.ACTIVE,
                actor=actor,
                reason=reason,
                source="factor_lifecycle.activate",
                evidence_refs={
                    **dict(evidence_refs or {}),
                    "projection": projection,
                    "health": health,
                },
                idempotency_key=idempotency_key,
                v16=v16 or FactorV16Binding(),
                weight=explicit_weight,
            )
            return self._execute(mutation, current=current)
        except Exception as exc:
            return self._failure(exc, name=name)

    def quarantine(
        self,
        *,
        name: str,
        expression: str = "",
        artifact_hash: str = "",
        actor: str = "operator:shadow_api",
        reason: str = "factor quarantined",
        evidence_refs: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return self._terminal(
            name=name,
            expression=expression,
            artifact_hash=artifact_hash,
            target=FactorLifecycleStage.QUARANTINED,
            actor=actor,
            reason=reason,
            evidence_refs=evidence_refs,
            idempotency_key=idempotency_key,
        )

    def retire(
        self,
        *,
        name: str,
        expression: str = "",
        artifact_hash: str = "",
        actor: str = "operator:shadow_api",
        reason: str = "factor retired",
        evidence_refs: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return self._terminal(
            name=name,
            expression=expression,
            artifact_hash=artifact_hash,
            target=FactorLifecycleStage.RETIRED,
            actor=actor,
            reason=reason,
            evidence_refs=evidence_refs,
            idempotency_key=idempotency_key,
        )

    def acknowledge_projection(
        self,
        *,
        factor_id: str,
        process_role: str,
        process_id: str = "",
        boot_id: str,
        artifact_hash: str,
        generation: int,
        mutation_id: str,
        loaded: bool,
        status: str,
        error_message: str = "",
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        """Record an operational load acknowledgement, bound to prepared fact."""
        try:
            self._prepare_storage()
            role = str(process_role or "").strip().lower()
            if role not in _LIVE_PROJECTION_ROLES:
                raise FactorLifecycleError("invalid_projection_process_role")
            if not str(boot_id or "").strip():
                raise FactorLifecycleError("projection_boot_id_required")
            if str(status or "").strip().lower() not in _PROJECTION_OK | {"error"}:
                raise FactorLifecycleError("invalid_projection_status")
            state = self.get_state(factor_id=factor_id)
            if not state:
                raise FactorLifecycleError("factor_lifecycle_state_missing")
            if int(generation) != int(state.get("generation") or 0):
                raise FactorLifecycleError("projection_generation_mismatch")
            if _artifact_hash(artifact_hash) != str(state.get("artifact_hash") or ""):
                raise FactorLifecycleError("projection_artifact_mismatch")
            if str(mutation_id or "") != str(state.get("mutation_id") or ""):
                raise FactorLifecycleError("projection_mutation_mismatch")
            now = float(observed_at or time.time())
            projection_id = _hash(
                {
                    "factor_id": factor_id,
                    "process_role": role,
                    "process_id": process_id or str(os.getpid()),
                    "boot_id": boot_id,
                }
            )
            conn = self._connect()
            try:
                conn.execute(
                    _p(
                        self.db_path,
                        """INSERT INTO factor_runtime_projection
                           (projection_id, factor_id, factor_name, process_role,
                            process_id, boot_id, generation, artifact_hash,
                            mutation_id, config_version, config_hash, loaded,
                            status, error_message, heartbeat_at, loaded_at,
                            created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(factor_id, process_role, process_id, boot_id)
                           DO UPDATE SET generation=excluded.generation,
                              artifact_hash=excluded.artifact_hash,
                              mutation_id=excluded.mutation_id,
                              config_version=excluded.config_version,
                              config_hash=excluded.config_hash,
                              loaded=excluded.loaded, status=excluded.status,
                              error_message=excluded.error_message,
                              heartbeat_at=excluded.heartbeat_at,
                              loaded_at=excluded.loaded_at,
                              updated_at=excluded.updated_at""",
                    ),
                    (
                        projection_id,
                        str(factor_id),
                        str(state.get("factor_name") or ""),
                        role,
                        str(process_id or os.getpid()),
                        str(boot_id or ""),
                        int(generation),
                        str(state.get("artifact_hash") or ""),
                        str(mutation_id),
                        int(state.get("config_version") or 0),
                        str(state.get("config_hash") or ""),
                        1 if loaded else 0,
                        str(status).lower(),
                        str(error_message or "")[:2000],
                        now,
                        now if loaded else 0.0,
                        now,
                        now,
                    ),
                )
                if loaded and str(status).lower() in _PROJECTION_OK:
                    conn.execute(
                        _p(
                            self.db_path,
                            """UPDATE factor_lifecycle_state
                               SET runtime_admission='projection_acknowledged', updated_at=?
                               WHERE factor_id=? AND mutation_id=?
                                 AND lifecycle_stage='PROMOTION_PREPARED'""",
                        ),
                        (now, str(factor_id), str(mutation_id)),
                    )
                conn.commit()
            finally:
                conn.close()
            return {
                "ok": True,
                "status": "projection_acknowledged" if loaded else "projection_recorded",
                "projection_id": projection_id,
                "factor_id": factor_id,
            }
        except Exception as exc:
            return self._failure(exc, factor_id=factor_id)

    def acknowledge_loaded_prepared_factors(
        self,
        *,
        engine: Any,
        boot_id: str,
        process_id: str = "",
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        """Publish live-alpha acknowledgements for genuinely loaded candidates.

        A prepared factor remains excluded from normal engine voting.  This
        method executes it once against the already-warm buffer, verifies its
        Registry identity/artifact binding, and only then records the exact
        lifecycle generation and mutation.  Any state-store failure is
        returned as a blocked activation result; it never raises into safety
        or broker execution.
        """
        try:
            if not bool(getattr(engine, "is_warm", False)):
                raise FactorLifecycleError("factor_engine_not_warm")
            if not hasattr(engine, "validate_loaded_factor"):
                raise FactorLifecycleError("factor_engine_load_validation_unavailable")
            stable_boot_id = str(boot_id or "").strip()
            if not stable_boot_id:
                raise FactorLifecycleError("projection_boot_id_required")
            states = self.list_states(stages={
                FactorLifecycleStage.PROMOTION_PREPARED.value,
                FactorLifecycleStage.ACTIVE.value,
            })
        except Exception as exc:
            return {
                "ok": False,
                "status": "projection_ack_unavailable",
                "reason": str(exc),
                "acknowledged_count": 0,
                "blocked_count": 0,
                "results": [],
            }

        now = float(observed_at or time.time())
        results: list[dict[str, Any]] = []
        acknowledged = 0
        for state in states:
            factor_id = str(state.get("factor_id") or "")
            name = str(state.get("factor_name") or "")
            stage = str(state.get("lifecycle_stage") or "")
            try:
                definition = self._definition_from_state(state)
                origin = str(state.get("origin") or "dsl").strip().lower()
                if origin != SOURCE_BUILTIN and name not in factor_registry:
                    # A governance/learning process can commit a prepared
                    # DSL definition after this live process booted.  The
                    # committed lifecycle fact is the only authority for
                    # loading that callable; keep it shadow-only until the
                    # live proof below succeeds.
                    self._project_registry(state)
                meta = self.adapter.get_meta(name)
                expected_source = SOURCE_BUILTIN if origin == SOURCE_BUILTIN else (
                    SOURCE_DISCOVERED
                    if stage == FactorLifecycleStage.ACTIVE.value
                    else SOURCE_SHADOW
                )
                if not meta or str(meta.get("source") or "") != expected_source:
                    raise FactorLifecycleError("prepared_factor_source_mismatch")
                if name not in factor_registry:
                    raise FactorLifecycleError("prepared_factor_not_in_registry")
                if origin == SOURCE_BUILTIN:
                    if _builtin_artifact_hash(name) != definition.artifact_hash:
                        raise FactorLifecycleError("registry_factor_artifact_mismatch")
                else:
                    registry_expression = str(meta.get("description") or "").strip()
                    if not registry_expression:
                        raise FactorLifecycleError("registry_factor_expression_missing")
                    registry_fingerprint = factor_definition_fingerprint(
                        registry_expression
                    )
                    if (
                        canonical_factor_id(registry_expression) != definition.factor_id
                        or registry_fingerprint != definition.definition_fingerprint
                    ):
                        raise FactorLifecycleError("registry_factor_identity_mismatch")
                    registry_artifact = str(meta.get("artifact_hash") or "").strip().lower()
                    if registry_artifact:
                        if _artifact_hash(registry_artifact) != definition.artifact_hash:
                            raise FactorLifecycleError(
                                "registry_factor_artifact_mismatch"
                            )
                    elif definition.artifact_hash != definition.definition_fingerprint:
                        raise FactorLifecycleError(
                            "registry_factor_artifact_unverifiable"
                        )
                validation = dict(engine.validate_loaded_factor(name) or {})
                if not validation.get("ok"):
                    raise FactorLifecycleError(
                        f"prepared_factor_load_validation_failed:{validation.get('status', 'unknown')}"
                    )
                if (
                    stage == FactorLifecycleStage.PROMOTION_PREPARED.value
                    and bool(validation.get("voting_admitted"))
                ):
                    raise FactorLifecycleError("prepared_factor_unexpectedly_voting")
                result = self.acknowledge_projection(
                    factor_id=factor_id,
                    process_role="live_alpha",
                    process_id=str(process_id or os.getpid()),
                    boot_id=stable_boot_id,
                    artifact_hash=definition.artifact_hash,
                    generation=int(state.get("generation") or 0),
                    mutation_id=str(state.get("mutation_id") or ""),
                    loaded=True,
                    status="loaded",
                    observed_at=now,
                )
                if not result.get("ok"):
                    raise FactorLifecycleError(str(result.get("reason") or result.get("status") or "ack_failed"))
                acknowledged += 1
                results.append(
                    {
                        **result,
                        "factor_name": name,
                        "lifecycle_stage": stage,
                        "generation": int(state.get("generation") or 0),
                        "mutation_id": str(state.get("mutation_id") or ""),
                        "artifact_hash": definition.artifact_hash,
                        "load_validation": validation,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "ok": False,
                        "status": "projection_ack_blocked",
                        "factor_id": factor_id,
                        "factor_name": name,
                        "reason": str(exc),
                    }
                )
        return {
            "ok": True,
            "status": "projection_ack_complete",
            "prepared_count": sum(
                1
                for state in states
                if str(state.get("lifecycle_stage") or "")
                == FactorLifecycleStage.PROMOTION_PREPARED.value
            ),
            "active_count": sum(
                1
                for state in states
                if str(state.get("lifecycle_stage") or "")
                == FactorLifecycleStage.ACTIVE.value
            ),
            "acknowledged_count": acknowledged,
            "blocked_count": len(states) - acknowledged,
            "results": results,
        }

    def get_state(self, *, factor_id: str = "", factor_name: str = "") -> dict[str, Any]:
        self._prepare_storage()
        conn = self._connect(read_only=True)
        try:
            if factor_id:
                row = conn.execute(
                    _p(self.db_path, "SELECT * FROM factor_lifecycle_state WHERE factor_id=?"),
                    (str(factor_id),),
                ).fetchone()
            elif factor_name:
                row = conn.execute(
                    _p(
                        self.db_path,
                        """SELECT * FROM factor_lifecycle_state
                           WHERE factor_name=? ORDER BY updated_at DESC LIMIT 1""",
                    ),
                    (str(factor_name),),
                ).fetchone()
            else:
                return {}
            return _row_dict(row)
        finally:
            conn.close()

    def list_states(self, *, stages: set[str] | None = None) -> list[dict[str, Any]]:
        self._prepare_storage()
        conn = self._connect(read_only=True)
        try:
            rows = conn.execute(
                "SELECT * FROM factor_lifecycle_state ORDER BY updated_at DESC, factor_name"
            ).fetchall()
            allowed = {str(item).upper() for item in stages} if stages else None
            return [
                _row_dict(row)
                for row in rows
                if allowed is None or str(_row_dict(row).get("lifecycle_stage") or "").upper() in allowed
            ]
        finally:
            conn.close()

    def recover_projection(self, mutation_id: str) -> dict[str, Any]:
        return self.coordinator.replay_projection(str(mutation_id))

    def _prune_legacy_coordinator_projections(self) -> int:
        """Remove exited PID projections before backend registry recovery."""
        self._prepare_storage()
        conn = self._connect()
        try:
            result = conn.execute(
                _p(
                    self.db_path,
                    """DELETE FROM factor_runtime_projection
                       WHERE process_role='governance_coordinator'
                         AND NOT (process_id=? AND boot_id=?)""",
                ),
                (
                    _COORDINATOR_PROJECTION_PROCESS_ID,
                    _COORDINATOR_PROJECTION_BOOT_ID,
                ),
            )
            conn.commit()
            return int(result.rowcount or 0)
        finally:
            conn.close()

    def recover_committed_projections(
        self,
        *,
        limit: int = 100,
        process_role: str = "",
    ) -> dict[str, Any]:
        """Backend-only recovery for current factor lifecycle projections.

        The service-owned publisher is required here because a generic config
        publish cannot project the committed domain state into Registry.
        Learning workers deliberately do not call this boundary.
        """
        if str(process_role or "").strip().lower() != "backend":
            return {
                "ok": False,
                "status": "backend_process_required",
                "attempted_count": 0,
                "current_count": 0,
                "degraded_count": 0,
                "results": [],
            }
        return self.coordinator.recover_committed_projections(
            limit=limit,
            control_surface="factor_lifecycle",
        )

    def restore_committed_registry(
        self,
        *,
        process_role: str = "",
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Rebuild process-local Registry only from committed lifecycle facts.

        Registry memory disappears on restart even when the last coordinator
        projection was marked current.  Replaying only pending/degraded
        intents therefore cannot restore an already-active factor.  This
        bootstrap reads the current lifecycle row joined to its committed
        mutation and reconstructs the callable from the committed canonical
        DSL metadata.  Legacy lifecycle events are deliberately not an input.
        """
        if str(process_role or "").strip().lower() != "backend":
            return {
                "ok": False,
                "status": "backend_process_required",
                "attempted_count": 0,
                "current_count": 0,
                "degraded_count": 0,
                "results": [],
            }
        self._prune_legacy_coordinator_projections()
        self._prepare_storage()
        self.coordinator._prepare_storage()
        safe_limit = max(1, min(5000, int(limit)))
        conn = self._connect(read_only=True)
        try:
            rows = conn.execute(
                _p(
                    self.db_path,
                    """SELECT s.*
                       FROM factor_lifecycle_state s
                       JOIN governance_mutation_intent g
                         ON g.mutation_id=s.mutation_id
                       WHERE g.status='committed'
                         AND g.control_surface='factor_lifecycle'
                       ORDER BY CASE
                                  WHEN s.lifecycle_stage IN ('ACTIVE', 'PROMOTION_PREPARED')
                                  THEN 0
                                  ELSE 1
                                END,
                                s.updated_at DESC, s.factor_name
                       LIMIT ?""",
                ),
                (safe_limit,),
            ).fetchall()
        finally:
            conn.close()

        results: list[dict[str, Any]] = []
        for row in rows:
            state = _row_dict(row)
            mutation_id = str(state.get("mutation_id") or "")
            try:
                self._project_registry(state)
                stage = str(state.get("lifecycle_stage") or "")
                self._record_projection_result(
                    state,
                    loaded=stage == FactorLifecycleStage.ACTIVE.value,
                    status="current",
                    error_message="",
                )
                admission = (
                    "admitted"
                    if stage == FactorLifecycleStage.ACTIVE.value
                    else "awaiting_projection_ack"
                    if stage == FactorLifecycleStage.PROMOTION_PREPARED.value
                    else "blocked"
                )
                self._set_runtime_admission(state, admission)
                self.coordinator._record_projection(
                    mutation_id,
                    status="current",
                    error={},
                )
                results.append(
                    {
                        "ok": True,
                        "status": "current",
                        "factor_id": str(state.get("factor_id") or ""),
                        "factor_name": str(state.get("factor_name") or ""),
                        "lifecycle_stage": stage,
                        "mutation_id": mutation_id,
                    }
                )
            except Exception as exc:
                error = {
                    "stage": "committed_registry_bootstrap",
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
                try:
                    self._record_projection_result(
                        state,
                        loaded=False,
                        status="degraded",
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    pass
                try:
                    self._set_runtime_admission(state, "degraded")
                except Exception:
                    pass
                try:
                    self.coordinator._record_projection(
                        mutation_id,
                        status="degraded",
                        error=error,
                    )
                except Exception:
                    pass
                results.append(
                    {
                        "ok": False,
                        "status": "degraded",
                        "factor_id": str(state.get("factor_id") or ""),
                        "factor_name": str(state.get("factor_name") or ""),
                        "lifecycle_stage": str(state.get("lifecycle_stage") or ""),
                        "mutation_id": mutation_id,
                        "error": error,
                    }
                )
        degraded_count = sum(not bool(item.get("ok")) for item in results)
        return {
            "ok": degraded_count == 0,
            "status": (
                "committed_registry_restored"
                if degraded_count == 0
                else "committed_registry_degraded"
            ),
            "attempted_count": len(results),
            "current_count": len(results) - degraded_count,
            "degraded_count": degraded_count,
            "results": results,
        }

    def _terminal(
        self,
        *,
        name: str,
        expression: str,
        artifact_hash: str,
        target: FactorLifecycleStage,
        actor: str,
        reason: str,
        evidence_refs: Mapping[str, Any] | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        try:
            current = self.get_state(factor_name=name)
            if current:
                if current.get("lifecycle_stage") == target.value:
                    if str(current.get("runtime_admission") or "") == "degraded":
                        recovered = self.recover_projection(str(current.get("mutation_id") or ""))
                        return {
                            **recovered,
                            "factor_id": str(current.get("factor_id") or ""),
                            "lifecycle_stage": target.value,
                        }
                    return {
                        "ok": True,
                        "status": "already_terminal",
                        "factor_id": str(current.get("factor_id") or ""),
                        "lifecycle_stage": target.value,
                        "mutation_id": str(current.get("mutation_id") or ""),
                    }
                definition = self._definition_from_state(current)
            else:
                definition = self._definition(name, expression, artifact_hash)
            self._require_transition(current, target)
            mutation = FactorLifecycleMutation(
                definition=definition,
                target_stage=target,
                actor=actor,
                reason=reason,
                source=f"factor_lifecycle.{target.value.lower()}",
                evidence_refs=dict(evidence_refs or {}),
                idempotency_key=idempotency_key,
            )
            return self._execute(mutation, current=current)
        except Exception as exc:
            return self._failure(exc, name=name)

    def _execute(
        self,
        mutation: FactorLifecycleMutation,
        *,
        current: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        mutation_identity = str(mutation.idempotency_key or _hash({
            "factor_id": mutation.definition.factor_id,
            "target_stage": mutation.target_stage.value,
            "actor": mutation.actor,
            "evidence_refs": dict(mutation.evidence_refs),
        }))
        mutation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"factor-lifecycle:{mutation_identity}"))
        patch = self._runtime_patch(
            mutation,
            current=current,
            mutation_id=mutation_id,
        )
        action_by_stage = {
            FactorLifecycleStage.SHADOW: "register_shadow_factor",
            FactorLifecycleStage.PROMOTION_PREPARED: "promote_factor",
            FactorLifecycleStage.ACTIVE: "promote_factor",
            FactorLifecycleStage.QUARANTINED: "retire_factor",
            FactorLifecycleStage.RETIRED: "retire_factor",
        }
        plan = GovernanceMutationPlan(
            patch=patch,
            source=mutation.source,
            actor=mutation.actor,
            action=action_by_stage[mutation.target_stage],
            run_id=f"factor_lifecycle:{mutation.definition.factor_id}",
            reason=mutation.reason,
            control_surface="factor_lifecycle",
            scope_type="factor",
            scope_key=mutation.definition.factor_id,
            evidence_refs={
                **dict(mutation.evidence_refs),
                "factor_id": mutation.definition.factor_id,
                "definition_fingerprint": mutation.definition.definition_fingerprint,
                "artifact_hash": mutation.definition.artifact_hash,
                "origin": mutation.definition.origin,
                "target_stage": mutation.target_stage.value,
                "new_generation": mutation.new_generation,
                "reason": mutation.reason,
            },
            evidence_fingerprint=mutation.v16.evidence_fingerprint,
            idempotency_key=mutation.idempotency_key,
            mutation_id=mutation_id,
            v16_command_id=mutation.v16.command_id,
            v16_claim_token=mutation.v16.claim_token,
            v16_target_agent=mutation.v16.target_agent,
            v16_candidate_id=mutation.v16.candidate_id,
            v16_posterior_fingerprint=mutation.v16.posterior_fingerprint,
        )

        def writer(conn: Any, mutation_id: str, effective_config: RuntimeConfig) -> Mapping[str, Any]:
            return self._write_lifecycle_state(
                conn,
                mutation_id=mutation_id,
                mutation=mutation,
                effective_config=effective_config,
            )

        result = self.coordinator.execute(plan, transaction_writer=writer)
        return {
            **result,
            "factor_id": mutation.definition.factor_id,
            "factor_name": mutation.definition.name,
            "lifecycle_stage": mutation.target_stage.value,
        }

    def _runtime_patch(
        self,
        mutation: FactorLifecycleMutation,
        *,
        current: Mapping[str, Any] | None,
        mutation_id: str,
    ) -> dict[str, Any]:
        cfg = self._effective_config()
        name = mutation.definition.name
        existing = dict((cfg.factor_signal_config or {}).get(name) or {})
        target = mutation.target_stage
        if target in {
            FactorLifecycleStage.SHADOW,
            FactorLifecycleStage.PROMOTION_PREPARED,
            FactorLifecycleStage.ACTIVE,
        }:
            observation_enabled = bool(
                mutation.definition.origin == SOURCE_BUILTIN
                and target in {
                    FactorLifecycleStage.SHADOW,
                    FactorLifecycleStage.PROMOTION_PREPARED,
                }
            )
            entry = {
                **existing,
                "role": str(existing.get("role") or "alpha"),
                "source": (
                    SOURCE_BUILTIN
                    if mutation.definition.origin == SOURCE_BUILTIN
                    else SOURCE_DISCOVERED
                ),
                "expression": mutation.definition.expression,
                "factor_id": mutation.definition.factor_id,
                "definition_fingerprint": mutation.definition.definition_fingerprint,
                "artifact_hash": mutation.definition.artifact_hash,
                "lifecycle_status": target.value,
                "enabled": target is FactorLifecycleStage.ACTIVE or observation_enabled,
                "committed_mutation_id": str(mutation_id),
            }
            if mutation.new_generation:
                entry.pop("disabled_at", None)
                entry.pop("quarantined_at", None)
                if (
                    mutation.definition.origin == SOURCE_BUILTIN
                    and target is FactorLifecycleStage.SHADOW
                ):
                    entry["autonomous_activation"] = True
        else:
            # A restrictive operation must remain classifiable from before and
            # after facts even for a legacy factor missing RuntimeConfig data.
            entry = {
                **existing,
                "lifecycle_status": target.value,
                "enabled": False,
                "committed_mutation_id": str(mutation_id),
            }
        patch: dict[str, Any] = {"factor_signal_config": {name: entry}}
        if target is FactorLifecycleStage.ACTIVE:
            if mutation.weight is None or float(mutation.weight) <= 0:
                raise FactorLifecycleError("explicit_positive_weight_required")
            entry["weight"] = float(mutation.weight)
            patch["factor_portfolio_weights"] = {name: float(mutation.weight)}
        elif target in TERMINAL_STAGES:
            patch["factor_portfolio_weights"] = {name: 0.0}
        return patch

    def _write_lifecycle_state(
        self,
        conn: Any,
        *,
        mutation_id: str,
        mutation: FactorLifecycleMutation,
        effective_config: RuntimeConfig,
    ) -> dict[str, Any]:
        definition = mutation.definition
        lock_suffix = " FOR UPDATE" if self.production_state else ""
        by_id = conn.execute(
            _p(
                self.db_path,
                f"SELECT * FROM factor_lifecycle_state WHERE factor_id=?{lock_suffix}",
            ),
            (definition.factor_id,),
        ).fetchone()
        current = _row_dict(by_id)
        name_conflict = conn.execute(
            _p(
                self.db_path,
                f"""SELECT factor_id FROM factor_lifecycle_state
                    WHERE factor_name=? AND factor_id<>?{lock_suffix}""",
            ),
            (definition.name, definition.factor_id),
        ).fetchone()
        if name_conflict:
            raise FactorLifecycleError("factor_name_definition_conflict")
        if mutation.new_generation:
            if (
                str(current.get("origin") or "") != SOURCE_BUILTIN
                or str(current.get("lifecycle_stage") or "")
                != FactorLifecycleStage.QUARANTINED.value
                or mutation.target_stage is not FactorLifecycleStage.SHADOW
            ):
                raise FactorLifecycleError("invalid_factor_reenrollment_transition")
        else:
            self._require_transition(current, mutation.target_stage)
        snapshot = conn.execute(
            _p(
                self.db_path,
                """SELECT config_version, config_hash FROM runtime_config_snapshot
                   WHERE mutation_id=? ORDER BY config_version DESC LIMIT 1""",
            ),
            (mutation_id,),
        ).fetchone()
        snapshot_item = _row_dict(snapshot)
        now = time.time()
        generation = (
            int(current.get("generation") or 1) + 1
            if mutation.new_generation
            else int(current.get("generation") or 1)
            if current
            else 1
        )
        activated_at = (
            now
            if mutation.target_stage is FactorLifecycleStage.ACTIVE
            else 0.0
            if mutation.new_generation
            else float(current.get("activated_at") or 0.0)
        )
        retired_at = (
            now
            if mutation.target_stage is FactorLifecycleStage.RETIRED
            else float(current.get("retired_at") or 0.0)
        )
        admission = (
            "awaiting_projection_ack"
            if mutation.target_stage is FactorLifecycleStage.PROMOTION_PREPARED
            else "projection_pending"
            if mutation.target_stage is FactorLifecycleStage.ACTIVE
            else "blocked"
        )
        evidence = {
            **dict(mutation.evidence_refs),
            "reason": mutation.reason,
            "actor": mutation.actor,
        }
        metadata = {
            **_loads(current.get("metadata_json")),
            "expression": definition.expression,
            "identity_version": (
                "builtin_factor_identity.v1"
                if definition.origin == SOURCE_BUILTIN
                else "factor_dsl_ast.v1"
            ),
        }
        if mutation.new_generation:
            metadata["reenrolled_from"] = {
                "generation": int(current.get("generation") or 1),
                "lifecycle_stage": str(current.get("lifecycle_stage") or ""),
                "mutation_id": str(current.get("mutation_id") or ""),
                "reenrolled_at": now,
            }
        conn.execute(
            _p(
                self.db_path,
                """INSERT INTO factor_lifecycle_state
                   (factor_id, factor_name, definition_fingerprint, artifact_hash,
                    origin, lifecycle_stage, generation, runtime_admission,
                    mutation_id, config_version, config_hash, evidence_json,
                    metadata_json, activated_at, retired_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(factor_id) DO UPDATE SET
                      factor_name=excluded.factor_name,
                      definition_fingerprint=excluded.definition_fingerprint,
                      artifact_hash=excluded.artifact_hash,
                      origin=excluded.origin,
                      lifecycle_stage=excluded.lifecycle_stage,
                      generation=excluded.generation,
                      runtime_admission=excluded.runtime_admission,
                      mutation_id=excluded.mutation_id,
                      config_version=excluded.config_version,
                      config_hash=excluded.config_hash,
                      evidence_json=excluded.evidence_json,
                      metadata_json=excluded.metadata_json,
                      activated_at=excluded.activated_at,
                      retired_at=excluded.retired_at,
                      updated_at=excluded.updated_at""",
            ),
            (
                definition.factor_id,
                definition.name,
                definition.definition_fingerprint,
                definition.artifact_hash,
                definition.origin,
                mutation.target_stage.value,
                generation,
                admission,
                mutation_id,
                int(snapshot_item.get("config_version") or 0),
                str(
                    snapshot_item.get("config_hash")
                    or _hash(canonical_runtime_config_payload(effective_config))
                ),
                _json(evidence),
                _json(metadata),
                activated_at,
                retired_at,
                float(current.get("created_at") or now),
                now,
            ),
        )
        return {
            "factor_id": definition.factor_id,
            "factor_name": definition.name,
            "lifecycle_stage": mutation.target_stage.value,
            "generation": generation,
            "artifact_hash": definition.artifact_hash,
            "mutation_id": mutation_id,
        }

    def _publish_committed(self, config: RuntimeConfig, transaction: dict[str, Any]) -> None:
        mutation_id = str(transaction.get("mutation_id") or "")
        # The transaction writer returns a compact summary.  Projection must
        # reload the committed row so expression/identity metadata can never
        # come from a caller-owned or pre-commit object.
        durable_state = self._state_for_mutation(mutation_id)
        state = {
            **durable_state,
            **dict(transaction.get("domain_result") or {}),
        }
        if not state:
            raise FactorLifecycleError("committed_lifecycle_state_missing")
        try:
            stage = str(state.get("lifecycle_stage") or "")
            if stage == FactorLifecycleStage.PROMOTION_PREPARED.value:
                # RuntimeConfig subscribers may synchronously publish the live
                # load ack. Set the pending state first so that ack remains the
                # final projection instead of being overwritten afterwards.
                self._set_runtime_admission(state, "awaiting_projection_ack")
            self._project_registry(state)
            runtime_config.replace(config)
            runtime_loaded = stage == FactorLifecycleStage.ACTIVE.value
            self._record_projection_result(
                state,
                loaded=runtime_loaded,
                status="current",
                error_message="",
            )
            admission = (
                "admitted"
                if stage == FactorLifecycleStage.ACTIVE.value
                else "awaiting_projection_ack"
                if stage == FactorLifecycleStage.PROMOTION_PREPARED.value
                else "blocked"
            )
            if stage != FactorLifecycleStage.PROMOTION_PREPARED.value:
                self._set_runtime_admission(state, admission)
        except Exception as exc:
            # The coordinator will also mark its intent degraded.  Keep these
            # two domain projections best-effort and preserve the original
            # publish error so recovery is never mistaken for a new mutation.
            try:
                self._record_projection_result(
                    state,
                    loaded=False,
                    status="degraded",
                    error_message=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            try:
                self._set_runtime_admission(state, "degraded")
            except Exception:
                pass
            raise

    def _project_registry(self, state: Mapping[str, Any]) -> None:
        name = str(state.get("factor_name") or "")
        stage = str(state.get("lifecycle_stage") or "")
        origin = str(state.get("origin") or "dsl").strip().lower()
        if origin == SOURCE_BUILTIN:
            # Native callables remain code-owned. Lifecycle mutations only
            # govern their RuntimeConfig admission and explicit weight; a
            # terminal transition must not physically delete builtin code.
            if stage not in {
                FactorLifecycleStage.QUARANTINED.value,
                FactorLifecycleStage.RETIRED.value,
            }:
                meta = self.adapter.get_meta(name)
                if (
                    name not in factor_registry
                    or str(meta.get("source") or "") != SOURCE_BUILTIN
                ):
                    raise FactorLifecycleError("builtin_factor_projection_missing")
            return
        if stage in {
            FactorLifecycleStage.SHADOW.value,
            FactorLifecycleStage.PROMOTION_PREPARED.value,
            FactorLifecycleStage.ACTIVE.value,
        }:
            allowed_sources = (
                {SOURCE_SHADOW, SOURCE_DISCOVERED}
                if stage == FactorLifecycleStage.ACTIVE.value
                else {SOURCE_SHADOW}
            )
            source = self._ensure_committed_definition_loaded(
                state,
                allowed_sources=allowed_sources,
            )
            if stage in {
                FactorLifecycleStage.SHADOW.value,
                FactorLifecycleStage.PROMOTION_PREPARED.value,
            }:
                # Preparation loads the callable for live validation but is
                # intentionally not a Registry promotion.
                return
        if stage == FactorLifecycleStage.ACTIVE.value:
            if source == SOURCE_DISCOVERED:
                return
            if source != SOURCE_SHADOW:
                raise FactorLifecycleError(f"runtime_factor_source_invalid:{source or 'missing'}")
            if not self.adapter.promote(
                name,
                new_source=SOURCE_DISCOVERED,
                reason=f"committed lifecycle mutation {state.get('mutation_id', '')}",
            ):
                raise FactorLifecycleError("registry_promotion_failed")
            return
        if stage in {FactorLifecycleStage.QUARANTINED.value, FactorLifecycleStage.RETIRED.value}:
            meta = self.adapter.get_meta(name)
            if not meta or str(meta.get("source") or "") == SOURCE_REMOVED or name not in factor_registry:
                return
            if not self.adapter.unregister(
                name,
                reason=f"committed lifecycle mutation {state.get('mutation_id', '')}",
            ):
                raise FactorLifecycleError("registry_removal_failed")

    def _ensure_committed_definition_loaded(
        self,
        state: Mapping[str, Any],
        *,
        allowed_sources: set[str],
    ) -> str:
        """Load and validate one committed DSL definition as a shadow."""
        name = str(state.get("factor_name") or "")
        metadata = _loads(state.get("metadata_json"))
        expression = str(metadata.get("expression") or "").strip()
        if not expression:
            raise FactorLifecycleError("committed_factor_expression_missing")
        definition = self._definition(
            name,
            expression,
            str(state.get("artifact_hash") or ""),
        )
        if (
            definition.factor_id != str(state.get("factor_id") or "")
            or definition.definition_fingerprint
            != str(state.get("definition_fingerprint") or "")
        ):
            raise FactorLifecycleError("committed_factor_identity_mismatch")
        meta = self.adapter.get_meta(name) or {}
        if name in factor_registry:
            source = str(meta.get("source") or "")
            if source not in allowed_sources:
                raise FactorLifecycleError(
                    f"runtime_factor_source_invalid:{source or 'missing'}"
                )
            registry_expression = str(meta.get("description") or "").strip()
            if not registry_expression:
                raise FactorLifecycleError("runtime_factor_expression_missing")
            try:
                registry_factor_id = canonical_factor_id(registry_expression)
            except Exception as exc:
                raise FactorLifecycleError(
                    "runtime_factor_identity_invalid"
                ) from exc
            if registry_factor_id != definition.factor_id:
                raise FactorLifecycleError("runtime_factor_identity_mismatch")
            registry_artifact = str(meta.get("artifact_hash") or "").strip().lower()
            if registry_artifact and _artifact_hash(registry_artifact) != definition.artifact_hash:
                raise FactorLifecycleError("runtime_factor_artifact_mismatch")
            return source

        from alpha.factor_dsl import evaluate_dsl

        fn = lambda frame, _expression=expression: evaluate_dsl(
            _expression, frame
        )
        if not self.adapter.register_runtime(
            name=name,
            func=fn,
            source=SOURCE_SHADOW,
            description=expression,
        ):
            raise FactorLifecycleError("registry_shadow_projection_failed")
        return SOURCE_SHADOW

    def _record_projection_result(
        self,
        state: Mapping[str, Any],
        *,
        loaded: bool,
        status: str,
        error_message: str,
    ) -> None:
        now = time.time()
        projection_id = _hash(
            {
                "factor_id": state.get("factor_id"),
                "process_role": "governance_coordinator",
                "process_id": _COORDINATOR_PROJECTION_PROCESS_ID,
                "boot_id": _COORDINATOR_PROJECTION_BOOT_ID,
            }
        )
        conn = self._connect()
        try:
            # Coordinator projections describe the latest registry/config
            # projection, not an append-only audit trail.  PID-bound rows made
            # every restart look concurrently current and grew without bound.
            conn.execute(
                _p(
                    self.db_path,
                    """DELETE FROM factor_runtime_projection
                       WHERE factor_id=? AND process_role='governance_coordinator'
                         AND NOT (process_id=? AND boot_id=?)""",
                ),
                (
                    str(state.get("factor_id") or ""),
                    _COORDINATOR_PROJECTION_PROCESS_ID,
                    _COORDINATOR_PROJECTION_BOOT_ID,
                ),
            )
            conn.execute(
                _p(
                    self.db_path,
                    """INSERT INTO factor_runtime_projection
                       (projection_id, factor_id, factor_name, process_role,
                        process_id, boot_id, generation, artifact_hash,
                        mutation_id, config_version, config_hash, loaded, status,
                        error_message, heartbeat_at, loaded_at, created_at, updated_at)
                       VALUES (?, ?, ?, 'governance_coordinator', ?, 'canonical', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(factor_id, process_role, process_id, boot_id)
                       DO UPDATE SET generation=excluded.generation,
                          artifact_hash=excluded.artifact_hash,
                          mutation_id=excluded.mutation_id,
                          config_version=excluded.config_version,
                          config_hash=excluded.config_hash,
                          loaded=excluded.loaded, status=excluded.status,
                          error_message=excluded.error_message,
                          heartbeat_at=excluded.heartbeat_at,
                          loaded_at=excluded.loaded_at,
                          updated_at=excluded.updated_at""",
                ),
                (
                    projection_id,
                    str(state.get("factor_id") or ""),
                    str(state.get("factor_name") or ""),
                    _COORDINATOR_PROJECTION_PROCESS_ID,
                    int(state.get("generation") or 0),
                    str(state.get("artifact_hash") or ""),
                    str(state.get("mutation_id") or ""),
                    int(state.get("config_version") or 0),
                    str(state.get("config_hash") or ""),
                    1 if loaded else 0,
                    str(status),
                    str(error_message or "")[:2000],
                    now,
                    now if loaded else 0.0,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _set_runtime_admission(self, state: Mapping[str, Any], admission: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                _p(
                    self.db_path,
                    """UPDATE factor_lifecycle_state SET runtime_admission=?, updated_at=?
                       WHERE factor_id=? AND mutation_id=?""",
                ),
                (
                    str(admission),
                    time.time(),
                    str(state.get("factor_id") or ""),
                    str(state.get("mutation_id") or ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _require_loaded_projection(self, state: Mapping[str, Any], *, now: float) -> dict[str, Any]:
        conn = self._connect(read_only=True)
        try:
            placeholders = ",".join("?" for _ in _LIVE_PROJECTION_ROLES)
            row = conn.execute(
                _p(
                    self.db_path,
                    f"""SELECT * FROM factor_runtime_projection
                        WHERE factor_id=? AND generation=? AND artifact_hash=?
                          AND mutation_id=? AND loaded=1
                          AND process_role IN ({placeholders})
                        ORDER BY heartbeat_at DESC LIMIT 1""",
                ),
                (
                    str(state.get("factor_id") or ""),
                    int(state.get("generation") or 0),
                    str(state.get("artifact_hash") or ""),
                    str(state.get("mutation_id") or ""),
                    *sorted(_LIVE_PROJECTION_ROLES),
                ),
            ).fetchone()
        finally:
            conn.close()
        projection = _row_dict(row)
        if not projection:
            raise FactorLifecycleError("fresh_loaded_projection_ack_required")
        status = str(projection.get("status") or "").lower()
        heartbeat_at = float(projection.get("heartbeat_at") or 0.0)
        loaded_at = float(projection.get("loaded_at") or 0.0)
        if status not in _PROJECTION_OK or heartbeat_at <= 0 or loaded_at <= 0:
            raise FactorLifecycleError("fresh_loaded_projection_ack_required")
        if now - heartbeat_at > self.projection_stale_after_sec or heartbeat_at > now + 5.0:
            raise FactorLifecycleError("projection_ack_stale")
        return {
            "projection_id": str(projection.get("projection_id") or ""),
            "process_role": str(projection.get("process_role") or ""),
            "heartbeat_at": heartbeat_at,
            "loaded_at": loaded_at,
        }

    def _require_fresh_health(self, state: Mapping[str, Any], *, now: float) -> dict[str, Any]:
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                _p(
                    self.db_path,
                    """SELECT factor, score, status, n_obs, rolling_ic, updated_at
                       FROM factor_health WHERE factor=?""",
                ),
                (str(state.get("factor_name") or ""),),
            ).fetchone()
        finally:
            conn.close()
        health = _row_dict(row)
        if not health:
            raise FactorLifecycleError("fresh_valid_factor_health_required")
        cfg = self._effective_config()
        updated_at = float(health.get("updated_at") or 0.0)
        score = float(health.get("score") or 0.0)
        n_obs = int(health.get("n_obs") or 0)
        rolling_ic = abs(float(health.get("rolling_ic") or 0.0))
        # Activation health gate is aligned with promotion evidence: WATCH +
        # score >= watch threshold is acceptable, not only HEALTHY + >=70.
        # The promotion evidence check (factor_governance_orchestrator
        # _promotion_evidence) already accepts {UNKNOWN, HEALTHY, WATCH} or
        # score >= watch(40); requiring HEALTHY + >=70 at the final lifecycle
        # gate makes promotion evidence pass but activation impossible for
        # healthy-enough WATCH factors. IC/n_obs/freshness stay hard checks.
        status_ok = (
            str(health.get("status") or "").upper() in {"HEALTHY", "WATCH"}
            and score >= float(cfg.factor_health_watch_threshold)
        )
        valid = (
            status_ok
            and n_obs >= int(cfg.factor_health_min_n_obs)
            and rolling_ic >= float(cfg.factor_health_ic_active_threshold)
            and updated_at > 0
            and -5.0 <= now - updated_at <= self.health_stale_after_sec
        )
        if not valid:
            raise FactorLifecycleError("fresh_valid_factor_health_required")
        return {
            "factor": str(health.get("factor") or ""),
            "score": score,
            "status": str(health.get("status") or ""),
            "n_obs": n_obs,
            "rolling_ic": float(health.get("rolling_ic") or 0.0),
            "updated_at": updated_at,
        }

    def _definition(self, name: str, expression: str, artifact_hash: str) -> FactorDefinition:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise FactorLifecycleError("factor_name_required")
        meta = self.adapter.get_meta(clean_name)
        origin = (
            SOURCE_BUILTIN
            if str(meta.get("source") or "").strip().lower() == SOURCE_BUILTIN
            else "dsl"
        )
        clean_expression = str(
            expression
            or (clean_name if origin == SOURCE_BUILTIN else meta.get("description"))
            or ""
        ).strip()
        if not clean_expression:
            raise FactorLifecycleError("canonical_dsl_expression_required")
        if origin == SOURCE_BUILTIN:
            stable_artifact = _artifact_hash(
                artifact_hash,
                fallback=str(meta.get("artifact_hash") or _builtin_artifact_hash(clean_name)),
            )
            fingerprint = _hash(
                {
                    "schema_version": "builtin_factor_identity.v1",
                    "name": clean_name,
                    "artifact_hash": stable_artifact,
                }
            )
            factor_id = f"builtin:{fingerprint}"
        else:
            fingerprint = factor_definition_fingerprint(clean_expression)
            stable_artifact = _artifact_hash(
                artifact_hash,
                fallback=str(meta.get("artifact_hash") or fingerprint),
            )
            factor_id = canonical_factor_id(clean_expression)
        return FactorDefinition(
            name=clean_name,
            expression=clean_expression,
            factor_id=factor_id,
            definition_fingerprint=fingerprint,
            artifact_hash=stable_artifact,
            origin=origin,
        )

    def _definition_from_state(self, state: Mapping[str, Any]) -> FactorDefinition:
        metadata = _loads(state.get("metadata_json"))
        expression = str(metadata.get("expression") or "")
        definition = self._definition(
            str(state.get("factor_name") or ""),
            expression,
            str(state.get("artifact_hash") or ""),
        )
        if not self._same_definition(state, definition):
            raise FactorLifecycleError("stored_factor_identity_invalid")
        return definition

    @staticmethod
    def _same_definition(state: Mapping[str, Any], definition: FactorDefinition) -> bool:
        return (
            str(state.get("factor_id") or "") == definition.factor_id
            and str(state.get("definition_fingerprint") or "") == definition.definition_fingerprint
            and str(state.get("artifact_hash") or "") == definition.artifact_hash
        )

    @staticmethod
    def _require_transition(
        current: Mapping[str, Any] | None,
        target: FactorLifecycleStage,
    ) -> None:
        if not current:
            if target is FactorLifecycleStage.SHADOW:
                return
            source = FactorLifecycleStage.SHADOW
        else:
            try:
                source = FactorLifecycleStage(str(current.get("lifecycle_stage") or ""))
            except ValueError as exc:
                raise FactorLifecycleError("invalid_persisted_lifecycle_stage") from exc
        if source == target:
            raise FactorLifecycleError(f"lifecycle_transition_rejected:{source.value}->{target.value}")
        if target not in ALLOWED_TRANSITIONS[source]:
            raise FactorLifecycleError(f"lifecycle_transition_rejected:{source.value}->{target.value}")

    def _effective_config(self) -> RuntimeConfig:
        self.coordinator._prepare_storage()
        conn = self.coordinator._connect(read_only=True)
        try:
            overlay = self.coordinator._read_overlay(conn)
        finally:
            conn.close()
        return runtime_config.config_from_overlay(overlay, self.db_path)

    def _state_for_mutation(self, mutation_id: str) -> dict[str, Any]:
        self._prepare_storage()
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                _p(
                    self.db_path,
                    """SELECT * FROM factor_lifecycle_state
                       WHERE mutation_id=? ORDER BY updated_at DESC LIMIT 1""",
                ),
                (str(mutation_id),),
            ).fetchone()
            return _row_dict(row)
        finally:
            conn.close()

    def _connect(self, *, read_only: bool = False):
        if self.production_state:
            return get_state_pg_conn(read_only=read_only)
        conn = connect_sqlite(self.db_path, read_only=read_only)
        conn.row_factory = __import__("sqlite3").Row
        return conn

    def _prepare_storage(self) -> None:
        if self.production_state:
            conn = self._connect(read_only=True)
            try:
                missing = [
                    table
                    for table in ("factor_lifecycle_state", "factor_runtime_projection")
                    if not state_table_exists(conn, table)
                ]
                if missing:
                    raise FactorLifecycleError(f"factor_lifecycle_schema_missing:{','.join(missing)}")
            finally:
                conn.close()
            return
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS factor_lifecycle_state (
                    factor_id TEXT PRIMARY KEY,
                    factor_name TEXT NOT NULL DEFAULT '',
                    definition_fingerprint TEXT NOT NULL DEFAULT '',
                    artifact_hash TEXT NOT NULL DEFAULT '',
                    origin TEXT NOT NULL DEFAULT '',
                    lifecycle_stage TEXT NOT NULL DEFAULT 'SHADOW',
                    generation INTEGER NOT NULL DEFAULT 0,
                    runtime_admission TEXT NOT NULL DEFAULT 'blocked',
                    mutation_id TEXT NOT NULL DEFAULT '',
                    config_version INTEGER NOT NULL DEFAULT 0,
                    config_hash TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    activated_at REAL NOT NULL DEFAULT 0.0,
                    retired_at REAL NOT NULL DEFAULT 0.0,
                    created_at REAL NOT NULL DEFAULT 0.0,
                    updated_at REAL NOT NULL DEFAULT 0.0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_factor_lifecycle_unique_name
                    ON factor_lifecycle_state(factor_name);
                CREATE TABLE IF NOT EXISTS factor_runtime_projection (
                    projection_id TEXT PRIMARY KEY,
                    factor_id TEXT NOT NULL,
                    factor_name TEXT NOT NULL DEFAULT '',
                    process_role TEXT NOT NULL DEFAULT '',
                    process_id TEXT NOT NULL DEFAULT '',
                    boot_id TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL DEFAULT 0,
                    artifact_hash TEXT NOT NULL DEFAULT '',
                    mutation_id TEXT NOT NULL DEFAULT '',
                    config_version INTEGER NOT NULL DEFAULT 0,
                    config_hash TEXT NOT NULL DEFAULT '',
                    loaded INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT NOT NULL DEFAULT '',
                    heartbeat_at REAL NOT NULL DEFAULT 0.0,
                    loaded_at REAL NOT NULL DEFAULT 0.0,
                    created_at REAL NOT NULL DEFAULT 0.0,
                    updated_at REAL NOT NULL DEFAULT 0.0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_factor_runtime_projection_identity
                    ON factor_runtime_projection(factor_id, process_role, process_id, boot_id);
                CREATE TABLE IF NOT EXISTS factor_health (
                    factor TEXT PRIMARY KEY,
                    score REAL DEFAULT 50.0,
                    status TEXT DEFAULT 'UNKNOWN',
                    n_obs INTEGER DEFAULT 0,
                    rolling_ic REAL DEFAULT 0.0,
                    updated_at REAL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _failure(exc: Exception, **context: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "blocked",
            "reason": str(exc),
            "error_type": type(exc).__name__,
            **context,
        }
