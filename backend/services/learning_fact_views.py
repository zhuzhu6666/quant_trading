"""Endpoint-level ``_fact`` contracts for learning and model read APIs.

The learning API exposes durable audit/history views whose business payloads
predate ``fact.v1``.  This module keeps those payloads intact and attaches
provenance without recursively guessing generic fields such as ``status`` or
``items``.  Every adapter below names the timestamp fields owned by its
specific endpoint.

For a non-empty durable result, freshness is based only on a persisted record
timestamp.  A successful authoritative query that proves the result set is
empty is a fact observed at query time; it is deliberately distinguished from
using response generation time to make old records appear fresh.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import backend.core.db as core_db
from backend.core.db_helpers import row_value as _row_value
from backend.services.canonical_v2 import _sql
from backend.services.canonical_v2_reader import canonical_fact_observation
from backend.services.fact_envelope import DEFAULT_STALE_AFTER_SEC, attach_fact, observed_epoch


LEARNING_STALE_AFTER_SEC = DEFAULT_STALE_AFTER_SEC["learning"]
CANONICAL_SOURCE = "canonical"


@dataclass(frozen=True)
class DurableSourceObservation:
    """Result of a separate, read-only observation of durable source rows."""

    observed_at: float | None
    authoritative_empty: bool
    record_count: int
    tables: tuple[str, ...]
    error: str | None = None


def _copy(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(payload or {})


def _items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = payload.get("items")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _latest_persisted_value(
    items: Sequence[Mapping[str, Any]],
    timestamp_fields: Sequence[str],
) -> Any:
    latest_value: Any = None
    latest_epoch = 0.0
    for item in items:
        for field in timestamp_fields:
            value = item.get(field)
            epoch = observed_epoch(value)
            if epoch > latest_epoch:
                latest_epoch = epoch
                latest_value = value
    return latest_value


def _previous_observed_at(payload: Mapping[str, Any]) -> Any:
    fact = payload.get("_fact")
    if isinstance(fact, Mapping):
        return fact.get("observed_at")
    return None


def _payload_error(payload: Mapping[str, Any]) -> str | None:
    # ``_learning_cached_read`` preserves old HTTP behavior by serving the last
    # good payload with these additive markers.  The boundary must still expose
    # that the current source read failed.
    if payload.get("stale") is True:
        return str(payload.get("stale_reason") or "source_read_failed")
    if payload.get("ok") is False:
        return str(payload.get("error") or payload.get("detail") or "source_reported_failure")
    return None


def _durable_list_fact(
    payload: Mapping[str, Any],
    *,
    contract: str,
    source: str,
    timestamp_fields: Sequence[str],
    query_observed_at: float | None = None,
    source_error: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    items = _items(payload)
    observed_at = _latest_persisted_value(items, timestamp_fields)
    try:
        reported_count = max(0, int(payload.get("count", len(items)) or 0))
    except (TypeError, ValueError):
        reported_count = len(items)
    authoritative_empty = not items and reported_count == 0
    resolved_error = source_error or _payload_error(payload)

    if authoritative_empty:
        # Preserve the first query observation across the 30-second learning
        # cache instead of renewing empty data on every response render.
        observed_at = _previous_observed_at(payload) or query_observed_at or generated_at

    missing_persisted_timestamp = (
        not authoritative_empty and observed_epoch(observed_at) <= 0
    )
    return dict(
        attach_fact(
            result,
            contract=contract,
            source=source,
            observed_at=observed_at,
            stale_after_sec=LEARNING_STALE_AFTER_SEC,
            error=resolved_error,
            reason_code=(
                resolved_error
                or (
                    "persisted_timestamp_missing"
                    if missing_persisted_timestamp
                    else None
                )
            ),
            components={
                "record_count": max(len(items), reported_count),
                "authoritative_empty": authoritative_empty,
                "timestamp_fields": list(timestamp_fields),
            },
            now=generated_at,
        )
    )


def active_parameter_templates_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.parameter-templates-active.v2",
        source=CANONICAL_SOURCE,
        timestamp_fields=("updated_at", "activated_at"),
        now=now,
    )


def suggestions_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.suggestions.v2",
        source=CANONICAL_SOURCE,
        timestamp_fields=("reviewed_at", "created_at"),
        now=now,
    )


def applications_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.applications.v2",
        source=CANONICAL_SOURCE,
        timestamp_fields=("last_review_at", "created_at", "cycle_ts"),
        now=now,
    )


def lifecycle_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.lifecycle.v2",
        source=CANONICAL_SOURCE,
        timestamp_fields=("ts",),
        now=now,
    )


def model_permission_audits_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.model-permission-audits.v2",
        source=CANONICAL_SOURCE,
        timestamp_fields=("created_at",),
        now=now,
    )


def model_shadow_queue_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.model-shadow-queue.v2",
        source="model_registry",
        timestamp_fields=("updated_at", "created_at"),
        now=now,
    )


def model_canary_reviews_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.model-canary-reviews.v2",
        source="model_registry",
        timestamp_fields=("created_at",),
        now=now,
    )


def factor_governance_audits_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.factor-governance-lightgbm-audits.v2",
        source=CANONICAL_SOURCE,
        timestamp_fields=("created_at",),
        now=now,
    )


def factor_governance_advisories_fact_payload(
    payload: Mapping[str, Any],
    *,
    audit_observed_at: Any,
    audit_count: int,
    now: float | None = None,
) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    previous = _previous_observed_at(payload)
    authoritative_empty = int(audit_count) == 0
    observed_at = audit_observed_at
    payload_error = _payload_error(payload)
    if authoritative_empty:
        observed_at = previous or generated_at
    return dict(
        attach_fact(
            result,
            contract="learning.factor-governance-lightgbm-advisories.v2",
            source=CANONICAL_SOURCE,
            observed_at=observed_at,
            stale_after_sec=LEARNING_STALE_AFTER_SEC,
            error=payload_error,
            reason_code=(
                payload_error
                or (
                    "persisted_audit_timestamp_missing"
                    if audit_count > 0 and observed_epoch(observed_at) <= 0
                    else None
                )
            ),
            components={
                "source_audit_count": int(audit_count),
                "advisory_count": len(_items(payload)),
                "authoritative_empty": authoritative_empty,
                "timestamp_fields": ["created_at"],
            },
            now=generated_at,
        )
    )


def learning_summary_fact_payload(
    payload: Mapping[str, Any], *, query_observed_at: float | None = None, now: float | None = None
) -> dict[str, Any]:
    """Attach the aggregate learning summary to its newest durable member.

    The summary is not an event itself.  Only its explicitly named latest
    records may establish observation time; arbitrary nested ``created_at``
    fields are deliberately ignored.
    """

    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    latest_values: list[Any] = []
    for key, fields in (
        ("latest_review", ("created_at",)),
        ("latest_parameter_template_candidate", ("updated_at", "created_at")),
        ("latest_parameter_template_candidate_trace", ("updated_at", "created_at")),
        ("latest_parameter_template_recommendation", ("updated_at", "created_at")),
    ):
        item = payload.get(key)
        if not isinstance(item, Mapping):
            continue
        latest_values.extend(item.get(field) for field in fields)
    observed_at = max(latest_values, key=observed_epoch, default=None)
    has_durable_member = observed_epoch(observed_at) > 0
    aggregate_count = 0
    for key in (
        "suggestions",
        "reviews",
        "parameter_template_candidates",
        "parameter_template_recommendations",
    ):
        counts = payload.get(key)
        if not isinstance(counts, Mapping):
            continue
        for value in counts.values():
            try:
                aggregate_count += max(0, int(value or 0))
            except (TypeError, ValueError):
                continue
    try:
        aggregate_count += max(0, int(payload.get("applications") or 0))
    except (TypeError, ValueError):
        pass
    authoritative_empty = aggregate_count == 0 and not has_durable_member
    previous = _previous_observed_at(payload)
    if authoritative_empty:
        observed_at = previous or query_observed_at or generated_at
    source_error = _payload_error(payload)
    return dict(
        attach_fact(
            result,
            contract="learning.summary.v2",
            source=CANONICAL_SOURCE,
            observed_at=observed_at,
            stale_after_sec=LEARNING_STALE_AFTER_SEC,
            error=source_error,
            reason_code=(
                source_error
                or (
                    "persisted_timestamp_missing"
                    if aggregate_count > 0 and not has_durable_member
                    else None
                )
            ),
            components={
                "record_count": aggregate_count,
                "authoritative_empty": authoritative_empty,
                "timestamp_fields": [
                    "latest_review.created_at",
                    "latest_parameter_template_candidate.updated_at",
                    "latest_parameter_template_recommendation.updated_at",
                ],
            },
            now=generated_at,
        )
    )


def learning_reviews_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.reviews.v2",
        source=CANONICAL_SOURCE,
        timestamp_fields=("created_at",),
        now=now,
    )


def autonomous_samples_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.autonomous-samples.v2",
        source=CANONICAL_SOURCE,
        timestamp_fields=("updated_at", "event_ts", "created_at"),
        now=now,
    )


def model_position_quality_audits_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.model-position-quality-audits.v2",
        source=CANONICAL_SOURCE,
        timestamp_fields=("created_at",),
        now=now,
    )


def model_open_quality_audits_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.model-open-quality-audits.v2",
        source=CANONICAL_SOURCE,
        timestamp_fields=("created_at",),
        now=now,
    )


def model_inference_audits_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.model-inference-audits.v2",
        source="model_registry",
        timestamp_fields=("created_at",),
        now=now,
    )


def model_offmarket_high_load_audits_fact_payload(
    payload: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return _durable_list_fact(
        payload,
        contract="learning.model-offmarket-high-load-audits.v2",
        source=CANONICAL_SOURCE,
        timestamp_fields=("finished_at", "started_at"),
        now=now,
    )


def _connect_state_source(db_path: str | Path):
    if core_db.is_state_db_path(db_path):
        try:
            return core_db.get_state_conn(read_only=True)
        except TypeError:
            return core_db.get_state_conn()
    return core_db.connect_sqlite(db_path, read_only=True)


def observe_learning_dataset_source(
    db_path: str | Path,
    *,
    include_trade_reviews: bool,
    now: float | None = None,
) -> DurableSourceObservation:
    """Read record counts and real source timestamps without writing schema."""

    queried_at = float(time.time() if now is None else now)
    # All durable learning facts are owned by canonical_v2. The event streams
    # are observed through the canonical reader; the sample row is read from
    # canonical_v2.training_sample_row.
    specs: list[tuple[str, tuple[str, ...], str | None]] = [
        ("canonical_v2.training_sample_row", ("updated_at", "event_ts", "created_at"), None),
        ("canonical_v2.event", ("observed_at",), "risk_decision"),
    ]
    if include_trade_reviews:
        specs.append(("canonical_v2.event", ("observed_at",), "trade_review"))

    conn = None
    try:
        conn = _connect_state_source(db_path)
        record_count = 0
        latest = 0.0
        missing_tables: list[str] = []
        for table, timestamp_columns, canonical_event_type in specs:
            if canonical_event_type is not None:
                # Decision/review counts and freshness come from the canonical
                # reader; this module holds no private fact SQL.
                observation = canonical_fact_observation(
                    conn,
                    {
                        "risk_decision": "decision",
                        "trade_review": "review",
                    }.get(canonical_event_type, ""),
                    timestamp_columns=timestamp_columns,
                )
                if observation is not None:
                    record_count += int(observation["count"] or 0)
                    latest = max(latest, observed_epoch(observation.get("latest")))
                    continue
                missing_tables.append(table)
                continue
            if not core_db.state_table_exists(conn, table):
                if table == "canonical_v2.training_sample_row":
                    from backend.services.canonical_v2_reader import iter_training_sample_rows
                    sample_rows = iter_training_sample_rows(conn)
                    if sample_rows:
                        record_count += len(sample_rows)
                        for col in timestamp_columns:
                            latest = max(
                                latest,
                                max((float(r.get(col) or 0.0) for r in sample_rows), default=0.0),
                            )
                        continue
                missing_tables.append(table)
                continue
            projections = ["COUNT(*) AS record_count"]
            projections.extend(f"MAX({column}) AS max_{column}" for column in timestamp_columns)
            row = conn.execute(
                _sql(conn, f"SELECT {', '.join(projections)} FROM {table}")  # noqa: S608 - fixed internal identifiers
            ).fetchone()
            record_count += int(_row_value(row, "record_count", 0) or 0)
            for index, column in enumerate(timestamp_columns, start=1):
                latest = max(latest, observed_epoch(_row_value(row, f"max_{column}", index)))

        if missing_tables:
            source_tables = tuple(dict.fromkeys(table for table, _cols, _event_type in specs))
            return DurableSourceObservation(
                observed_at=latest or None,
                authoritative_empty=False,
                record_count=record_count,
                tables=source_tables,
                error="missing_source_tables:" + ",".join(dict.fromkeys(missing_tables)),
            )
        source_tables = tuple(dict.fromkeys(table for table, _cols, _event_type in specs))
        return DurableSourceObservation(
            observed_at=queryed_at if record_count == 0 else (latest or None),
            authoritative_empty=record_count == 0,
            record_count=record_count,
            tables=source_tables,
            error=None if record_count == 0 or latest > 0 else "persisted_timestamp_missing",
        )
    except Exception as exc:
        source_tables = tuple(dict.fromkeys(table for table, _cols, _event_type in specs))
        return DurableSourceObservation(
            observed_at=None,
            authoritative_empty=False,
            record_count=0,
            tables=source_tables,
            error=f"source_observation_failed:{type(exc).__name__}",
        )
    finally:
        if conn is not None:
            conn.close()


def dataset_readiness_fact_payload(
    payload: Mapping[str, Any],
    *,
    observation: DurableSourceObservation,
    now: float | None = None,
) -> dict[str, Any]:
    return _dataset_fact_payload(
        payload,
        contract="learning.dataset-readiness.v2",
        observation=observation,
        now=now,
    )


def dataset_quality_health_fact_payload(
    payload: Mapping[str, Any],
    *,
    observation: DurableSourceObservation,
    now: float | None = None,
) -> dict[str, Any]:
    return _dataset_fact_payload(
        payload,
        contract="learning.dataset-quality-health.v2",
        observation=observation,
        now=now,
    )


def _dataset_fact_payload(
    payload: Mapping[str, Any],
    *,
    contract: str,
    observation: DurableSourceObservation,
    now: float | None,
) -> dict[str, Any]:
    generated_at = float(time.time() if now is None else now)
    result = _copy(payload)
    return dict(
        attach_fact(
            result,
            contract=contract,
            source=CANONICAL_SOURCE,
            observed_at=observation.observed_at,
            stale_after_sec=LEARNING_STALE_AFTER_SEC,
            error=observation.error or _payload_error(payload),
            reason_code=observation.error or _payload_error(payload),
            components={
                "record_count": observation.record_count,
                "authoritative_empty": observation.authoritative_empty,
                "tables": list(observation.tables),
            },
            now=generated_at,
        )
    )
