"""Local durable safety state for broker risk-reducing operations.

PostgreSQL is the governance source of truth, but emergency broker actions
must remain available while PostgreSQL or an audit sink is unavailable.  This
module therefore owns two deliberately small, append-only local ledgers:

* ``no_new_risk_latch.jsonl`` is a fail-closed admission latch.  Its additive
  cause-set events determine whether new broker risk is allowed.  Historical
  v1 activate/clear records remain readable.
* ``safety_outbox.jsonl`` records audit/enrichment failures for later replay.
* ``safety_shadow_observations.jsonl`` (owned by the shadow-observation
  service) records full-cycle rollout evidence without granting broker truth.

Neither ledger is a broker-position fact source.  Broker reconciliation stays
authoritative for deciding whether a close actually completed.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


class SafetyStatePersistenceError(RuntimeError):
    """Raised when the fail-closed latch cannot be persisted."""


_WRITE_LOCK = threading.RLock()
_PERSISTENCE_FAILURE_LATCH = False
_PERSISTENCE_FAILURE_CAUSES: dict[tuple[str, str], dict[str, Any]] = {}
_LATCH_SCHEMA_V1 = "live_no_new_risk_latch.v1"
_LATCH_CAUSE_CONTRACT = "live_no_new_risk_latch_causes.v2"


def _normalized_token(value: Any, *, default: str) -> str:
    token = "_".join(str(value or "").strip().lower().replace("-", "_").split())
    return token or default


def _infer_cause(
    *,
    reason: str,
    actor: str,
    correlation_id: str,
    metadata: Mapping[str, Any],
) -> tuple[str, str]:
    """Map historical/caller-compatible activations onto stable safety causes."""

    normalized_reason = _normalized_token(reason, default="safety_condition")
    normalized_actor = _normalized_token(actor, default="system_safety")
    correlation = str(correlation_id or "").strip()
    if normalized_reason == "broker_execution_outcome_unknown":
        action = _normalized_token(metadata.get("action"), default="broker_mutation")
        try:
            position_id = int(metadata.get("position_id") or 0)
        except (TypeError, ValueError):
            position_id = 0
        cause_id = correlation or f"{action}:{position_id}"
        return "broker_execution_unknown", cause_id
    if normalized_reason == "safety_v2_forced_shadow":
        return "safety_v2_forced_shadow", "candidate_comparison"
    if normalized_reason.startswith("emergency_close") or "emergency_close" in normalized_actor:
        return "emergency_resume", "emergency_close"
    if (
        normalized_reason == "safety_freshness_failed"
        or "safety_watchdog" in normalized_actor
    ):
        return "safety_freshness", "live_safety_watchdog"
    if correlation.startswith("live_revoke_"):
        return "live_autonomy_revoke", "live_autonomy"
    if correlation.startswith("incident_control_"):
        return "incident_control", "runtime_incident_mode"
    return "safety_condition", normalized_reason


def _cause_identity(
    *,
    cause: str,
    cause_id: str,
    reason: str,
    actor: str,
    correlation_id: str,
    metadata: Mapping[str, Any],
) -> tuple[str, str]:
    if not str(cause or "").strip():
        return _infer_cause(
            reason=reason,
            actor=actor,
            correlation_id=correlation_id,
            metadata=metadata,
        )
    normalized_cause = _normalized_token(cause, default="safety_condition")
    normalized_id = str(cause_id or "").strip()
    if not normalized_id:
        _, inferred_id = _infer_cause(
            reason=reason,
            actor=actor,
            correlation_id=correlation_id,
            metadata=metadata,
        )
        normalized_id = inferred_id or normalized_cause
    return normalized_cause, normalized_id


def _state_dir() -> Path:
    configured = str(os.getenv("QUANT_SAFETY_STATE_DIR", "") or "").strip()
    return Path(configured) if configured else Path("data/safety")


def safety_latch_path() -> Path:
    return _state_dir() / "no_new_risk_latch.jsonl"


def safety_outbox_path() -> Path:
    return _state_dir() / "safety_outbox.jsonl"


def _append_fsynced(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(dict(record), ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    with _WRITE_LOCK:
        fd = os.open(path, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short append to safety ledger")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)


def activate_no_new_risk_latch(
    *,
    reason: str,
    actor: str = "system:emergency_close",
    correlation_id: str = "",
    metadata: Mapping[str, Any] | None = None,
    cause: str = "",
    cause_id: str = "",
) -> dict[str, Any]:
    """Persist and return an active fail-closed new-risk latch.

    The append and fsync happen before this function returns.  A persistence
    failure installs the process-local fail-closed latch before raising.  New
    risk must remain blocked; an already requested risk-reducing broker action
    may continue and report the durability failure separately.
    """

    metadata_payload = dict(metadata or {})
    normalized_cause, normalized_cause_id = _cause_identity(
        cause=cause,
        cause_id=cause_id,
        reason=str(reason or "safety_condition"),
        actor=str(actor or "system:safety"),
        correlation_id=str(correlation_id or ""),
        metadata=metadata_payload,
    )
    record = {
        # Keep the v1 envelope so an older binary fails closed on the latest
        # event.  cause_contract/cause/cause_id are additive v2 fields.
        "schema_version": _LATCH_SCHEMA_V1,
        "cause_contract": _LATCH_CAUSE_CONTRACT,
        "event_id": str(uuid.uuid4()),
        "event": "activate",
        "active": True,
        "cause": normalized_cause,
        "cause_id": normalized_cause_id,
        "reason": str(reason or "safety_condition"),
        "actor": str(actor or "system:safety"),
        "correlation_id": str(correlation_id or ""),
        "metadata": metadata_payload,
        "created_at": time.time(),
    }
    global _PERSISTENCE_FAILURE_LATCH
    try:
        _append_fsynced(safety_latch_path(), record)
    except Exception as exc:  # the latch itself must fail closed
        # Even when durable storage is unavailable, the current process must
        # stop admitting risk immediately.  This cannot replace durability;
        # callers surface the failure and require operator repair while close
        # or reduce operations remain available.
        failure = {
            **record,
            "state": "persistence_failed_fail_closed",
            "reason": "latch_persistence_failed",
        }
        _PERSISTENCE_FAILURE_CAUSES[(normalized_cause, normalized_cause_id)] = failure
        _PERSISTENCE_FAILURE_LATCH = True
        raise SafetyStatePersistenceError(f"no_new_risk_latch_persist_failed:{type(exc).__name__}:{exc}") from exc
    return record


def _decoded_latch_events() -> list[dict[str, Any]]:
    path = safety_latch_path()
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not raw.strip():
            continue
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("schema_version") != _LATCH_SCHEMA_V1:
            continue
        events.append(payload)
    return events


def _replay_latch_causes(
    events: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], bool]:
    active: dict[tuple[str, str], dict[str, Any]] = {}
    legacy_records = False
    for payload in events:
        event = str(payload.get("event") or "").strip().lower()
        metadata = payload.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        explicit_cause = str(payload.get("cause") or "").strip()
        explicit_cause_id = str(payload.get("cause_id") or "").strip()
        if not explicit_cause:
            legacy_records = True
        cause, cause_id = _cause_identity(
            cause=explicit_cause,
            cause_id=explicit_cause_id,
            reason=str(payload.get("reason") or "safety_condition"),
            actor=str(payload.get("actor") or "system:safety"),
            correlation_id=str(payload.get("correlation_id") or ""),
            metadata=metadata,
        )
        if event == "activate":
            active[(cause, cause_id)] = {
                **payload,
                "cause": cause,
                "cause_id": cause_id,
                "metadata": metadata,
            }
            continue
        if event == "release_cause":
            released_ids = payload.get("released_cause_ids")
            if isinstance(released_ids, list) and released_ids:
                for released_id in released_ids:
                    active.pop((cause, str(released_id or "")), None)
            elif explicit_cause_id:
                active.pop((cause, cause_id), None)
            else:
                for key in [item for item in active if item[0] == cause]:
                    active.pop(key, None)
            continue
        if event == "clear":
            # Historical v1 clear had blanket semantics.  New code never
            # appends this event, but must preserve the old replay contract.
            legacy_records = True
            active.clear()
    return active, legacy_records


def _cause_summaries(
    active: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "cause": cause,
            "cause_id": cause_id,
            "reason": str(record.get("reason") or ""),
            "actor": str(record.get("actor") or ""),
            "correlation_id": str(record.get("correlation_id") or ""),
            "created_at": float(record.get("created_at") or 0.0),
            # Additive recovery cursor.  Safety callers use this to prove the
            # exact broker/deal fact that allows only this cause to release.
            "metadata": dict(record.get("metadata") or {}),
        }
        for (cause, cause_id), record in sorted(active.items())
    ]


def release_no_new_risk_latch_cause(
    *,
    cause: str,
    reason: str,
    actor: str,
    correlation_id: str = "",
    cause_id: str = "",
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Release only one safety cause (or one cause family when id is empty)."""

    normalized_cause = _normalized_token(cause, default="")
    if not normalized_cause or not str(reason or "").strip() or not str(actor or "").strip():
        raise ValueError("latch_cause_release_requires_cause_reason_and_actor")
    requested_id = str(cause_id or "").strip()
    try:
        durable_active, _legacy = _replay_latch_causes(_decoded_latch_events())
    except Exception as exc:
        raise SafetyStatePersistenceError(
            f"no_new_risk_latch_read_before_release_failed:{type(exc).__name__}:{exc}"
        ) from exc
    matching_keys = [
        key
        for key in durable_active
        if key[0] == normalized_cause and (not requested_id or key[1] == requested_id)
    ]
    process_matching = [
        key
        for key in _PERSISTENCE_FAILURE_CAUSES
        if key[0] == normalized_cause and (not requested_id or key[1] == requested_id)
    ]
    remaining = dict(durable_active)
    for key in matching_keys:
        remaining.pop(key, None)
    for key, record in _PERSISTENCE_FAILURE_CAUSES.items():
        if key not in process_matching:
            remaining.setdefault(key, record)
    record = {
        "schema_version": _LATCH_SCHEMA_V1,
        "cause_contract": _LATCH_CAUSE_CONTRACT,
        "event_id": str(uuid.uuid4()),
        "event": "release_cause",
        # Older binaries understand only the latest aggregate ``active``
        # field.  Keep them conservatively latched across a rolling deploy;
        # v2 readers replay the cause set and may report inactive below.
        "active": True,
        "active_after_release_hint": bool(remaining),
        "cause": normalized_cause,
        "cause_id": requested_id,
        "released_cause_ids": [key[1] for key in matching_keys],
        "reason": str(reason),
        "actor": str(actor),
        "correlation_id": str(correlation_id or ""),
        "metadata": {"evidence": dict(evidence or {})},
        "created_at": time.time(),
    }
    try:
        _append_fsynced(safety_latch_path(), record)
    except Exception as exc:
        raise SafetyStatePersistenceError(
            f"no_new_risk_latch_cause_release_failed:{type(exc).__name__}:{exc}"
        ) from exc
    for key in process_matching:
        _PERSISTENCE_FAILURE_CAUSES.pop(key, None)
    global _PERSISTENCE_FAILURE_LATCH
    _PERSISTENCE_FAILURE_LATCH = bool(_PERSISTENCE_FAILURE_CAUSES)
    status = no_new_risk_latch_status(fail_closed=True)
    released_keys = set(matching_keys) | set(process_matching)
    return {
        **record,
        "active": bool(status.get("active")),
        "state": str(status.get("state") or ""),
        "released": len(released_keys),
        "remaining_causes": list(status.get("causes") or []),
    }


