"""Endpoint-level ``_fact`` views for Ops and governance APIs.

The underlying services keep their legacy response shapes.  These helpers add
one shallow ``_fact`` envelope and deliberately use only domain timestamps
that were returned by a durable read/write boundary.  In particular, request
generation time is never reused as the observation time.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from backend.services.fact_envelope import (
    DEFAULT_STALE_AFTER_SEC,
    attach_fact,
    fact_envelope,
    observed_epoch,
)


OPS_LEDGER_STALE_AFTER_SEC = DEFAULT_STALE_AFTER_SEC["ops"]
OPS_RISK_STALE_AFTER_SEC = DEFAULT_STALE_AFTER_SEC["risk"]


def _copy(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(payload or {})


def runtime_config_projection_observation() -> dict[str, Any]:
    """Read the committed overlay timestamp without running schema DDL."""

    from backend.core.db import get_state_pg_conn
    from backend.services.runtime_config_overlay import OVERLAY_ID

    conn = None
    try:
        conn = get_state_pg_conn(read_only=True)
        row = conn.execute(
            """
            SELECT updated_at
            FROM runtime_config_overlay
            WHERE overlay_id=%s
            LIMIT 1
            """,
            (OVERLAY_ID,),
        ).fetchone()
        observed_at = row["updated_at"] if row else None
        return {
            "ok": observed_epoch(observed_at) > 0,
            "observed_at": observed_at,
            "error": "",
            "reason_code": (
                None
                if observed_epoch(observed_at) > 0
                else "runtime_overlay_observation_missing"
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "observed_at": None,
            "error": f"{type(exc).__name__}: {exc}",
            "reason_code": "runtime_overlay_read_failed",
        }
    finally:
        if conn is not None:
            conn.close()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _lookup(value: Any, path: Sequence[str]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_observation(value: Any, paths: Sequence[Sequence[str]]) -> Any:
    for path in paths:
        candidate = _lookup(value, path)
        if observed_epoch(candidate) > 0:
            return candidate
    return None


def _latest_item_observation(
    value: Any,
    *,
    item_paths: Sequence[Sequence[str]],
    timestamp_fields: Sequence[str] = ("updated_at", "created_at"),
) -> Any:
    latest_value: Any = None
    latest_epoch = 0.0
    for item_path in item_paths:
        items = _lookup(value, item_path)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            candidate = _first_observation(
                item,
                tuple((field,) for field in timestamp_fields),
            )
            epoch = observed_epoch(candidate)
            if epoch > latest_epoch:
                latest_value = candidate
                latest_epoch = epoch
    return latest_value


def _latest_observation(*values: Any) -> Any:
    latest_value: Any = None
    latest_epoch = 0.0
    for value in values:
        epoch = observed_epoch(value)
        if epoch > latest_epoch:
            latest_value = value
            latest_epoch = epoch
    return latest_value


def _source_error(*values: Any) -> str | None:
    """Return only source failures, not missing/no-op/business blockers."""

    failure_statuses = {
        "error",
        "failed",
        "source_error",
        "database_error",
        "read_failed",
        "write_failed",
        "persist_failed",
        "commit_failed",
    }
    for value in values:
        item = _mapping(value)
        explicit = item.get("error") or item.get("exception")
        if explicit:
            return str(explicit)
        status = str(item.get("status") or "").strip().lower()
        if status in failure_statuses or status.endswith("_error") or status.endswith("_failed"):
            return str(item.get("reason") or status)
    return None


def _attach(
    payload: Mapping[str, Any],
    *,
    contract: str,
    source: str,
    observed_at: Any,
    stale_after_sec: float = OPS_LEDGER_STALE_AFTER_SEC,
    error: Any = None,
    reason_code: str | None = None,
    components: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    result = _copy(payload)
    return dict(
        attach_fact(
            result,
            contract=contract,
            source=source,
            observed_at=observed_at,
            stale_after_sec=stale_after_sec,
            error=error,
            reason_code=reason_code,
            components=components,
            now=float(time.time() if now is None else now),
        )
    )


def unverified_compat_fact_payload(
    payload: Mapping[str, Any],
    *,
    contract: str,
    reason_code: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Attach an explicit non-authoritative fact to a compatibility view.

    Some Ops endpoints are assembled from process-local configuration,
    request-time calculations, or compatibility services that do not return a
    durable identifier and observation timestamp.  Their legacy fields remain
    useful, but they must not be presented as current operational truth.  A
    source failure is still surfaced as ``error``; otherwise the fact remains
    ``unknown`` with ``source=none``.
    """

    return _attach(
        payload,
        contract=contract,
        source="none",
        observed_at=None,
        error=_source_error(payload),
        reason_code=reason_code,
        now=now,
    )


