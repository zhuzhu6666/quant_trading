"""Additive ``_fact`` contracts for public API response boundaries.

The domain services intentionally keep returning their legacy payloads.  This
module creates shallow copies and adds provenance/freshness metadata without
changing, deleting, or recursively guessing any existing field.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from backend.services.fact_envelope import (
    DEFAULT_STALE_AFTER_SEC,
    attach_fact,
    fact_envelope,
    observed_epoch,
)


def _copy(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(payload or {})


def _error(payload: Mapping[str, Any]) -> str | None:
    if payload.get("ok") is False:
        return str(payload.get("error") or payload.get("detail") or "source_reported_failure")
    return None


def _positive_min(values: list[Any]) -> float | None:
    normalized = [observed_epoch(value) for value in values]
    if not normalized or any(value <= 0 for value in normalized):
        return None
    return min(normalized)


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


def _position_reconcile_component_views(
    raw_components: Mapping[str, Any] | None,
    *,
    now: float,
) -> dict[str, dict[str, Any]]:
    """Normalize broker sub-facts without erasing their explicit state."""

    views: dict[str, dict[str, Any]] = {}
    for name, value in dict(raw_components or {}).items():
        raw = dict(value) if isinstance(value, Mapping) else {}
        state = str(raw.get("state") or "unknown")
        if state not in {"known", "unknown", "stale", "error"}:
            state = "unknown"
        views[str(name)] = {
            "envelope": "fact.v1",
            "contract": f"live.positions.{name}.v1",
            "state": state,
            "source": str(raw.get("source") or "none"),
            "observed_at": raw.get("observed_at") or None,
            "generated_at": float(now),
            "stale_after_sec": DEFAULT_STALE_AFTER_SEC["positions"],
            "reason_code": str(raw.get("reason_code") or "") or None,
            "components": {
                "known_position_ids": list(raw.get("known_position_ids") or []),
                "unknown_position_ids": list(raw.get("unknown_position_ids") or []),
            },
        }
    return views


def account_fact_payload(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), Mapping) else {}
    observed_at = readiness.get("account_updated_at")
    warming = bool(payload.get("warming_up"))
    event_observed_at = readiness.get("account_event_updated_at")
    reconcile_failed = (
        observed_epoch(readiness.get("account_reconcile_failed_at"))
        > observed_epoch(observed_at)
    )
    reconcile_error = (
        str(readiness.get("account_reconcile_error") or "account_reconcile_failed")
        if reconcile_failed
        else None
    )
    return dict(attach_fact(
        result,
        contract="live.account.v2",
        source="ctrader" if observed_epoch(observed_at) > 0 else "none",
        observed_at=None if warming else observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["account"],
        error=None if warming else (reconcile_error or _error(payload)),
        reason_code=(
            "account_warming_up"
            if warming
            else None
            if observed_epoch(observed_at) > 0
            else "account_reconcile_not_observed"
        ),
        components={
            "event_projection": _component(
                contract="live.account-event-projection.v1",
                source=(
                    "ctrader_event_cache"
                    if observed_epoch(event_observed_at) > 0
                    else "none"
                ),
                observed_at=event_observed_at,
                stale_after_sec=DEFAULT_STALE_AFTER_SEC["ws"],
                reason_code=str(readiness.get("account_event_reason") or "") or None,
                now=generated_at,
            )
        },
        now=generated_at,
    ))


def positions_fact_payload(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), Mapping) else {}
    observed_at = readiness.get("positions_updated_at")
    warming = bool(payload.get("warming_up"))
    event_observed_at = readiness.get("positions_event_updated_at")
    reconcile_failed = (
        observed_epoch(readiness.get("positions_reconcile_failed_at"))
        > observed_epoch(observed_at)
    )
    reconcile_error = (
        str(readiness.get("positions_reconcile_error") or "positions_reconcile_failed")
        if reconcile_failed
        else None
    )
    raw_broker_components = (
        dict(readiness.get("positions_component_facts") or {})
        if isinstance(readiness.get("positions_component_facts"), Mapping)
        else {}
    )
    broker_components = _position_reconcile_component_views(
        raw_broker_components,
        now=generated_at,
    )
    position_items = payload.get("positions")
    has_positions = bool(position_items) if isinstance(position_items, list) else False
    observed_ts = observed_epoch(observed_at)
    top_observation_stale = (
        observed_ts > 0
        and generated_at - observed_ts > DEFAULT_STALE_AFTER_SEC["positions"]
    )
    required_components = (
        ("identity", "protection", "price", "pnl")
        if has_positions
        else ()
    )
    missing_components = [
        name for name in required_components if not isinstance(broker_components.get(name), Mapping)
    ]
    error_components = [
        name
        for name in required_components
        if isinstance(broker_components.get(name), Mapping)
        and str((broker_components.get(name) or {}).get("state") or "") == "error"
    ]
    unknown_components = [
        name
        for name in required_components
        if isinstance(broker_components.get(name), Mapping)
        and str((broker_components.get(name) or {}).get("state") or "")
        in {"", "unknown"}
    ]
    stale_components = [
        name
        for name in required_components
        if isinstance(broker_components.get(name), Mapping)
        and str((broker_components.get(name) or {}).get("state") or "") == "stale"
    ]
    component_reason = None
    component_error = None
    effective_observed_at = observed_at
    # Once the last authoritative position snapshot has aged out, preserve its
    # timestamp and report ``stale``.  Missing component metadata is a contract
    # failure for a *fresh* non-empty snapshot; it must not erase an older fact
    # and turn a stale snapshot into a timestamp-less ``unknown``.
    if not top_observation_stale:
        if has_positions and not broker_components:
            effective_observed_at = None
            component_reason = "position_components_not_reported"
        elif error_components:
            component_error = "positions_component_error:" + ",".join(error_components)
        elif missing_components:
            effective_observed_at = None
            component_reason = "positions_component_missing:" + ",".join(missing_components)
        elif unknown_components:
            effective_observed_at = None
            component_reason = "positions_component_unknown:" + ",".join(
                unknown_components
            )
        elif stale_components:
            component_times = [
                (broker_components.get(name) or {}).get("observed_at")
                for name in required_components
            ]
            effective_observed_at = _positive_min([observed_at, *component_times])
            component_reason = "positions_component_stale:" + ",".join(
                stale_components
            )
    effective_error = (
        None
        if warming
        else _error(payload)
        if top_observation_stale
        else (reconcile_error or component_error or _error(payload))
    )
    return dict(attach_fact(
        result,
        contract="live.positions.v2",
        source="ctrader" if observed_epoch(observed_at) > 0 else "none",
        observed_at=None if warming else effective_observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["positions"],
        error=effective_error,
        reason_code=(
            "positions_warming_up"
            if warming
            else component_reason
            if component_reason
            else None
            if observed_epoch(effective_observed_at) > 0
            else "positions_reconcile_not_observed"
        ),
        components={
            "broker_reconcile": broker_components,
            "event_projection": _component(
                contract="live.positions-event-projection.v1",
                source=(
                    "ctrader_event_cache"
                    if observed_epoch(event_observed_at) > 0
                    else "none"
                ),
                observed_at=event_observed_at,
                stale_after_sec=DEFAULT_STALE_AFTER_SEC["ws"],
                reason_code=str(readiness.get("positions_event_reason") or "") or None,
                now=generated_at,
            )
        },
        now=generated_at,
    ))


def _loop_fact(
    payload: Mapping[str, Any],
    *,
    diagnostic_ts: Any = None,
    now: float,
) -> dict[str, Any]:
    running = bool(payload.get("running"))
    phase = str(payload.get("phase") or ("running" if running else "stopped"))
    if running:
        observed_at = (
            payload.get("safety_heartbeat_at")
            or payload.get("heartbeat_at")
            or diagnostic_ts
        )
        reason_code = None if observed_epoch(observed_at) > 0 else "loop_heartbeat_missing"
    else:
        # A stopped/draining status is read synchronously from process/thread
        # ownership, so the request itself is the observation.
        observed_at = payload.get("updated_at") or now
        reason_code = None
    failed = phase == "failed" or bool(payload.get("failed_reason"))
    return _component(
        contract="live.loop.v2",
        source="live_loop_controller" if payload.get("generation") else "live_process",
        observed_at=observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["loop"],
        error=str(payload.get("failed_reason") or "loop_failed") if failed else None,
        reason_code=reason_code,
        now=now,
    )


def loop_fact_payload(
    payload: Mapping[str, Any],
    *,
    diagnostic_ts: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    result["_fact"] = _loop_fact(payload, diagnostic_ts=diagnostic_ts, now=generated_at)
    return result


def live_status_fact_payload(
    payload: Mapping[str, Any],
    *,
    diagnostic_ts: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), Mapping) else {}
    account_ts = readiness.get("account_updated_at")
    positions_ts = readiness.get("positions_updated_at")
    broker = payload.get("ctrader") if isinstance(payload.get("ctrader"), Mapping) else {}
    broker_status = str(broker.get("status") or "unknown")
    loop = payload.get("loop") if isinstance(payload.get("loop"), Mapping) else {}
    loop_fact = _loop_fact(loop, diagnostic_ts=diagnostic_ts, now=generated_at)
    components = {
        "broker": _component(
            contract="live.broker-connection.v1",
            source="ctrader",
            observed_at=generated_at,
            stale_after_sec=DEFAULT_STALE_AFTER_SEC["loop"],
            error=broker.get("error") if broker_status in {"error", "disconnected", "no_token"} else None,
            reason_code=f"broker_{broker_status}" if broker_status != "connected" else None,
            now=generated_at,
        ),
        "account": _component(
            contract="live.account.v2",
            source="ctrader",
            observed_at=account_ts,
            stale_after_sec=DEFAULT_STALE_AFTER_SEC["account"],
            now=generated_at,
        ),
        "positions": _component(
            contract="live.positions.v2",
            source="ctrader",
            observed_at=positions_ts,
            stale_after_sec=DEFAULT_STALE_AFTER_SEC["positions"],
            now=generated_at,
        ),
        "loop": loop_fact,
    }
    if loop.get("running"):
        loop_observed_at = (
            loop.get("safety_heartbeat_at")
            or loop.get("heartbeat_at")
            or diagnostic_ts
        )
    else:
        loop_observed_at = loop.get("updated_at") or generated_at
    observed_at = _positive_min([account_ts, positions_ts, loop_observed_at])
    reason_code = None
    if broker_status == "warming_up":
        observed_at = None
        reason_code = "broker_warming_up"
    elif broker_status == "unknown":
        observed_at = None
        reason_code = "broker_status_unknown"
    broker_error = broker.get("error") if broker_status in {"error", "disconnected", "no_token"} else None
    composite_error = broker_error
    if loop_fact.get("state") == "error":
        composite_error = composite_error or loop_fact.get("reason_code") or "loop_source_error"
    return dict(attach_fact(
        result,
        contract="live.status.v2",
        source="ctrader",
        observed_at=observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["loop"],
        error=composite_error,
        reason_code=reason_code,
        components=components,
        now=generated_at,
    ))


def strategy_fact_payload(
    payload: Mapping[str, Any],
    *,
    diagnostic_ts: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    pipeline = payload.get("v4_status") if isinstance(payload.get("v4_status"), Mapping) else {}
    active = bool(pipeline.get("pipeline_active"))
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), Mapping) else {}
    error = readiness.get("broker_error")
    return dict(attach_fact(
        result,
        contract="live.strategy.v2",
        source="factor_pipeline" if active else "none",
        observed_at=diagnostic_ts,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["loop"],
        error=error,
        reason_code=None if active else "factor_pipeline_inactive",
        now=generated_at,
    ))


def session_fact_payload(
    payload: Mapping[str, Any],
    *,
    source: str,
    observed_at: Any,
    now: float | None = None,
) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    authoritative = str(source or "").startswith("ctrader_deals")
    return dict(attach_fact(
        result,
        contract="live.session-risk.v2",
        source=source if authoritative else "degraded_cache",
        observed_at=observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["session"],
        reason_code=None if authoritative else "session_authority_unavailable",
        components={"reported_source": source or "none"},
        now=generated_at,
    ))


def realized_fact_payload(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    return dict(attach_fact(
        result,
        contract="live.realized-pnl.v2",
        source=str(payload.get("source") or "none"),
        observed_at=payload.get("to_ts"),
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["risk"],
        error=_error(payload),
        components={"fallback_source": payload.get("fallback_source")},
        now=generated_at,
    ))


def sync_status_fact_payload(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Attach provenance to the persisted bar-sync status snapshot.

    The endpoint reads a status file rather than executing a synchronization.
    Its observation is therefore the last successful status-file write, not
    the time at which the HTTP response happened.
    """

    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    per_tf = payload.get("per_tf") if isinstance(payload.get("per_tf"), Mapping) else {}
    per_tf_observations = [
        item.get("updated_at") or item.get("last_time")
        for item in per_tf.values()
        if isinstance(item, Mapping)
    ]
    observed_at = payload.get("last_run_at")
    if observed_epoch(observed_at) <= 0:
        observed_at = max(
            (observed_epoch(value) for value in per_tf_observations),
            default=0.0,
        ) or None
    error = payload.get("error")
    return dict(attach_fact(
        result,
        contract="ops.sync-status.v2",
        source="live_sync_status_file" if observed_epoch(observed_at) > 0 else "none",
        observed_at=observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["readiness"],
        error=error,
        reason_code=(
            "sync_status_not_observed"
            if not error and observed_epoch(observed_at) <= 0
            else None
        ),
        components={"timeframe_count": len(per_tf)},
        now=generated_at,
    ))