def clear_no_new_risk_latch(
    *,
    reason: str,
    actor: str,
    correlation_id: str = "",
    cause: str = "incident_control",
    cause_id: str = "",
) -> dict[str, Any]:
    """Compatibility wrapper for a governed, cause-specific resume.

    Blanket clear is intentionally no longer available.  Existing callers are
    mapped to the incident-control cause and therefore cannot erase broker,
    heartbeat, emergency-resume, or other independent safety evidence.
    """

    return release_no_new_risk_latch_cause(
        cause=cause,
        cause_id=cause_id,
        reason=reason,
        actor=actor,
        correlation_id=correlation_id,
    )


def no_new_risk_latch_status(*, fail_closed: bool = True) -> dict[str, Any]:
    """Replay durable causes; corrupt/unreadable state fails closed."""

    path = safety_latch_path()
    if not path.exists() and not _PERSISTENCE_FAILURE_CAUSES:
        return {
            "schema_version": _LATCH_SCHEMA_V1,
            "cause_contract": _LATCH_CAUSE_CONTRACT,
            "active": False,
            "state": "not_set",
            "reason": "",
            "causes": [],
            "cause_count": 0,
        }
    try:
        events = _decoded_latch_events()
        active, legacy_records = _replay_latch_causes(events)
        for key, record in _PERSISTENCE_FAILURE_CAUSES.items():
            active[key] = record
        if not events and not active:
            raise ValueError("no valid latch records")
        summaries = _cause_summaries(active)
        latest = dict(events[-1]) if events else {}
        persistence_failed = bool(_PERSISTENCE_FAILURE_CAUSES)
        return {
            **latest,
            "schema_version": _LATCH_SCHEMA_V1,
            "cause_contract": _LATCH_CAUSE_CONTRACT,
            "active": bool(active),
            "state": (
                "persistence_failed_fail_closed"
                if persistence_failed
                else "active"
                if active
                else "cleared"
            ),
            "reason": (
                "latch_persistence_failed"
                if persistence_failed
                else "multiple_safety_causes"
                if len(active) > 1
                else str(next(iter(active.values())).get("reason") or "")
                if active
                else str(latest.get("reason") or "")
            ),
            "causes": summaries,
            "cause_count": len(summaries),
            "legacy_records_replayed": legacy_records,
        }
    except Exception as exc:
        return {
            "schema_version": _LATCH_SCHEMA_V1,
            "cause_contract": _LATCH_CAUSE_CONTRACT,
            "active": bool(fail_closed),
            "state": "error",
            "reason": "latch_state_unreadable",
            "error": f"{type(exc).__name__}: {exc}",
            "causes": [],
            "cause_count": 0,
        }


