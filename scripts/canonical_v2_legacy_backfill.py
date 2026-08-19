#!/usr/bin/env python3
"""Bounded, read-only legacy backfill planner for canonical_v2.

The command only plans the first vertical domain.  It walks stable text
primary keys in bounded batches and emits mapping/quarantine counts.  It does
not read large JSON payloads into Python, insert ``legacy_mapping`` rows, or
touch any source/canonical table.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402
from backend.services.state_payloads import stable_json  # noqa: E402


REPORT_SCHEMA = "canonical_v2_legacy_backfill_dry_run.v1"

SOURCE_SPECS = (
    ("decision_ledger", "decision_id", "source_event"),
    ("order_lifecycle_event", "event_id", "source_event"),
    ("position_lifecycle_event", "event_id", "source_event"),
    ("trade_outcome_review", "review_id", "source_event"),
)

KNOWN_PROJECTION_SOURCES = frozenset(
    {
        "decision_ledger",
        "trade_outcome_review",
        "position_supervisor_trace",
        "supervisor_counterfactual_review",
    }
)


def _digest_update(digest: hashlib._Hash, *, table: str, key: str, classification: str, confidence: str, reason: str = "") -> None:
    digest.update(
        stable_json(
            {
                "legacy_table": table,
                "legacy_primary_key": key,
                "classification": classification,
                "mapping_confidence": confidence,
                "unresolved_reason": reason,
            }
        ).encode("utf-8")
    )
    digest.update(b"\n")


def _classify_source_key(*, table: str, key: Any) -> tuple[str, str, str]:
    normalized = str(key or "")
    if not normalized:
        return "quarantine", "unresolved", "empty_legacy_primary_key"
    return "source_event", "exact", ""


def _classify_sample_source(
    *,
    source_table: Any,
    source_id: Any,
    source_exists: bool | None = None,
) -> tuple[str, str, str]:
    table = str(source_table or "")
    key = str(source_id or "")
    if not table or not key:
        return "quarantine", "unresolved", "missing_projection_source_reference"
    if table not in KNOWN_PROJECTION_SOURCES:
        return "quarantine", "unresolved", "unsupported_projection_source_table"
    if source_exists is False:
        return "quarantine", "unresolved", "missing_projection_source_row"
    return "projection_reference", "strong", ""


def _row_value(row: Mapping[str, Any] | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    value = row.get(key, default)
    return default if value is None else value


def _scan_source_table(conn: Any, *, table: str, primary_key: str, classification: str, batch_size: int, max_rows: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    last_key = ""
    scanned = 0
    mapped = 0
    quarantined = 0
    reasons: Counter[str] = Counter()
    quarantine_preview: list[str] = []
    query = (
        f"SELECT {primary_key} AS legacy_primary_key "
        f"FROM {table} WHERE {primary_key} > %s "
        f"ORDER BY {primary_key} ASC LIMIT %s"
    )
    while scanned < max_rows:
        page_limit = min(int(batch_size), int(max_rows) - scanned)
        rows = conn.execute(query, (last_key, page_limit)).fetchmany(page_limit)
        if not rows:
            break
        for row in rows:
            key = str(_row_value(row, "legacy_primary_key", "") or "")
            item_classification, confidence, reason = _classify_source_key(table=table, key=key)
            _digest_update(
                digest,
                table=table,
                key=key,
                classification=item_classification,
                confidence=confidence,
                reason=reason,
            )
            scanned += 1
            last_key = key
            if confidence == "unresolved":
                quarantined += 1
                reasons[reason] += 1
                if len(quarantine_preview) < 20:
                    quarantine_preview.append(key)
            else:
                mapped += 1
        if len(rows) < page_limit:
            break
    return {
        "table": table,
        "primary_key": primary_key,
        "classification": classification,
        "scanned_rows": scanned,
        "mapped_rows": mapped,
        "quarantine_rows": quarantined,
        "quarantine_reasons": dict(sorted(reasons.items())),
        "quarantine_preview": quarantine_preview,
        "last_primary_key": last_key,
        "mapping_digest": digest.hexdigest(),
        "complete_scan": scanned < max_rows,
    }


def _scan_samples(conn: Any, *, batch_size: int, max_rows: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    last_key = ""
    scanned = 0
    mapped = 0
    quarantined = 0
    reasons: Counter[str] = Counter()
    quarantine_preview: list[str] = []
    query = """
    SELECT sample_id, sample_type, source_table, source_id,
           content_fingerprint,
           CASE
             WHEN source_table='decision_ledger' THEN EXISTS (
                 SELECT 1 FROM decision_ledger d WHERE d.decision_id=autonomous_learning_sample.source_id
             )
             WHEN source_table='trade_outcome_review' THEN EXISTS (
                 SELECT 1 FROM trade_outcome_review r WHERE r.review_id=autonomous_learning_sample.source_id
             )
             WHEN source_table='position_supervisor_trace' THEN EXISTS (
                 SELECT 1 FROM position_supervisor_trace t
                 WHERE t.trace_id=autonomous_learning_sample.source_id
                    OR t.position_id=autonomous_learning_sample.source_id
             )
             WHEN source_table='supervisor_counterfactual_review' THEN EXISTS (
                 SELECT 1 FROM supervisor_counterfactual_review c
                 WHERE c.counterfactual_id=autonomous_learning_sample.source_id
             )
             ELSE FALSE
           END AS source_reference_exists,
           COALESCE(OCTET_LENGTH(features_json), 0)
             + COALESCE(OCTET_LENGTH(verdict_json), 0)
             + COALESCE(OCTET_LENGTH(label_json), 0)
             + COALESCE(OCTET_LENGTH(trace_json), 0)
             + COALESCE(OCTET_LENGTH(evidence_contract_json), 0) AS payload_bytes
    FROM autonomous_learning_sample
    WHERE sample_id > %s
    ORDER BY sample_id ASC
    LIMIT %s
    """
    payload_bytes = 0
    sample_types: Counter[str] = Counter()
    while scanned < max_rows:
        page_limit = min(int(batch_size), int(max_rows) - scanned)
        rows = conn.execute(query, (last_key, page_limit)).fetchmany(page_limit)
        if not rows:
            break
        for row in rows:
            sample_id = str(_row_value(row, "sample_id", "") or "")
            source_table = _row_value(row, "source_table", "")
            source_id = _row_value(row, "source_id", "")
            item_classification, confidence, reason = _classify_sample_source(
                source_table=source_table,
                source_id=source_id,
                source_exists=bool(_row_value(row, "source_reference_exists", False)),
            )
            _digest_update(
                digest,
                table="autonomous_learning_sample",
                key=sample_id,
                classification=item_classification,
                confidence=confidence,
                reason=reason,
            )
            scanned += 1
            last_key = sample_id
            payload_bytes += int(_row_value(row, "payload_bytes", 0) or 0)
            sample_types[str(_row_value(row, "sample_type", "") or "")] += 1
            if confidence == "unresolved":
                quarantined += 1
                reasons[reason] += 1
                if len(quarantine_preview) < 20:
                    quarantine_preview.append(sample_id)
            else:
                mapped += 1
        if len(rows) < page_limit:
            break
    return {
        "table": "autonomous_learning_sample",
        "primary_key": "sample_id",
        "classification": "projection_reference",
        "scanned_rows": scanned,
        "mapped_rows": mapped,
        "quarantine_rows": quarantined,
        "quarantine_reasons": dict(sorted(reasons.items())),
        "quarantine_preview": quarantine_preview,
        "last_primary_key": last_key,
        "mapping_digest": digest.hexdigest(),
        "payload_bytes_observed_without_fetching_json": payload_bytes,
        "source_reference_check": "verified_by_exists_subquery",
        "sample_type_counts": dict(sorted(sample_types.items())),
        "complete_scan": scanned < max_rows,
    }


def plan_backfill(*, batch_size: int = 500, max_rows_per_table: int = 100_000) -> dict[str, Any]:
    if int(batch_size) <= 0 or int(max_rows_per_table) <= 0:
        raise ValueError("batch_size and max_rows_per_table must be positive")
    conn = get_state_pg_conn(read_only=True)
    try:
        table_plans = [
            _scan_source_table(
                conn,
                table=table,
                primary_key=primary_key,
                classification=classification,
                batch_size=int(batch_size),
                max_rows=int(max_rows_per_table),
            )
            for table, primary_key, classification in SOURCE_SPECS
        ]
        sample_plan = _scan_samples(
            conn,
            batch_size=int(batch_size),
            max_rows=int(max_rows_per_table),
        )
        source_reuse_rows = conn.execute(
            """
            SELECT source_table, source_id, COUNT(*) AS sample_count,
                   COUNT(DISTINCT sample_type) AS sample_type_count
            FROM autonomous_learning_sample
            WHERE COALESCE(source_table, '')<>'' AND COALESCE(source_id, '')<>''
            GROUP BY source_table, source_id
            HAVING COUNT(*) > 1
            ORDER BY COUNT(*) DESC, source_table ASC, source_id ASC
            LIMIT 20
            """
        ).fetchmany(20)
        source_reuse_summary = conn.execute(
            """
            SELECT COUNT(*) AS reused_source_count,
                   COALESCE(SUM(sample_count - 1), 0) AS excess_sample_rows,
                   COALESCE(MAX(sample_count), 0) AS max_samples_per_source
            FROM (
                SELECT source_table, source_id, COUNT(*) AS sample_count
                FROM autonomous_learning_sample
                WHERE COALESCE(source_table, '')<>'' AND COALESCE(source_id, '')<>''
                GROUP BY source_table, source_id
                HAVING COUNT(*) > 1
            ) reused
            """
        ).fetchone()
        source_reuse_by_table = conn.execute(
            """
            SELECT source_table,
                   COUNT(*) AS reused_source_count,
                   COALESCE(SUM(sample_count - 1), 0) AS excess_sample_rows
            FROM (
                SELECT source_table, source_id, COUNT(*) AS sample_count
                FROM autonomous_learning_sample
                WHERE COALESCE(source_table, '')<>'' AND COALESCE(source_id, '')<>''
                GROUP BY source_table, source_id
                HAVING COUNT(*) > 1
            ) reused
            GROUP BY source_table
            ORDER BY excess_sample_rows DESC, source_table ASC
            """
        ).fetchmany(20)
        chain_coverage = conn.execute(
            """
            SELECT
                COUNT(*) AS review_count,
                COUNT(*) FILTER (WHERE COALESCE(entry_decision_id, '')<>'') AS with_entry_decision,
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM order_lifecycle_event o
                    WHERE (NULLIF(trade_outcome_review.entry_decision_id, '') IS NOT NULL
                           AND o.decision_id=trade_outcome_review.entry_decision_id)
                       OR (NULLIF(trade_outcome_review.trade_id, '') IS NOT NULL
                           AND o.trade_id=trade_outcome_review.trade_id)
                )) AS with_order_lifecycle,
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM position_lifecycle_event p
                    WHERE (NULLIF(trade_outcome_review.position_id, '') IS NOT NULL
                           AND p.position_id=trade_outcome_review.position_id)
                       OR (NULLIF(trade_outcome_review.trade_id, '') IS NOT NULL
                           AND p.trade_id=trade_outcome_review.trade_id)
                )) AS with_position_lifecycle,
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM autonomous_learning_sample s
                    WHERE s.source_table='trade_outcome_review'
                      AND s.source_id=trade_outcome_review.review_id
                )) AS with_learning_projection
            FROM trade_outcome_review
            """
        ).fetchone()
        watermark = conn.execute(
            """
            SELECT GREATEST(
                COALESCE((SELECT MAX(decision_ts) FROM decision_ledger), 0),
                COALESCE((SELECT MAX(event_ts) FROM order_lifecycle_event), 0),
                COALESCE((SELECT MAX(event_ts) FROM position_lifecycle_event), 0),
                COALESCE((SELECT MAX(created_at) FROM trade_outcome_review), 0),
                COALESCE((SELECT MAX(updated_at) FROM autonomous_learning_sample), 0)
            ) AS source_watermark
            """
        ).fetchone()
        mapping_digest = hashlib.sha256(
            stable_json({
                "tables": table_plans,
                "samples": sample_plan,
            }).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": REPORT_SCHEMA,
            "dry_run": True,
            "read_only": True,
            "writes_performed": False,
            "scope": "vertical_decision_execution_position_review_sample",
            "batch_size": int(batch_size),
            "max_rows_per_table": int(max_rows_per_table),
            "source_watermark": _row_value(watermark, "source_watermark", 0.0),
            "mapping_digest": mapping_digest,
            "tables": table_plans + [sample_plan],
            "sample_source_reuse_preview": [dict(row) for row in source_reuse_rows],
            "sample_source_reuse_summary": dict(source_reuse_summary or {}),
            "sample_source_reuse_by_table": [dict(row) for row in source_reuse_by_table],
            "vertical_chain_coverage": dict(chain_coverage or {}),
            "next_action": "review_manifest_before_any_apply",
            "excluded_from_scope": [
                "governance_mutation_intent",
                "evolution_events",
                "brain_state_snapshot",
                "brain_memory",
                "factor_catalog_snapshot",
                "state_payload_archive",
            ],
        }
    finally:
        conn.rollback()
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only canonical_v2 legacy backfill planner")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-rows-per-table", type=int, default=100_000)
    args = parser.parse_args()
    report = plan_backfill(
        batch_size=args.batch_size,
        max_rows_per_table=args.max_rows_per_table,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
