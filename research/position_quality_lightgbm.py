from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

from backend.core.db import (
    DATA_DIR,
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
)
from backend.core.state_store import (
    is_state_schema_write_sql,
    validate_runtime_state_schema,
)
from backend.services.agent_authority import AgentAuthorityRegistryService
from backend.services.model_permissions import validate_model_artifact
from backend.services.review_contract import (
    SYSTEM_CONTAMINATION_LABELS,
    review_execution_evidence_is_trainable,
)
from backend.services.canonical_v2_reader import (
    canonical_ready,
    iter_review_rows_desc,
    iter_supervisor_trace_rows,
)


def _review_text_contaminated(review_json: str) -> bool:
    """Fast contamination check on the serialized review text.

    New-format reviews carry system_issue_context with an explicit
    contaminates_learning flag; a single regex scan beats a full JSON
    parse of the (large) review payload. Reviews without that context
    use a quoted-label substring check for the
    failure-tag path (responsibility_labels/failure_tags arrays are
    serialized with quoted members), never a full parse.
    """
    if not review_json:
        return False
    match = re.search(r'"contaminates_learning"\s*:\s*(true|false)', review_json)
    if match is not None:
        return match.group(1) == "true"
    return any(f'"{label}"' in review_json for label in SYSTEM_CONTAMINATION_LABELS)


MODEL_TYPE = "position_quality_lightgbm"
MODEL_VERSION = "3.0"
FEATURE_SCHEMA_VERSION = "pit.v2.position_h30"
FEATURE_NAMES = [
    "current_pnl",
    "mfe",
    "mae",
    "giveback_ratio",
    "profit_capture_ratio",
    "time_in_profit",
    "holding_efficiency",
    "time_decay_score",
    "holding_seconds",
    "stop_loss_progress",
    "take_profit_progress",
    "holding_timeout_ratio",
    "completed_bars_after_entry",
    "hard_risk_active",
    "thesis_broken",
    "thesis_weakening",
    "regime_shift_confirmed",
]


class TrainingMemoryBudgetExceeded(RuntimeError):
    """Raised before materialising a training window that exceeds its budget."""

    def __init__(self, message: str, *, data_quality: dict[str, Any]):
        super().__init__(message)
        self.data_quality = dict(data_quality)


# These budgets bound the bytes held by the reader, not the amount of history
# retained in PostgreSQL.  A blocked training run is fail-closed: no event or
# numeric value is removed, and an operator can retry after changing the
# window/profile or the host memory budget.
MAX_UNIQUE_REVIEW_BYTES = 128 * 1024 * 1024
MAX_TRACE_WINDOW_BYTES = 128 * 1024 * 1024
TRACE_FETCH_BATCH_SIZE = 256


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def _review_execution_evidence_complete(row: Any) -> bool:
    review = _loads(row.get("review_json") if isinstance(row, dict) else row["review_json"], {})
    failure_tags = _loads(
        row.get("failure_tags_json") if isinstance(row, dict) else row["failure_tags_json"],
        [],
    )
    if isinstance(failure_tags, list):
        review = {**review, "failure_tags": failure_tags}
    return review_execution_evidence_is_trainable(review)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dependency_error() -> str:
    try:
        import lightgbm  # noqa: F401
        import joblib  # noqa: F401
        import pandas  # noqa: F401
        import sklearn  # noqa: F401
    except Exception as exc:
        return str(exc)
    return ""


def _label(row: dict[str, Any], payload: dict[str, Any]) -> int:
    outcome = str(row.get("outcome_label") or "").lower()
    pnl = _safe_float(row.get("pnl"))
    if outcome in {"good_win", "small_win", "win", "good_loss"}:
        return 1
    if outcome in {"bad_loss", "loss"}:
        return 0
    if outcome == "small_loss":
        return 0
    tags = {str(x).lower() for x in payload.get("failure_tags") or []}
    if "good_loss" in tags:
        return 1
    return 1 if pnl > 0 else 0


def _features_from_review(row: dict[str, Any]) -> dict[str, float]:
    payload = _loads(str(row.get("review_json") or "{}"), {})
    close_reason = str(payload.get("close_reason") or "").lower()
    thesis = str(payload.get("thesis_status_at_exit") or payload.get("thesis_status") or "").lower()
    regime_shift = str(payload.get("regime_shift_at_exit") or payload.get("regime_shift") or "").lower()
    features = {
        "mfe": _safe_float(row.get("mfe") if row.get("mfe") is not None else payload.get("mfe")),
        "mae": _safe_float(row.get("mae") if row.get("mae") is not None else payload.get("mae")),
        "giveback_ratio": _safe_float(payload.get("giveback_ratio")),
        "profit_capture_ratio": _safe_float(payload.get("profit_capture_ratio")),
        "time_in_profit": _safe_float(payload.get("time_in_profit") or payload.get("time_in_profit_seconds")),
        "holding_efficiency": _safe_float(payload.get("holding_efficiency")),
        "time_decay_score": _safe_float(payload.get("time_decay_score")),
        "holding_seconds": _safe_float(payload.get("holding_seconds")),
        "entry_quality": _safe_float(row.get("entry_quality") if row.get("entry_quality") is not None else payload.get("entry_quality")),
        "hold_quality": _safe_float(row.get("hold_quality") if row.get("hold_quality") is not None else payload.get("hold_quality")),
        "exit_quality": _safe_float(row.get("exit_quality") if row.get("exit_quality") is not None else payload.get("exit_quality")),
        "regime_fit_score": _safe_float(row.get("regime_fit_score") if row.get("regime_fit_score") is not None else payload.get("regime_fit_score")),
        "execution_quality": _safe_float(row.get("execution_quality") if row.get("execution_quality") is not None else payload.get("execution_quality")),
        "thesis_broken": 1.0 if thesis == "broken" else 0.0,
        "thesis_weakening": 1.0 if thesis == "weakening" else 0.0,
        "regime_shift_confirmed": 1.0 if regime_shift == "confirmed" else 0.0,
        "close_reason_thesis_broken": 1.0 if close_reason == "thesis_broken" else 0.0,
        "close_reason_broker_close": 1.0 if close_reason == "broker_close" else 0.0,
    }
    return {name: features.get(name, 0.0) for name in FEATURE_NAMES}


def _features_from_trace(row: dict[str, Any]) -> dict[str, float]:
    verdict = _loads(str(row.get("verdict_json") or "{}"), {})
    evidence = dict(verdict.get("evidence") or {})
    features = {
        "current_pnl": _safe_float(evidence.get("current_pnl")),
        "mfe": _safe_float(evidence.get("mfe")),
        "mae": _safe_float(evidence.get("mae")),
        "giveback_ratio": _safe_float(evidence.get("giveback_ratio")),
        "profit_capture_ratio": _safe_float(evidence.get("profit_capture_ratio")),
        "time_in_profit": _safe_float(evidence.get("time_in_profit")),
        "holding_efficiency": _safe_float(evidence.get("holding_efficiency")),
        "time_decay_score": _safe_float(evidence.get("time_decay_score")),
        "holding_seconds": _safe_float(evidence.get("holding_seconds")),
        "stop_loss_progress": _safe_float(evidence.get("stop_loss_progress")),
        "take_profit_progress": _safe_float(evidence.get("take_profit_progress")),
        "holding_timeout_ratio": _safe_float(evidence.get("holding_timeout_ratio")),
        "completed_bars_after_entry": _safe_float(
            evidence.get("completed_bars_after_entry"),
            math.floor(_safe_float(evidence.get("holding_seconds")) / 300.0),
        ),
        "hard_risk_active": 1.0 if bool(evidence.get("hard_risk_active")) else 0.0,
        "thesis_broken": 1.0 if str(evidence.get("thesis_status") or "").lower() == "broken" else 0.0,
        "thesis_weakening": 1.0 if str(evidence.get("thesis_status") or "").lower() == "weakening" else 0.0,
        "regime_shift_confirmed": 1.0 if str(evidence.get("regime_shift") or "").lower() == "confirmed" else 0.0,
    }
    return {name: features.get(name, 0.0) for name in FEATURE_NAMES}