def no_new_risk_latched(*, fail_closed: bool = True) -> bool:
    return bool(no_new_risk_latch_status(fail_closed=fail_closed).get("active"))


def safety_v2_forced_shadow_status() -> dict[str, Any]:
    """Return the durable V2 authority override until its cause is released.

    A candidate-comparison failure must survive a loop generation restart.
    The normal no-new-risk latch prevents entries, while this projection also
    prevents a freshly constructed ``LiveSafetyPlane(mode="enforce")`` from
    reclaiming broker mutation authority before an operator explicitly
    releases the ``safety_v2_forced_shadow`` cause.
    """

    path = safety_latch_path()
    if not path.exists() and not _PERSISTENCE_FAILURE_CAUSES:
        return {
            "schema_version": "live_safety_v2_forced_shadow.v1",
            "active": False,
            "state": "not_set",
            "reason": "",
        }
    try:
        active, _legacy = _replay_latch_causes(_decoded_latch_events())
        for key, record in _PERSISTENCE_FAILURE_CAUSES.items():
            active[key] = record
        forced_record: dict[str, Any] | None = None
        for (cause, _cause_id), payload in active.items():
            metadata = payload.get("metadata")
            metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
            blockers = {str(item) for item in metadata.get("blockers", []) or []}
            if (
                cause == "safety_v2_forced_shadow"
                or str(payload.get("reason") or "") == "safety_v2_forced_shadow"
                or "safety_v2_forced_shadow" in blockers
            ):
                forced_record = dict(payload)
                break
        if forced_record is None:
            return {
                "schema_version": "live_safety_v2_forced_shadow.v1",
                "active": False,
                "state": "not_set",
                "reason": "",
            }
        return {
            "schema_version": "live_safety_v2_forced_shadow.v1",
            "active": True,
            "state": "active",
            "reason": "safety_v2_forced_shadow",
            "event_id": str(forced_record.get("event_id") or ""),
            "created_at": float(forced_record.get("created_at") or 0.0),
            "metadata": dict(forced_record.get("metadata") or {}),
        }
    except Exception as exc:
        # If the authority ledger cannot be read, enforcing V2 would be an
        # unsafe optimistic interpretation.  Risk-reducing legacy protection
        # remains available while new risk stays blocked by the latch reader.
        return {
            "schema_version": "live_safety_v2_forced_shadow.v1",
            "active": True,
            "state": "error_fail_closed",
            "reason": "safety_authority_state_unreadable",
            "error": f"{type(exc).__name__}: {exc}",
        }


