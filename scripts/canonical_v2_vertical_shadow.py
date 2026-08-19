#!/usr/bin/env python3
"""Read-only shadow check for the first canonical_v2 vertical slice.

This command reads the legacy PostgreSQL source tables only.  It does not
insert canonical rows, materialize samples, create a dataset, or invoke a
training/governance path.  The report deliberately distinguishes logical
lineage coverage from payload-deduplication candidates: equal normalized
content is not treated as equal business events.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402
from backend.services.canonical_v2 import canonical_payload  # noqa: E402
from backend.services.state_payload_archive import load_json_payload  # noqa: E402
from backend.services.state_payloads import stable_json  # noqa: E402


REPORT_SCHEMA = "canonical_v2_vertical_shadow.v1"
SHADOW_SCHEMA_VERSION = "shadow.v1"

VERTICAL_QUERY = """
SELECT
    r.review_id,
    r.trade_id,
    r.position_id,
    r.entry_decision_id,
    r.exit_decision_id,
    r.created_at AS review_created_at,
    r.entry_quality,
    r.hold_quality,
    r.exit_quality,
    r.regime_fit_score,
    r.execution_quality,
    r.pnl,
    r.mae,
    r.mfe,
    r.outcome_label,
    r.failure_tags_json,
    r.review_json,
    r.review_archive_hash,
    d.decision_id,
    d.event_type AS decision_event_type,
    d.symbol AS decision_symbol,
    d.timeframe AS decision_timeframe,
    d.decision_ts,
    d.regime_id,
    d.regime_confidence,
    d.action_score,
    d.action_json,
    d.risk_state_json,
    d.portfolio_state_json,
    oe.event_count AS order_event_count,
    oe.first_event_id AS first_order_event_id,
    oe.last_event_id AS last_order_event_id,
    pe.event_count AS position_event_count,
    pe.first_event_id AS first_position_event_id,
    pe.last_event_id AS last_position_event_id
FROM trade_outcome_review r
LEFT JOIN decision_ledger d
  ON NULLIF(r.entry_decision_id, '') IS NOT NULL
 AND d.decision_id=r.entry_decision_id
LEFT JOIN LATERAL (
    SELECT
        COUNT(*) AS event_count,
        (array_agg(o.event_id ORDER BY o.event_ts ASC, o.event_id ASC))[1] AS first_event_id,
        (array_agg(o.event_id ORDER BY o.event_ts DESC, o.event_id DESC))[1] AS last_event_id
    FROM order_lifecycle_event o
    WHERE (NULLIF(r.entry_decision_id, '') IS NOT NULL AND o.decision_id=r.entry_decision_id)
       OR (NULLIF(r.trade_id, '') IS NOT NULL AND o.trade_id=r.trade_id)
) oe ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COUNT(*) AS event_count,
        (array_agg(p.event_id ORDER BY p.event_ts ASC, p.event_id ASC))[1] AS first_event_id,
        (array_agg(p.event_id ORDER BY p.event_ts DESC, p.event_id DESC))[1] AS last_event_id
    FROM position_lifecycle_event p
    WHERE (NULLIF(r.position_id, '') IS NOT NULL AND p.position_id=r.position_id)
       OR (NULLIF(r.trade_id, '') IS NOT NULL AND p.trade_id=r.trade_id)
) pe ON TRUE
ORDER BY r.created_at DESC, r.review_id DESC
LIMIT %s
"""

SAMPLE_QUERY = """
SELECT sample_id, sample_type, source_id, content_fingerprint,
       features_json, verdict_json, label_json, trace_json,
       evidence_contract_json, label_status, config_version, config_hash,
       system_contaminated, governance_eligible
FROM autonomous_learning_sample
WHERE source_table='trade_outcome_review'
  AND source_id = ANY(%s)
ORDER BY source_id ASC, sample_type ASC, sample_id ASC
"""

COUNT_QUERY = "SELECT COUNT(*) AS review_count FROM trade_outcome_review"

_IDENTITY_KEYS = frozenset(
    {
        "event_id",
        "sample_id",
        "review_id",
        "trade_id",
        "position_id",
        "decision_id",
        "entry_decision_id",
        "exit_decision_id",
        "source_id",
        "counterfactual_id",
        "created_at",
        "updated_at",
        "event_ts",
        "timestamp",
        "close_ts",
    }
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_value(value: Any) -> Any:
    if value in (None, ""):
        return {}
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"_invalid_json": True}


def _shape(value: Any) -> Any:
    """Return a value-independent JSON schema shape for a small projection."""

    if isinstance(value, Mapping):
        return {
            "object": {str(key): _shape(value[key]) for key in sorted(value, key=str)}
        }
    if isinstance(value, list):
        return {"array": _shape(value[0]) if value else {"empty": True}}
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    return "string"


def _semantic_payload(value: Any) -> Any:
    """Remove occurrence/lineage identifiers before reporting reuse candidates."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value, key=str):
            name = str(key)
            lowered = name.lower()
            if lowered in _IDENTITY_KEYS or lowered.endswith("_id"):
                continue
            result[name] = _semantic_payload(value[key])
        return result
    if isinstance(value, list):
        return [_semantic_payload(item) for item in value]
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _payload_shadow_ref(value: Any, *, payload_kind: str) -> dict[str, Any]:
    ref, _compressed = canonical_payload(
        value,
        payload_kind=payload_kind,
        schema_version=SHADOW_SCHEMA_VERSION,
    )
    return {
        "payload_hash": ref.payload_hash,
        "raw_sha256": ref.raw_sha256,
        "raw_bytes": ref.raw_bytes,
        "compressed_bytes": ref.compressed_bytes,
    }


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = row.get(key, default)
    return default if value is None else value


