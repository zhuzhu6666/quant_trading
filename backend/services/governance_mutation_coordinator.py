"""Transactional commit authority for governed runtime mutations.

The coordinator deliberately owns no policy judgement.  Domain services build
typed plans; this module derives risk direction from before/after facts,
serializes one scope, commits the durable intent plus legacy overlay/snapshot in
one PostgreSQL transaction, finalizes V16 in that transaction, and only then
publishes the in-process projection.

The production schema is migration-owned.  SQLite DDL below exists solely for
isolated unit tests and offline fixtures.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
    state_table_exists,
)
from backend.core.db_helpers import dump_json as _json, load_json as _loads
from backend.services.evolution_ledger import (
    ensure_evolution_ledger_tables,
    persist_runtime_config_snapshot,
)
from backend.services.runtime_config_overlay import (
    OVERLAY_ID,
    RuntimeConfigOverlayService,
    _deep_merge,
    _sanitize_patch,
)
from config import runtime_config
from config.runtime_config import RuntimeConfig, runtime_config_hash


INTENT_STATUSES = frozenset(
    {"reserved", "prepared", "committed", "aborted", "rolled_back", "superseded"}
)
PROJECTION_STATUSES = frozenset({"pending", "current", "degraded"})
ACTIVE_INTENT_STATUSES = frozenset({"reserved", "prepared"})
GOVERNANCE_MUTATION_SCHEMA_VERSION = "governance_mutation.v2"


class GovernanceMutationError(RuntimeError):
    pass


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _runtime_config_hash(value: Any) -> str:
    """Hash the shared canonical runtime-config representation."""

    return runtime_config_hash(value)


def _p(db_path: str | Path, sql: str) -> str:
    return sql.replace("?", "%s") if is_state_db_path(db_path) else sql


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    return dict(row)


def _mirror_mutation_stage(
    conn: Any,
    intent: Mapping[str, Any],
    *,
    stage: str,
    stage_timestamp: Any,
) -> None:
    """Mirror one governance mutation lifecycle stage into canonical.

    Fail-open: mirroring problems never abort the legacy transaction; the
    incremental backfill pipeline reconciles any gaps.
    """
    try:
        from backend.services.canonical_v2 import record_governance_mutation_event

        record_governance_mutation_event(
            conn,
            mutation_id=str(intent.get("mutation_id") or ""),
            stage=stage,
            stage_timestamp=stage_timestamp,
            row_fields=dict(intent),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[governance] canonical mutation mirror failed mutation_id=%s stage=%s: %s",
            intent.get("mutation_id"),
            stage,
            exc,
        )


def _deep_slice(payload: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Project ``payload`` down to the leaf paths a patch actually touches.

    The historical ``_slice`` copied the *entire* value of every top-level
    patch key, so one ``register_shadow_factor`` mutation stored three full
    copies of the 500+ entry ``factor_signal_config`` map (~800 KB/row) even
    though its patch touched exactly one factor.  This projection keeps only
    the branches the patch reaches; untouched siblings are omitted.

    Classification equivalence: :func:`classify_governance_risk` walks
    changed leaves only, and for any path the patch touches, this projection
    preserves the same before/target leaf values as a full slice (absent
    branches stay absent on both sides).  Paths outside the patch are
    unchanged by definition of the overlay merge and contribute no leaves.
    """

    projected: dict[str, Any] = {}

    def _walk(src: Mapping[str, Any], pat: Mapping[str, Any], out: dict[str, Any]) -> None:
        for key, patch_value in pat.items():
            present = isinstance(src, Mapping) and key in src
            src_value = src.get(key) if present else None
            if isinstance(patch_value, Mapping):
                if isinstance(src_value, Mapping):
                    branch: dict[str, Any] = {}
                    _walk(src_value, patch_value, branch)
                    # An empty branch means that the patch only adds keys
                    # absent from this source.  Do not fall back to the full
                    # source mapping: doing so reintroduces all untouched
                    # siblings into the before projection.
                    if branch:
                        out[key] = branch
                elif present:
                    # _deep_merge replaces a non-mapping value with a mapping;
                    # retain that old scalar so the before/after classifier can
                    # see the replacement without copying unrelated data.
                    out[key] = deepcopy(src_value)
                # An absent mapping is represented by omission.  The target
                # projection contains the newly added leaves, so the reader
                # still observes an additive change.
            elif present:
                out[key] = deepcopy(src_value)
            # Absent scalar keys are omitted for the same overlay semantics.

    if isinstance(payload, Mapping):
        _walk(payload, patch, projected)
    return projected


def _changed_leaves(before: Any, target: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any, Any]]:
    # Walk additive/removal mappings down to their typed leaves.  Treating a
    # newly added restrictive object as one opaque value would incorrectly
    # classify ``{enabled: false, weight: 0}`` as expansionary.
    if isinstance(before, Mapping) or isinstance(target, Mapping):
        before_mapping = before if isinstance(before, Mapping) else {}
        target_mapping = target if isinstance(target, Mapping) else {}
        changes: list[tuple[tuple[str, ...], Any, Any]] = []
        for key in sorted(set(before_mapping) | set(target_mapping), key=str):
            changes.extend(
                _changed_leaves(
                    before_mapping.get(key),
                    target_mapping.get(key),
                    (*path, str(key)),
                )
            )
        return changes
    if before == target:
        return []
    return [(path, before, target)]


@dataclass(frozen=True)
class RiskClassification:
    risk_class: str
    tightening_paths: tuple[str, ...] = ()
    expansion_paths: tuple[str, ...] = ()

    @property
    def v16_required(self) -> bool:
        return self.risk_class == "risk_expanding"

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_class": self.risk_class,
            "tightening_paths": list(self.tightening_paths),
            "expansion_paths": list(self.expansion_paths),
            "v16_required": self.v16_required,
            "classification_source": "coordinator_before_after",
        }