def unresolved_broker_outcome_mutations() -> list[dict[str, Any]]:
    """Return unknown broker mutations not explicitly resolved by recovery."""

    path = safety_latch_path()
    if not path.exists() and not _PERSISTENCE_FAILURE_CAUSES:
        return []
    try:
        replayed, _legacy = _replay_latch_causes(_decoded_latch_events())
        for key, record in _PERSISTENCE_FAILURE_CAUSES.items():
            replayed[key] = record
        active: dict[tuple[str, int, str], dict[str, Any]] = {}
        for (cause, cause_id), payload in replayed.items():
            if cause != "broker_execution_unknown":
                continue
            metadata = payload.get("metadata")
            metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
            action = str(metadata.get("action") or "").strip().lower()
            try:
                position_id = int(metadata.get("position_id") or 0)
            except (TypeError, ValueError):
                position_id = 0
            if not action or (position_id <= 0 and action != "market_open"):
                continue
            intent_id = str(payload.get("correlation_id") or cause_id or "")
            active[(action, position_id, intent_id)] = {
                "intent_id": intent_id,
                "status": "unknown",
                "action": action,
                "position_id": position_id,
                "created_at": float(payload.get("created_at") or 0.0),
                "evidence": dict(metadata.get("evidence") or {}),
            }
        return list(active.values())
    except Exception:
        # The normal latch reader already fails new-risk admission closed for
        # unreadable state.  Risk-reduction remains an escape hatch; callers
        # additionally retain process-local unknown identities when possible.
        return []