def _text(row: Mapping[str, Any], key: str) -> str:
    return str(_row_value(row, key, "") or "")


def _chain_reasons(row: Mapping[str, Any], sample_rows: Iterable[Mapping[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if not _text(row, "decision_id"):
        reasons.append("missing_entry_decision")
    if int(_row_value(row, "order_event_count", 0) or 0) <= 0:
        reasons.append("missing_order_lifecycle")
    if int(_row_value(row, "position_event_count", 0) or 0) <= 0:
        reasons.append("missing_position_lifecycle")
    if not list(sample_rows):
        reasons.append("missing_learning_projection")
    return reasons


def _sample_contract(row: Mapping[str, Any]) -> dict[str, Any]:
    features = _json_value(_row_value(row, "features_json", "{}"))
    label = _json_value(_row_value(row, "label_json", "{}"))
    trace = _json_value(_row_value(row, "trace_json", "{}"))
    verdict = _json_value(_row_value(row, "verdict_json", "{}"))
    evidence = _json_value(_row_value(row, "evidence_contract_json", "{}"))
    semantic = _semantic_payload(
        {
            "features": features,
            "label": label,
            "trace": trace,
            "verdict": verdict,
            "evidence_contract": evidence,
        }
    )
    target_source = (
        label.get("target_source")
        or label.get("label_source")
        or verdict.get("target_source")
        or verdict.get("label_source")
        or evidence.get("target_source")
        or trace.get("target_source")
        or "legacy_unset"
    ) if all(
        isinstance(value, Mapping)
        for value in (label, verdict, evidence, trace)
    ) else "legacy_unset"
    return {
        "sample_id": _text(row, "sample_id"),
        "sample_type": _text(row, "sample_type"),
        "source_id": _text(row, "source_id"),
        "content_fingerprint": _text(row, "content_fingerprint"),
        "label_status": _text(row, "label_status"),
        "config_version": int(_row_value(row, "config_version", 0) or 0),
        "config_hash": _text(row, "config_hash"),
        "system_contaminated": bool(_row_value(row, "system_contaminated", 0)),
        "governance_eligible": bool(_row_value(row, "governance_eligible", 0)),
        "feature_schema_hash": _digest(_shape(features)),
        "label_schema_hash": _digest(_shape(label)),
        "target_source": str(target_source or "legacy_unset"),
        "semantic_payload_hash": _digest(semantic),
        "payload_ref": _payload_shadow_ref(semantic, payload_kind="learning_sample"),
    }


def _fetch_named(cursor: Any, *, batch_size: int = 1000) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        rows.extend(dict(row) for row in batch)
    return rows


def inspect_vertical(*, limit: int = 50) -> dict[str, Any]:
    if int(limit) <= 0:
        raise ValueError("limit must be positive")

    conn = get_state_pg_conn(read_only=True)
    try:
        total_row = conn.execute(COUNT_QUERY).fetchone()
        total_reviews = int(_row_value(total_row, "review_count", 0) or 0)

        review_cursor = conn.cursor(name="canonical_v2_vertical_reviews")
        try:
            review_cursor.execute(VERTICAL_QUERY, (int(limit),))
            review_rows = _fetch_named(review_cursor, batch_size=min(100, int(limit)))
        finally:
            review_cursor.close()
        for row in review_rows:
            row["review_json"] = load_json_payload(
                conn,
                source_table="trade_outcome_review",
                source_id=str(row.get("review_id") or ""),
                inline_json=row.get("review_json", "{}"),
                archive_hash=row.get("review_archive_hash", ""),
                default={},
            )

        review_ids = [_text(row, "review_id") for row in review_rows if _text(row, "review_id")]
        samples_by_review: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if review_ids:
            sample_cursor = conn.cursor(name="canonical_v2_vertical_samples")
            try:
                sample_cursor.execute(SAMPLE_QUERY, (review_ids,))
                for row in _fetch_named(sample_cursor, batch_size=1000):
                    samples_by_review[_text(row, "source_id")].append(_sample_contract(row))
            finally:
                sample_cursor.close()

        chain_rows: list[dict[str, Any]] = []
        sample_contracts: list[dict[str, Any]] = []
        missing_reason_counts: Counter[str] = Counter()
        review_payload_hashes: list[str] = []
        decision_payload_hashes: list[str] = []

        for row in review_rows:
            review_id = _text(row, "review_id")
            sample_contracts_for_review = samples_by_review.get(review_id, [])
            reasons = _chain_reasons(row, sample_contracts_for_review)
            missing_reason_counts.update(reasons)
            sample_contracts.extend(sample_contracts_for_review)

            review_payload = {
                "review": _json_value(_row_value(row, "review_json", "{}")),
                "outcome_label": _text(row, "outcome_label"),
                "pnl": _row_value(row, "pnl", 0.0),
                "mae": _row_value(row, "mae", 0.0),
                "mfe": _row_value(row, "mfe", 0.0),
                "failure_tags": _json_value(_row_value(row, "failure_tags_json", "[]")),
                "entry_quality": _row_value(row, "entry_quality", 0.0),
                "hold_quality": _row_value(row, "hold_quality", 0.0),
                "exit_quality": _row_value(row, "exit_quality", 0.0),
                "regime_fit_score": _row_value(row, "regime_fit_score", 0.0),
                "execution_quality": _row_value(row, "execution_quality", 0.0),
            }
            review_payload_ref = _payload_shadow_ref(
                review_payload,
                payload_kind="legacy.trade_outcome_review",
            )
            review_payload_hashes.append(review_payload_ref["payload_hash"])

            decision_id = _text(row, "decision_id")
            decision_payload_ref: dict[str, Any] | None = None
            if decision_id:
                decision_payload_ref = _payload_shadow_ref(
                    {
                        "event_type": _text(row, "decision_event_type"),
                        "symbol": _text(row, "decision_symbol"),
                        "timeframe": _text(row, "decision_timeframe"),
                        "regime_id": _text(row, "regime_id"),
                        "regime_confidence": _row_value(row, "regime_confidence", 0.0),
                        "action_score": _row_value(row, "action_score", 0.0),
                        "action": _json_value(_row_value(row, "action_json", "{}")),
                        "risk_state": _json_value(_row_value(row, "risk_state_json", "{}")),
                        "portfolio_state": _json_value(_row_value(row, "portfolio_state_json", "{}")),
                    },
                    payload_kind="legacy.decision_ledger",
                )
                decision_payload_hashes.append(decision_payload_ref["payload_hash"])

            event_refs = []
            if decision_id:
                event_refs.append({"legacy_table": "decision_ledger", "legacy_id": decision_id})
            for key in ("first_order_event_id", "last_order_event_id"):
                event_id = _text(row, key)
                if event_id:
                    event_refs.append({"legacy_table": "order_lifecycle_event", "legacy_id": event_id})
            for key in ("first_position_event_id", "last_position_event_id"):
                event_id = _text(row, key)
                if event_id:
                    event_refs.append({"legacy_table": "position_lifecycle_event", "legacy_id": event_id})
            event_refs.append({"legacy_table": "trade_outcome_review", "legacy_id": review_id})

            chain_rows.append(
                {
                    "review_id": review_id,
                    "trade_id": _text(row, "trade_id"),
                    "position_id": _text(row, "position_id"),
                    "entry_decision_id": decision_id,
                    "exit_decision_id": _text(row, "exit_decision_id"),
                    "order_event_count": int(_row_value(row, "order_event_count", 0) or 0),
                    "position_event_count": int(_row_value(row, "position_event_count", 0) or 0),
                    "sample_ids": [item["sample_id"] for item in sample_contracts_for_review],
                    "sample_types": sorted({item["sample_type"] for item in sample_contracts_for_review}),
                    "event_refs": event_refs,
                    "payload_refs": {
                        "review": review_payload_ref,
                        "decision": decision_payload_ref,
                    },
                    "status": "complete" if not reasons else "incomplete",
                    "exclusion_reasons": reasons,
                }
            )

        duplicate_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sample in sample_contracts:
            duplicate_groups[sample["semantic_payload_hash"]].append(
                {
                    "sample_id": sample["sample_id"],
                    "sample_type": sample["sample_type"],
                    "source_id": sample["source_id"],
                }
            )
        duplicate_candidates = [
            {"semantic_payload_hash": digest, "samples": items}
            for digest, items in sorted(duplicate_groups.items())
            if len(items) > 1
        ]
        duplicate_candidates.sort(key=lambda item: len(item["samples"]), reverse=True)

        review_payload_reuse = {
            digest: count
            for digest, count in Counter(review_payload_hashes).items()
            if count > 1
        }
        decision_payload_reuse = {
            digest: count
            for digest, count in Counter(decision_payload_hashes).items()
            if count > 1
        }

        sample_digest_items = [
            {
                "sample_id": item["sample_id"],
                "sample_type": item["sample_type"],
                "content_fingerprint": item["content_fingerprint"],
                "config_hash": item["config_hash"],
                "label_status": item["label_status"],
            }
            for item in sorted(sample_contracts, key=lambda value: (value["sample_id"], value["sample_type"]))
        ]
        sample_id_digest = _digest([item["sample_id"] for item in sample_digest_items])
        sample_digest = _digest(sample_digest_items)
        feature_schema_hashes = {
            sample_type: _digest(sorted({item["feature_schema_hash"] for item in sample_contracts if item["sample_type"] == sample_type}))
            for sample_type in sorted({item["sample_type"] for item in sample_contracts})
        }
        label_schema_hashes = {
            sample_type: _digest(sorted({item["label_schema_hash"] for item in sample_contracts if item["sample_type"] == sample_type}))
            for sample_type in sorted({item["sample_type"] for item in sample_contracts})
        }
        query_contract_hash = _digest(
            {"count_query": COUNT_QUERY, "vertical_query": VERTICAL_QUERY, "sample_query": SAMPLE_QUERY}
        )
        dataset_manifest_shadow = {
            "dataset_id": "shadow:" + _digest(sample_digest_items)[:24],
            "purpose": "canonical_v2_vertical_shadow",
            "training_window": "selected_latest_reviews",
            "horizon_minutes": None,
            "query_contract_hash": query_contract_hash,
            "sample_digest": sample_digest,
            "feature_schema_hash": _digest(feature_schema_hashes),
            "label_contract_hash": _digest(label_schema_hashes),
            "target_source": (
                next(iter({item["target_source"] for item in sample_contracts}))
                if len({item["target_source"] for item in sample_contracts}) == 1
                else "mixed"
            ),
            "config_hash": (
                next(iter({item["config_hash"] for item in sample_contracts}))
                if len({item["config_hash"] for item in sample_contracts}) == 1
                else "mixed"
            ),
            "source_watermark": max(
                (_row_value(row, "review_created_at", 0.0) or 0.0 for row in review_rows),
                default=0.0,
            ),
            "artifact_hash": "",
            "status": "shadow_only",
        }

        return {
            "schema_version": REPORT_SCHEMA,
            "read_only": True,
            "writes_performed": False,
            "total_review_rows": total_reviews,
            "selected_review_rows": len(review_rows),
            "selected_sample_rows": len(sample_contracts),
            "limit": int(limit),
            "query_contract_hash": query_contract_hash,
            "chain": {
                "complete_count": sum(1 for item in chain_rows if item["status"] == "complete"),
                "incomplete_count": sum(1 for item in chain_rows if item["status"] != "complete"),
                "missing_reason_counts": dict(sorted(missing_reason_counts.items())),
            },
            "sample_contract": {
                "sample_id_digest": sample_id_digest,
                "sample_digest": sample_digest,
                "feature_schema_hashes": feature_schema_hashes,
                "label_schema_hashes": label_schema_hashes,
                "target_sources": dict(
                    sorted(Counter(item["target_source"] for item in sample_contracts).items())
                ),
                "config_hashes": dict(
                    sorted(Counter(item["config_hash"] or "legacy_unset" for item in sample_contracts).items())
                ),
                "label_statuses": dict(sorted(Counter(item["label_status"] for item in sample_contracts).items())),
                "contaminated_count": sum(1 for item in sample_contracts if item["system_contaminated"]),
                "governance_eligible_count": sum(1 for item in sample_contracts if item["governance_eligible"]),
            },
            "payload_shadow": {
                "review_payload_count": len(review_payload_hashes),
                "review_payload_digest": _digest(sorted(review_payload_hashes)),
                "decision_payload_count": len(decision_payload_hashes),
                "decision_payload_digest": _digest(sorted(decision_payload_hashes)),
                "review_payload_reuse": review_payload_reuse,
                "decision_payload_reuse": decision_payload_reuse,
                "semantic_duplicate_candidate_count": len(duplicate_candidates),
                "semantic_duplicate_candidates": duplicate_candidates[:20],
            },
            "dataset_manifest_shadow": dataset_manifest_shadow,
            "chains": chain_rows,
        }
    finally:
        conn.rollback()
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only canonical_v2 vertical shadow verifier")
    parser.add_argument("--limit", type=int, default=50, help="bounded number of latest trade reviews")
    args = parser.parse_args()
    report = inspect_vertical(limit=args.limit)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