def classify_governance_risk(before: Mapping[str, Any], target: Mapping[str, Any]) -> RiskClassification:
    """Classify risk from facts; callers cannot self-report an exemption."""
    tightening: list[str] = []
    expansion: list[str] = []
    incident_rank = {
        "normal": 0,
        "shadow_only": 1,
        "no_new_risk": 2,
        "only_close": 3,
        "frozen": 4,
    }
    lifecycle_rank = {
        "active": 0,
        "promotion_prepared": 1,
        "shadow": 2,
        "quarantined": 3,
        "retired": 4,
    }
    model_stage_rank = {
        "shadow": 0,
        "demo_canary": 0,
        "demo_active": 0,
        "quarantined": 1,
        "retired": 2,
    }
    autonomy_mode_rank = {
        "manual": 0,
        "live_candidate": 0,
        "demo_nursery": 1,
        "demo_autonomous": 2,
        "live_autonomous": 3,
    }
    for path, old, new in _changed_leaves(before, target):
        dotted = ".".join(path) or "<root>"
        lower = dotted.lower()
        if len(path) >= 3 and path[0] == "factor_signal_config":
            # Factor identity/artifact fields are descriptive.  They must not
            # turn an otherwise restrictive SHADOW/QUARANTINED projection into
            # a risk expansion merely because the committed projection records
            # a new fingerprint or mutation id.  The executable controls below
            # (lifecycle, enabled and weight) still decide the risk class.
            leaf = path[-1].lower()
            if leaf in {
                "role",
                "source",
                "expression",
                "factor_id",
                "definition_fingerprint",
                "artifact_hash",
                "committed_mutation_id",
                "direction",
                "polarity",
                "normalizer",
            }:
                factor_name = path[1]
                target_entry = (
                    target.get("factor_signal_config", {}).get(factor_name, {})
                    if isinstance(target.get("factor_signal_config"), Mapping)
                    else {}
                )
                target_stage = str(
                    target_entry.get("lifecycle_status")
                    or target_entry.get("lifecycle_stage")
                    or ""
                ).lower()
                target_enabled = target_entry.get("enabled") is True
                if not target_enabled and target_stage != "active":
                    continue
        if lower.endswith("runtime_incident_mode"):
            old_rank = incident_rank.get(str(old or "").lower(), -1)
            new_rank = incident_rank.get(str(new or "").lower(), -1)
            (tightening if new_rank > old_rank >= 0 else expansion).append(dotted)
            continue
        if lower.endswith("autonomy_mode"):
            old_rank = autonomy_mode_rank.get(str(old or "").lower(), -1)
            new_rank = autonomy_mode_rank.get(str(new or "").lower(), -1)
            if old_rank < 0 or new_rank < 0:
                expansion.append(dotted)
            else:
                (tightening if new_rank < old_rank else expansion).append(dotted)
            continue
        if lower.endswith("autonomy_expansion_frozen") or lower.endswith(
            "governance_expansion_paused"
        ):
            if isinstance(old, bool) and isinstance(new, bool):
                (tightening if new and not old else expansion).append(dotted)
            else:
                expansion.append(dotted)
            continue
        if lower.endswith("live_autonomy_unlock_id"):
            old_value = str(old or "").strip()
            new_value = str(new or "").strip()
            (tightening if old_value and not new_value else expansion).append(dotted)
            continue
        if "model_influence_config.models." in lower and lower.endswith(".stage"):
            old_rank = model_stage_rank.get(str(old or "").lower(), -1)
            new_rank = model_stage_rank.get(str(new or "").lower(), -1)
            inactive_bootstrap = old_rank < 0 and new_rank >= model_stage_rank["quarantined"]
            (
                tightening
                if inactive_bootstrap or new_rank > old_rank >= 0
                else expansion
            ).append(dotted)
            continue
        if lower.endswith("lifecycle_status") or lower.endswith("lifecycle_stage"):
            old_rank = lifecycle_rank.get(str(old or "").lower(), -1)
            new_rank = lifecycle_rank.get(str(new or "").lower(), -1)
            # A legacy or research-only control may have no RuntimeConfig
            # projection yet. Adding SHADOW/terminal state is still inactive
            # and must not depend on V16 availability.
            inactive_bootstrap = old_rank < 0 and new_rank >= lifecycle_rank["shadow"]
            (tightening if inactive_bootstrap or new_rank > old_rank >= 0 else expansion).append(dotted)
            continue
        if old is None and isinstance(new, bool):
            restrictive_bool = any(
                token in lower
                for token in ("enabled", "unlocked", "send_orders", "allow", "active")
            )
            if restrictive_bool and new is False:
                tightening.append(dotted)
            else:
                expansion.append(dotted)
            continue
        if isinstance(old, bool) and isinstance(new, bool):
            restrictive_bool = any(
                token in lower
                for token in ("enabled", "unlocked", "send_orders", "allow", "active")
            )
            if restrictive_bool:
                (tightening if old and not new else expansion).append(dotted)
            else:
                expansion.append(dotted)
            continue
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            decreasing_tightens = any(
                token in lower
                for token in (
                    "weight",
                    "risk",
                    "volume",
                    "leverage",
                    "exposure",
                    "max_",
                    "kelly_fraction",
                    "position_size",
                )
            )
            increasing_tightens = any(
                token in lower
                for token in (
                    "min_stop_distance",
                    "safety_buffer",
                    "evidence_min",
                    "min_health",
                    "min_abs_signal_score",
                    "strong_signal_override",
                )
            )
            if decreasing_tightens:
                (tightening if float(new) < float(old) else expansion).append(dotted)
            elif increasing_tightens:
                (tightening if float(new) > float(old) else expansion).append(dotted)
            else:
                expansion.append(dotted)
            continue
        if old is None and isinstance(new, (int, float)):
            decreasing_tightens = any(
                token in lower
                for token in (
                    "weight",
                    "risk",
                    "volume",
                    "leverage",
                    "exposure",
                    "max_",
                    "kelly_fraction",
                    "position_size",
                )
            )
            if decreasing_tightens and float(new) == 0.0:
                tightening.append(dotted)
            else:
                expansion.append(dotted)
            continue
        # Unknown/template/string changes are expansionary by default.  A
        # domain service may not bypass V16 by naming an action "rollback".
        expansion.append(dotted)
    if expansion:
        risk_class = "risk_expanding"
    elif tightening:
        risk_class = "risk_tightening"
    else:
        risk_class = "no_change"
    return RiskClassification(
        risk_class=risk_class,
        tightening_paths=tuple(sorted(set(tightening))),
        expansion_paths=tuple(sorted(set(expansion))),
    )


@dataclass(frozen=True)
class GovernanceMutationPlan:
    patch: Mapping[str, Any]
    source: str
    actor: str = "system:governance_mutation"
    action: str = ""
    run_id: str = ""
    reason: str = ""
    control_surface: str = "runtime_config"
    scope_type: str = "runtime_config"
    scope_key: str = "global"
    rollback: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Mapping[str, Any] = field(default_factory=dict)
    evidence_fingerprint: str = ""
    idempotency_key: str = ""
    mutation_id: str = ""
    v16_command_id: str = ""
    v16_claim_token: str = ""
    v16_target_agent: str = ""
    v16_candidate_id: str = ""
    v16_posterior_fingerprint: str = ""
    domain_only: bool = False
    domain_before: Mapping[str, Any] = field(default_factory=dict)
    domain_target: Mapping[str, Any] = field(default_factory=dict)


Publisher = Callable[[RuntimeConfig, dict[str, Any]], Any]
TransactionWriter = Callable[[Any, str, RuntimeConfig], Mapping[str, Any] | None]
FaultInjector = Callable[[str], None]