def resolve_broker_outcome_mutation(
    *,
    outcome: str,
    evidence: Mapping[str, Any],
    intent_id: str = "",
    action: str = "",
    position_id: int = 0,
    actor: str = "execution:ctrader_recovery",
) -> dict[str, Any]:
    """Release unknown broker evidence only after an explicit terminal proof."""

    terminal = str(outcome or "").strip().lower()
    if terminal not in {"confirmed", "rejected"}:
        raise ValueError("broker_outcome_resolution_requires_confirmed_or_rejected")
    if not isinstance(evidence, Mapping) or not dict(evidence):
        raise ValueError("broker_outcome_resolution_requires_evidence")
    normalized_intent = str(intent_id or "").strip()
    normalized_action = str(action or "").strip().lower()
    try:
        normalized_position_id = int(position_id or 0)
    except (TypeError, ValueError):
        normalized_position_id = 0
    if not normalized_intent and (not normalized_action or normalized_position_id <= 0):
        raise ValueError("broker_outcome_resolution_requires_intent_or_action_position")

    replayed, _legacy = _replay_latch_causes(_decoded_latch_events())
    for key, record in _PERSISTENCE_FAILURE_CAUSES.items():
        replayed[key] = record
    matches: list[tuple[str, str, dict[str, Any]]] = []
    for (cause, cause_id), record in replayed.items():
        if cause != "broker_execution_unknown":
            continue
        metadata = record.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        record_intent = str(record.get("correlation_id") or cause_id or "")
        record_action = str(metadata.get("action") or "").strip().lower()
        try:
            record_position_id = int(metadata.get("position_id") or 0)
        except (TypeError, ValueError):
            record_position_id = 0
        if normalized_intent:
            matched = record_intent == normalized_intent
        else:
            matched = (
                record_action == normalized_action
                and record_position_id == normalized_position_id
            )
        if matched:
            matches.append((cause, cause_id, record))

    releases: list[dict[str, Any]] = []
    for cause, cause_id, _record in matches:
        releases.append(
            release_no_new_risk_latch_cause(
                cause=cause,
                cause_id=cause_id,
                reason=f"broker_execution_outcome_{terminal}_by_recovery",
                actor=actor,
                correlation_id=normalized_intent,
                evidence={
                    "outcome": terminal,
                    "action": normalized_action,
                    "position_id": normalized_position_id,
                    **dict(evidence),
                },
            )
        )
    status = no_new_risk_latch_status(fail_closed=True)
    return {
        "schema_version": "broker_outcome_local_resolution.v1",
        "status": "resolved" if matches else "not_found",
        "outcome": terminal,
        "intent_id": normalized_intent,
        "action": normalized_action,
        "position_id": normalized_position_id,
        "released": len(matches),
        "release_events": releases,
        "no_new_risk_latch": status,
    }


def append_safety_outbox(
    *,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    error: str = "",
    correlation_id: str = "",
) -> dict[str, Any]:
    """Append a replayable safety audit event.

    This function intentionally raises on local I/O failure so callers can log
    the secondary failure, but broker action results must never be rewritten
    because of it.
    """

    record = {
        "schema_version": "live_safety_outbox.v1",
        "event_id": str(uuid.uuid4()),
        "event_type": str(event_type or "unknown"),
        "correlation_id": str(correlation_id or ""),
        "payload": dict(payload or {}),
        "error": str(error or ""),
        "created_at": time.time(),
    }
    _append_fsynced(safety_outbox_path(), record)
    return record


def reset_safety_state_for_tests() -> None:
    global _PERSISTENCE_FAILURE_LATCH
    _PERSISTENCE_FAILURE_LATCH = False
    _PERSISTENCE_FAILURE_CAUSES.clear()
