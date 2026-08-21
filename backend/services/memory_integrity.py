"""Read-only integrity contract for the three-layer learning-memory path.

The raw review tables remain the authority.  ``experience_memory`` is the
canonical lesson projection and ``brain_memory`` is a bounded, rebuildable
retrieval index.  This module only compares those existing facts; it never
creates labels, rebuilds rows, or changes trading/governance authority.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, state_table_exists
from backend.services._brain_helpers import connect, execute, safe_float
from backend.services.canonical_v2_reader import canonical_ready, iter_review_rows
from backend.services.review_contract import review_has_system_contamination


CANONICAL_APPEND_SOURCE = "trade_lesson_memory.v1"
CANONICAL_SOURCE_TABLE = "canonical_v2.trade_review"
REPORT_VERSION = "memory_integrity_report.v1"
_SAMPLE_LIMIT = 10


def _sample(values: list[str]) -> list[str]:
    return sorted({str(value) for value in values if str(value)})[:_SAMPLE_LIMIT]


def _row_dict(row: Any) -> dict[str, Any]:
    try:
        return dict(row)
    except Exception:
        return {}


class MemoryIntegrityReportService:
    """Build the sole read-only health report for learning-memory consumers."""

    def __init__(
        self,
        db_path: str | Path = STATE_DB,
        *,
        connection_factory: Callable[..., Any] | None = None,
    ):
        self.db_path = Path(db_path)
        self._connection_factory = connection_factory

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "memory_integrity_boundary.v1",
            "read_only": True,
            "affects_trading": False,
            "does_not_authorize_actions": True,
            "source_facts_remain_authoritative": True,
            "experience_memory_is_rebuildable_projection": True,
            "brain_memory_is_bounded_rebuildable_index": True,
        }

    def build(self) -> dict[str, Any]:
        observed_at = time.time()
        try:
            conn = (
                self._connection_factory(read_only=True)
                if self._connection_factory is not None
                else connect(self.db_path, read_only=True)
            )
        except Exception as exc:
            return self._unavailable(observed_at, f"state_unavailable:{type(exc).__name__}: {exc}")
        try:
            required_tables = ("experience_memory", "brain_memory")
            missing_tables = [table for table in required_tables if not state_table_exists(conn, table)]
            if missing_tables or not canonical_ready(conn):
                return self._unavailable(
                    observed_at,
                    "required_facts_missing:canonical_v2.trade_review"
                    if not canonical_ready(conn)
                    else "required_tables_missing:" + ",".join(sorted(missing_tables)),
                )

            review_rows = [_row_dict(row) for row in iter_review_rows(conn, limit=0)]
            projection_rows = [
                _row_dict(row)
                for row in execute(
                    conn,
                    """
                    SELECT experience_id, source_table, source_id, append_source, created_at
                    FROM experience_memory
                    ORDER BY created_at DESC
                    """,
                ).fetchall()
            ]
            index_rows = [
                _row_dict(row)
                for row in execute(
                    conn,
                    """
                    SELECT memory_id, source_table, source_id, created_at, last_used_at
                    FROM brain_memory
                    ORDER BY last_used_at DESC, created_at DESC
                    """,
                ).fetchall()
            ]
            contaminated_review_ids = [
                str(row.get("review_id") or "")
                for row in review_rows
                if str(row.get("review_id") or "")
                and review_has_system_contamination(
                    row.get("review_json")
                    if isinstance(row.get("review_json"), dict)
                    else {}
                )
            ]
        except Exception as exc:
            return self._unavailable(observed_at, f"memory_integrity_query_failed:{type(exc).__name__}: {exc}")
        finally:
            conn.close()

        reviews = {
            str(row.get("review_id") or ""): row
            for row in review_rows
            if str(row.get("review_id") or "")
        }
        projections_by_source: dict[str, list[dict[str, Any]]] = {}
        noncanonical_projection_ids: list[str] = []
        for row in projection_rows:
            source_table = str(row.get("source_table") or "")
            source_id = str(row.get("source_id") or "")
            append_source = str(row.get("append_source") or "")
            if (
                append_source != CANONICAL_APPEND_SOURCE
                or source_table != CANONICAL_SOURCE_TABLE
                or not source_id
            ):
                noncanonical_projection_ids.append(str(row.get("experience_id") or ""))
                continue
            projections_by_source.setdefault(source_id, []).append(row)

        missing_projection_ids = [review_id for review_id in reviews if review_id not in projections_by_source]
        orphan_projection_ids = [
            str(row.get("experience_id") or "")
            for source_id, rows in projections_by_source.items()
            if source_id not in reviews
            for row in rows
        ]
        duplicate_projection_source_ids = [
            source_id for source_id, rows in projections_by_source.items() if len(rows) > 1
        ]
        timestamp_mismatch_ids = [
            source_id
            for source_id, rows in projections_by_source.items()
            if source_id in reviews
            and any(
                abs(safe_float(row.get("created_at")) - safe_float(reviews[source_id].get("created_at"))) > 0.000001
                for row in rows
            )
        ]
        projection_by_id = {
            str(row.get("experience_id") or ""): row
            for row in projection_rows
            if str(row.get("experience_id") or "")
        }
        index_orphan_ids: list[str] = []
        indexed_contaminated_ids: list[str] = []
        indexed_experience_count = 0
        indexed_review_count = 0
        for row in index_rows:
            source_table = str(row.get("source_table") or "")
            source_id = str(row.get("source_id") or "")
            memory_id = str(row.get("memory_id") or "")
            review_id = ""
            if source_table == "experience_memory":
                indexed_experience_count += 1
                projection = projection_by_id.get(source_id)
                if not projection:
                    index_orphan_ids.append(memory_id)
                    continue
                review_id = str(projection.get("source_id") or "")
                if str(projection.get("source_table") or "") != CANONICAL_SOURCE_TABLE or review_id not in reviews:
                    index_orphan_ids.append(memory_id)
                    continue
            elif source_table == CANONICAL_SOURCE_TABLE:
                indexed_review_count += 1
                review_id = source_id
                if review_id not in reviews:
                    index_orphan_ids.append(memory_id)
                    continue
            if review_id and review_id in contaminated_review_ids:
                indexed_contaminated_ids.append(memory_id)

        source_latest_at = max((safe_float(row.get("created_at")) for row in reviews.values()), default=0.0)
        projection_latest_at = max((safe_float(row.get("created_at")) for row in projection_rows), default=0.0)
        index_latest_at = max(
            (max(safe_float(row.get("last_used_at")), safe_float(row.get("created_at"))) for row in index_rows),
            default=0.0,
        )
        projection_lag = max(0.0, source_latest_at - projection_latest_at)
        projection_lead = max(0.0, projection_latest_at - source_latest_at)
        eligible_source_total = len(reviews) - len(contaminated_review_ids)
        index_window_available = not eligible_source_total or bool(index_rows)
        checks = {
            "source_projection_complete": not missing_projection_ids,
            "projection_sources_resolved": not orphan_projection_ids and not noncanonical_projection_ids,
            "one_projection_per_source": not duplicate_projection_source_ids,
            "projection_timestamps_aligned": not timestamp_mismatch_ids,
            "index_references_resolved": not index_orphan_ids,
            "contaminated_reviews_excluded_from_index": not indexed_contaminated_ids,
            "index_window_available": index_window_available,
        }
        status = "healthy" if all(checks.values()) else "degraded"
        return {
            "ok": status == "healthy",
            "schema_version": REPORT_VERSION,
            "status": status,
            "observed_at": observed_at,
            "raw_evidence": {
                "source_table": CANONICAL_SOURCE_TABLE,
                "total": len(reviews),
                "eligible_total": eligible_source_total,
                "contaminated_quarantined_count": len(contaminated_review_ids),
                "contaminated_quarantined_samples": _sample(contaminated_review_ids),
                "latest_created_at": source_latest_at,
            },
            "experience_projection": {
                "table": "experience_memory",
                "append_source": CANONICAL_APPEND_SOURCE,
                "total": len(projection_rows),
                "missing_source_count": len(missing_projection_ids),
                "missing_source_samples": _sample(missing_projection_ids),
                "orphan_projection_count": len(orphan_projection_ids),
                "orphan_projection_samples": _sample(orphan_projection_ids),
                "noncanonical_projection_count": len(noncanonical_projection_ids),
                "noncanonical_projection_samples": _sample(noncanonical_projection_ids),
                "duplicate_source_count": len(duplicate_projection_source_ids),
                "duplicate_source_samples": _sample(duplicate_projection_source_ids),
                "timestamp_mismatch_count": len(timestamp_mismatch_ids),
                "timestamp_mismatch_samples": _sample(timestamp_mismatch_ids),
                "latest_created_at": projection_latest_at,
                "source_lag_seconds": round(projection_lag, 6),
                "source_lead_seconds": round(projection_lead, 6),
            },
            "retrieval_index": {
                "table": "brain_memory",
                "role": "bounded_rebuildable_index_not_archive",
                "total": len(index_rows),
                "indexed_experience_count": indexed_experience_count,
                "indexed_review_count": indexed_review_count,
                "missing_source_reference_count": len(index_orphan_ids),
                "missing_source_reference_samples": _sample(index_orphan_ids),
                "contaminated_indexed_count": len(indexed_contaminated_ids),
                "contaminated_indexed_samples": _sample(indexed_contaminated_ids),
                "latest_indexed_at": index_latest_at,
                "window_available": index_window_available,
            },
            "checks": checks,
            "errors": [],
            "boundary": self.boundary(),
        }

    def _unavailable(self, observed_at: float, error: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": REPORT_VERSION,
            "status": "unavailable",
            "observed_at": observed_at,
            "raw_evidence": {},
            "experience_projection": {},
            "retrieval_index": {},
            "checks": {},
            "errors": [str(error)],
            "boundary": self.boundary(),
        }