def ctrader_token_status_fact_payload(
    payload: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Describe the directly observed cTrader token metadata.

    A missing token is a known negative business state.  A token whose expiry
    cannot be observed is not sufficient evidence for a green "valid" badge.
    """

    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    has_token = bool(payload.get("has_token"))
    expiry_observed = observed_epoch(payload.get("expires_at")) > 0
    expiry_unknown = has_token and not expiry_observed
    return dict(attach_fact(
        result,
        contract="ops.ctrader-token-status.v2",
        source="ctrader_environment",
        observed_at=None if expiry_unknown else generated_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["readiness"],
        reason_code="token_expiry_not_observed" if expiry_unknown else None,
        components={"expiry_observed": expiry_observed},
        now=generated_at,
    ))


def external_data_status_fact_payload(
    payload: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Attach a fact to the synchronous external-data status probe.

    Source staleness remains a business value on each item.  Probe failures,
    however, are envelope errors so retained source rows cannot look healthy.
    """

    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    structured_sources = [item for item in sources if isinstance(item, Mapping)]
    source_errors = [
        str(item.get("error") or "").strip()
        for item in structured_sources
        if str(item.get("error") or "").strip()
    ]
    error = "; ".join(source_errors) or _error(payload)
    has_sources = bool(structured_sources)
    return dict(attach_fact(
        result,
        contract="ops.external-data-status.v2",
        source="external_status_probe" if has_sources else "none",
        observed_at=generated_at if has_sources else None,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["readiness"],
        error=error,
        reason_code="external_sources_not_observed" if not has_sources and not error else None,
        components={
            "source_count": len(structured_sources),
            "stale_count": sum(
                1
                for item in structured_sources
                if bool(item.get("stale"))
            ),
        },
        now=generated_at,
    ))


def trade_traces_fact_payload(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Mark a successful PostgreSQL trade-review query, including empty sets."""

    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    latest_record_at = max(
        (observed_epoch(item.get("created_at")) for item in items if isinstance(item, Mapping)),
        default=0.0,
    )
    return dict(attach_fact(
        result,
        contract="risk.trade-trace-recent.v2",
        source="state_pg",
        # This is a synchronous query of immutable review history.  Freshness
        # tracks the successful query, while record age stays informational.
        observed_at=generated_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["risk"],
        error=_error(payload),
        components={"latest_record_at": latest_record_at or None},
        now=generated_at,
    ))


def readiness_fact_payload(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    cache = payload.get("cache") if isinstance(payload.get("cache"), Mapping) else {}
    cache_source = str(cache.get("source") or "none")
    warming = str(payload.get("status") or "") == "warming_snapshot" or cache_source == "warming"
    return dict(attach_fact(
        result,
        contract="ops.backend-readiness.v2",
        source="none" if warming else (cache_source or "backend_readiness"),
        observed_at=None if warming else payload.get("generated_at"),
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["readiness"],
        error=_error(payload),
        reason_code="snapshot_warming" if warming else None,
        now=generated_at,
    ))


def live_autonomy_status_fact_payload(
    payload: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Attach freshness to the exact live-autonomy status endpoint.

    The endpoint reads RuntimeConfig synchronously, but its unlock posture is
    computed from a readiness snapshot.  That readiness timestamp is therefore
    the limiting observation for any UI action that could expand risk.
    """

    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    return dict(attach_fact(
        result,
        contract="ops.live-autonomy-status.v2",
        source="live_autonomy_service",
        observed_at=payload.get("readiness_generated_at"),
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["readiness"],
        reason_code=(
            None
            if observed_epoch(payload.get("readiness_generated_at")) > 0
            else "live_autonomy_readiness_not_observed"
        ),
        now=generated_at,
    ))


def live_autonomy_evaluation_fact_payload(
    payload: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Attach freshness to a read-only unlock evaluation response.

    ``evaluation.ok == false`` is a valid, known governance decision.  It must
    not be converted into a source error merely because the business gate is
    blocked.
    """

    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    return dict(attach_fact(
        result,
        contract="ops.live-autonomy-unlock-evaluation.v2",
        source="live_autonomy_service",
        observed_at=payload.get("readiness_generated_at"),
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["readiness"],
        reason_code=(
            None
            if observed_epoch(payload.get("readiness_generated_at")) > 0
            else "live_autonomy_readiness_not_observed"
        ),
        now=generated_at,
    ))


def db_health_fact_payload(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Describe the cached database inventory without treating cache age as zero."""

    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    checked_at = payload.get("checked_at")
    return dict(attach_fact(
        result,
        contract="system.db-health.v2",
        source="db_health_cache",
        observed_at=checked_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["readiness"],
        error=_error(payload),
        reason_code=None if observed_epoch(checked_at) > 0 else "db_health_not_observed",
        now=generated_at,
    ))


def alerts_fact_payload(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Keep static rule configuration separate from runtime delivery health."""

    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    delivery = payload.get("delivery") if isinstance(payload.get("delivery"), Mapping) else {}
    result.setdefault("delivery", {"status": "not_registered", "registered": False})
    return dict(attach_fact(
        result,
        contract="ops.alerts.v2",
        source="static_rule_config",
        observed_at=generated_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["readiness"],
        components={
            "runtime_delivery": fact_envelope(
                contract="ops.alert-delivery.v1",
                source=str(delivery.get("source") or "not_registered"),
                observed_at=delivery.get("observed_at"),
                stale_after_sec=DEFAULT_STALE_AFTER_SEC["recovery"],
                reason_code="alert_delivery_not_registered"
                if not bool(delivery.get("registered"))
                else None,
                now=generated_at,
            ).to_dict(),
        },
        now=generated_at,
    ))


def recovery_fact_payload(
    payload: Mapping[str, Any],
    *,
    registered: bool,
    now: float | None = None,
) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    result["registered"] = bool(registered)
    if not registered:
        result["status"] = "not_registered"
    else:
        result.setdefault("status", "running" if result.get("running") else "stopped")
    return dict(attach_fact(
        result,
        contract="ops.auto-recovery.v2",
        source="auto_recovery" if registered else "not_registered",
        observed_at=result.get("last_check") if registered else None,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["recovery"],
        reason_code=None if registered else "auto_recovery_not_registered",
        now=generated_at,
    ))


def policy_verdicts_fact_payload(
    payload: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Mark a successful PostgreSQL verdict query, including a valid empty set."""

    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    observed_at = max(
        (observed_epoch(item.get("decision_ts")) for item in items if isinstance(item, Mapping)),
        default=generated_at,
    )
    return dict(attach_fact(
        result,
        contract="risk.policy-verdicts.v2",
        source="state_pg",
        observed_at=observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["risk"],
        error=_error(payload),
        now=generated_at,
    ))


def risk_summary_fact_payload(
    payload: Mapping[str, Any],
    *,
    risk_observed_at: Any = None,
    risk_error: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    health = payload.get("system_health") if isinstance(payload.get("system_health"), Mapping) else {}
    health_observed_at = health.get("ts")
    observed_at = _positive_min([health_observed_at, risk_observed_at])
    errors = health.get("errors") if isinstance(health.get("errors"), list) else []
    health_error = "; ".join(str(item) for item in errors if item) or None
    components = {
        "system_health": _component(
            contract="system.runtime-health.v1",
            source="system_health" if observed_epoch(health_observed_at) > 0 else "none",
            observed_at=health_observed_at,
            stale_after_sec=DEFAULT_STALE_AFTER_SEC["risk"],
            error=health_error,
            now=generated_at,
        ),
        "risk_inputs": _component(
            contract="risk.inputs.v1",
            source="state_pg" if observed_epoch(risk_observed_at) > 0 else "none",
            observed_at=risk_observed_at,
            stale_after_sec=DEFAULT_STALE_AFTER_SEC["risk"],
            error=risk_error,
            now=generated_at,
        ),
    }
    return dict(attach_fact(
        result,
        contract="risk.summary.v2",
        source="system_health+state_pg" if observed_epoch(observed_at) > 0 else "none",
        observed_at=observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["risk"],
        error=risk_error or health_error,
        reason_code="risk_sources_incomplete" if observed_epoch(observed_at) <= 0 else None,
        components=components,
        now=generated_at,
    ))


def state_snapshot_fact_payload(
    payload: Mapping[str, Any],
    *,
    account: Mapping[str, Any] | None,
    account_updated_at: Any,
    positions_updated_at: Any,
    diagnostic_ts: Any,
    spot_quote: Mapping[str, Any] | None,
    positions_component_facts: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    raw_source = str(payload.get("source") or "none")
    source = "ctrader" if raw_source in {"live", "frozen"} else "none"
    spot_ts = (spot_quote or {}).get("ts")
    running = raw_source == "live"
    required = [account_updated_at, positions_updated_at]
    if running:
        required.append(diagnostic_ts)
    observed_at = _positive_min(required)
    account_error = None
    if isinstance(account, Mapping) and account.get("ok") is False:
        account_error = account.get("error") or "account_source_failure"
    positions_component = _component(
        contract="live.positions.v2",
        source="ctrader" if observed_epoch(positions_updated_at) > 0 else "none",
        observed_at=positions_updated_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["positions"],
        now=generated_at,
    )
    positions_component["components"] = _position_reconcile_component_views(
        positions_component_facts,
        now=generated_at,
    )
    components = {
        "account": _component(
            contract="live.account.v2",
            source="ctrader" if account else "none",
            observed_at=account_updated_at,
            stale_after_sec=DEFAULT_STALE_AFTER_SEC["account"],
            error=account_error,
            now=generated_at,
        ),
        "positions": positions_component,
        "loop": _component(
            contract="live.loop.v2",
            source="live_process" if running else "live_process_stopped",
            observed_at=diagnostic_ts if running else generated_at,
            stale_after_sec=DEFAULT_STALE_AFTER_SEC["loop"],
            reason_code="loop_heartbeat_missing" if running and observed_epoch(diagnostic_ts) <= 0 else None,
            now=generated_at,
        ),
        "spot": _component(
            contract="live.spot-quote.v1",
            source=str((spot_quote or {}).get("source") or "none"),
            observed_at=spot_ts,
            stale_after_sec=DEFAULT_STALE_AFTER_SEC["spot"],
            now=generated_at,
        ),
        "transport": {"mode": raw_source},
    }
    return dict(attach_fact(
        result,
        contract="live.state.v2",
        source=source,
        observed_at=observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["state"],
        error=account_error,
        reason_code="state_source_unavailable" if source == "none" else None,
        components=components,
        now=generated_at,
    ))


def health_fact_payload(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    error = None
    if str(payload.get("status") or "") != "ok":
        error = f"backend_health_{payload.get('status') or 'unknown'}"
    ctrader_status = str(payload.get("ctrader") or "unknown").lower()
    observed_at = generated_at
    reason_code = None
    if not error and ctrader_status in {"", "unknown", "warming_up"}:
        observed_at = None
        reason_code = "ctrader_health_unknown"
    return dict(attach_fact(
        result,
        contract="system.health.v2",
        source="backend_health_probe",
        observed_at=observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["ws"],
        error=error,
        reason_code=reason_code,
        components={
            "db": {"status": payload.get("db")},
            "ctrader": {"status": payload.get("ctrader")},
        },
        now=generated_at,
    ))