def _row_as_dict(row: Any, description: Any = None) -> dict[str, Any]:
    """Convert SQLite and psycopg rows without retaining cursor-owned data."""
    if isinstance(row, dict):
        return dict(row)
    try:
        keys = row.keys()
        return {str(key): row[key] for key in keys}
    except (AttributeError, KeyError, TypeError):
        pass
    if description:
        return {str(item[0]): row[index] for index, item in enumerate(description)}
    return dict(row)


def _review_payload_text(row: dict[str, Any]) -> tuple[str, str]:
    """Serialize the review payload returned by canonical_v2_reader."""

    payload = _loads(row.get("review_json"), {})
    if not isinstance(payload, dict):
        raise ValueError("canonical trade review payload must be a JSON object")
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        "canonical",
    )


def _sample_from_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    payload = _loads(str(item.get("review_json") or "{}"), {})
    return {
        "sample_id": str(item.get("review_id") or item.get("position_id") or ""),
        "review_id": str(item.get("review_id") or ""),
        "trade_id": str(item.get("trade_id") or ""),
        "position_id": str(item.get("position_id") or ""),
        "created_at": _safe_float(item.get("created_at")),
        "pnl": _safe_float(item.get("pnl")),
        "outcome_label": str(item.get("outcome_label") or ""),
        "label": _label(item, payload),
        "features": _features_from_review(item),
    }