def _component(
    *,
    contract: str,
    source: str,
    observed_at: Any,
    stale_after_sec: float,
    error: Any = None,
    reason_code: str | None = None,
    now: float,
) -> dict[str, Any]:
    return fact_envelope(
        contract=contract,
        source=source,
        observed_at=observed_at,
        stale_after_sec=stale_after_sec,
        error=error,
        reason_code=reason_code,
        now=now,
    ).to_dict()


def ledger_read_fact_payload(
    payload: Mapping[str, Any],
    *,
    contract: str,
    source: str,
    entity_path: Sequence[str],
    observed_paths: Sequence[Sequence[str]] = (("updated_at",), ("created_at",)),
    item_paths: Sequence[Sequence[str]] = (),
    item_timestamp_fields: Sequence[str] = ("updated_at", "created_at"),
    reason_code: str = "ledger_observation_missing",
    stale_after_sec: float = OPS_LEDGER_STALE_AFTER_SEC,
    now: float | None = None,
) -> dict[str, Any]:
    """Attach a fact for one explicitly named ledger/read-model entity."""

    entity = _lookup(payload, entity_path)
    observed_at = _first_observation(entity, observed_paths)
    observed_at = _latest_observation(
        observed_at,
        _latest_item_observation(
            entity,
            item_paths=item_paths,
            timestamp_fields=item_timestamp_fields,
        ),
    )
    error = _source_error(payload, entity)
    return _attach(
        payload,
        contract=contract,
        source=source,
        observed_at=observed_at,
        stale_after_sec=stale_after_sec,
        error=error,
        reason_code=None if observed_epoch(observed_at) > 0 else reason_code,
        now=now,
    )


def persisted_record_fact_payload(
    payload: Mapping[str, Any],
    *,
    contract: str,
    source: str,
    record_path: Sequence[str],
    id_fields: Sequence[str],
    observed_paths: Sequence[Sequence[str]] = (("updated_at",), ("created_at",)),
    item_paths: Sequence[Sequence[str]] = (),
    item_id_fields: Sequence[str] = (),
    item_timestamp_fields: Sequence[str] = ("updated_at", "created_at"),
    required_fields: Sequence[str] = (),
    stale_after_sec: float = OPS_LEDGER_STALE_AFTER_SEC,
    now: float | None = None,
) -> dict[str, Any]:
    """Attach a write fact only when a returned durable record is identifiable."""

    record = _mapping(_lookup(payload, record_path))
    direct_id = any(str(record.get(field) or "").strip() for field in id_fields)
    item_id = False
    for item_path in item_paths:
        items = _lookup(record, item_path)
        if not isinstance(items, list):
            continue
        item_id = item_id or any(
            isinstance(item, Mapping)
            and any(str(item.get(field) or "").strip() for field in item_id_fields)
            for item in items
        )
    required_fields_present = all(
        str(record.get(field) or "").strip() for field in required_fields
    )
    confirmed = bool(direct_id or item_id) and required_fields_present
    observed_at = _latest_observation(
        _first_observation(record, observed_paths),
        _latest_item_observation(
            record,
            item_paths=item_paths,
            timestamp_fields=item_timestamp_fields,
        ),
    )
    error = _source_error(payload, record)
    if not confirmed:
        observed_at = None
    reason_code = None
    if not confirmed:
        reason_code = "durable_commit_not_confirmed"
    elif observed_epoch(observed_at) <= 0:
        reason_code = "committed_observation_timestamp_missing"
    return _attach(
        payload,
        contract=contract,
        source=source,
        observed_at=observed_at,
        stale_after_sec=stale_after_sec,
        error=error,
        reason_code=reason_code,
        now=now,
    )


def v15_phase0_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    phase0 = _mapping(payload.get("phase0"))
    observed_at = payload.get("readiness_generated_at")
    return _attach(
        payload,
        contract="ops.v15-phase0-completion.v2",
        source="backend_readiness",
        observed_at=observed_at,
        error=_source_error(payload, phase0),
        reason_code=(
            None
            if phase0 and observed_epoch(observed_at) > 0
            else "phase0_readiness_observation_missing"
        ),
        now=now,
    )