class GovernanceMutationCoordinator:
    """Unique durable commit boundary for governance mutations."""

    def __init__(
        self,
        db_path: str | Path = STATE_DB,
        *,
        overlay: RuntimeConfigOverlayService | None = None,
        publisher: Publisher | None = None,
    ) -> None:
        self.db_path = db_path
        self.overlay = overlay or RuntimeConfigOverlayService(db_path)
        self._domain_publisher_supplied = publisher is not None
        self.publisher = publisher or self._publish_runtime_config

    @property
    def production_state(self) -> bool:
        return is_state_db_path(self.db_path) and Path(self.db_path).resolve() == Path(STATE_DB).resolve()

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": GOVERNANCE_MUTATION_SCHEMA_VERSION,
            "intent_states": [
                "reserved",
                "prepared",
                "committed",
                "aborted",
                "rolled_back",
                "superseded",
            ],
            "projection_states": ["pending", "current", "degraded"],
            "single_scope_serialized": True,
            "legacy_overlay_snapshot_same_transaction": True,
            "v16_finalized_same_transaction": True,
            "publish_after_commit": True,
            "risk_classification_from_before_after": True,
            "risk_tightening_v16_exemption": True,
            "does_not_submit_orders": True,
        }

    def execute(
        self,
        plan: GovernanceMutationPlan,
        *,
        transaction_writer: TransactionWriter | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> dict[str, Any]:
        """Reserve, commit, finalize, then publish one typed mutation plan."""
        try:
            reserved = self.reserve(plan)
        except Exception as exc:
            return self._failed("reserve_failed", exc)
        if reserved.get("status") == "committed":
            if str(reserved.get("projection_status") or "") == "degraded":
                return self.replay_projection(str(reserved.get("mutation_id") or ""))
            return {**reserved, "ok": True, "idempotent": True, "boundary": self.boundary()}
        if reserved.get("status") != "reserved" or not reserved.get("ok"):
            return {**reserved, "boundary": self.boundary()}

        mutation_id = str(reserved["mutation_id"])
        claim: dict[str, Any] = {}
        committed = False
        try:
            self._fault(fault_injector, "after_reserved")
            claim = self._claim_v16(plan, reserved)
            if not claim.get("allowed"):
                self._abort(mutation_id, "v16_claim", claim.get("status", "v16_command_required"))
                return {
                    "ok": False,
                    "status": "aborted",
                    "reason": "v16_command_required",
                    "mutation_id": mutation_id,
                    "risk_classification": reserved.get("risk_classification", {}),
                    "v16_authority": claim,
                    "boundary": self.boundary(),
                }
            self._fault(fault_injector, "after_claimed")
            transaction = self._commit_transaction(
                plan,
                reserved,
                claim,
                transaction_writer=transaction_writer,
                fault_injector=fault_injector,
            )
            committed = True
        except Exception as exc:
            if not committed:
                self._abort(mutation_id, "transaction", f"{type(exc).__name__}: {exc}")
                self._release_claim(claim, reason="governance_transaction_aborted")
            return self._failed("aborted" if not committed else "committed_projection_degraded", exc, mutation_id)

        try:
            self._fault(fault_injector, "after_commit_before_publish")
            projection = self._publish_committed(transaction)
        except Exception as exc:
            projection = self._record_projection(
                mutation_id,
                status="degraded",
                error={"type": type(exc).__name__, "message": str(exc), "stage": "publish"},
            )
        return {
            "ok": projection.get("projection_status") == "current",
            "status": "committed" if projection.get("projection_status") == "current" else "committed_projection_degraded",
            "mutation_id": mutation_id,
            "idempotency_key": reserved.get("idempotency_key", ""),
            "risk_classification": reserved.get("risk_classification", {}),
            "projection_status": projection.get("projection_status", "degraded"),
            "snapshot": transaction.get("snapshot", {}),
            "overlay_hash": transaction.get("overlay_hash", ""),
            "v16_authority": {**claim, "finalized": transaction.get("v16_finalized", {})},
            "domain_result": transaction.get("domain_result", {}),
            "domain_hash": transaction.get("domain_hash", ""),
            "boundary": self.boundary(),
        }

    def reserve(self, plan: GovernanceMutationPlan) -> dict[str, Any]:
        sanitized = _sanitize_patch(dict(plan.patch or {}))
        domain_before = dict(plan.domain_before or {})
        domain_target = dict(plan.domain_target or {})
        domain_only = bool(plan.domain_only)
        if not sanitized and not (
            domain_only and domain_target and _hash(domain_before) != _hash(domain_target)
        ):
            return {"ok": False, "status": "empty_governance_patch", "boundary": self.boundary()}
        if (
            "governance_expansion_paused" in sanitized
            and str(plan.actor or "").startswith("system:")
        ):
            return {
                "ok": False,
                "status": "operator_governance_pause_required",
                "reason": "autonomous_services_cannot_modify_operator_kill_switch",
                "boundary": self.boundary(),
            }
        self._prepare_storage()
        evidence_refs = dict(plan.evidence_refs or {})
        evidence_fingerprint = str(plan.evidence_fingerprint or _hash(evidence_refs))
        idempotency_key = str(
            plan.idempotency_key
            or _hash(
                {
                    "control_surface": plan.control_surface,
                    "scope_type": plan.scope_type,
                    "scope_key": plan.scope_key,
                    "action": plan.action,
                    "source": plan.source,
                    "run_id": plan.run_id,
                    "patch": sanitized,
                    "domain_only": domain_only,
                    "domain_before": domain_before,
                    "domain_target": domain_target,
                    "evidence_fingerprint": evidence_fingerprint,
                }
            )
        )
        mutation_id = str(plan.mutation_id or f"gmut_{uuid.uuid4().hex}")
        conn = self._connect()
        try:
            self._begin_scope_write(conn, plan)
            existing = conn.execute(
                _p(self.db_path, "SELECT * FROM governance_mutation_intent WHERE idempotency_key=?"),
                (idempotency_key,),
            ).fetchone()
            if existing:
                conn.commit()
                return self._intent_payload(_row_dict(existing))
            active = conn.execute(
                _p(
                    self.db_path,
                    """SELECT mutation_id, status FROM governance_mutation_intent
                       WHERE control_surface=? AND scope_type=? AND scope_key=?
                         AND status IN ('reserved', 'prepared')
                       ORDER BY updated_at DESC LIMIT 1""",
                ),
                (str(plan.control_surface), str(plan.scope_type), str(plan.scope_key)),
            ).fetchone()
            if active:
                conn.rollback()
                item = _row_dict(active)
                return {
                    "ok": False,
                    "status": "scope_busy",
                    "blocking_mutation_id": str(item.get("mutation_id") or ""),
                    "blocking_status": str(item.get("status") or ""),
                }
            current_overlay = self._read_overlay(conn)
            current_config = runtime_config.config_from_overlay(current_overlay, self.db_path)
            target_overlay = _deep_merge(current_overlay, sanitized)
            target_config = runtime_config.config_from_overlay(target_overlay, self.db_path)
            # Deep projection: store only the branches the patch touches so a
            # single-factor mutation does not persist three full copies of the
            # factor map.  Classification and the prepare-stage drift check
            # both consume the same projected shape, keeping them consistent.
            before = domain_before if domain_only else _deep_slice(current_config.to_dict(), sanitized)
            target = domain_target if domain_only else _deep_slice(target_config.to_dict(), sanitized)
            rollback = dict(plan.rollback or before)
            classification = classify_governance_risk(before, target)
            current_paused = bool(
                getattr(current_config, "governance_expansion_paused", False)
            )
            operator_pause_resume = bool(
                set(sanitized) == {"governance_expansion_paused"}
                and current_paused
                and target.get("governance_expansion_paused") is False
                and not str(plan.actor or "").startswith("system:")
            )
            operator_demo_control = (
                runtime_config.operator_bounded_demo_control_exempt(
                    actor=plan.actor,
                    patch=sanitized,
                    cfg=current_config,
                )
                or runtime_config.operator_classic_builtin_factor_activation_exempt(
                    actor=plan.actor,
                    patch=sanitized,
                    cfg=current_config,
                )
            )
            if (
                current_paused
                and classification.risk_class == "risk_expanding"
                and not operator_pause_resume
                and not operator_demo_control
            ):
                conn.rollback()
                return {
                    "ok": False,
                    "status": "blocked_governance_expansion_paused",
                    "reason": "operator_all_mode_governance_expansion_pause",
                    "risk_classification": classification.to_dict(),
                    "boundary": self.boundary(),
                }
            target_hash = _runtime_config_hash(target_config.to_dict())
            domain_hash = _hash(
                {
                    "control_surface": plan.control_surface,
                    "scope_type": plan.scope_type,
                    "scope_key": plan.scope_key,
                    "target": target,
                }
            )
            now = time.time()
            conn.execute(
                _p(
                    self.db_path,
                    """INSERT INTO governance_mutation_intent
                       (mutation_id, idempotency_key, control_surface, scope_type,
                        scope_key, action, actor, source, producer, run_id,
                        risk_class, status, projection_status, before_json,
                        target_json, patch_json, rollback_json, evidence_refs_json,
                        evidence_fingerprint, v16_command_id, target_config_version,
                        target_config_hash, committed_config_version,
                        committed_config_hash, domain_hash, error_stage, error_type,
                        error_message, reserved_at, prepared_at, committed_at,
                        aborted_at, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', 'pending',
                               ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, '', ?, '', '', '',
                               ?, 0.0, 0.0, 0.0, ?, ?)""",
                ),
                (
                    mutation_id,
                    idempotency_key,
                    str(plan.control_surface),
                    str(plan.scope_type),
                    str(plan.scope_key),
                    str(plan.action or plan.source),
                    str(plan.actor),
                    str(plan.source),
                    str(plan.v16_target_agent or ""),
                    str(plan.run_id),
                    classification.risk_class,
                    _json(before),
                    _json(target),
                    _json(sanitized),
                    _json(rollback),
                    _json(evidence_refs),
                    evidence_fingerprint,
                    str(plan.v16_command_id or ""),
                    target_hash,
                    domain_hash,
                    now,
                    now,
                    now,
                ),
            )
            # ── canonical 增量镜像（reserved 阶段；同事务、幂等、fail-open）──
            try:
                from backend.services.canonical_v2 import record_governance_mutation_event
                record_governance_mutation_event(
                    conn,
                    mutation_id=mutation_id,
                    stage="reserved",
                    stage_timestamp=now,
                    row_fields={
                        "mutation_id": mutation_id,
                        "idempotency_key": idempotency_key,
                        "control_surface": str(plan.control_surface),
                        "scope_type": str(plan.scope_type),
                        "scope_key": str(plan.scope_key),
                        "action": str(plan.action or plan.source),
                        "actor": str(plan.actor),
                        "source": str(plan.source),
                        "producer": str(plan.v16_target_agent or ""),
                        "run_id": str(plan.run_id),
                        "risk_class": classification.risk_class,
                        "status": "reserved",
                        "evidence_fingerprint": evidence_fingerprint,
                        "evidence_refs": evidence_refs,
                        "v16_command_id": str(plan.v16_command_id or ""),
                        "target_config_version": 0,
                        "target_config_hash": target_hash,
                        "committed_config_version": 0,
                        "committed_config_hash": "",
                        "domain_hash": domain_hash,
                        "before_json": before,
                        "target_json": target,
                        "patch_json": sanitized,
                        "rollback_json": rollback,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[governance] canonical mutation mirror failed mutation_id=%s stage=reserved: %s",
                    mutation_id,
                    exc,
                )
            conn.commit()
            return {
                "ok": True,
                "status": "reserved",
                "mutation_id": mutation_id,
                "idempotency_key": idempotency_key,
                "before": before,
                "target": target,
                "patch": sanitized,
                "rollback": rollback,
                "evidence_fingerprint": evidence_fingerprint,
                "target_config_hash": target_hash,
                "domain_hash": domain_hash,
                "risk_classification": classification.to_dict(),
                "operator_bounded_demo_control_exempt": operator_demo_control,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def replay_projection(self, mutation_id: str) -> dict[str, Any]:
        """Replay one recoverable post-commit projection without config rollback.

        The intent's domain fact remains the projection subject, but RuntimeConfig
        must always come from the *current durable overlay*.  Reconstructing the
        historical snapshot for ``mutation_id`` could otherwise overwrite a
        later committed mutation in this process.
        """
        self._prepare_storage()
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                _p(self.db_path, "SELECT * FROM governance_mutation_intent WHERE mutation_id=?"),
                (str(mutation_id),),
            ).fetchone()
            item = _row_dict(row)
            if str(item.get("status") or "") != "committed":
                return {"ok": False, "status": "projection_not_committed", "mutation_id": mutation_id}
            projection_status = str(item.get("projection_status") or "")
            if projection_status not in {"pending", "degraded"}:
                return {
                    "ok": projection_status == "current",
                    "status": "projection_already_current"
                    if projection_status == "current"
                    else "projection_not_recoverable",
                    "projection_status": projection_status,
                    "mutation_id": mutation_id,
                    "idempotent": projection_status == "current",
                }
            if (
                str(item.get("control_surface") or "") == "factor_lifecycle"
                and not self._domain_publisher_supplied
            ):
                return {
                    "ok": False,
                    "status": "factor_projection_requires_domain_publisher",
                    "projection_status": projection_status,
                    "mutation_id": mutation_id,
                }
            if not self._is_current_scope_intent(conn, item):
                return {
                    "ok": False,
                    "status": "projection_not_current_scope",
                    "projection_status": projection_status,
                    "mutation_id": mutation_id,
                }
        finally:
            conn.close()

        # A concurrent commit on another scope may advance the shared overlay
        # while this projection is being published.  Retry against the newest
        # durable token so recovery cannot leave an older full config in memory.
        for _attempt in range(3):
            try:
                config, durable_token = self._effective_durable_config()
                transaction = {
                    "mutation_id": mutation_id,
                    "effective_config": config,
                    "intent": self._intent_payload(item),
                    "recovery": True,
                    "durable_config_token": durable_token,
                }
                self.publisher(config, transaction)
                latest_config, latest_token = self._effective_durable_config()
                if latest_token != durable_token:
                    config = latest_config
                    continue
                conn = self._connect(read_only=True)
                try:
                    if not self._is_current_scope_intent(conn, item):
                        return {
                            "ok": False,
                            "status": "projection_not_current_scope",
                            "projection_status": projection_status,
                            "mutation_id": mutation_id,
                        }
                finally:
                    conn.close()
                return self._record_projection(mutation_id, status="current", error={})
            except Exception as exc:
                return self._record_projection(
                    mutation_id,
                    status="degraded",
                    error={"stage": "replay", "type": type(exc).__name__, "message": str(exc)},
                )
        return self._record_projection(
            mutation_id,
            status="degraded",
            error={
                "stage": "replay",
                "type": "DurableConfigChanged",
                "message": "durable config changed repeatedly during projection recovery",
            },
        )

    def recover_committed_projections(
        self,
        *,
        limit: int = 100,
        control_surface: str = "",
        exclude_control_surfaces: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """Recover only latest committed pending/degraded intents per scope.

        Callers choose the publisher appropriate to the control surface.  The
        backend uses ``FactorLifecycleService`` for factor lifecycle intents so
        Registry projection is never faked by a generic RuntimeConfig publish.
        """
        self._prepare_storage()
        safe_limit = max(1, min(1000, int(limit)))
        excluded = {str(value) for value in exclude_control_surfaces}
        conn = self._connect(read_only=True)
        try:
            rows = conn.execute(
                """SELECT * FROM governance_mutation_intent
                   WHERE status='committed'
                     AND projection_status IN ('pending', 'degraded')
                   ORDER BY committed_config_version DESC, committed_at DESC,
                            created_at DESC, mutation_id DESC"""
            ).fetchall()
            candidates: list[dict[str, Any]] = []
            skipped_noncurrent = 0
            skipped_surface = 0
            for row in rows:
                item = _row_dict(row)
                surface = str(item.get("control_surface") or "")
                if (control_surface and surface != str(control_surface)) or surface in excluded:
                    skipped_surface += 1
                    continue
                if not self._is_current_scope_intent(conn, item):
                    skipped_noncurrent += 1
                    continue
                candidates.append(item)
                if len(candidates) >= safe_limit:
                    break
        finally:
            conn.close()

        results = [
            self.replay_projection(str(item.get("mutation_id") or ""))
            for item in candidates
        ]
        current_count = sum(
            str(result.get("projection_status") or "") == "current" for result in results
        )
        degraded_count = sum(
            str(result.get("projection_status") or "") == "degraded" for result in results
        )
        return {
            "ok": degraded_count == 0,
            "status": "projection_recovery_complete"
            if degraded_count == 0
            else "projection_recovery_degraded",
            "attempted_count": len(results),
            "current_count": current_count,
            "degraded_count": degraded_count,
            "skipped_noncurrent_count": skipped_noncurrent,
            "skipped_surface_count": skipped_surface,
            "results": results,
        }

    def recover_stale_intents(self, *, stale_after_seconds: float = 300.0) -> dict[str, Any]:
        """Abort transaction-less reservations left by a dead worker."""
        self._prepare_storage()
        cutoff = time.time() - max(15.0, float(stale_after_seconds))
        conn = self._connect()
        try:
            if not is_state_db_path(self.db_path):
                conn.execute("BEGIN IMMEDIATE")
            result = conn.execute(
                _p(
                    self.db_path,
                    """UPDATE governance_mutation_intent
                       SET status='aborted', aborted_at=?, error_stage='recovery',
                           error_type='StaleIntent', error_message='stale reserved/prepared intent',
                           updated_at=?
                       WHERE status IN ('reserved', 'prepared') AND updated_at<?""",
                ),
                (time.time(), time.time(), cutoff),
            )
            conn.commit()
            return {"ok": True, "status": "recovered", "aborted_count": int(result.rowcount or 0)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_rolled_back(self, mutation_id: str, *, rollback_mutation_id: str) -> dict[str, Any]:
        return self._mark_committed_terminal(
            mutation_id,
            status="rolled_back",
            link_column="rollback_mutation_id",
            link_value=rollback_mutation_id,
            timestamp_column="rolled_back_at",
        )

    def mark_superseded(self, mutation_id: str, *, superseded_by_mutation_id: str) -> dict[str, Any]:
        return self._mark_committed_terminal(
            mutation_id,
            status="superseded",
            link_column="superseded_by_mutation_id",
            link_value=superseded_by_mutation_id,
            timestamp_column="superseded_at",
        )

    def _commit_transaction(
        self,
        plan: GovernanceMutationPlan,
        reserved: dict[str, Any],
        claim: dict[str, Any],
        *,
        transaction_writer: TransactionWriter | None,
        fault_injector: FaultInjector | None,
    ) -> dict[str, Any]:
        mutation_id = str(reserved["mutation_id"])
        conn = self._connect()
        try:
            self._begin_scope_write(conn, plan)
            self._lock_overlay(conn)
            lock_suffix = " FOR UPDATE" if is_state_db_path(self.db_path) else ""
            row = conn.execute(
                _p(
                    self.db_path,
                    f"SELECT * FROM governance_mutation_intent WHERE mutation_id=?{lock_suffix}",
                ),
                (mutation_id,),
            ).fetchone()
            intent = _row_dict(row)
            if str(intent.get("status") or "") != "reserved":
                raise GovernanceMutationError(f"intent_not_reserved:{intent.get('status')}")
            patch = _loads(intent.get("patch_json"), {})
            keys = sorted(patch)
            current_overlay = self._read_overlay(conn)
            current_config = runtime_config.config_from_overlay(current_overlay, self.db_path)
            if keys:
                # Compare like-for-like: reserved-stage before_json is a deep
                # projection of the patch-touched branches only.
                current_before = _deep_slice(current_config.to_dict(), patch)
                expected_before = _loads(intent.get("before_json"), {})
                if _hash(current_before) != _hash(expected_before):
                    raise GovernanceMutationError("before_state_changed")
            target_overlay = _deep_merge(current_overlay, patch)
            effective_config = runtime_config.config_from_overlay(target_overlay, self.db_path)
            target_hash = _runtime_config_hash(effective_config.to_dict())
            if target_hash != str(intent.get("target_config_hash") or ""):
                raise GovernanceMutationError("target_config_hash_changed")
            now = time.time()
            conn.execute(
                _p(
                    self.db_path,
                    """UPDATE governance_mutation_intent
                       SET status='prepared', prepared_at=?, v16_command_id=?, updated_at=?
                       WHERE mutation_id=? AND status='reserved'""",
                ),
                (now, str(claim.get("command_id") or ""), now, mutation_id),
            )
            self._fault(fault_injector, "after_prepared")
            overlay_hash = _hash(target_overlay)
            if plan.domain_only:
                # A domain-only mutation must not take ownership of the shared
                # RuntimeConfig overlay.  Re-stamping an unchanged overlay with
                # this mutation would make startup validate domain evidence as
                # config authority and can fail closed when runtime-derived
                # config differs from YAML+overlay reconstruction.
                overlay_authority = conn.execute(
                    _p(
                        self.db_path,
                        """SELECT mutation_id FROM runtime_config_overlay
                           WHERE overlay_id=?""",
                    ),
                    (OVERLAY_ID,),
                ).fetchone()
                authority_id = str(
                    _row_dict(overlay_authority).get("mutation_id") or ""
                )
                authority = {}
                if authority_id:
                    authority = _row_dict(
                        conn.execute(
                            _p(
                                self.db_path,
                                """SELECT committed_config_version,
                                          committed_config_hash
                                   FROM governance_mutation_intent
                                   WHERE mutation_id=? AND status='committed'
                                   LIMIT 1""",
                            ),
                            (authority_id,),
                        ).fetchone()
                    )
                snapshot = {
                    "config_version": int(
                        authority.get("committed_config_version") or 0
                    ),
                    "config_hash": str(
                        authority.get("committed_config_hash") or target_hash
                    ),
                    "source": "existing_runtime_config_authority",
                    "run_id": str(plan.run_id),
                    "mutation_id": authority_id,
                    "created_at": now,
                    "reused": True,
                }
            else:
                self._persist_overlay(
                    conn,
                    target_overlay,
                    overlay_hash=overlay_hash,
                    source=plan.source,
                    run_id=plan.run_id,
                    mutation_id=mutation_id,
                    updated_at=now,
                )
                snapshot = self._persist_snapshot(
                    conn,
                    effective_config,
                    source=plan.source,
                    run_id=plan.run_id,
                    mutation_id=mutation_id,
                    created_at=now,
                )
            domain_result = dict(transaction_writer(conn, mutation_id, effective_config) or {}) if transaction_writer else {}
            # Bind V16 to the domain facts written by the open transaction, not
            # merely to the caller's target patch.  The writer result is a
            # compact, deterministic receipt of the rows committed alongside
            # the overlay/snapshot.  Updating the intent before finalize keeps
            # all three hashes under the same transaction boundary.
            committed_domain_hash = _hash(
                {
                    "control_surface": str(intent.get("control_surface") or ""),
                    "scope_type": str(intent.get("scope_type") or ""),
                    "scope_key": str(intent.get("scope_key") or ""),
                    "target": _loads(intent.get("target_json"), {}),
                    "domain_result": domain_result,
                }
            )
            domain_update = conn.execute(
                _p(
                    self.db_path,
                    """UPDATE governance_mutation_intent
                       SET domain_hash=?, updated_at=?
                       WHERE mutation_id=? AND status='prepared'""",
                ),
                (committed_domain_hash, now, mutation_id),
            )
            if int(domain_update.rowcount or 0) != 1:
                raise GovernanceMutationError("intent_domain_hash_update_failed")
            finalized: dict[str, Any] = {}
            if claim.get("claim_token"):
                from backend.services.v16_command_gate import V16CommandGate

                validation = V16CommandGate.validate_claim_in_transaction(
                    conn,
                    command_id=str(claim.get("command_id") or ""),
                    claim_token=str(claim.get("claim_token") or ""),
                    target_agent=str(plan.v16_target_agent or ""),
                    scope_type=str(plan.scope_type),
                    scope_key=str(plan.scope_key),
                    action=str(plan.action or plan.source),
                    candidate_id=str(plan.v16_candidate_id or ""),
                    posterior_fingerprint=str(plan.v16_posterior_fingerprint or ""),
                    evidence_fingerprint=str(reserved.get("evidence_fingerprint") or ""),
                    mutation_id=mutation_id,
                )
                if not validation.get("allowed"):
                    raise GovernanceMutationError(str(validation.get("status") or "v16_claim_binding_failed"))
                finalized = V16CommandGate.finalize_in_transaction(
                    conn,
                    command_id=str(claim.get("command_id") or ""),
                    claim_token=str(claim.get("claim_token") or ""),
                    mutation_id=mutation_id,
                    config_hash=str(snapshot["config_hash"]),
                    domain_hash=committed_domain_hash,
                    now=now,
                )
                if not finalized.get("allowed"):
                    raise GovernanceMutationError(str(finalized.get("status") or "v16_finalize_failed"))
            conn.execute(
                _p(
                    self.db_path,
                    """UPDATE governance_mutation_intent
                       SET status='committed', projection_status='pending',
                           target_config_version=?, target_config_hash=?,
                           committed_config_version=?, committed_config_hash=?,
                           committed_at=?, error_stage='', error_type='', error_message='',
                           updated_at=?
                       WHERE mutation_id=? AND status='prepared'""",
                ),
                (
                    int(snapshot["config_version"]),
                    str(snapshot["config_hash"]),
                    int(snapshot["config_version"]),
                    str(snapshot["config_hash"]),
                    now,
                    now,
                    mutation_id,
                ),
            )
            self._fault(fault_injector, "before_commit")
            committed_row = conn.execute(
                _p(self.db_path, "SELECT * FROM governance_mutation_intent WHERE mutation_id=?"),
                (mutation_id,),
            ).fetchone()
            if committed_row is not None:
                committed_intent = _row_dict(committed_row)
                if str(committed_intent.get("status") or "") == "committed":
                    _mirror_mutation_stage(
                        conn, committed_intent, stage="committed",
                        stage_timestamp=committed_intent.get("committed_at") or now,
                    )
            conn.commit()
            return {
                "mutation_id": mutation_id,
                "effective_config": effective_config,
                "snapshot": snapshot,
                "overlay_hash": overlay_hash,
                "domain_result": domain_result,
                "domain_hash": committed_domain_hash,
                "v16_finalized": finalized,
                "intent": self._intent_payload(
                    {**intent, "domain_hash": committed_domain_hash}
                ),
                "domain_only": bool(plan.domain_only),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _claim_v16(self, plan: GovernanceMutationPlan, reserved: dict[str, Any]) -> dict[str, Any]:
        risk_class = str((reserved.get("risk_classification") or {}).get("risk_class") or "")
        if risk_class in {"risk_tightening", "no_change"}:
            return {
                "ok": True,
                "allowed": True,
                "status": "risk_tightening_exempt" if risk_class == "risk_tightening" else "no_change_exempt",
            }
        if not self.production_state:
            return {"ok": True, "allowed": True, "status": "isolated_test_state"}
        if reserved.get("operator_bounded_demo_control_exempt"):
            return {
                "ok": True,
                "allowed": True,
                "status": "operator_bounded_demo_control_exempt",
            }
        if plan.v16_command_id and plan.v16_claim_token:
            return {
                "ok": True,
                "allowed": True,
                "status": "v16_command_claim_supplied",
                "command_id": str(plan.v16_command_id),
                "claim_token": str(plan.v16_claim_token),
            }
        from backend.services.v16_command_gate import V16CommandGate

        return V16CommandGate.claim(
            self.db_path,
            target_agent=str(plan.v16_target_agent or "factor_governance"),
            scope_type=str(plan.scope_type),
            scope_key=str(plan.scope_key),
            action=str(plan.action or plan.source),
            command_id=str(plan.v16_command_id or ""),
            candidate_id=str(plan.v16_candidate_id or ""),
            posterior_fingerprint=str(plan.v16_posterior_fingerprint or ""),
            evidence_fingerprint=str(reserved.get("evidence_fingerprint") or ""),
            claim_ttl_seconds=900.0,
            risk_reduction=False,
        )

    def _release_claim(self, claim: Mapping[str, Any], *, reason: str) -> None:
        if not claim.get("claim_token") or not claim.get("command_id"):
            return
        try:
            from backend.services.v16_command_gate import V16CommandGate

            V16CommandGate.release(
                self.db_path,
                command_id=str(claim.get("command_id") or ""),
                claim_token=str(claim.get("claim_token") or ""),
                reason=reason,
            )
        except Exception:
            return

    def _publish_committed(self, transaction: dict[str, Any]) -> dict[str, Any]:
        mutation_id = str(transaction["mutation_id"])
        if transaction.get("domain_only"):
            return self._record_projection(
                mutation_id,
                status="current",
                error={},
            )
        config = transaction["effective_config"]
        self.publisher(config, transaction)
        return self._record_projection(mutation_id, status="current", error={})

    def _effective_durable_config(self) -> tuple[RuntimeConfig, dict[str, Any]]:
        """Read the latest effective config and a token that detects races."""
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                _p(
                    self.db_path,
                    """SELECT overlay_json, overlay_hash, mutation_id, updated_at
                       FROM runtime_config_overlay WHERE overlay_id=?""",
                ),
                (OVERLAY_ID,),
            ).fetchone()
            if not row:
                raise GovernanceMutationError("durable_overlay_missing")
            overlay_item = _row_dict(row)
            raw_overlay = overlay_item.get("overlay_json")
            if isinstance(raw_overlay, dict):
                overlay = dict(raw_overlay)
            else:
                try:
                    overlay = json.loads(str(raw_overlay or "{}"))
                except Exception as exc:
                    raise GovernanceMutationError("durable_overlay_invalid") from exc
            if not isinstance(overlay, dict):
                raise GovernanceMutationError("durable_overlay_invalid")
            latest = conn.execute(
                """SELECT mutation_id, committed_config_version, committed_config_hash
                   FROM governance_mutation_intent
                   WHERE status='committed'
                   ORDER BY committed_config_version DESC, committed_at DESC,
                            created_at DESC, mutation_id DESC
                   LIMIT 1"""
            ).fetchone()
            latest_item = _row_dict(latest)
        finally:
            conn.close()
        config = runtime_config.config_from_overlay(overlay, self.db_path)
        token = {
            "overlay_hash": _hash(overlay),
            "overlay_mutation_id": str(overlay_item.get("mutation_id") or ""),
            "overlay_updated_at": float(overlay_item.get("updated_at") or 0.0),
            "latest_committed_mutation_id": str(latest_item.get("mutation_id") or ""),
            "latest_committed_config_version": int(
                latest_item.get("committed_config_version") or 0
            ),
            "effective_config_hash": _runtime_config_hash(config.to_dict()),
        }
        return config, token

    def _is_current_scope_intent(self, conn: Any, item: Mapping[str, Any]) -> bool:
        if str(item.get("status") or "") != "committed":
            return False
        row = conn.execute(
            _p(
                self.db_path,
                """SELECT mutation_id
                   FROM governance_mutation_intent
                   WHERE control_surface=? AND scope_type=? AND scope_key=?
                     AND status IN ('committed', 'rolled_back', 'superseded')
                   ORDER BY committed_config_version DESC, committed_at DESC,
                            created_at DESC, mutation_id DESC
                   LIMIT 1""",
            ),
            (
                str(item.get("control_surface") or ""),
                str(item.get("scope_type") or ""),
                str(item.get("scope_key") or ""),
            ),
        ).fetchone()
        latest = _row_dict(row)
        return str(latest.get("mutation_id") or "") == str(item.get("mutation_id") or "")

    @staticmethod
    def _publish_runtime_config(config: RuntimeConfig, _transaction: dict[str, Any]) -> int:
        return runtime_config.replace(config)

    def _record_projection(self, mutation_id: str, *, status: str, error: Mapping[str, Any]) -> dict[str, Any]:
        if status not in PROJECTION_STATUSES:
            raise ValueError(f"invalid_projection_status:{status}")
        conn = self._connect()
        try:
            if not is_state_db_path(self.db_path):
                conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            result = conn.execute(
                _p(
                    self.db_path,
                    """UPDATE governance_mutation_intent
                       SET projection_status=?, projection_attempts=projection_attempts+1,
                           last_projection_at=?, projection_error_json=?, updated_at=?
                       WHERE mutation_id=? AND status='committed'
                         AND projection_status IN ('pending', 'degraded')""",
                ),
                (status, now, _json(dict(error or {})), now, str(mutation_id)),
            )
            conn.commit()
            if int(result.rowcount or 0) == 0:
                existing = conn.execute(
                    _p(
                        self.db_path,
                        """SELECT status, projection_status
                           FROM governance_mutation_intent WHERE mutation_id=?""",
                    ),
                    (str(mutation_id),),
                ).fetchone()
                existing_item = _row_dict(existing)
                existing_projection = str(existing_item.get("projection_status") or "")
                if (
                    str(existing_item.get("status") or "") == "committed"
                    and existing_projection == "current"
                ):
                    return {
                        "ok": True,
                        "status": "projection_already_current",
                        "projection_status": "current",
                        "mutation_id": mutation_id,
                        "error": {},
                        "idempotent": True,
                    }
            return {
                "ok": status == "current" and int(result.rowcount or 0) == 1,
                "status": "projection_published" if status == "current" else "projection_degraded",
                "projection_status": status,
                "mutation_id": mutation_id,
                "error": dict(error or {}),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _abort(self, mutation_id: str, stage: str, message: str) -> None:
        conn = self._connect()
        try:
            if not is_state_db_path(self.db_path):
                conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            conn.execute(
                _p(
                    self.db_path,
                    """UPDATE governance_mutation_intent
                       SET status='aborted', aborted_at=?, error_stage=?,
                           error_type='GovernanceMutationError', error_message=?, updated_at=?
                       WHERE mutation_id=? AND status IN ('reserved', 'prepared')""",
                ),
                (now, str(stage), str(message)[:2000], now, str(mutation_id)),
            )
            row = conn.execute(
                _p(self.db_path, "SELECT * FROM governance_mutation_intent WHERE mutation_id=?"),
                (str(mutation_id),),
            ).fetchone()
            if row is not None:
                intent = _row_dict(row)
                if str(intent.get("status") or "") == "aborted":
                    _mirror_mutation_stage(
                        conn, intent, stage="aborted", stage_timestamp=intent.get("aborted_at")
                    )
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()

    def _mark_committed_terminal(
        self,
        mutation_id: str,
        *,
        status: str,
        link_column: str,
        link_value: str,
        timestamp_column: str,
    ) -> dict[str, Any]:
        if status not in {"rolled_back", "superseded"}:
            raise ValueError(f"invalid_terminal_status:{status}")
        self._prepare_storage()
        conn = self._connect()
        try:
            if not is_state_db_path(self.db_path):
                conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            result = conn.execute(
                _p(
                    self.db_path,
                    f"""UPDATE governance_mutation_intent
                        SET status=?, {timestamp_column}=?, {link_column}=?, updated_at=?
                        WHERE mutation_id=? AND status='committed'""",
                ),
                (status, now, str(link_value), now, str(mutation_id)),
            )
            row = conn.execute(
                _p(self.db_path, "SELECT * FROM governance_mutation_intent WHERE mutation_id=?"),
                (str(mutation_id),),
            ).fetchone()
            if row is not None:
                intent = _row_dict(row)
                if str(intent.get("status") or "") == status:
                    _mirror_mutation_stage(
                        conn, intent, stage=status,
                        stage_timestamp=intent.get(timestamp_column) or now,
                    )
            conn.commit()
            return {
                "ok": int(result.rowcount or 0) == 1,
                "status": status if int(result.rowcount or 0) == 1 else "transition_rejected",
                "mutation_id": mutation_id,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _prepare_storage(self) -> None:
        if self.production_state:
            conn = self._connect(read_only=True)
            try:
                required_tables = {
                    "governance_mutation_intent",
                    "runtime_config_overlay",
                    "runtime_config_snapshot",
                }
                missing_tables = sorted(table for table in required_tables if not state_table_exists(conn, table))
                required_columns = {
                    "governance_mutation_intent": {
                        "projection_attempts",
                        "projection_error_json",
                        "rolled_back_at",
                        "superseded_at",
                    },
                    "runtime_config_overlay": {
                        "mutation_id",
                        "legacy_authority_json",
                    },
                    "runtime_config_snapshot": {"mutation_id"},
                }
                missing_columns = {
                    table: sorted(columns - state_table_columns(conn, table))
                    for table, columns in required_columns.items()
                    if table not in missing_tables and columns - state_table_columns(conn, table)
                }
                if missing_tables or missing_columns:
                    raise GovernanceMutationError(
                        f"governance_coordinator_schema_missing:tables={missing_tables},columns={missing_columns}"
                    )
            finally:
                conn.close()
            return
        ensure_evolution_ledger_tables(self.db_path)
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS governance_mutation_intent (
                    mutation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    control_surface TEXT NOT NULL DEFAULT '',
                    scope_type TEXT NOT NULL DEFAULT '',
                    scope_key TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    producer TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    risk_class TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'reserved',
                    projection_status TEXT NOT NULL DEFAULT 'pending',
                    before_json TEXT NOT NULL DEFAULT '{}',
                    target_json TEXT NOT NULL DEFAULT '{}',
                    patch_json TEXT NOT NULL DEFAULT '{}',
                    rollback_json TEXT NOT NULL DEFAULT '{}',
                    evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                    evidence_fingerprint TEXT NOT NULL DEFAULT '',
                    v16_command_id TEXT NOT NULL DEFAULT '',
                    target_config_version INTEGER NOT NULL DEFAULT 0,
                    target_config_hash TEXT NOT NULL DEFAULT '',
                    committed_config_version INTEGER NOT NULL DEFAULT 0,
                    committed_config_hash TEXT NOT NULL DEFAULT '',
                    domain_hash TEXT NOT NULL DEFAULT '',
                    error_stage TEXT NOT NULL DEFAULT '',
                    error_type TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    reserved_at REAL NOT NULL DEFAULT 0.0,
                    prepared_at REAL NOT NULL DEFAULT 0.0,
                    committed_at REAL NOT NULL DEFAULT 0.0,
                    aborted_at REAL NOT NULL DEFAULT 0.0,
                    created_at REAL NOT NULL DEFAULT 0.0,
                    updated_at REAL NOT NULL DEFAULT 0.0,
                    projection_attempts INTEGER NOT NULL DEFAULT 0,
                    last_projection_at REAL NOT NULL DEFAULT 0.0,
                    projection_error_json TEXT NOT NULL DEFAULT '{}',
                    rolled_back_at REAL NOT NULL DEFAULT 0.0,
                    rollback_mutation_id TEXT NOT NULL DEFAULT '',
                    superseded_at REAL NOT NULL DEFAULT 0.0,
                    superseded_by_mutation_id TEXT NOT NULL DEFAULT ''
                )"""
            )
            self._ensure_sqlite_columns(
                conn,
                "runtime_config_overlay",
                {
                    "mutation_id": "TEXT NOT NULL DEFAULT ''",
                    "legacy_authority_json": "TEXT NOT NULL DEFAULT '{}'",
                },
            )
            self._ensure_sqlite_columns(
                conn,
                "runtime_config_snapshot",
                {"mutation_id": "TEXT NOT NULL DEFAULT ''"},
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_governance_mutation_active_scope
                   ON governance_mutation_intent(control_surface, scope_type, scope_key)
                   WHERE status IN ('reserved', 'prepared')"""
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_sqlite_columns(conn: Any, table: str, columns: Mapping[str, str]) -> None:
        existing = state_table_columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')

    def _connect(self, *, read_only: bool = False):
        if is_state_db_path(self.db_path):
            return get_state_pg_conn(read_only=read_only)
        conn = connect_sqlite(self.db_path, read_only=read_only)
        conn.row_factory = __import__("sqlite3").Row
        return conn

    def _begin_scope_write(self, conn: Any, plan: GovernanceMutationPlan) -> None:
        if is_state_db_path(self.db_path):
            conn.execute(
                _p(self.db_path, "SELECT pg_advisory_xact_lock(hashtext(?))"),
                (self._scope_lock_key(plan),),
            )
        else:
            conn.execute("BEGIN IMMEDIATE")

    def _lock_overlay(self, conn: Any) -> None:
        if is_state_db_path(self.db_path):
            conn.execute(
                _p(self.db_path, "SELECT pg_advisory_xact_lock(hashtext(?))"),
                ("quant_runtime_config_overlay",),
            )

    @staticmethod
    def _scope_lock_key(plan: GovernanceMutationPlan) -> str:
        return f"governance:{plan.control_surface}:{plan.scope_type}:{plan.scope_key}"

    def _read_overlay(self, conn: Any) -> dict[str, Any]:
        row = conn.execute(
            _p(
                self.db_path,
                "SELECT overlay_json FROM runtime_config_overlay WHERE overlay_id=?",
            ),
            (OVERLAY_ID,),
        ).fetchone()
        if not row:
            return {}
        item = _row_dict(row)
        raw = item.get("overlay_json") if item else row[0]
        parsed = _loads(raw, {})
        return parsed if isinstance(parsed, dict) else {}

    def _persist_overlay(
        self,
        conn: Any,
        overlay: Mapping[str, Any],
        *,
        overlay_hash: str,
        source: str,
        run_id: str,
        mutation_id: str,
        updated_at: float,
    ) -> None:
        conn.execute(
            _p(
                self.db_path,
                """INSERT INTO runtime_config_overlay
                   (overlay_id, overlay_json, overlay_hash, source, run_id,
                    mutation_id, legacy_authority_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, '{}', ?)
                   ON CONFLICT(overlay_id) DO UPDATE SET
                       overlay_json=excluded.overlay_json,
                       overlay_hash=excluded.overlay_hash,
                       source=excluded.source,
                       run_id=excluded.run_id,
                       mutation_id=excluded.mutation_id,
                       legacy_authority_json='{}',
                       updated_at=excluded.updated_at""",
            ),
            (
                OVERLAY_ID,
                _json(dict(overlay)),
                overlay_hash,
                str(source),
                str(run_id),
                str(mutation_id),
                updated_at,
            ),
        )

    def _persist_snapshot(
        self,
        conn: Any,
        config: RuntimeConfig,
        *,
        source: str,
        run_id: str,
        mutation_id: str,
        created_at: float,
    ) -> dict[str, Any]:
        return persist_runtime_config_snapshot(
            config,
            source=str(source),
            db_path=self.db_path,
            run_id=str(run_id),
            mutation_id=str(mutation_id),
            conn=conn,
            created_at=created_at,
        )

    def _intent_payload(self, item: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(item)
        for source_key, target_key, default in (
            ("before_json", "before", {}),
            ("target_json", "target", {}),
            ("patch_json", "patch", {}),
            ("rollback_json", "rollback", {}),
            ("evidence_refs_json", "evidence_refs", {}),
            ("projection_error_json", "projection_error", {}),
        ):
            payload[target_key] = _loads(payload.pop(source_key, None), default)
        payload["ok"] = str(payload.get("status") or "") in {"reserved", "prepared", "committed"}
        payload["risk_classification"] = {
            "risk_class": str(payload.get("risk_class") or ""),
            "v16_required": str(payload.get("risk_class") or "") == "risk_expanding",
            "classification_source": "coordinator_before_after",
        }
        payload["boundary"] = self.boundary()
        return payload

    @staticmethod
    def _fault(injector: FaultInjector | None, stage: str) -> None:
        if injector is not None:
            injector(stage)

    def _failed(self, status: str, exc: Exception, mutation_id: str = "") -> dict[str, Any]:
        return {
            "ok": False,
            "status": status,
            "mutation_id": mutation_id,
            "error": f"{type(exc).__name__}: {exc}",
            "boundary": self.boundary(),
        }