def _sample_semantics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a compact, deterministic manifest for one selected sample set."""
    ordered = sorted(
        samples,
        key=lambda item: (
            _safe_float(item.get("created_at")),
            str(item.get("sample_id") or ""),
        ),
    )
    sample_id_digest = hashlib.sha256()
    feature_digest = hashlib.sha256()
    semantic_digest = hashlib.sha256()
    label_distribution = {"negative": 0, "positive": 0}
    target_source_counts: dict[str, int] = {}
    config_hashes: set[str] = set()
    for item in ordered:
        sample_id = str(item.get("sample_id") or "")
        sample_id_digest.update((sample_id + "\n").encode("utf-8"))
        features = {
            name: _safe_float((item.get("features") or {}).get(name))
            for name in FEATURE_NAMES
        }
        feature_row = {
            "sample_id": sample_id,
            "feature_schema_version": str(item.get("feature_schema_version") or FEATURE_SCHEMA_VERSION),
            "features": features,
        }
        feature_encoded = json.dumps(
            feature_row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        feature_digest.update(feature_encoded + b"\n")
        label = int(item.get("label") or 0)
        label_distribution["positive" if label else "negative"] += 1
        target_source = str(item.get("target_source") or "unknown")
        target_source_counts[target_source] = target_source_counts.get(target_source, 0) + 1
        config_hash = str(item.get("config_hash") or "")
        if config_hash:
            config_hashes.add(config_hash)
        semantic_row = {
            **feature_row,
            "label": label,
            "target_source": target_source,
            "target_pnl": _safe_float(item.get("target_pnl")),
            "target_pnl_delta": _safe_float(item.get("target_pnl_delta")),
            "config_hash": config_hash,
        }
        semantic_digest.update(
            json.dumps(
                semantic_row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            + b"\n"
        )
    feature_schema_payload = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": FEATURE_NAMES,
    }
    feature_schema_hash = hashlib.sha256(
        json.dumps(
            feature_schema_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    sample_ids = [str(item.get("sample_id") or "") for item in ordered]
    return {
        "sample_count": len(ordered),
        "sample_id_digest": sample_id_digest.hexdigest(),
        "sample_id_preview": {
            "first": sample_ids[:5],
            "last": sample_ids[-5:] if sample_ids else [],
        },
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_schema_hash": feature_schema_hash,
        "feature_values_digest": feature_digest.hexdigest(),
        "semantic_digest": semantic_digest.hexdigest(),
        "label_distribution": label_distribution,
        "target_source_counts": dict(sorted(target_source_counts.items())),
        "config_hashes": sorted(config_hashes),
    }


class PositionQualityLightGBMService:
    """LightGBM sidecar model for position quality scoring.

    The service is intentionally shadow-only. It trains from reviewed trades,
    stores artifacts, and logs every inference to state.db. It cannot place
    orders, close positions, or mutate live risk parameters.
    """

    def __init__(
        self,
        *,
        db_path: str | Path = STATE_DB,
        artifact_dir: str | Path | None = None,
    ):
        self.db_path = Path(db_path)
        self.artifact_dir = Path(artifact_dir) if artifact_dir else DATA_DIR / "model_artifacts" / MODEL_TYPE
        self._inference_bundle_cache: tuple[str, int, dict[str, Any], Any] | None = None
        self.last_data_quality: dict[str, Any] = {}
        self._last_training_queries: list[tuple[str, str, tuple[Any, ...]]] = []

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _execute(self, conn, sql: str, params: tuple | list | None = None):
        rendered = self._sql(sql)
        if self._use_pg() and is_state_schema_write_sql(rendered):
            return validate_runtime_state_schema(conn, rendered)
        if params is None:
            return conn.execute(rendered)
        return conn.execute(rendered, tuple(params))

    def _conn(self, *, read_only: bool = False):
        conn = (
            get_state_pg_conn(read_only=read_only)
            if self._use_pg()
            else connect_sqlite(self.db_path, read_only=read_only)
        )
        if not self._use_pg():
            conn.row_factory = __import__("sqlite3").Row
        return conn

    def _ensure_audit_table(self) -> None:
        conn = self._conn()
        try:
            self._execute(conn,
                """
                CREATE TABLE IF NOT EXISTS position_quality_shadow_audit (
                    inference_id TEXT PRIMARY KEY,
                    model_type TEXT NOT NULL,
                    model_version TEXT DEFAULT '',
                    artifact_path TEXT DEFAULT '',
                    review_id TEXT DEFAULT '',
                    trade_id TEXT DEFAULT '',
                    position_id TEXT DEFAULT '',
                    mode TEXT DEFAULT 'shadow',
                    hold_score REAL DEFAULT 0.0,
                    exit_risk_score REAL DEFAULT 0.0,
                    prediction INTEGER DEFAULT 0,
                    payload_json TEXT DEFAULT '{}',
                    result_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL DEFAULT 0.0
                )
                """
            )
            self._execute(conn,
                """
                CREATE INDEX IF NOT EXISTS idx_position_quality_shadow_audit_created
                ON position_quality_shadow_audit(created_at)
                """
            )
            self._execute(conn,
                """
                CREATE INDEX IF NOT EXISTS idx_position_quality_shadow_audit_position
                ON position_quality_shadow_audit(position_id, created_at)
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _stream_query_rows(self, conn, sql: str, params: tuple | list = ()):
        """Yield bounded batches from PostgreSQL or SQLite."""
        cursor = None
        try:
            if self._use_pg():
                cursor = conn.cursor(name=f"position_quality_query_{id(sql)}_{id(conn)}")
                cursor.execute(self._sql(sql), tuple(params))
            else:
                cursor = conn.execute(self._sql(sql), tuple(params))
            while True:
                batch = cursor.fetchmany(TRACE_FETCH_BATCH_SIZE)
                if not batch:
                    break
                for row in batch:
                    yield _row_as_dict(row, getattr(cursor, "description", None))
        finally:
            if cursor is not None:
                cursor.close()

    def _remember_training_query(
        self,
        name: str,
        sql: str,
        params: tuple | list = (),
    ) -> None:
        self._last_training_queries.append((name, sql, tuple(params)))

    def load_samples(
        self,
        *,
        limit: int = 4000,
        horizon_minutes: int = 30,
        pnl_tolerance: float = 0.25,
        read_only: bool = False,
    ) -> list[dict[str, Any]]:
        conn = self._conn(read_only=read_only)
        self._last_training_queries = []
        try:
            if not canonical_ready(conn):
                raise RuntimeError("canonical_v2 reader is unavailable")
            max_positions = max(20, int(limit) // 2)
            horizon_seconds = max(5, int(horizon_minutes)) * 60.0
            self._remember_training_query(
                "review_reader", "canonical_v2_reader.iter_review_rows_desc", ()
            )
            self._remember_training_query(
                "trace_reader", "canonical_v2_reader.iter_supervisor_trace_rows", ()
            )
            self._remember_training_query(
                "canonical_training_window", "canonical_v2_reader review/trace join", ()
            )
            canonical_traces = [
                dict(row)
                for row in iter_supervisor_trace_rows(
                    conn, limit=0, stage="evaluated", reverse=False
                )
                if str(row.get("trace_integrity") or "full") == "full"
            ]
            trace_positions = {
                str(row.get("position_id") or "")
                for row in canonical_traces
                if str(row.get("position_id") or "")
            }
            review_rows = [
                dict(row)
                for row in iter_review_rows_desc(conn, limit=0)
                if str(row.get("position_id") or "") in trace_positions
            ]
            estimated_review_count = len(review_rows)
            estimated_review_bytes = sum(
                len(_review_payload_text(row)[0].encode("utf-8"))
                for row in review_rows
            )
            if estimated_review_bytes > MAX_UNIQUE_REVIEW_BYTES:
                self.last_data_quality = {
                    "schema_version": "model_training_data_quality.v1",
                    "status": "blocked_memory_budget",
                    "raw_candidate_row_count": 0,
                    "unique_review_count": estimated_review_count,
                    "unique_review_bytes": estimated_review_bytes,
                    "max_unique_review_bytes": MAX_UNIQUE_REVIEW_BYTES,
                    "peak_buffered_bytes": 0,
                    "horizon_minutes": int(horizon_minutes),
                    "pnl_tolerance": float(pnl_tolerance),
                    "canonical_reader_prefetched": True,
                }
                raise TrainingMemoryBudgetExceeded(
                    "estimated unique review payload exceeds training memory budget",
                    data_quality=self.last_data_quality,
                )

            # A review is an immutable card for a position.  Parse each card
            # cards for one position are ambiguous and therefore excluded
            # rather than silently selecting one by join order.
            review_by_position: dict[str, dict[str, Any]] = {}
            review_counts: dict[str, int] = {}
            unique_review_count = 0
            unique_review_bytes = 0
            review_cardinality_ambiguous: set[str] = set()
            excluded_review_contaminated_positions: set[str] = set()
            excluded_review_incomplete_positions: set[str] = set()
            for raw_review in review_rows:
                review = _row_as_dict(raw_review)
                position_id = str(review.get("position_id") or "")
                if not position_id:
                    continue
                review_counts[position_id] = review_counts.get(position_id, 0) + 1
                review_json, _review_payload_source = _review_payload_text(review)
                failure_tags_json = str(review.get("failure_tags_json") or "[]")
                unique_review_count += 1
                unique_review_bytes += len(review_json.encode("utf-8"))
                review["review_contaminated"] = _review_text_contaminated(review_json)
                review["review_json"] = review_json
                review["failure_tags_json"] = failure_tags_json
                review["review_created_at"] = review.get("created_at", 0.0)
                review["review_trainable"] = _review_execution_evidence_complete(review)
                if review["review_contaminated"]:
                    excluded_review_contaminated_positions.add(position_id)
                elif not review["review_trainable"]:
                    excluded_review_incomplete_positions.add(position_id)
                if review_counts[position_id] > 1:
                    review_cardinality_ambiguous.add(position_id)
                else:
                    review_by_position[position_id] = review

            # Cardinality ambiguity is the primary fail-closed reason; do not
            # double-count the same position as contaminated/incomplete.
            excluded_review_contaminated_positions.difference_update(review_cardinality_ambiguous)
            excluded_review_incomplete_positions.difference_update(review_cardinality_ambiguous)

            if unique_review_bytes > MAX_UNIQUE_REVIEW_BYTES:
                self.last_data_quality = {
                    "schema_version": "model_training_data_quality.v1",
                    "status": "blocked_memory_budget",
                    "raw_candidate_row_count": 0,
                    "unique_review_count": unique_review_count,
                    "unique_review_bytes": unique_review_bytes,
                    "max_unique_review_bytes": MAX_UNIQUE_REVIEW_BYTES,
                    "peak_buffered_bytes": 0,
                    "horizon_minutes": int(horizon_minutes),
                    "pnl_tolerance": float(pnl_tolerance),
                }
                raise TrainingMemoryBudgetExceeded(
                    "unique review payload exceeds training memory budget",
                    data_quality=self.last_data_quality,
                )

            all_review_positions = tuple(review_by_position)
            candidate_count = 0
            trace_counts: dict[str, int] = {}
            if all_review_positions:
                for trace in canonical_traces:
                    position_id = str(trace.get("position_id") or "")
                    if position_id not in review_by_position:
                        continue
                    trace_counts[position_id] = trace_counts.get(position_id, 0) + 1
                candidate_count = sum(trace_counts.values())

            eligible_positions = {
                position_id
                for position_id in trace_counts
                if position_id not in review_cardinality_ambiguous
                and position_id not in excluded_review_contaminated_positions
                and position_id not in excluded_review_incomplete_positions
            }

            # Estimate each eligible position before selecting the newest
            # window. An oversized historical position is excluded with an
            # explicit reason; it must never cross the Python boundary or
            # force the whole otherwise-safe window to abort.
            oversized_trace_positions: set[str] = set()
            trace_position_bytes: dict[str, int] = {}
            latest_template_version = ""
            if eligible_positions:
                eligible_traces = [
                    trace for trace in canonical_traces
                    if str(trace.get("position_id") or "") in eligible_positions
                    and str(trace.get("config_hash") or "")
                ]
                latest_template_version = str(
                    max(
                        eligible_traces,
                        key=lambda trace: (
                            _safe_float(trace.get("event_ts")),
                            str(trace.get("position_id") or ""),
                            str(trace.get("trace_id") or trace.get("event_id") or ""),
                        ),
                        default={},
                    ).get("template_version") or ""
                )
                if latest_template_version:
                    for trace in eligible_traces:
                        if str(trace.get("template_version") or "") != latest_template_version:
                            continue
                        position_id = str(trace.get("position_id") or "")
                        trace_position_bytes[position_id] = trace_position_bytes.get(position_id, 0) + len(
                            str(trace.get("verdict_json") or "").encode("utf-8")
                        )
                    oversized_trace_positions = {
                        position_id
                        for position_id, position_bytes in trace_position_bytes.items()
                        if position_bytes > MAX_TRACE_WINDOW_BYTES
                    }
                    eligible_positions.difference_update(oversized_trace_positions)

            # Select the newest max_positions safe positions in SQL. The
            # database never sends traces for older or oversized positions
            # into Python.
            selected_positions: list[str] = []
            if eligible_positions:
                latest_by_position: dict[str, float] = {}
                for trace in canonical_traces:
                    position_id = str(trace.get("position_id") or "")
                    if (
                        position_id in eligible_positions
                        and str(trace.get("template_version") or "") == latest_template_version
                        and str(trace.get("config_hash") or "")
                    ):
                        latest_by_position[position_id] = max(
                            latest_by_position.get(position_id, 0.0),
                            _safe_float(trace.get("event_ts")),
                        )
                selected_positions = [
                    position_id
                    for position_id, _latest in sorted(
                        latest_by_position.items(),
                        key=lambda item: (item[1], item[0]),
                        reverse=True,
                    )[:max_positions]
                ]

            selected_set = set(selected_positions)
            selected_candidate_count = sum(trace_counts.get(position_id, 0) for position_id in selected_positions)
            lineage_trace_count = 0
            selected_verdict_bytes = 0
            peak_buffer_bytes = 0
            samples: list[dict[str, Any]] = []
            excluded_other_lineage_count = 0
            stream_excluded_counts = {
                "other_lineage": 0,
                "missing_position": 0,
            }

            max_position_bytes = max(
                (trace_position_bytes.get(position_id, 0) for position_id in selected_positions),
                default=0,
            )
            if not selected_positions and oversized_trace_positions:
                self.last_data_quality = {
                    "schema_version": "model_training_data_quality.v1",
                    "status": "blocked_memory_budget",
                    "raw_candidate_row_count": candidate_count,
                    "candidate_trace_count": selected_candidate_count,
                    "unique_review_count": unique_review_count,
                    "unique_review_bytes": unique_review_bytes,
                    "max_trace_window_bytes": MAX_TRACE_WINDOW_BYTES,
                    "max_position_trace_bytes_estimate": max(
                        trace_position_bytes.values(), default=0
                    ),
                    "excluded_trace_window_position_count": len(oversized_trace_positions),
                    "excluded_reason_counts": {
                        "trace_window_budget_exceeded": sum(
                            trace_counts.get(position_id, 0)
                            for position_id in oversized_trace_positions
                        )
                    },
                    "peak_buffered_bytes": 0,
                    "horizon_minutes": int(horizon_minutes),
                    "pnl_tolerance": float(pnl_tolerance),
                    "blocked_before_payload_fetch": True,
                }
                raise TrainingMemoryBudgetExceeded(
                    "all eligible trace windows exceed training memory budget",
                    data_quality=self.last_data_quality,
                )

            def _iter_trace_rows():
                if not selected_set:
                    return
                rows = [
                    dict(row)
                    for row in canonical_traces
                    if str(row.get("position_id") or "") in selected_set
                    and str(row.get("template_version") or "") == latest_template_version
                    and str(row.get("config_hash") or "")
                ]
                rows.sort(
                    key=lambda row: (
                        str(row.get("position_id") or ""),
                        _safe_float(row.get("event_ts")),
                        str(row.get("trace_id") or row.get("event_id") or ""),
                    )
                )
                yield from rows

            def _payload_bytes(item: dict[str, Any]) -> int:
                return len(str(item.get("verdict_json") or "").encode("utf-8"))

            def _process_position(position_id: str, rows_for_position: Any) -> None:
                nonlocal lineage_trace_count, selected_verdict_bytes, peak_buffer_bytes
                review = review_by_position.get(position_id) or {}
                window: deque[dict[str, Any]] = deque()
                buffered_bytes = 0
                row_iter = iter(rows_for_position)
                lookahead: dict[str, Any] | None = None
                first_ts: float | None = None
                seen_buckets: set[int] = set()

                def _next_row() -> dict[str, Any] | None:
                    try:
                        return next(row_iter)
                    except StopIteration:
                        return None

                current = _next_row()
                current_in_window = False
                while current is not None:
                    current_ts = _safe_float(current.get("event_ts"))
                    if first_ts is None:
                        first_ts = current_ts
                    if not current_in_window:
                        window.append(current)
                        current_bytes = _payload_bytes(current)
                        buffered_bytes += current_bytes
                        lineage_trace_count += 1
                        selected_verdict_bytes += current_bytes
                        if buffered_bytes > peak_buffer_bytes:
                            peak_buffer_bytes = buffered_bytes
                        if buffered_bytes > MAX_TRACE_WINDOW_BYTES:
                            quality = {
                                "schema_version": "model_training_data_quality.v1",
                                "status": "blocked_memory_budget",
                                "raw_candidate_row_count": candidate_count,
                                "candidate_trace_count": selected_candidate_count,
                                "unique_review_count": unique_review_count,
                                "unique_review_bytes": unique_review_bytes,
                                "selected_verdict_bytes": selected_verdict_bytes,
                                "peak_buffer_bytes": peak_buffer_bytes,
                                "peak_buffered_bytes": peak_buffer_bytes,
                                "max_trace_window_bytes": MAX_TRACE_WINDOW_BYTES,
                                "horizon_minutes": int(horizon_minutes),
                                "pnl_tolerance": float(pnl_tolerance),
                            }
                            self.last_data_quality = quality
                            raise TrainingMemoryBudgetExceeded(
                                "trace sliding window exceeds training memory budget",
                                data_quality=quality,
                            )

                    target_limit = current_ts + horizon_seconds + 900.0
                    while True:
                        if lookahead is None:
                            lookahead = _next_row()
                        if lookahead is None or _safe_float(lookahead.get("event_ts")) > target_limit:
                            break
                        window.append(lookahead)
                        lookahead_bytes = _payload_bytes(lookahead)
                        buffered_bytes += lookahead_bytes
                        lineage_trace_count += 1
                        selected_verdict_bytes += lookahead_bytes
                        if buffered_bytes > peak_buffer_bytes:
                            peak_buffer_bytes = buffered_bytes
                        if buffered_bytes > MAX_TRACE_WINDOW_BYTES:
                            quality = {
                                "schema_version": "model_training_data_quality.v1",
                                "status": "blocked_memory_budget",
                                "raw_candidate_row_count": candidate_count,
                                "candidate_trace_count": selected_candidate_count,
                                "unique_review_count": unique_review_count,
                                "unique_review_bytes": unique_review_bytes,
                                "selected_verdict_bytes": selected_verdict_bytes,
                                "peak_buffer_bytes": peak_buffer_bytes,
                                "peak_buffered_bytes": peak_buffer_bytes,
                                "max_trace_window_bytes": MAX_TRACE_WINDOW_BYTES,
                                "horizon_minutes": int(horizon_minutes),
                                "pnl_tolerance": float(pnl_tolerance),
                            }
                            self.last_data_quality = quality
                            raise TrainingMemoryBudgetExceeded(
                                "trace sliding window exceeds training memory budget",
                                data_quality=quality,
                            )
                        lookahead = None

                    target_ts = current_ts + horizon_seconds
                    bucket = int(max(0.0, current_ts - float(first_ts or current_ts)) // horizon_seconds)
                    if bucket not in seen_buckets:
                        review_close_ts = _safe_float(review.get("review_created_at"))
                        target_pnl: float | None = None
                        target_source = ""
                        if review_close_ts > current_ts and review_close_ts <= target_ts:
                            target_pnl = _safe_float(review.get("pnl"))
                            target_source = "closed_before_horizon"
                        else:
                            future_row = next(
                                (
                                    candidate for candidate in window
                                    if target_ts <= _safe_float(candidate.get("event_ts")) <= target_ts + 900.0
                                ),
                                None,
                            )
                            if future_row is not None:
                                future_verdict = _loads(str(future_row.get("verdict_json") or "{}"), {})
                                future_evidence = dict(future_verdict.get("evidence") or {})
                                if "current_pnl" in future_evidence:
                                    target_pnl = _safe_float(future_evidence.get("current_pnl"))
                                    target_source = "trace_at_horizon"
                        if target_pnl is not None:
                            features = _features_from_trace(current)
                            current_pnl = _safe_float(features.get("current_pnl"))
                            pnl_delta = target_pnl - current_pnl
                            seen_buckets.add(bucket)
                            samples.append({
                                "sample_id": str(current.get("trace_id") or f"{position_id}:{bucket}"),
                                "review_id": str(review.get("review_id") or ""),
                                "trade_id": str(current.get("trade_id") or ""),
                                "position_id": position_id,
                                "created_at": current_ts,
                                "pnl": _safe_float(review.get("pnl")),
                                "outcome_label": str(review.get("outcome_label") or ""),
                                "label": 1 if pnl_delta >= -abs(float(pnl_tolerance)) else 0,
                                "label_source": f"fixed_horizon_{int(horizon_minutes)}m_pnl_delta",
                                "target_pnl": target_pnl,
                                "target_pnl_delta": pnl_delta,
                                "target_source": target_source,
                                "features": features,
                                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                                "config_hash": str(current.get("config_hash") or ""),
                                "template_version": latest_template_version,
                            })

                    removed = window.popleft()
                    buffered_bytes -= _payload_bytes(removed)
                    if window:
                        current = window[0]
                        current_in_window = True
                    elif lookahead is not None:
                        current = lookahead
                        lookahead = None
                        current_in_window = False
                    else:
                        current = _next_row()
                        current_in_window = False

            trace_iter = _iter_trace_rows()
            if trace_iter is not None:
                for position_id, raw_group in itertools.groupby(
                    trace_iter,
                    key=lambda item: str(item.get("position_id") or ""),
                ):
                    if not position_id or position_id not in selected_set:
                        stream_excluded_counts["missing_position"] += 1
                        for _ in raw_group:
                            pass
                        continue

                    def _lineage_rows(group=raw_group, grouped_position=position_id):
                        nonlocal excluded_other_lineage_count
                        for item in group:
                            if (
                                str(item.get("template_version") or "") != latest_template_version
                                or not str(item.get("config_hash") or "")
                            ):
                                excluded_other_lineage_count += 1
                                stream_excluded_counts["other_lineage"] += 1
                                continue
                            review = review_by_position.get(grouped_position) or {}
                            item.update(
                                {
                                    "review_id": review.get("review_id", ""),
                                    "pnl": review.get("pnl", 0.0),
                                    "outcome_label": review.get("outcome_label", ""),
                                    "review_created_at": review.get("review_created_at", 0.0),
                                }
                            )
                            yield item

                    _process_position(position_id, _lineage_rows())

            samples = sorted(
                samples,
                key=lambda item: (_safe_float(item.get("created_at")), str(item.get("sample_id") or "")),
            )[-int(limit):]
            excluded_reason_counts = {
                "review_cardinality_ambiguous": sum(
                    trace_counts.get(position_id, 0) for position_id in review_cardinality_ambiguous
                ),
                "system_contaminated": sum(
                    trace_counts.get(position_id, 0) for position_id in excluded_review_contaminated_positions
                ),
                "execution_evidence_incomplete": sum(
                    trace_counts.get(position_id, 0) for position_id in excluded_review_incomplete_positions
                ),
                "trace_window_budget_exceeded": sum(
                    trace_counts.get(position_id, 0) for position_id in oversized_trace_positions
                ),
                "other_lineage": excluded_other_lineage_count,
            }
            self.last_data_quality = {
                "schema_version": "model_training_data_quality.v1",
                "raw_candidate_row_count": candidate_count,
                "candidate_trace_count": selected_candidate_count,
                "lineage_trace_count": lineage_trace_count,
                "selected_count": len(samples),
                "selected_position_count": len({item["position_id"] for item in samples}),
                "selected_position_window_count": len(selected_positions),
                "template_version": latest_template_version,
                "config_hashes": sorted({item["config_hash"] for item in samples}),
                "horizon_minutes": int(horizon_minutes),
                "pnl_tolerance": float(pnl_tolerance),
                "unique_review_count": unique_review_count,
                "unique_review_bytes": unique_review_bytes,
                "selected_verdict_bytes": selected_verdict_bytes,
                "peak_buffer_bytes": peak_buffer_bytes,
                "peak_buffered_bytes": peak_buffer_bytes,
                "input_bytes_estimate": unique_review_bytes + selected_verdict_bytes,
                "max_position_trace_bytes_estimate": max_position_bytes,
                "trace_window_budget_policy": "exclude_oversized_position",
                "excluded_trace_window_position_count": len(oversized_trace_positions),
                "excluded_trace_window_positions_preview": sorted(oversized_trace_positions)[:20],
                "excluded_other_lineage_count": excluded_other_lineage_count,
                "excluded_system_contaminated_count": excluded_reason_counts["system_contaminated"],
                "excluded_execution_incomplete_count": excluded_reason_counts["execution_evidence_incomplete"],
                "excluded_review_cardinality_ambiguous_count": excluded_reason_counts["review_cardinality_ambiguous"],
                "excluded_other_non_trainable_count": (
                    excluded_reason_counts["system_contaminated"]
                    + excluded_reason_counts["execution_evidence_incomplete"]
                    + excluded_reason_counts["review_cardinality_ambiguous"]
                    + excluded_reason_counts["trace_window_budget_exceeded"]
                ),
                "excluded_reason_counts": excluded_reason_counts,
                "review_parse_count": unique_review_count,
                "canonical_review_reader": True,
                "canonical_trace_reader": True,
                "trace_fetch_batch_size": TRACE_FETCH_BATCH_SIZE,
            }
            self.last_data_quality.update(_sample_semantics(samples))
            return samples
        finally:
            conn.close()

    def _explain_training_queries(self) -> list[dict[str, Any]]:
        """Describe the canonical readers used by the bounded training window."""
        queries = list(self._last_training_queries)
        if not queries:
            queries = [
                (
                    "review_reader",
                    "canonical_v2_reader.iter_review_rows_desc",
                    (),
                ),
                (
                    "trace_reader",
                    "canonical_v2_reader.iter_supervisor_trace_rows",
                    (),
                ),
                (
                    "canonical_training_window",
                    "canonical_v2_reader review/trace join",
                    (),
                ),
            ]
        return [
            {
                "name": name,
                "reader": reader,
                "params": list(params),
                "elapsed_ms": 0.0,
                "analyze": False,
                "plan": {
                    "authority": "canonical_v2_reader",
                    "reader": reader,
                    "read_only": True,
                },
            }
            for name, reader, params in queries
        ]

    def inspect_training_window(
        self,
        *,
        limit: int = 4000,
        horizon_minutes: int = 30,
        pnl_tolerance: float = 0.25,
    ) -> dict[str, Any]:
        """Inspect one training window without writing data or artifacts."""
        explain: list[dict[str, Any]] = []
        try:
            samples = self.load_samples(
                limit=limit,
                horizon_minutes=horizon_minutes,
                pnl_tolerance=pnl_tolerance,
                read_only=True,
            )
            explain = self._explain_training_queries()
        except TrainingMemoryBudgetExceeded as exc:
            if not explain:
                try:
                    explain = self._explain_training_queries()
                except Exception:
                    explain = []
            quality = dict(getattr(exc, "data_quality", {}) or self.last_data_quality)
            quality.setdefault("peak_buffered_bytes", quality.get("peak_buffer_bytes", 0))
            quality["read_only"] = True
            return {
                "ok": False,
                "read_only": True,
                "model_type": MODEL_TYPE,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "status": "blocked_memory_budget",
                "error": "blocked_memory_budget",
                "detail": str(exc),
                "raw_candidate_row_count": int(quality.get("raw_candidate_row_count") or 0),
                "unique_review_bytes": int(quality.get("unique_review_bytes") or 0),
                "selected_verdict_bytes": int(quality.get("selected_verdict_bytes") or 0),
                "peak_buffered_bytes": int(quality.get("peak_buffered_bytes") or 0),
                "sample_count": 0,
                "sample_id": {"digest": "", "preview": {}},
                "features": {
                    "schema_version": quality.get("feature_schema_version", FEATURE_SCHEMA_VERSION),
                    "schema_hash": quality.get("feature_schema_hash", ""),
                    "values_digest": "",
                },
                "label": {},
                "target_source": {},
                "config_hash": [],
                "exclusion_reasons": quality.get("excluded_reason_counts", {}),
                "data_quality": quality,
                "sample_manifest": {
                    "sample_count": 0,
                    "reason": "reader_blocked_before_payload_fetch",
                },
                "explain": explain,
                "writes": {"database": False, "artifact": False, "model_registry": False},
            }
        except Exception as exc:
            return {
                "ok": False,
                "read_only": True,
                "model_type": MODEL_TYPE,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "error": "training_window_inspection_failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "data_quality": dict(self.last_data_quality),
                "explain": explain,
                "writes": {"database": False, "artifact": False, "model_registry": False},
            }
        quality = dict(self.last_data_quality)
        quality.setdefault("peak_buffered_bytes", quality.get("peak_buffer_bytes", 0))
        quality["read_only"] = True
        quality.update(_sample_semantics(samples))
        return {
            "ok": True,
            "read_only": True,
            "model_type": MODEL_TYPE,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "limit": int(limit),
            "horizon_minutes": int(horizon_minutes),
            "pnl_tolerance": float(pnl_tolerance),
            "raw_candidate_row_count": int(quality.get("raw_candidate_row_count") or 0),
            "unique_review_bytes": int(quality.get("unique_review_bytes") or 0),
            "selected_verdict_bytes": int(quality.get("selected_verdict_bytes") or 0),
            "peak_buffered_bytes": int(quality.get("peak_buffered_bytes") or 0),
            "sample_count": len(samples),
            "sample_id": {
                "digest": quality.get("sample_id_digest", ""),
                "preview": quality.get("sample_id_preview", {}),
            },
            "features": {
                "schema_version": quality.get("feature_schema_version", FEATURE_SCHEMA_VERSION),
                "schema_hash": quality.get("feature_schema_hash", ""),
                "values_digest": quality.get("feature_values_digest", ""),
            },
            "label": quality.get("label_distribution", {}),
            "target_source": quality.get("target_source_counts", {}),
            "config_hash": quality.get("config_hashes", []),
            "exclusion_reasons": quality.get("excluded_reason_counts", {}),
            "data_quality": quality,
            "sample_manifest": {
                key: quality[key]
                for key in (
                    "sample_count",
                    "sample_id_digest",
                    "sample_id_preview",
                    "feature_schema_version",
                    "feature_schema_hash",
                    "label_distribution",
                    "target_source_counts",
                    "config_hashes",
                    "semantic_digest",
                )
                if key in quality
            },
            "explain": explain,
            "writes": {"database": False, "artifact": False, "model_registry": False},
        }

    def train(
        self,
        *,
        limit: int = 4000,
        holdout_ratio: float = 0.25,
        min_samples: int = 20,
        register: bool = True,
        registry_db_path: str | None = None,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
        horizon_minutes: int = 30,
        pnl_tolerance: float = 0.25,
    ) -> dict[str, Any]:
        dep_error = _dependency_error()
        if dep_error:
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "error": "dependency_missing",
                "detail": dep_error,
                "required": ["lightgbm", "scikit-learn", "joblib", "pandas"],
            }

        import joblib
        import lightgbm as lgb
        import pandas as pd
        from sklearn.metrics import accuracy_score, balanced_accuracy_score, recall_score, roc_auc_score

        try:
            samples = self.load_samples(
                limit=limit,
                horizon_minutes=horizon_minutes,
                pnl_tolerance=pnl_tolerance,
            )
            self.last_data_quality.update(_sample_semantics(samples))
        except TrainingMemoryBudgetExceeded as exc:
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "status": "blocked_memory_budget",
                "reason_codes": ["blocked_memory_budget"],
                "error": "blocked_memory_budget",
                "detail": str(exc),
                "data_quality": dict(getattr(exc, "data_quality", {}) or self.last_data_quality),
            }
        if len(samples) < int(min_samples):
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "sample_count": len(samples),
                "data_quality": dict(self.last_data_quality),
                "error": "insufficient_review_samples",
            }
        labels = [int(item["label"]) for item in samples]
        if len(set(labels)) < 2:
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "sample_count": len(samples),
                "positive_count": sum(labels),
                "label_distribution": dict(self.last_data_quality.get("label_distribution") or {}),
                "data_quality": dict(self.last_data_quality),
                "error": "single_class_training_data",
            }

        ordered_positions: list[str] = []
        for item in samples:
            position_id = str(item.get("position_id") or "")
            if position_id and position_id not in ordered_positions:
                ordered_positions.append(position_id)
        holdout_group_count = max(1, int(round(len(ordered_positions) * max(0.0, min(float(holdout_ratio), 0.8)))))
        holdout_positions = set(ordered_positions[-holdout_group_count:])
        train_samples = [item for item in samples if str(item.get("position_id") or "") not in holdout_positions]
        holdout_samples = [item for item in samples if str(item.get("position_id") or "") in holdout_positions]
        if not train_samples or not holdout_samples:
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "sample_count": len(samples),
                "data_quality": dict(self.last_data_quality),
                "error": "grouped_time_split_empty",
            }

        x_train = pd.DataFrame([item["features"] for item in train_samples], columns=FEATURE_NAMES)
        y_train = [int(item["label"]) for item in train_samples]
        x_holdout = pd.DataFrame([item["features"] for item in holdout_samples], columns=FEATURE_NAMES)
        y_holdout = [int(item["label"]) for item in holdout_samples]
        train_position_counts: dict[str, int] = {}
        for item in train_samples:
            key = str(item.get("position_id") or "")
            train_position_counts[key] = train_position_counts.get(key, 0) + 1
        train_weights = [
            1.0 / max(1, train_position_counts.get(str(item.get("position_id") or ""), 1))
            for item in train_samples
        ]

        model = lgb.LGBMClassifier(
            objective="binary",
            n_estimators=120,
            learning_rate=0.05,
            num_leaves=15,
            min_child_samples=max(1, min(20, len(train_samples) // 4)),
            subsample=0.9,
            colsample_bytree=0.9,
            class_weight="balanced",
            random_state=42,
            n_jobs=1,
            verbosity=-1,
        )
        model.fit(x_train, y_train, sample_weight=train_weights)
        train_prob = model.predict_proba(x_train)[:, 1]
        holdout_prob = model.predict_proba(x_holdout)[:, 1]

        def _metrics(y_true: list[int], probs: Any) -> dict[str, Any]:
            preds = [1 if float(x) >= 0.5 else 0 for x in probs]
            positive_rate = sum(y_true) / max(len(y_true), 1)
            majority_label = 1 if positive_rate >= 0.5 else 0
            majority_preds = [majority_label] * len(y_true)
            auc = None
            if len(set(y_true)) > 1:
                try:
                    auc = round(float(roc_auc_score(y_true, probs)), 6)
                except Exception:
                    auc = None
            return {
                "count": len(y_true),
                "accuracy": round(float(accuracy_score(y_true, preds)), 6) if y_true else None,
                "balanced_accuracy": round(float(balanced_accuracy_score(y_true, preds)), 6) if y_true else None,
                "majority_baseline_accuracy": round(float(accuracy_score(y_true, majority_preds)), 6) if y_true else None,
                "auc": auc,
                "positive_rate": round(positive_rate, 6),
                "prediction_positive_rate": round(sum(preds) / max(len(preds), 1), 6),
                "negative_recall": round(float(recall_score(y_true, preds, pos_label=0, zero_division=0)), 6),
                "positive_recall": round(float(recall_score(y_true, preds, pos_label=1, zero_division=0)), 6),
                "majority_class": majority_label,
            }

        feature_importance = [
            {"feature": name, "importance": int(value)}
            for name, value in sorted(
                zip(FEATURE_NAMES, model.feature_importances_),
                key=lambda item: (-int(item[1]), item[0]),
            )
        ]
        metrics = {
            "train": _metrics(y_train, train_prob),
            "holdout": _metrics(y_holdout, holdout_prob),
            "sample_count": len(samples),
            "feature_count": len(FEATURE_NAMES),
            "split": "time_ordered_grouped_purged",
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "train_position_count": len({item["position_id"] for item in train_samples}),
            "holdout_position_count": len({item["position_id"] for item in holdout_samples}),
            "holdout_ratio": float(holdout_ratio),
            "train_count": len(train_samples),
            "holdout_count": len(holdout_samples),
            "label_distribution": {"negative": labels.count(0), "positive": labels.count(1)},
            "safe_for_live_trading": False,
            "data_quality": dict(self.last_data_quality),
            "label_contract": {
                "label": "hold_value_preserved_at_fixed_horizon",
                "horizon_minutes": int(self.last_data_quality.get("horizon_minutes") or 30),
                "pnl_tolerance": float(self.last_data_quality.get("pnl_tolerance") or 0.25),
                "position_balanced_training_weight": True,
            },
        }
        now = time.time()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_base = f"{MODEL_TYPE}_{int(now)}"
        model_path = self.artifact_dir / f"{artifact_base}.joblib"
        metadata_path = self.artifact_dir / f"{artifact_base}.json"
        joblib.dump({"model": model, "feature_names": FEATURE_NAMES}, model_path)
        artifact = {
            "schema_version": "position_quality_lightgbm_artifact.v2",
            "model_type": MODEL_TYPE,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "created_at": now,
            "artifact_path": str(metadata_path),
            "model_file": str(model_path),
            "model_file_sha256": _sha256(model_path),
            "feature_names": FEATURE_NAMES,
            "label": "hold_value_preserved_at_fixed_horizon",
            "sample_window": {"limit": int(limit), "sample_count": len(samples)},
            "training_lineage": dict(self.last_data_quality),
            "metrics": metrics,
            "explainability": {
                "feature_importance": feature_importance,
                "summary": "LightGBM shadow-only position quality model. Scores are advisory and logged. Holdout metrics include majority baseline and class recall to expose imbalance.",
            },
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
                "can_place_orders": False,
                "can_close_positions": False,
                "can_change_risk_limits": False,
            },
            "guardrails": [
                "MUST NOT place orders",
                "MUST NOT close positions",
                "MUST NOT change RiskPolicyService limits",
                "MUST log every inference before downstream review",
            ],
        }
        metadata_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        artifact["artifact_path"] = str(metadata_path)
        artifact["artifact_sha256"] = _sha256(metadata_path)

        registry_version = None
        if register:
            from research.model_registry import ModelRegistry

            registry_version = ModelRegistry(db_path=registry_db_path).register(
                MODEL_TYPE,
                artifact_path=str(metadata_path),
                params={
                    "model_version": MODEL_VERSION,
                    "label": artifact["label"],
                    "feature_names": FEATURE_NAMES,
                    "safe_for_live_trading": False,
                },
                metrics={
                    "sample_count": len(samples),
                    "feature_count": len(FEATURE_NAMES),
                    "holdout_accuracy": metrics["holdout"]["accuracy"],
                    "holdout_auc": metrics["holdout"]["auc"],
                    "safe_for_live_trading": False,
                },
                symbol=symbol,
                timeframe=timeframe,
            ).to_dict()

        return {
            "ok": True,
            "model_type": MODEL_TYPE,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "sample_count": len(samples),
            "artifact_path": str(metadata_path),
            "artifact_sha256": artifact["artifact_sha256"],
            "model_file": str(model_path),
            "data_quality": dict(self.last_data_quality),
            "metrics": metrics,
            "explainability": artifact["explainability"],
            "capabilities": artifact["capabilities"],
            "registry_version": registry_version,
        }

    def latest_artifact_path(self) -> str:
        paths = sorted(self.artifact_dir.glob(f"{MODEL_TYPE}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return str(paths[0]) if paths else ""

    def _existing_shadow_sample_ids(self, *, artifact_path: str) -> set[str]:
        conn = self._conn()
        try:
            rows = self._execute(
                conn,
                """
                SELECT inference_id
                FROM position_quality_shadow_audit
                WHERE model_type=? AND artifact_path=?
                """,
                (MODEL_TYPE, artifact_path),
            ).fetchall()
            prefix = f"{MODEL_TYPE}:"
            sample_ids = set()
            for row in rows:
                inference_id = str(row["inference_id"] or "")
                if not inference_id.startswith(prefix):
                    continue
                sample_id = inference_id[len(prefix):].rsplit(":", 1)[0]
                if sample_id:
                    sample_ids.add(sample_id)
            return sample_ids
        finally:
            conn.close()

    def score_position_context(
        self,
        position_context: dict[str, Any],
        *,
        artifact_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Score a current position as an advisory, risk-reducing-only input.

        The model remains unable to call the broker or amend risk parameters. Callers may
        only use high exit risk to tighten an existing rule-based verdict.
        """
        dep_error = _dependency_error()
        if dep_error:
            return {"ok": False, "error": "dependency_missing", "detail": dep_error}
        import joblib
        import pandas as pd

        path = Path(str(artifact_path or self.latest_artifact_path()))
        if not path.exists():
            return {"ok": False, "error": "artifact_missing"}
        cache_key = (str(path), path.stat().st_mtime_ns)
        if self._inference_bundle_cache and self._inference_bundle_cache[:2] == cache_key:
            artifact = self._inference_bundle_cache[2]
            bundle = self._inference_bundle_cache[3]
        else:
            artifact = json.loads(path.read_text(encoding="utf-8"))
            bundle = None
        if str(artifact.get("feature_schema_version") or "") != FEATURE_SCHEMA_VERSION:
            return {"ok": False, "error": "artifact_feature_schema_not_pit_v2"}
        permission = validate_model_artifact(
            artifact, model_type=MODEL_TYPE, db_path=self.db_path,
            context={"mode": "advisory", "operation": "position_quality_demo_advisory"},
        )
        if not permission.get("ok"):
            return {"ok": False, "error": "model_permission_violation", "permission": permission}
        model_file = Path(str(artifact.get("model_file") or ""))
        if not model_file.exists():
            return {"ok": False, "error": "model_file_missing"}
        if bundle is None:
            bundle = joblib.load(model_file)
            self._inference_bundle_cache = (cache_key[0], cache_key[1], artifact, bundle)
        model = bundle["model"]
        feature_names = list(bundle.get("feature_names") or FEATURE_NAMES)
        risk = dict(position_context.get("risk") or {})
        temporal = dict(position_context.get("temporal_context") or {})
        thesis = str(risk.get("thesis_status") or "").lower()
        regime_shift = str(risk.get("regime_shift") or "").lower()
        features = {
            "mfe": _safe_float(risk.get("mfe")),
            "mae": _safe_float(risk.get("mae")),
            "giveback_ratio": _safe_float(risk.get("giveback_ratio")),
            "profit_capture_ratio": _safe_float(risk.get("profit_capture_ratio")),
            "time_in_profit": _safe_float(risk.get("time_in_profit") or risk.get("time_in_profit_seconds")),
            "holding_efficiency": _safe_float(risk.get("holding_efficiency")),
            "time_decay_score": _safe_float(risk.get("time_decay_score")),
            "holding_seconds": _safe_float(temporal.get("holding_seconds")),
            "thesis_broken": 1.0 if thesis == "broken" else 0.0,
            "thesis_weakening": 1.0 if thesis == "weakening" else 0.0,
            "regime_shift_confirmed": 1.0 if regime_shift == "confirmed" else 0.0,
        }
        frame = pd.DataFrame([[features.get(name, 0.0) for name in feature_names]], columns=feature_names)
        hold_score = float(model.predict_proba(frame)[:, 1][0])
        exit_risk = max(0.0, min(1.0, 1.0 - hold_score))
        return {
            "ok": True,
            "schema_version": "position_quality_demo_advisory.v1",
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "hold_score": round(hold_score, 8),
            "exit_risk_score": round(exit_risk, 8),
            "risk_bucket": "high_exit_risk" if exit_risk >= 0.65 else "medium_exit_risk" if exit_risk >= 0.4 else "low_exit_risk",
            "features": features,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "advisory_only": True,
            "risk_reducing_only": True,
        }

    def score_samples(
        self,
        *,
        artifact_path: str | Path | None = None,
        limit: int = 100,
        mode: str = "shadow",
        skip_existing: bool = False,
    ) -> dict[str, Any]:
        dep_error = _dependency_error()
        if dep_error:
            return {"ok": False, "error": "dependency_missing", "detail": dep_error}
        import joblib
        import pandas as pd

        path = Path(str(artifact_path or self.latest_artifact_path()))
        if not path.exists():
            return {"ok": False, "error": "artifact_missing", "artifact_path": str(path)}
        artifact = json.loads(path.read_text(encoding="utf-8"))
        permission = validate_model_artifact(
            artifact,
            model_type=MODEL_TYPE,
            db_path=self.db_path,
            context={"mode": mode, "operation": "position_quality_score_samples"},
        )
        if not permission.get("ok"):
            return {
                "ok": False,
                "error": "model_permission_violation",
                "artifact_path": str(path),
                "permission": permission,
            }
        model_file = Path(str(artifact.get("model_file") or ""))
        if not model_file.exists():
            return {"ok": False, "error": "model_file_missing", "model_file": str(model_file)}
        bundle = joblib.load(model_file)
        model = bundle["model"]
        feature_names = list(bundle.get("feature_names") or FEATURE_NAMES)
        samples = self.load_samples(limit=limit)
        if not samples:
            return {"ok": False, "error": "no_samples"}
        if skip_existing:
            artifact_ref = str(artifact.get("artifact_path") or path)
            existing = self._existing_shadow_sample_ids(artifact_path=artifact_ref)
            samples = [
                sample for sample in samples
                if str(sample.get("sample_id") or "") not in existing
            ]
            if not samples:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "no_new_samples",
                    "model_type": MODEL_TYPE,
                    "model_version": str(artifact.get("model_version") or MODEL_VERSION),
                    "artifact_path": str(path),
                    "count": 0,
                    "items": [],
                    "capabilities": {
                        "live_trading": False,
                        "advisory_only": True,
                        "shadow_only": True,
                    },
                }
        x = pd.DataFrame([item["features"] for item in samples], columns=feature_names)
        probs = model.predict_proba(x)[:, 1]
        self._ensure_audit_table()
        items = []
        for sample, prob in zip(samples, probs):
            items.append(self._persist_inference(artifact, sample, float(prob), mode=mode))
        return {
            "ok": True,
            "model_type": MODEL_TYPE,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "artifact_path": str(path),
            "count": len(items),
            "items": items,
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
            },
        }

    def _persist_inference(
        self,
        artifact: dict[str, Any],
        sample: dict[str, Any],
        hold_score: float,
        *,
        mode: str,
    ) -> dict[str, Any]:
        now = time.time()
        exit_risk = max(0.0, min(1.0, 1.0 - float(hold_score)))
        prediction = 1 if hold_score >= 0.5 else 0
        risk_bucket = "high_exit_risk" if exit_risk >= 0.65 else "medium_exit_risk" if exit_risk >= 0.4 else "low_exit_risk"
        result = {
            "schema_version": "position_quality_shadow_result.v1",
            "model_type": MODEL_TYPE,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "hold_score": round(float(hold_score), 8),
            "exit_risk_score": round(exit_risk, 8),
            "prediction": prediction,
            "prediction_label": "acceptable_position_quality" if prediction else "weak_position_quality",
            "risk_bucket": risk_bucket,
            "advice": "review_only",
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
            },
            "guardrails": list(artifact.get("guardrails") or []),
        }
        result["source_agent"] = "lightgbm_shadow_models"
        result["authority_verdict"] = AgentAuthorityRegistryService().evaluate(
            "lightgbm_shadow_models",
            "model_stage",
            "shadow_model_audit",
            requested_writes=["position_quality_shadow_audit"],
            status=mode,
            impact_level="shadow",
        )
        payload = {
            "sample_id": sample["sample_id"],
            "review_id": sample["review_id"],
            "trade_id": sample["trade_id"],
            "position_id": sample["position_id"],
            "features": sample["features"],
            "label": sample["label"],
            "pnl": sample["pnl"],
            "outcome_label": sample["outcome_label"],
            "source_agent": "lightgbm_shadow_models",
            "authority_verdict": result["authority_verdict"],
        }
        inference_id = f"{MODEL_TYPE}:{sample['sample_id']}:{int(now * 1000)}"
        conn = self._conn()
        try:
            self._execute(conn,
                """
                INSERT INTO position_quality_shadow_audit
                (inference_id, model_type, model_version, artifact_path, review_id,
                 trade_id, position_id, mode, hold_score, exit_risk_score, prediction,
                 payload_json, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inference_id,
                    MODEL_TYPE,
                    str(artifact.get("model_version") or MODEL_VERSION),
                    str(artifact.get("artifact_path") or ""),
                    sample["review_id"],
                    sample["trade_id"],
                    sample["position_id"],
                    mode,
                    float(hold_score),
                    float(exit_risk),
                    int(prediction),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "inference_id": inference_id,
            "review_id": sample["review_id"],
            "position_id": sample["position_id"],
            "hold_score": result["hold_score"],
            "exit_risk_score": result["exit_risk_score"],
            "prediction": prediction,
            "risk_bucket": risk_bucket,
            "created_at": now,
        }

    def list_audits(self, *, limit: int = 100, position_id: str | None = None) -> dict[str, Any]:
        clauses = []
        params: list[Any] = []
        if position_id:
            clauses.append("position_id=?")
            params.append(str(position_id))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._conn()
        try:
            try:
                rows = self._execute(conn,
                    f"""
                    SELECT *
                    FROM position_quality_shadow_audit
                    {where}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (*params, int(limit)),
                ).fetchall()
            except Exception as exc:
                message = str(exc).lower()
                if "no such table" not in message and "does not exist" not in message:
                    raise
                return {"items": [], "count": 0}
            items = []
            for row in rows:
                items.append(
                    {
                        "inference_id": str(row["inference_id"] or ""),
                        "model_type": str(row["model_type"] or ""),
                        "model_version": str(row["model_version"] or ""),
                        "artifact_path": str(row["artifact_path"] or ""),
                        "review_id": str(row["review_id"] or ""),
                        "trade_id": str(row["trade_id"] or ""),
                        "position_id": str(row["position_id"] or ""),
                        "mode": str(row["mode"] or ""),
                        "hold_score": _safe_float(row["hold_score"]),
                        "exit_risk_score": _safe_float(row["exit_risk_score"]),
                        "prediction": int(row["prediction"] or 0),
                        "payload": _loads(row["payload_json"], {}),
                        "result": _loads(row["result_json"], {}),
                        "created_at": _safe_float(row["created_at"]),
                    }
                )
            return {"items": items, "count": len(items)}
        finally:
            conn.close()