def incident_control_status_fact_payload(
    payload: Mapping[str, Any],
    *,
    projection_observed_at: Any = None,
    projection_error: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    status = _mapping(payload.get("incident_control"))
    latch = _mapping(status.get("local_safety_latch"))
    latch_observed_at = latch.get("created_at")
    latch_is_active = bool(latch.get("active"))
    observed_at = (
        _latest_observation(projection_observed_at, latch_observed_at)
        if latch_is_active
        else projection_observed_at
    )
    projection = _component(
        contract="ops.incident-control-projection.v2",
        source="state_v1.runtime_config_overlay",
        observed_at=projection_observed_at,
        stale_after_sec=OPS_RISK_STALE_AFTER_SEC,
        error=projection_error,
        reason_code=(
            None
            if observed_epoch(projection_observed_at) > 0
            else "runtime_overlay_observation_missing"
        ),
        now=generated_at,
    )
    latch_component = _component(
        contract="ops.no-new-risk-latch.v1",
        source="local_safety_latch",
        observed_at=latch_observed_at,
        stale_after_sec=OPS_RISK_STALE_AFTER_SEC,
        error=latch.get("error"),
        reason_code=(
            None
            if observed_epoch(latch_observed_at) > 0
            else "safety_latch_not_set"
        ),
        now=generated_at,
    )
    source = "state_v1.runtime_config_overlay"
    if latch_is_active and observed_epoch(latch_observed_at) > 0:
        source += "+local_safety_latch"
    return _attach(
        payload,
        contract="ops.incident-control.v2",
        source=source,
        observed_at=observed_at,
        stale_after_sec=OPS_RISK_STALE_AFTER_SEC,
        error=_source_error(payload, status) or projection_error,
        reason_code=(
            None
            if observed_epoch(observed_at) > 0
            else "incident_control_authoritative_observation_missing"
        ),
        components={"projection": projection, "local_safety_latch": latch_component},
        now=generated_at,
    )


def _committed_governance_mutation(value: Any) -> bool:
    mutation = _mapping(value)
    return bool(
        str(mutation.get("status") or "").strip().lower() == "committed"
        and str(mutation.get("mutation_id") or "").strip()
    )


def _governance_observation(value: Any) -> Any:
    mutation = _mapping(value)
    return _first_observation(
        mutation,
        (
            ("committed_at",),
            ("snapshot", "created_at"),
            ("snapshot", "updated_at"),
            ("domain_result", "created_at"),
            ("domain_result", "updated_at"),
        ),
    )


def governance_mutation_fact_payload(
    payload: Mapping[str, Any],
    *,
    contract: str,
    result_path: Sequence[str],
    mutation_path: Sequence[str] = ("mutation",),
    now: float | None = None,
) -> dict[str, Any]:
    """Attach a mutation fact only after the coordinator reports committed."""

    generated_at = float(time.time() if now is None else now)
    result = _mapping(_lookup(payload, result_path))
    mutation = _mapping(_lookup(result, mutation_path))
    committed = _committed_governance_mutation(mutation)
    observed_at = _governance_observation(mutation) if committed else None
    latch = _mapping(result.get("local_safety_latch"))
    components = {
        "governance_commit": _component(
            contract=f"{contract}.commit",
            source="state_v1.governance_mutation_intent",
            observed_at=observed_at,
            stale_after_sec=OPS_RISK_STALE_AFTER_SEC,
            error=_source_error(mutation),
            reason_code=(
                None
                if committed and observed_epoch(observed_at) > 0
                else "governance_mutation_not_committed"
            ),
            now=generated_at,
        ),
        "local_safety_latch": _component(
            contract="ops.no-new-risk-latch.v1",
            source="local_safety_latch",
            observed_at=latch.get("created_at"),
            stale_after_sec=OPS_RISK_STALE_AFTER_SEC,
            error=latch.get("error"),
            reason_code=(
                None
                if observed_epoch(latch.get("created_at")) > 0
                else "safety_latch_not_set"
            ),
            now=generated_at,
        ),
    }
    return _attach(
        payload,
        contract=contract,
        source="state_v1.governance_mutation_intent",
        observed_at=observed_at,
        stale_after_sec=OPS_RISK_STALE_AFTER_SEC,
        error=_source_error(payload, result, mutation),
        reason_code=(
            None
            if committed and observed_epoch(observed_at) > 0
            else "governance_mutation_not_committed"
        ),
        components=components,
        now=generated_at,
    )


def autonomy_scope_enforcement_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    event = _mapping(payload.get("enforcement_event"))
    status = str(event.get("status") or "").strip().lower()
    mutation = _mapping(event.get("mutation"))
    no_mutation_needed = status == "already_at_or_stricter"
    committed = no_mutation_needed or _committed_governance_mutation(mutation)
    observed_at = event.get("created_at") if committed else None
    return _attach(
        payload,
        contract="ops.autonomy-scope-enforcement.v2",
        source=(
            "state_v1.autonomy_scope_enforcement_event"
            if no_mutation_needed
            else "state_v1.governance_mutation_intent"
        ),
        observed_at=observed_at,
        stale_after_sec=OPS_RISK_STALE_AFTER_SEC,
        error=_source_error(payload, event, mutation),
        reason_code=(
            None
            if committed and observed_epoch(observed_at) > 0
            else "scope_enforcement_mutation_not_committed"
        ),
        now=now,
    )


def factor_catalog_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    mode = str(payload.get("snapshot_mode") or "live").strip().lower()
    if mode == "latest":
        observed_at = payload.get("created_at")
        source = str(payload.get("source") or "state_v1.factor_catalog_snapshot")
    else:
        observed_at = _latest_item_observation(
            payload,
            item_paths=(("items",),),
            timestamp_fields=("health_updated_at", "last_action_ts", "created_at", "updated_at"),
        )
        source = "factor_registry+state_v1"
    return _attach(
        payload,
        contract="factor.catalog.v4",
        source=source,
        observed_at=observed_at,
        error=_source_error(payload),
        reason_code=(
            None
            if observed_epoch(observed_at) > 0
            else "factor_catalog_observation_missing"
        ),
        now=now,
    )


def proposal_refresh_fact_payload(
    payload: Mapping[str, Any],
    *,
    reconciled_projection: Mapping[str, Any] | None,
    now: float | None = None,
) -> dict[str, Any]:
    projection = _mapping(reconciled_projection)
    observed_at = _latest_item_observation(
        projection,
        item_paths=(("items",),),
        timestamp_fields=("updated_at", "created_at"),
    )
    confirmed = bool(payload.get("ok")) and bool(projection.get("ok")) and observed_epoch(observed_at) > 0
    return _attach(
        payload,
        contract="ops.autonomy-proposals-refresh.v2",
        source="state_v1.proposal_registry",
        observed_at=observed_at if confirmed else None,
        error=_source_error(payload, projection),
        reason_code=None if confirmed else "proposal_projection_reconcile_missing",
        now=now,
    )


def scope_approval_fact_payload(
    payload: Mapping[str, Any], *, mutation: bool, now: float | None = None
) -> dict[str, Any]:
    if mutation:
        return persisted_record_fact_payload(
            payload,
            contract="ops.autonomy-scope-approval-event.v2",
            source="state_v1.autonomy_scope_approval_event",
            record_path=("approval_event",),
            id_fields=("event_id",),
            observed_paths=(("created_at",),),
            now=now,
        )
    return ledger_read_fact_payload(
        payload,
        contract="ops.autonomy-scope-approval-latest.v2",
        source="state_v1.autonomy_scope_approval_event",
        entity_path=("approval_event",),
        observed_paths=(("created_at",),),
        reason_code="scope_approval_event_missing",
        now=now,
    )


def scope_enforcement_read_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return ledger_read_fact_payload(
        payload,
        contract="ops.autonomy-scope-enforcement-latest.v2",
        source="state_v1.autonomy_scope_enforcement_event",
        entity_path=("enforcement_event",),
        observed_paths=(("created_at",),),
        reason_code="scope_enforcement_event_missing",
        stale_after_sec=OPS_RISK_STALE_AFTER_SEC,
        now=now,
    )


def release_approval_trail_fact_payload(
    payload: Mapping[str, Any],
    *,
    release: Mapping[str, Any] | None,
    now: float | None = None,
) -> dict[str, Any]:
    trail = _mapping(payload.get("approval_trail"))
    release_row = _mapping(release)
    observed_at = _latest_observation(
        _first_observation(release_row, (("updated_at",), ("created_at",))),
        _latest_item_observation(
            trail,
            item_paths=(("events",),),
            timestamp_fields=("created_at",),
        ),
    )
    confirmed = bool(trail.get("ok")) and bool(release_row.get("run_id"))
    return _attach(
        payload,
        contract="ops.release-approval-trail.v2",
        source="state_v1.release_approval_event",
        observed_at=observed_at if confirmed else None,
        error=_source_error(payload, trail, release_row),
        reason_code=(
            None
            if confirmed and observed_epoch(observed_at) > 0
            else "release_approval_trail_observation_missing"
        ),
        now=now,
    )
