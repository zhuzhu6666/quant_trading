from __future__ import annotations

import gc
import hashlib
import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    ensure_sqlite_columns,
    get_state_pg_conn,
    is_state_db_path,
    state_pg_enabled,
    state_table_columns,
    state_table_exists,
)
from backend.services.evolution_ledger import (
    ensure_evolution_columns,
    ensure_evolution_ledger_tables,
    finish_evolution_run,
    record_evolution_decision,
    start_evolution_run,
)
from backend.services.failure_taxonomy import (
    FACTOR_PENALTY_BLOCKED_RESPONSIBILITIES,
    build_failure_taxonomy,
)
from backend.services.governance_eligibility import (
    GOVERNANCE_ELIGIBILITY_VERSION,
    GovernanceEligibility,
    evaluate_governance_eligibility,
)
from backend.services.review_contract import (
    build_entry_timing_context,
    extract_decision_freshness_context,
    normalize_trade_review_contract,
    review_has_system_contamination,
)
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context
from backend.services.state_payload_archive import (
    archive_json_payload,
    load_json_payload,
    supervisor_trace_archive_text,
)
from research.features.evidence_contract import build_evidence_contract
from backend.services.supervisor_payload_contract import (
    compact_supervisor_mapping as _compact_supervisor_mapping,
)

_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()

EVENT_WINDOW_CONTEXT_SCHEMA_VERSION = "event_sizing.short_window.v2"
EVENT_WINDOW_ALLOWED_BUCKETS = {"post_0_5m", "pre_0_15m", "pre_15_30m", "pre_30_60m"}
EVENT_WINDOW_MIN_VALID_MULTIPLIER = 0.5
EXECUTABLE_GOVERNANCE_SAMPLE_TYPES = {
    "shadow_open_decision",
    "risk_rejection",
    "entry_supervisor_feedback",
    "supervisor_trajectory",
    "supervisor_execution_trace",
    "trade_review_outcome",
    "post_close_counterfactual",
}

OPEN_QUALITY_CONSUMER = "open_quality_lightgbm"
OPEN_QUALITY_CONTEXT_FIELDS = (
    "entry_cluster",
    "market_micro_context",
    "bar_context",
    "execution_context",
    "decision_quality_context",
    "event_context",
    "data_quality_context",
)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _process_memory_snapshot() -> dict[str, Any]:
    """Return a compact, best-effort Linux process memory observation."""

    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    fields = {
        "VmRSS": "rss_kib",
        "RssAnon": "anonymous_rss_kib",
        "VmSwap": "swap_kib",
    }
    snapshot: dict[str, Any] = {"observed_at": time.time()}
    for line in status.splitlines():
        key, separator, raw_value = line.partition(":")
        output_key = fields.get(key)
        if not separator or output_key is None:
            continue
        try:
            snapshot[output_key] = int(raw_value.strip().split()[0])
        except (IndexError, TypeError, ValueError):
            continue
    return snapshot if len(snapshot) > 1 else {}


def _compact_learning_cycle_stage(value: Any) -> dict[str, Any]:
    """Drop row collections and evidence JSON from the cycle-level projection."""

    if not isinstance(value, dict):
        return {"ok": True, "status": "completed"}
    status = str(value.get("status") or "completed")[:256]
    failed = any(marker in status.lower() for marker in ("fail", "error", "unavailable"))
    summary: dict[str, Any] = {
        "ok": bool(value.get("ok", not failed)),
        "status": status,
    }
    for key, item in value.items():
        if key in {"ok", "status"}:
            continue
        if key == "counts" and isinstance(item, dict):
            summary[key] = {
                str(count_key): count_value
                for count_key, count_value in item.items()
                if count_value is None or isinstance(count_value, (bool, int, float))
            }
            continue
        if item is None or isinstance(item, (bool, int, float)):
            summary[str(key)] = item
            continue
        if isinstance(item, str) and (
            key in {"schema_version", "reason", "error", "mode"}
            or key.endswith("_id")
        ):
            summary[str(key)] = item[:512]
    return summary


def _run_compact_learning_stage(
    name: str,
    operation: Callable[[], Any],
    memory_profile: list[dict[str, Any]],
) -> dict[str, Any]:
    started_at = time.time()
    before = _process_memory_snapshot()
    raw_result = operation()
    summary = _compact_learning_cycle_stage(raw_result)
    del raw_result
    gc.collect()
    memory_profile.append(
        {
            "stage": name,
            "started_at": started_at,
            "finished_at": time.time(),
            "before": before,
            "after": _process_memory_snapshot(),
        }
    )
    return summary


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = sqlite3.Row
    return conn


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


def _sample_id(sample_type: str, source_table: str, source_id: str) -> str:
    digest = hashlib.sha1(f"{sample_type}:{source_table}:{source_id}".encode("utf-8")).hexdigest()[:18]
    return f"als_{digest}"


def _sample_causal_level(sample_type: str, label_status: str, requested: Any = None) -> str:
    if label_status != "matured" and sample_type in {"supervisor_trajectory", "supervisor_execution_trace"}:
        return "observational"
    if requested:
        return str(requested)
    if sample_type == "post_close_counterfactual":
        return "counterfactual"
    return "intervention_observed"


def _sample_integrity_level(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"full", "recovered", "partial", "missing"}:
        return text
    return "missing"


def ensure_autonomous_learning_tables(db_path: str | Path = STATE_DB) -> None:
    ensure_evolution_ledger_tables(db_path)
    if _use_pg(db_path):
        return
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evolution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS autonomous_learning_sample (
                sample_id TEXT PRIMARY KEY,
                sample_type TEXT NOT NULL,
                source_table TEXT DEFAULT '',
                source_id TEXT DEFAULT '',
                decision_id TEXT DEFAULT '',
                trade_id TEXT DEFAULT '',
                position_id TEXT DEFAULT '',
                symbol TEXT DEFAULT '',
                timeframe TEXT DEFAULT '',
                event_ts REAL NOT NULL DEFAULT 0.0,
                label_status TEXT DEFAULT 'pending',
                integrity TEXT DEFAULT 'full',
                train_weight REAL DEFAULT 1.0,
                features_json TEXT DEFAULT '{}',
                verdict_json TEXT DEFAULT '{}',
                label_json TEXT DEFAULT '{}',
                trace_json TEXT DEFAULT '{}',
                evidence_contract_json TEXT DEFAULT '{}',
                content_fingerprint TEXT NOT NULL DEFAULT '',
                config_version INTEGER DEFAULT 0,
                config_hash TEXT DEFAULT '',
                evolution_run_id TEXT DEFAULT '',
                system_contaminated INTEGER NOT NULL DEFAULT 0,
                governance_eligible INTEGER NOT NULL DEFAULT 0,
                governance_effective_weight REAL NOT NULL DEFAULT 0.0,
                governance_eligibility_version TEXT NOT NULL DEFAULT '',
                governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
                governance_ineligible_reason TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        ensure_sqlite_columns(
            db_path,
            "autonomous_learning_sample",
            {"content_fingerprint": "content_fingerprint TEXT NOT NULL DEFAULT ''"},
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS position_supervisor_trace (
                trace_id TEXT PRIMARY KEY,
                decision_id TEXT DEFAULT '',
                position_id TEXT NOT NULL,
                trade_id TEXT DEFAULT '',
                symbol TEXT DEFAULT '',
                timeframe TEXT DEFAULT '',
                tick INTEGER DEFAULT 0,
                event_ts REAL NOT NULL DEFAULT 0.0,
                action TEXT DEFAULT '',
                summary_reason TEXT DEFAULT '',
                confidence REAL DEFAULT 0.0,
                template_id TEXT DEFAULT '',
                template_version TEXT DEFAULT '',
                stage TEXT DEFAULT '',
                outcome TEXT DEFAULT '',
                risk_action TEXT DEFAULT '',
                risk_allowed INTEGER DEFAULT 0,
                risk_reason TEXT DEFAULT '',
                execution_status TEXT DEFAULT '',
                execution_reason TEXT DEFAULT '',
                context_json TEXT DEFAULT '{}',
                verdict_json TEXT DEFAULT '{}',
                risk_verdict_json TEXT DEFAULT '{}',
                execution_json TEXT DEFAULT '{}',
                trace_integrity TEXT DEFAULT 'full',
                config_version INTEGER DEFAULT 0,
                config_hash TEXT DEFAULT '',
                evolution_run_id TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        cols = state_table_columns(conn, "autonomous_learning_sample")
        if "evidence_contract_json" not in cols:
            conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN evidence_contract_json TEXT DEFAULT '{}'")
        if "config_version" not in cols:
            conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN config_version INTEGER DEFAULT 0")
        if "config_hash" not in cols:
            conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN config_hash TEXT DEFAULT ''")
        if "evolution_run_id" not in cols:
            conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN evolution_run_id TEXT DEFAULT ''")
        for column, ddl in {
            "system_contaminated": "INTEGER NOT NULL DEFAULT 0",
            "governance_eligible": "INTEGER NOT NULL DEFAULT 0",
            "governance_effective_weight": "REAL NOT NULL DEFAULT 0.0",
            "governance_eligibility_version": "TEXT NOT NULL DEFAULT ''",
            "governance_eligibility_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "governance_ineligible_reason": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in cols:
                conn.execute(
                    f'ALTER TABLE autonomous_learning_sample ADD COLUMN "{column}" {ddl}'
                )
        trace_cols = state_table_columns(conn, "position_supervisor_trace")
        if "trace_integrity" not in trace_cols:
            conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN trace_integrity TEXT DEFAULT 'full'")
        if "config_version" not in trace_cols:
            conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN config_version INTEGER DEFAULT 0")
        if "config_hash" not in trace_cols:
            conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN config_hash TEXT DEFAULT ''")
        if "evolution_run_id" not in trace_cols:
            conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN evolution_run_id TEXT DEFAULT ''")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_autonomous_learning_sample_type
            ON autonomous_learning_sample(sample_type, label_status, event_ts)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_autonomous_learning_sample_source
            ON autonomous_learning_sample(sample_type, source_table, source_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_position_supervisor_trace_position_ts
            ON position_supervisor_trace(position_id, event_ts)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_position_supervisor_trace_action_outcome
            ON position_supervisor_trace(action, outcome, event_ts)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experience_pattern_stats (
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                sample_count INTEGER DEFAULT 0,
                win_count INTEGER DEFAULT 0,
                bad_loss_count INTEGER DEFAULT 0,
                avg_reward REAL DEFAULT 0.0,
                effective_sample_count REAL NOT NULL DEFAULT 0.0,
                weighted_win_count REAL NOT NULL DEFAULT 0.0,
                weighted_bad_loss_count REAL NOT NULL DEFAULT 0.0,
                weighted_avg_reward REAL NOT NULL DEFAULT 0.0,
                governance_eligibility_version TEXT NOT NULL DEFAULT '',
                governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
                last_outcome_label TEXT DEFAULT '',
                recommended_action TEXT DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0.0,
                PRIMARY KEY (scope_type, scope_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS policy_suggestion (
                suggestion_id TEXT PRIMARY KEY,
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                action TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                reason TEXT DEFAULT '',
                evidence_json TEXT DEFAULT '{}',
                status TEXT DEFAULT 'proposed',
                reviewed_at REAL DEFAULT 0.0,
                review_note TEXT DEFAULT '',
                applied_mutation_id TEXT NOT NULL DEFAULT '',
                governance_eligible INTEGER NOT NULL DEFAULT 0,
                governance_eligibility_version TEXT NOT NULL DEFAULT '',
                governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
                governance_ineligible_reason TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        stats_cols = state_table_columns(conn, "experience_pattern_stats")
        for column, ddl in {
            "effective_sample_count": "REAL NOT NULL DEFAULT 0.0",
            "weighted_win_count": "REAL NOT NULL DEFAULT 0.0",
            "weighted_bad_loss_count": "REAL NOT NULL DEFAULT 0.0",
            "weighted_avg_reward": "REAL NOT NULL DEFAULT 0.0",
            "governance_eligibility_version": "TEXT NOT NULL DEFAULT ''",
            "governance_eligibility_fingerprint": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in stats_cols:
                conn.execute(
                    f'ALTER TABLE experience_pattern_stats ADD COLUMN "{column}" {ddl}'
                )
        suggestion_cols = state_table_columns(conn, "policy_suggestion")
        for column, ddl in {
            "applied_mutation_id": "TEXT NOT NULL DEFAULT ''",
            "governance_eligible": "INTEGER NOT NULL DEFAULT 0",
            "governance_eligibility_version": "TEXT NOT NULL DEFAULT ''",
            "governance_eligibility_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "governance_ineligible_reason": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in suggestion_cols:
                conn.execute(
                    f'ALTER TABLE policy_suggestion ADD COLUMN "{column}" {ddl}'
                )
        conn.commit()
    finally:
        conn.close()
    ensure_evolution_columns(db_path)


def _sample_is_system_contaminated(item: dict[str, Any]) -> bool:
    def _contaminated(value: Any) -> bool:
        if isinstance(value, dict):
            return bool(value.get("contaminated") or value.get("contaminates_learning"))
        return bool(value)

    label = item.get("label") if isinstance(item.get("label"), dict) else {}
    verdict = item.get("verdict") if isinstance(item.get("verdict"), dict) else {}
    features = item.get("features") if isinstance(item.get("features"), dict) else {}
    return any(
        _contaminated(value)
        for value in (
            item.get("system_contaminated"),
            item.get("system_contamination"),
            label.get("system_contaminated"),
            label.get("system_contamination"),
            verdict.get("system_contaminated"),
            verdict.get("system_contamination"),
            features.get("system_contamination"),
            features.get("system_issue_context"),
        )
    )


def _open_target_blockers(item: dict[str, Any]) -> list[str]:
    """Return fail-closed blockers for the versioned open target.

    The opening model may only consume a matured financial outcome with a
    trusted execution chain.  Legacy ``outcome_label`` remains in the label
    payload for audit, but it cannot silently restore training or governance
    eligibility when the versioned target is absent or incomplete.
    """
    if str(item.get("sample_type") or "") != "shadow_open_decision":
        return []
    label = item.get("label") if isinstance(item.get("label"), dict) else {}
    if str(label.get("label") or "") != "open_outcome":
        return []
    target = label.get("open_target_v2")
    target = target if isinstance(target, dict) else {}
    blockers: list[str] = []
    if str(target.get("schema_version") or "") != "open_target.v2":
        blockers.append("missing_open_target_v2")
    financial_label = str(target.get("financial_label") or "").strip().lower()
    if financial_label not in {"profit", "loss"}:
        blockers.append("flat_or_invalid_open_outcome")
    if target.get("trainable") is not True:
        blockers.append("open_target_not_trainable")
    if str(target.get("execution_evidence_state") or "").strip().lower() not in {
        "full",
        "replay_verified",
    }:
        blockers.append("incomplete_execution_evidence")
    if bool(target.get("contaminated")):
        blockers.append("open_target_contaminated")
    return blockers


def _positive_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0.0


def _open_quality_consumer_eligibility(item: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the entry model against entry evidence, not factor attribution.

    The common evidence contract intentionally remains strict for factor and
    governance consumers.  ``open_quality_lightgbm`` only needs the entry
    decision context, a matured versioned target, and a trusted execution
    chain.  Keeping this scope explicit prevents missing outcome-factor
    attribution from blocking a sample whose open-model features are complete,
    without turning that sample into a factor-governance sample.
    """

    blockers: list[str] = []
    sample_type = str(item.get("sample_type") or "")
    label = item.get("label") if isinstance(item.get("label"), dict) else {}
    features = item.get("features") if isinstance(item.get("features"), dict) else {}
    if sample_type != "shadow_open_decision":
        return {
            "schema_version": "consumer_eligibility.v1",
            "model_ready": False,
            "allowed_uses": [],
            "blockers": ["not_open_model_sample"],
        }
    if str(label.get("label") or "") != "open_outcome":
        blockers.append("not_matured_open_outcome")
    if str(item.get("label_status") or "") != "matured":
        blockers.append("label_not_matured")
    blockers.extend(_open_target_blockers(item))
    if _sample_is_system_contaminated(item):
        blockers.append("system_contaminated")
    if not features:
        blockers.append("missing_features")
    if not item.get("trace"):
        blockers.append("missing_trace")
    if not str(item.get("config_hash") or ""):
        blockers.append("missing_config_hash")

    contexts = {
        field: features.get(field)
        for field in OPEN_QUALITY_CONTEXT_FIELDS
    }
    for field, value in contexts.items():
        if not isinstance(value, dict) or not value:
            blockers.append(f"missing_{field}")

    micro = contexts.get("market_micro_context") or {}
    if isinstance(micro, dict) and micro:
        for field in ("bid", "ask", "mid", "spread", "signal_price"):
            if not _positive_number(micro.get(field)):
                blockers.append(f"invalid_market_micro_context_{field}")
        if micro.get("quote_fresh") is not True:
            blockers.append("stale_or_unknown_entry_quote")

    bar = contexts.get("bar_context") or {}
    if isinstance(bar, dict) and bar and bar.get("complete") is not True:
        blockers.append("decision_bar_not_complete")

    execution = contexts.get("execution_context") or {}
    if isinstance(execution, dict) and execution:
        for field in ("requested_volume", "actual_api_volume", "signal_price", "fill_price"):
            if not _positive_number(execution.get(field)):
                blockers.append(f"invalid_execution_context_{field}")

    decision_quality = contexts.get("decision_quality_context") or {}
    if isinstance(decision_quality, dict) and decision_quality:
        if str(decision_quality.get("schema_version") or "") != "decision_quality_context.v1":
            blockers.append("invalid_decision_quality_schema")
        if not str(decision_quality.get("composer_version") or ""):
            blockers.append("missing_composer_version")
        if not isinstance(decision_quality.get("factor_roles"), dict) or not decision_quality.get("factor_roles"):
            blockers.append("missing_factor_roles")
        if not _positive_number(decision_quality.get("n_active_alpha_factors")):
            blockers.append("missing_active_alpha_universe")

    data_quality = contexts.get("data_quality_context") or {}
    if isinstance(data_quality, dict) and data_quality:
        if str(data_quality.get("schema_version") or "") != "entry_data_quality_context.v1":
            blockers.append("invalid_data_quality_schema")
        if data_quality.get("quote_fresh") is not True:
            blockers.append("data_quality_quote_not_fresh")

    # New captures publish this marker after the filled-open context is built.
    # Historical rows do not have it, so the structural checks above remain
    # the compatibility path for already materialized evidence.
    capture_quality = features.get("open_context_quality")
    if isinstance(capture_quality, dict) and capture_quality and capture_quality.get("ready") is not True:
        blockers.append("open_context_capture_incomplete")

    unique_blockers = list(dict.fromkeys(blockers))
    ready = not unique_blockers
    return {
        "schema_version": "consumer_eligibility.v1",
        "consumer": OPEN_QUALITY_CONSUMER,
        "model_ready": ready,
        "allowed_uses": ["supervised_training"] if ready else [],
        "blockers": unique_blockers,
    }


def _sample_lineage(item: dict[str, Any]) -> tuple[list[str], bool, bool]:
    source_table = str(item.get("source_table") or "").strip()
    source_id = str(item.get("source_id") or "").strip()
    trace = item.get("trace") if isinstance(item.get("trace"), dict) else {}
    lineage_ids: list[str] = []
    if source_table and source_id:
        lineage_ids.append(f"source:{source_table}:{source_id}")
    for key, value in sorted(trace.items()):
        if not (str(key).endswith("_id") or str(key) == "id"):
            continue
        text = str(value or "").strip()
        if text:
            lineage_ids.append(f"trace:{key}:{text}")
    complete = bool(source_table and source_id and trace and len(lineage_ids) >= 2)
    unique = bool(lineage_ids) and len(lineage_ids) == len(set(lineage_ids))
    return lineage_ids, complete, unique


def _evaluate_sample_governance_eligibility(
    *,
    item: dict[str, Any],
    sample_id: str,
    evidence_contract: dict[str, Any],
) -> GovernanceEligibility:
    lineage_ids, lineage_complete, lineage_unique = _sample_lineage(item)
    trace = item.get("trace") if isinstance(item.get("trace"), dict) else {}
    eligibility_input = {
        **item,
        "sample_id": sample_id,
        "system_contaminated": _sample_is_system_contaminated(item),
        "model_ready": bool(evidence_contract.get("model_ready")),
        "allowed_uses": list(evidence_contract.get("allowed_uses") or []),
        "evidence_contract": evidence_contract,
        "verified_recovered": bool(
            item.get("verified_recovered")
            or trace.get("verified_recovered")
        ),
        "lineage_ids": lineage_ids,
        "lineage_complete": lineage_complete,
        "lineage_unique": lineage_unique,
    }
    return evaluate_governance_eligibility(eligibility_input)


def _canonical_sample_evidence_inputs(
    item: dict[str, Any],
    *,
    stored_system_contaminated: Any = None,
) -> dict[str, Any]:
    """Normalize one sample before building its evidence and eligibility.

    Materialization and historical repair must consume the same semantic
    inputs.  In particular, executable governance is an explicit evidence
    property, not a shortcut derived only from ``sample_type``.  The stored
    contamination bit is used only as a fail-closed fallback for legacy rows;
    current JSON evidence remains the primary source.
    """
    normalized = dict(item or {})
    sample_type = str(normalized.get("sample_type") or "")
    source_table = str(normalized.get("source_table") or "")
    source_id = str(normalized.get("source_id") or "")
    sample_id = str(normalized.get("sample_id") or "")
    features = normalized.get("features")
    verdict = normalized.get("verdict")
    label = normalized.get("label")
    trace = normalized.get("trace")
    features = features if isinstance(features, dict) else {}
    verdict = verdict if isinstance(verdict, dict) else {}
    label = label if isinstance(label, dict) else {}
    trace = trace if isinstance(trace, dict) else {}
    integrity = _sample_integrity_level(normalized.get("integrity") or "missing")
    label_status = str(normalized.get("label_status") or "pending")
    train_weight = float(
        normalized.get("train_weight")
        if normalized.get("train_weight") is not None
        else 0.0
    )
    causal_level = _sample_causal_level(
        sample_type,
        label_status,
        normalized.get("causal_level"),
    )
    model_ready = (
        label_status == "matured"
        and integrity in {"full", "recovered"}
        and bool(features)
        and bool(label)
        and bool(trace)
    )

    normalized.update(
        {
            "sample_id": sample_id,
            "sample_type": sample_type,
            "source_table": source_table,
            "source_id": source_id,
            "features": features,
            "verdict": verdict,
            "label": label,
            "trace": trace,
            "integrity": integrity,
            "label_status": label_status,
            "train_weight": train_weight,
            "causal_level": causal_level,
        }
    )

    contaminated = _sample_is_system_contaminated(normalized)
    if not contaminated and stored_system_contaminated is not None:
        # A legacy row can have lost its nested contamination marker.  Never
        # turn an already-blocked stored row into an executable sample during
        # a repair pass.
        contaminated = bool(stored_system_contaminated)
    normalized["system_contaminated"] = contaminated
    model_ready = model_ready and not contaminated
    target_blockers = _open_target_blockers(normalized)
    normalized["open_target_blockers"] = target_blockers
    model_ready = model_ready and not target_blockers

    existing_contract = normalized.get("evidence_contract")
    existing_contract = existing_contract if isinstance(existing_contract, dict) else {}
    existing_quality = existing_contract.get("quality")
    existing_quality = existing_quality if isinstance(existing_quality, dict) else {}
    explicit_executable = normalized.get("executable_governance_allowed")
    if explicit_executable is None:
        explicit_executable = existing_quality.get("executable_governance_allowed")
    if explicit_executable is None:
        # Missing evidence is not permission.  Builders must provide the
        # explicit flag; repair must never recreate it from sample type.
        explicit_executable = False
    # Contamination can never advertise executable governance, even though
    # the evaluator independently fail-closes the sample.
    executable_allowed = bool(explicit_executable) and not contaminated and not target_blockers
    normalized["executable_governance_allowed"] = executable_allowed
    normalized["model_ready"] = model_ready
    normalized["quality"] = {
        "quality_score": max(0.0, min(1.0, train_weight)),
        "model_ready": model_ready,
        "executable_governance_allowed": executable_allowed,
        "missing": list(target_blockers),
    }
    return normalized


def _build_sample_evidence_contract(
    item: dict[str, Any],
    *,
    stored_system_contaminated: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], GovernanceEligibility]:
    """Build the evidence contract and eligibility from canonical inputs."""
    normalized = _canonical_sample_evidence_inputs(
        item,
        stored_system_contaminated=stored_system_contaminated,
    )
    contract = build_evidence_contract(
        sample_id=str(normalized.get("sample_id") or ""),
        sample_kind=str(normalized.get("sample_type") or ""),
        source={
            "table": str(normalized.get("source_table") or ""),
            "source_id": str(normalized.get("source_id") or ""),
        },
        features=normalized["features"],
        label=normalized["label"],
        trace=normalized["trace"],
        quality=normalized["quality"],
        integrity=str(normalized.get("integrity") or "missing"),
        causal_level=str(normalized.get("causal_level") or "observational"),
        label_status=str(normalized.get("label_status") or "pending"),
        explanation={"verdict": normalized["verdict"]},
    )
    contract_blockers = list(normalized.get("open_target_blockers") or [])
    if normalized.get("system_contaminated"):
        contract_blockers.append("system_contaminated")
    if contract_blockers:
        # Keep the v1 contract shape, but remove strong uses from contaminated
        # or otherwise invalid open evidence so health/readiness cannot
        # describe it as trainable even before the eligibility columns are
        # inspected.
        strong_uses = {
            "supervised_training",
            "strong_governance",
            "executable_governance",
        }
        contract["allowed_uses"] = [
            use for use in list(contract.get("allowed_uses") or []) if use not in strong_uses
        ]
        blockers = list(contract.get("blockers") or [])
        for blocker in contract_blockers:
            if blocker not in blockers:
                blockers.append(blocker)
        contract["blockers"] = blockers
        contract["quality"]["model_ready"] = False
        contract["quality"]["executable_governance_allowed"] = False
        contract["model_ready"] = False
    eligibility = _evaluate_sample_governance_eligibility(
        item=normalized,
        sample_id=str(normalized.get("sample_id") or ""),
        evidence_contract=contract,
    )
    contract["governance_eligibility"] = eligibility.to_dict()
    if str(normalized.get("sample_type") or "") == "shadow_open_decision":
        contract.setdefault("consumer_eligibility", {})[OPEN_QUALITY_CONSUMER] = (
            _open_quality_consumer_eligibility(normalized)
        )
    return normalized, contract, eligibility


def _upsert_sample(conn, item: dict[str, Any]) -> bool:
    now = time.time()
    sample_type = str(item.get("sample_type") or "")
    source_table = str(item.get("source_table") or "")
    source_id = str(item.get("source_id") or "")
    if not sample_type or not source_table or not source_id:
        return False
    sample_id = str(item.get("sample_id") or _sample_id(sample_type, source_table, source_id))
    normalized_item = {
        **item,
        "sample_id": sample_id,
        "sample_type": sample_type,
        "source_table": source_table,
        "source_id": source_id,
        "integrity": item.get("integrity") or "full",
        "train_weight": (
            item.get("train_weight")
            if item.get("train_weight") is not None
            else 1.0
        ),
    }
    normalized_item, evidence_contract, eligibility = _build_sample_evidence_contract(
        normalized_item,
    )
    features = normalized_item["features"]
    verdict = normalized_item["verdict"]
    label = normalized_item["label"]
    trace = normalized_item["trace"]
    integrity = normalized_item["integrity"]
    label_status = normalized_item["label_status"]
    train_weight = normalized_item["train_weight"]
    causal_level = normalized_item["causal_level"]
    snapshot = item.get("runtime_config") or {}
    config_version = int(item.get("config_version") or (snapshot or {}).get("config_version") or 0)
    config_hash = str(item.get("config_hash") or (snapshot or {}).get("config_hash") or "")
    evolution_run_id = str(item.get("evolution_run_id") or "")
    existing = _execute(
        conn,
        """
        SELECT label_status, content_fingerprint
        FROM autonomous_learning_sample
        WHERE sample_id=?
        LIMIT 1
        """,
        (sample_id,),
    ).fetchone()
    if existing is not None:
        try:
            existing_label_status = str(existing["label_status"] or "")
            existing_fingerprint = str(existing["content_fingerprint"] or "")
        except Exception:
            existing_label_status = str(existing[0] or "")
            existing_fingerprint = str(existing[1] or "") if len(existing) > 1 else ""
        if existing_label_status == "matured" and label_status != "matured":
            return False
    row_payload = {
        "sample_id": sample_id,
        "sample_type": sample_type,
        "source_table": source_table,
        "source_id": source_id,
        "decision_id": str(item.get("decision_id") or ""),
        "trade_id": str(item.get("trade_id") or ""),
        "position_id": str(item.get("position_id") or ""),
        "symbol": str(item.get("symbol") or ""),
        "timeframe": str(item.get("timeframe") or ""),
        "event_ts": float(item.get("event_ts") or 0.0),
        "label_status": label_status,
        "integrity": integrity,
        "train_weight": train_weight,
        "features_json": _dumps(features),
        "verdict_json": _dumps(verdict),
        "label_json": _dumps(label),
        "trace_json": _dumps(trace),
        "evidence_contract_json": _dumps(evidence_contract),
        "config_version": config_version,
        "config_hash": config_hash,
        "evolution_run_id": evolution_run_id,
        "system_contaminated": 0 if eligibility.uncontaminated else 1,
        "governance_eligible": 1 if eligibility.eligible else 0,
        "governance_effective_weight": float(eligibility.effective_weight),
        "governance_eligibility_version": eligibility.eligibility_version,
        "governance_eligibility_fingerprint": eligibility.eligibility_fingerprint,
        "governance_ineligible_reason": ";".join(eligibility.exclusion_reasons),
        "created_at": now,
        "updated_at": now,
    }
    fingerprint_payload = {
        key: value
        for key, value in row_payload.items()
        if key not in {"created_at", "updated_at"}
    }
    content_fingerprint = hashlib.sha256(
        _dumps(fingerprint_payload).encode("utf-8")
    ).hexdigest()
    if existing is not None and existing_fingerprint and existing_fingerprint == content_fingerprint:
        return False
    row_payload["content_fingerprint"] = content_fingerprint
    cur = _execute(
        conn,
        """
        INSERT INTO autonomous_learning_sample
        (sample_id, sample_type, source_table, source_id, decision_id, trade_id,
         position_id, symbol, timeframe, event_ts, label_status, integrity,
         train_weight, features_json, verdict_json, label_json, trace_json,
         evidence_contract_json, content_fingerprint, config_version, config_hash, evolution_run_id,
         system_contaminated, governance_eligible, governance_effective_weight,
         governance_eligibility_version, governance_eligibility_fingerprint,
         governance_ineligible_reason, created_at, updated_at)
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(sample_id) DO UPDATE SET
            decision_id=excluded.decision_id,
            trade_id=excluded.trade_id,
            position_id=excluded.position_id,
            symbol=excluded.symbol,
            timeframe=excluded.timeframe,
            event_ts=excluded.event_ts,
            label_status=excluded.label_status,
            integrity=excluded.integrity,
            train_weight=excluded.train_weight,
            features_json=excluded.features_json,
            verdict_json=excluded.verdict_json,
            label_json=excluded.label_json,
            trace_json=excluded.trace_json,
            evidence_contract_json=excluded.evidence_contract_json,
            content_fingerprint=excluded.content_fingerprint,
            config_version=excluded.config_version,
            config_hash=excluded.config_hash,
            evolution_run_id=excluded.evolution_run_id,
            system_contaminated=excluded.system_contaminated,
            governance_eligible=excluded.governance_eligible,
            governance_effective_weight=excluded.governance_effective_weight,
            governance_eligibility_version=excluded.governance_eligibility_version,
            governance_eligibility_fingerprint=excluded.governance_eligibility_fingerprint,
            governance_ineligible_reason=excluded.governance_ineligible_reason,
            updated_at=excluded.updated_at
        """,
        tuple(row_payload[k] for k in (
            "sample_id",
            "sample_type",
            "source_table",
            "source_id",
            "decision_id",
            "trade_id",
            "position_id",
            "symbol",
            "timeframe",
            "event_ts",
            "label_status",
            "integrity",
            "train_weight",
            "features_json",
            "verdict_json",
            "label_json",
            "trace_json",
            "evidence_contract_json",
            "content_fingerprint",
            "config_version",
            "config_hash",
            "evolution_run_id",
            "system_contaminated",
            "governance_eligible",
            "governance_effective_weight",
            "governance_eligibility_version",
            "governance_eligibility_fingerprint",
            "governance_ineligible_reason",
            "created_at",
            "updated_at",
        )),
    )
    changed = getattr(cur, "rowcount", 0) != 0
    if changed:
        final = _execute(
            conn,
            "SELECT * FROM autonomous_learning_sample WHERE sample_id=?",
            (sample_id,),
        ).fetchone()
    return changed


def _insert_evolution_event(conn, event_type: str, payload: dict[str, Any]) -> None:
    now = time.time()
    payload_json = _dumps(payload)
    _execute(
        conn,
        """
        INSERT INTO evolution_events (timestamp, event_type, payload_json)
        VALUES (?, ?, ?)
        """,
        (now, event_type, payload_json),
    )
def _autonomy_mode() -> str:
    try:
        from config.runtime_config import shared as runtime_config

        cfg = runtime_config()
        if not bool(getattr(cfg, "autonomy_demo_auto_apply", True)):
            return "manual"
        return str(getattr(cfg, "autonomy_mode", "") or "manual")
    except Exception:
        return "manual"


def _demo_autonomous_enabled() -> bool:
    return _autonomy_mode() in {"demo_autonomous", "demo_nursery"}


def _new_experiment_id(prefix: str = "demoauto") -> str:
    return f"{prefix}_{int(time.time())}_{hashlib.sha1(str(time.time()).encode('utf-8')).hexdigest()[:8]}"


def _risk_rejection_label(action_json: dict[str, Any]) -> tuple[str, float]:
    skip_stage = str(action_json.get("skip_stage") or "")
    market_session = action_json.get("market_session") or {}
    session_status = str(market_session.get("status") or "")
    if skip_stage == "market_session":
        if session_status in {"closed_confirmed", "closed_pending_confirmation", "quote_stale"}:
            return "invalid", 0.0
        return "matured", 0.25
    return "matured", 1.0


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


def _review_for_open_decision(conn: Any, row: Any) -> Any | None:
    decision_id = str(_row_value(row, "decision_id", "") or "")
    position_id = str(_row_value(row, "position_id", "") or "")
    if not decision_id and not position_id:
        return None
    return _execute(
        conn,
        """
        SELECT *
        FROM trade_outcome_review
        WHERE (? <> '' AND entry_decision_id=?)
           OR (? <> '' AND position_id=?)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (decision_id, decision_id, position_id, position_id),
    ).fetchone()


def _review_integrity_for_training(review_json: dict[str, Any]) -> tuple[str, float]:
    integrity = _sample_integrity_level(
        review_json.get("attribution_integrity")
        or review_json.get("context_integrity")
        or "missing"
    )
    if integrity == "missing":
        return integrity, 0.0
    if review_has_system_contamination(review_json):
        return "partial", 0.25
    if integrity in {"partial", "recovered"}:
        return integrity, 0.5
    return integrity, 1.0


def _review_system_contamination(review_json: dict[str, Any]) -> dict[str, Any]:
    system_issue = (
        review_json.get("system_issue_context")
        if isinstance(review_json.get("system_issue_context"), dict)
        else {}
    )
    labels = list(system_issue.get("labels") or [])
    contaminated = bool(system_issue.get("contaminates_learning")) or review_has_system_contamination(review_json)
    return {
        "schema_version": "learning_system_contamination.v1",
        "contaminated": contaminated,
        "labels": labels,
        "primary_responsibility": str(system_issue.get("primary_responsibility") or ""),
        "recommended_use": "ops_quality_audit" if contaminated else "standard_learning",
    }


def _open_target_v2(
    *,
    review_json: dict[str, Any],
    outcome_label: str,
    pnl: float,
) -> dict[str, Any]:
    """Build the versioned financial target consumed by the open model.

    The legacy outcome label remains unchanged for audit.  The new target is
    explicitly financial: profit is positive, loss and flat are not.  A
    missing/partial execution chain is retained as evidence but cannot be a
    supervised open sample.
    """

    review = review_json if isinstance(review_json, dict) else {}
    execution = review.get("execution_quality_evidence")
    execution = execution if isinstance(execution, dict) else {}
    execution_state = str(
        review.get("execution_quality_state")
        or execution.get("evidence_state")
        or "unknown"
    ).strip().lower()
    execution_evidence_valid = (
        str(execution.get("schema_version") or "") == "execution_quality_evidence.v2"
        and str(execution.get("evidence_state") or "").strip().lower() == execution_state
    )
    financial_label = "profit" if float(pnl or 0.0) > 0.0 else "loss" if float(pnl or 0.0) < 0.0 else "flat"
    contaminated = _review_system_contamination(review).get("contaminated", False)
    trainable = (
        financial_label in {"profit", "loss"}
        and execution_evidence_valid
        and execution_state in {"full", "replay_verified"}
        and not bool(contaminated)
    )
    return {
        "schema_version": "open_target.v2",
        "objective": "profitable_open_outcome",
        "financial_label": financial_label,
        "legacy_outcome_label": str(outcome_label or ""),
        "execution_evidence_state": execution_state,
        "contaminated": bool(contaminated),
        "trainable": trainable,
    }


def _review_archive_select(
    conn: Any,
    *,
    alias: str = "r",
    output: str = "review_archive_hash",
) -> str:
    if "review_archive_hash" not in state_table_columns(conn, "trade_outcome_review"):
        return ""
    return f", {alias}.review_archive_hash AS {output}"


def _review_payload_value(
    conn: Any,
    row: Any,
    *,
    inline_key: str = "review_json",
    archive_key: str = "review_archive_hash",
    source_id_key: str = "review_id",
) -> dict[str, Any]:
    payload = load_json_payload(
        conn,
        source_table="trade_outcome_review",
        source_id=str(_row_value(row, source_id_key, "") or ""),
        inline_json=_row_value(row, inline_key, "{}"),
        archive_hash=_row_value(row, archive_key, ""),
        default={},
    )
    return payload if isinstance(payload, dict) else {}


def _counterfactual_source_is_clean(row: Any, conn: Any | None = None) -> bool:
    source_review_id = str(_row_value(row, "source_review_id", "") or "")
    if not source_review_id:
        return False
    source_review = (
        _review_payload_value(
            conn,
            row,
            inline_key="source_review_json",
            archive_key="source_review_archive_hash",
            source_id_key="source_review_id",
        )
        if conn is not None
        else _loads(_row_value(row, "source_review_json", ""), {})
    )
    if review_has_system_contamination(source_review):
        return False
    evidence = _loads(_row_value(row, "evidence_json", ""), {})
    return not bool(evidence.get("evidence_invalidated"))


def _sample_from_decision(
    row: Any,
    sample_type: str,
    *,
    outcome_review: Any | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    action_json = _loads(row["action_json"], {})
    risk_state = _loads(row["risk_state_json"], {})
    portfolio = _loads(row["portfolio_state_json"], {})
    risk_verdict = (
        risk_state.get("policy_verdict")
        or action_json.get("risk_verdict")
        or {}
    )
    label_status = "pending"
    train_weight = 0.5
    review_backed_sample = (
        outcome_review is not None
        and sample_type in {"shadow_open_decision", "supervisor_trajectory"}
    )
    outcome_review_json = (
        _review_payload_value(conn, outcome_review)
        if review_backed_sample and conn is not None
        else (_loads(outcome_review["review_json"], {}) if review_backed_sample else {})
    )
    outcome_contamination = _review_system_contamination(outcome_review_json)
    label = {
        "event_type": str(row["event_type"] or ""),
        "action_reason": str(row["action_reason"] or ""),
    }
    if sample_type == "risk_rejection":
        label_status, train_weight = _risk_rejection_label(action_json)
        label.update(
            {
                "label": "rejected_open",
                "skip_stage": str(action_json.get("skip_stage") or ""),
                "allowed": False,
            }
        )
    elif sample_type == "shadow_open_decision":
        if str(row["event_type"] or "") == "open":
            label["label"] = "opened"
            train_weight = 0.7
            review_json = {}
            contamination = _review_system_contamination(review_json)
            if outcome_review is not None:
                review_json = outcome_review_json
                contamination = outcome_contamination
                integrity, train_weight = _review_integrity_for_training(review_json)
                label_status = "matured"
                label.update(
                    {
                        "label": "open_outcome",
                        "review_id": str(outcome_review["review_id"] or ""),
                        "outcome_label": str(outcome_review["outcome_label"] or ""),
                        "pnl": float(outcome_review["pnl"] or 0.0),
                        "mae": float(outcome_review["mae"] or 0.0),
                        "mfe": float(outcome_review["mfe"] or 0.0),
                        "close_reason": str(review_json.get("close_reason") or ""),
                        "primary_responsibility": str(review_json.get("primary_responsibility") or ""),
                        "responsibility_labels": list(review_json.get("responsibility_labels", []) or []),
                        "failure_tags": _loads(outcome_review["failure_tags_json"], []),
                        "close_ts": float(review_json.get("close_ts") or outcome_review["created_at"] or 0.0),
                        "system_contamination": contamination,
                        "open_target_v2": _open_target_v2(
                            review_json=review_json,
                            outcome_label=str(outcome_review["outcome_label"] or ""),
                            pnl=float(outcome_review["pnl"] or 0.0),
                        ),
                    }
                )
        else:
            label["label"] = "not_opened"
            train_weight = 0.35
    elif sample_type == "supervisor_trajectory":
        verdict = action_json.get("supervisor_verdict") or {}
        label["label"] = str(verdict.get("action") or row["event_type"] or "")
        label["summary_reason"] = str(verdict.get("summary_reason") or row["action_reason"] or "")
        train_weight = 0.6
        if outcome_review is not None:
            review_json = outcome_review_json
            integrity, review_weight = _review_integrity_for_training(review_json)
            label_status = "matured" if integrity != "missing" else "pending"
            train_weight = min(train_weight, review_weight)
            label.update(
                {
                    "review_id": str(outcome_review["review_id"] or ""),
                    "outcome_label": str(outcome_review["outcome_label"] or ""),
                    "pnl": float(outcome_review["pnl"] or 0.0),
                    "mae": float(outcome_review["mae"] or 0.0),
                    "mfe": float(outcome_review["mfe"] or 0.0),
                    "primary_responsibility": str(review_json.get("primary_responsibility") or ""),
                    "close_ts": float(review_json.get("close_ts") or outcome_review["created_at"] or 0.0),
                    "system_contamination": outcome_contamination,
                }
            )
    return {
        "sample_type": sample_type,
        "source_table": "decision_ledger",
        "source_id": str(row["decision_id"] or ""),
        "decision_id": str(row["decision_id"] or ""),
        "trade_id": str(row["trade_id"] or ""),
        "position_id": str(row["position_id"] or ""),
        "symbol": str(row["symbol"] or ""),
        "timeframe": str(row["timeframe"] or ""),
        "event_ts": float(row["decision_ts"] or row["created_at"] or 0.0),
        "label_status": label_status,
        "executable_governance_allowed": sample_type in EXECUTABLE_GOVERNANCE_SAMPLE_TYPES,
        "integrity": (
            _review_integrity_for_training(outcome_review_json)[0]
            if review_backed_sample
            else ("full" if risk_verdict or action_json else "partial")
        ),
        "train_weight": train_weight,
        "causal_level": "intervention_observed",
        "features": {
            "portfolio_state": portfolio,
            "risk_state": risk_state,
            "action": action_json,
            "regime_id": str(row["regime_id"] or ""),
            "regime_confidence": float(row["regime_confidence"] or 0.0),
            "action_score": float(row["action_score"] or 0.0),
            "entry_cluster": action_json.get("entry_cluster") or {},
            "portfolio_exposure": action_json.get("portfolio_exposure") or {},
            "market_micro_context": action_json.get("market_micro_context") or {},
            "bar_context": action_json.get("bar_context") or {},
            "event_context": action_json.get("event_context") or action_json.get("event_sizing") or {},
            "execution_context": action_json.get("execution_context") or {},
            "sizing_trace": action_json.get("sizing_trace") or {},
            "data_quality_context": action_json.get("data_quality_context") or {},
            "decision_freshness_context": action_json.get("decision_freshness") or {},
            "entry_timing_context": action_json.get("entry_timing_context") or {},
            "decision_quality_context": action_json.get("decision_quality_context") or {},
            "market_session": action_json.get("market_session") or {},
            "open_context_quality": action_json.get("open_context_quality") or {},
        },
        "verdict": {
            "risk_verdict": risk_verdict,
            "event_type": str(row["event_type"] or ""),
            "outcome_review_id": (
                str(outcome_review["review_id"] or "")
                if review_backed_sample
                else ""
            ),
            "system_contamination": (
                outcome_contamination
                if review_backed_sample
                else {"schema_version": "learning_system_contamination.v1", "contaminated": False}
            ),
        },
        "label": label,
        "trace": {
            "decision_id": str(row["decision_id"] or ""),
            "position_id": str(row["position_id"] or ""),
            "trade_id": str(row["trade_id"] or ""),
            "review_id": (
                str(outcome_review["review_id"] or "")
                if review_backed_sample
                else ""
            ),
        },
    }


def _sample_from_review(row: Any, *, conn: Any | None = None) -> dict[str, Any]:
    review = _review_payload_value(conn, row) if conn is not None else _loads(row["review_json"], {})
    integrity, train_weight = _review_integrity_for_training(review)
    contamination = _review_system_contamination(review)
    return {
        "sample_type": "trade_review_outcome",
        "source_table": "trade_outcome_review",
        "source_id": str(row["review_id"] or ""),
        "decision_id": str(row["exit_decision_id"] or row["entry_decision_id"] or ""),
        "trade_id": str(row["trade_id"] or ""),
        "position_id": str(row["position_id"] or ""),
        "symbol": str(review.get("symbol") or ""),
        "timeframe": str(review.get("timeframe") or ""),
        "event_ts": float(review.get("close_ts") or row["created_at"] or 0.0),
        "label_status": "matured",
        "executable_governance_allowed": True,
        "integrity": integrity,
        "train_weight": train_weight,
        "causal_level": "intervention_observed",
        "features": {
            "entry_quality": float(row["entry_quality"] or 0.0),
            "hold_quality": float(row["hold_quality"] or 0.0),
            "exit_quality": float(row["exit_quality"] or 0.0),
            "regime_fit_score": float(row["regime_fit_score"] or 0.0),
            "execution_quality": float(row["execution_quality"] or 0.0),
            "mae": float(row["mae"] or 0.0),
            "mfe": float(row["mfe"] or 0.0),
            "review": review,
            "entry_timing_context": review.get("entry_timing_context") or {},
            "decision_freshness_context": review.get("decision_freshness_context") or {},
            "system_issue_context": review.get("system_issue_context") or {},
        },
        "verdict": {
            "close_reason_source": review.get("close_reason_source") or "",
            "inferred_close_supervisor": review.get("inferred_close_supervisor") or {},
            "system_contamination": contamination,
        },
        "label": {
            "outcome_label": str(row["outcome_label"] or ""),
            "pnl": float(row["pnl"] or 0.0),
            "failure_tags": _loads(row["failure_tags_json"], []),
            "system_contaminated": contamination["contaminated"],
            "open_target_v2": _open_target_v2(
                review_json=review,
                outcome_label=str(row["outcome_label"] or ""),
                pnl=float(row["pnl"] or 0.0),
            ),
        },
        "trace": {
            "review_id": str(row["review_id"] or ""),
            "entry_decision_id": str(row["entry_decision_id"] or ""),
            "exit_decision_id": str(row["exit_decision_id"] or ""),
            "position_id": str(row["position_id"] or ""),
        },
    }


def _sample_from_counterfactual(row: Any, *, conn: Any | None = None) -> dict[str, Any]:
    label = str(row["label"] or "")
    confidence = float(row["confidence"] or 0.0)
    evidence = _loads(row["evidence_json"], {})
    source_review_id = str(_row_value(row, "source_review_id", "") or "")
    if source_review_id:
        source_contamination = _review_system_contamination(
            _review_payload_value(
                conn,
                row,
                inline_key="source_review_json",
                archive_key="source_review_archive_hash",
                source_id_key="source_review_id",
            )
            if conn is not None
            else _loads(_row_value(row, "source_review_json", ""), {})
        )
    else:
        source_contamination = {
            "schema_version": "learning_system_contamination.v1",
            "contaminated": True,
            "reason": "canonical_source_review_missing",
        }
    if bool(evidence.get("evidence_invalidated")):
        source_contamination = {
            "schema_version": "learning_system_contamination.v1",
            "contaminated": True,
            "reason": str(evidence.get("invalidation_reason") or "evidence_invalidated"),
        }
    contaminated = bool(source_contamination.get("contaminated"))
    maturity = evidence.get("maturity") or {}
    maturity_status = str(maturity.get("status") or "")
    governance_eligible = bool(maturity.get("governance_eligible")) and not contaminated
    label_status = "matured" if governance_eligible else (
        "partially_matured" if maturity_status == "partially_matured" else "pending"
    )
    if label in {"", "insufficient_future_data"} and confidence <= 0.25:
        label_status = "pending" if maturity_status in {"", "pending"} else label_status
    return {
        "sample_type": "post_close_counterfactual",
        "source_table": "supervisor_counterfactual_review",
        "source_id": str(row["counterfactual_id"] or ""),
        "trade_id": str(row["trade_id"] or ""),
        "position_id": str(row["position_id"] or ""),
        "event_ts": float(row["close_ts"] or row["updated_at"] or 0.0),
        "label_status": label_status,
        "executable_governance_allowed": not contaminated,
        "integrity": "full" if label_status == "matured" and not contaminated else "partial",
        "train_weight": 0.0 if contaminated else max(0.0, min(1.0, confidence)),
        "causal_level": "counterfactual",
        "features": {
            "close_reason": str(row["close_reason"] or ""),
            "supervisor_event_type": str(row["supervisor_event_type"] or ""),
            "supervisor_reason": str(row["supervisor_reason"] or ""),
            "horizons": _loads(row["horizons_json"], []),
            "evidence": evidence,
            "maturity": maturity,
        },
        "verdict": {
            "counterfactual_label": label,
            "confidence": confidence,
            "system_contamination": source_contamination,
        },
        "label": {
            "label": label,
            "confidence": confidence,
        },
        "trace": {
            "counterfactual_id": str(row["counterfactual_id"] or ""),
            "review_id": str(row["review_id"] or ""),
            "position_id": str(row["position_id"] or ""),
        },
    }


def _sample_from_entry_supervisor_feedback(
    row: Any,
    *,
    conn: Any | None = None,
) -> dict[str, Any] | None:
    review = _review_payload_value(conn, row) if conn is not None else _loads(row["review_json"], {})
    inferred = review.get("inferred_close_supervisor") or {}
    close_reason_source = str(review.get("close_reason_source") or "")
    event_type = str(inferred.get("event_type") or "")
    action = str(inferred.get("action") or "")
    reason = str(inferred.get("summary_reason") or inferred.get("action_reason") or review.get("close_reason") or "")
    evidence = inferred.get("evidence") or {}
    has_feedback = bool(
        close_reason_source.startswith("supervisor")
        or event_type.startswith("supervisor_")
        or action in {"tighten", "reduce", "close"}
    )
    if not has_feedback:
        return None
    thesis_status = str(
        review.get("thesis_status_at_exit")
        or review.get("thesis_status")
        or evidence.get("thesis_status")
        or ""
    )
    pnl = float(row["pnl"] or review.get("pnl") or 0.0)
    context_integrity = str(review.get("context_integrity") or "full")
    attribution_integrity = str(review.get("attribution_integrity") or "full")
    contamination = _review_system_contamination(review)
    thesis_broken = bool(
        thesis_status == "broken"
        or reason == "thesis_broken"
        or str(review.get("close_reason") or "") == "thesis_broken"
    )
    entry_failure = bool(thesis_broken and pnl <= 0)
    label_status = "matured" if context_integrity == "full" and attribution_integrity != "missing" else "pending"
    if contamination["contaminated"]:
        label = "entry_feedback_system_contaminated"
        recommended_action = "review_data_pipeline"
        train_weight = 0.2
    elif entry_failure:
        label = "entry_thesis_broken"
        recommended_action = "downweight_entry_factor"
        train_weight = 0.82
    elif thesis_broken:
        label = "entry_thesis_broken_watch"
        recommended_action = "watch"
        train_weight = 0.55
    else:
        label = "supervisor_feedback_observed"
        recommended_action = "watch"
        train_weight = 0.35
    if label_status != "matured":
        train_weight *= 0.5
    sample_integrity = (
        "partial"
        if contamination["contaminated"]
        else ("full" if label_status == "matured" else "partial")
    )
    review_id = str(row["review_id"] or "")
    return {
        "sample_type": "entry_supervisor_feedback",
        "source_table": "trade_outcome_review",
        "source_id": review_id,
        "decision_id": str(row["entry_decision_id"] or review.get("entry_decision_id") or ""),
        "trade_id": str(row["trade_id"] or review.get("trade_id") or ""),
        "position_id": str(row["position_id"] or review.get("position_id") or ""),
        "symbol": str(review.get("symbol") or "XAUUSD"),
        "timeframe": str(review.get("timeframe") or ""),
        "event_ts": float(review.get("close_ts") or row["created_at"] or 0.0),
        "label_status": label_status,
        "executable_governance_allowed": True,
        "integrity": sample_integrity,
        "train_weight": round(max(0.0, min(1.0, train_weight)), 6),
        "causal_level": "post_trade_feedback",
        "features": {
            "feedback_target": "entry_agent",
            "entry_score": review.get("entry_score"),
            "top_weight_factor": review.get("top_weight_factor") or "",
            "top_factor": review.get("top_factor") or "",
            "worst_factor": review.get("worst_factor") or "",
            "factor_contributions": review.get("factor_contributions") or {},
            "primary_responsibility": review.get("primary_responsibility") or "",
            "responsibility_labels": review.get("responsibility_labels") or [],
            "close_reason": review.get("close_reason") or "",
            "close_reason_source": close_reason_source,
            "pnl": pnl,
            "mfe": review.get("mfe"),
            "mae": review.get("mae"),
            "holding_seconds": review.get("holding_seconds"),
            "thesis_status_at_exit": thesis_status,
            "supervisor": {
                "event_type": event_type,
                "action": action,
                "reason": reason,
                "evidence": evidence,
                "raw": inferred,
            },
            "system_contamination": contamination,
        },
        "verdict": {
            "feedback_target": "entry_agent",
            "supervisor_feedback": True,
            "entry_failure": entry_failure,
            "thesis_broken": thesis_broken,
            "recommended_action": recommended_action,
            "system_contamination": contamination,
        },
        "label": {
            "label": label,
            "recommended_action": recommended_action,
            "pnl": pnl,
        },
        "trace": {
            "review_id": review_id,
            "entry_decision_id": str(row["entry_decision_id"] or review.get("entry_decision_id") or ""),
            "exit_decision_id": str(row["exit_decision_id"] or review.get("exit_decision_id") or ""),
            "position_id": str(row["position_id"] or review.get("position_id") or ""),
        },
    }


def _sample_from_supervisor_trace(
    row: Any,
    *,
    source_review_row: Any | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    verdict = _loads(row["verdict_json"], {})
    context = _loads(row["context_json"], {})
    risk_verdict = _loads(row["risk_verdict_json"], {})
    execution = _loads(row["execution_json"], {})
    review_row = source_review_row if source_review_row is not None else row
    source_review_id = str(_row_value(review_row, "source_review_id", "") or "")
    if source_review_id:
        source_contamination = _review_system_contamination(
            _review_payload_value(
                conn,
                review_row,
                inline_key="source_review_json",
                archive_key="source_review_archive_hash",
                source_id_key="source_review_id",
            )
            if conn is not None
            else _loads(_row_value(review_row, "source_review_json", ""), {})
        )
    else:
        source_contamination = {
            "schema_version": "learning_system_contamination.v1",
            "contaminated": True,
            "reason": "canonical_source_review_missing",
        }
    contaminated = bool(source_contamination.get("contaminated"))
    outcome = str(row["outcome"] or "")
    execution_status = str(row["execution_status"] or "")
    label_status = "pending"
    train_weight = 0.35
    if outcome in {"blocked", "skipped", "failed"}:
        train_weight = 0.45
    if outcome == "hold":
        train_weight = 0.25
    return {
        "sample_type": "supervisor_execution_trace",
        "source_table": "position_supervisor_trace",
        "source_id": str(row["trace_id"] or ""),
        "decision_id": str(row["decision_id"] or ""),
        "trade_id": str(row["trade_id"] or ""),
        "position_id": str(row["position_id"] or ""),
        "symbol": str(row["symbol"] or ""),
        "timeframe": str(row["timeframe"] or ""),
        "event_ts": float(row["event_ts"] or row["created_at"] or 0.0),
        "label_status": label_status,
        "executable_governance_allowed": not contaminated,
        "integrity": "full" if verdict and not contaminated else "partial",
        "train_weight": 0.0 if contaminated else train_weight,
        "causal_level": "intervention_observed",
        "features": {
            "context": context,
            "verdict": verdict,
            "risk_verdict": risk_verdict,
            "execution": execution,
            "action": str(row["action"] or ""),
            "summary_reason": str(row["summary_reason"] or ""),
            "confidence": float(row["confidence"] or 0.0),
            "template_id": str(row["template_id"] or ""),
            "template_version": str(row["template_version"] or ""),
            "stage": str(row["stage"] or ""),
            "outcome": outcome,
            "risk_action": str(row["risk_action"] or ""),
            "risk_allowed": bool(row["risk_allowed"]),
            "risk_reason": str(row["risk_reason"] or ""),
            "execution_status": execution_status,
            "system_contamination": source_contamination,
            "execution_reason": str(row["execution_reason"] or ""),
        },
        "verdict": {
            "supervisor_action": str(row["action"] or ""),
            "summary_reason": str(row["summary_reason"] or ""),
            "risk_allowed": bool(row["risk_allowed"]),
            "execution_status": execution_status,
        },
        "label": {
            "label": outcome or execution_status or str(row["action"] or ""),
            "stage": str(row["stage"] or ""),
            "execution_status": execution_status,
        },
        "trace": {
            "trace_id": str(row["trace_id"] or ""),
            "decision_id": str(row["decision_id"] or ""),
            "position_id": str(row["position_id"] or ""),
            "trade_id": str(row["trade_id"] or ""),
            "source_review_id": source_review_id,
        },
    }


def _supervisor_label_from_counterfactual(label: str) -> tuple[str, str, str, float]:
    key = str(label or "").strip()
    if key in {"protection_too_tight", "premature_tighten", "noise_stopout"}:
        return "matured", "over_protected", "less_tighten", 0.85
    if key in {"sl_too_tight", "tp_too_near", "missed_extension"}:
        return "matured", key, "less_tighten", 0.85
    if key == "correct_stop":
        return "matured", "correct_action", "close", 0.9
    if key in {"entry_failure_or_correct_stop"}:
        return "matured", "correct_action", "hold", 0.65
    if key in {"missed_protection", "sl_too_loose", "tp_too_far", "mfe_capture_failed"}:
        return "matured", key, "tighten", 0.8
    if key in {"profit_protected"}:
        return "matured", "profit_protected", "keep", 0.8
    return "pending", "inconclusive", "hold", 0.2


def _dynamic_tpsl_labels(base: dict[str, Any], cf_label: str) -> list[str]:
    features = base.get("features") or {}
    verdict = features.get("verdict") or {}
    evidence = verdict.get("evidence") or features.get("supervisor_evidence") or {}
    execution = features.get("execution") or {}
    controls = verdict.get("recommended_controls") or {}
    action = str(features.get("action") or verdict.get("action") or "")
    protection_mode = str(controls.get("protection_mode") or "")
    labels: set[str] = set()
    if str(cf_label) in {"protection_too_tight", "premature_tighten", "noise_stopout"}:
        if action in {"tighten", "dynamic_tpsl"} or "stop" in protection_mode:
            labels.add("sl_too_tight")
        else:
            labels.add("over_protected")
    if str(cf_label) == "correct_stop" and (
        float((execution or {}).get("target_stop_loss_sent") or 0.0) > 0
        or float((controls or {}).get("target_stop_loss") or 0.0) > 0
    ):
        labels.add("profit_protected")
    giveback = float((evidence or {}).get("giveback_ratio") or 0.0)
    capture = float((evidence or {}).get("profit_capture_ratio") or 0.0)
    take_profit_progress = float((evidence or {}).get("take_profit_progress") or 0.0)
    if giveback >= 0.70 and capture <= 0.20:
        labels.add("mfe_capture_failed")
        labels.add("sl_too_loose")
    if take_profit_progress >= 0.92 and str(cf_label) in {"protection_too_tight", "premature_tighten"}:
        labels.add("tp_too_near")
    if bool((evidence or {}).get("tp_extension_candidate")) and str(cf_label) == "correct_stop":
        labels.add("profit_protected")
    return sorted(labels)


def _matured_sample_from_supervisor_trace(
    row: Any,
    cf_row: Any | None,
    *,
    run_context: dict[str, Any],
    conn: Any | None = None,
) -> dict[str, Any]:
    base = _sample_from_supervisor_trace(row, source_review_row=cf_row, conn=conn)
    cf_label = str(cf_row["label"] or "") if cf_row is not None else ""
    label_status, unified_label, recommended_action, weight = _supervisor_label_from_counterfactual(cf_label)
    protection_labels = _dynamic_tpsl_labels(base, cf_label)
    confidence = float(cf_row["confidence"] or 0.0) if cf_row is not None else 0.0
    integrity = str(row["trace_integrity"] or base["integrity"] or "partial")
    if integrity == "missing":
        weight = 0.0
    elif integrity in {"partial", "recovered"}:
        weight *= 0.5
    if bool(
        ((base.get("features") or {}).get("system_contamination") or {}).get(
            "contaminated"
        )
    ):
        weight = 0.0
    base.update(
        {
            "label_status": label_status,
            "integrity": integrity,
            "train_weight": round(max(0.0, min(1.0, weight * max(confidence, 0.5))), 6),
            "causal_level": "intervention_observed" if label_status == "matured" else "observational",
            "label": {
                "label": unified_label,
                "recommended_action": recommended_action,
                "counterfactual_label": cf_label,
                "counterfactual_confidence": confidence,
                "protection_labels": protection_labels,
                "source": "supervisor_counterfactual_review" if cf_row is not None else "pending_future_evidence",
            },
            "verdict": {
                **(base.get("verdict") or {}),
                "learning_label": unified_label,
                "recommended_action": recommended_action,
                "counterfactual_label": cf_label,
                "protection_labels": protection_labels,
            },
            "trace": {
                **(base.get("trace") or {}),
                "counterfactual_id": str(cf_row["counterfactual_id"] or "") if cf_row is not None else "",
                "trace_integrity": integrity,
            },
            **run_context,
        }
    )
    return base


def backfill_position_supervisor_traces(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 1000,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="position_supervisor_trace_backfill", trigger_source="decision_ledger", db_path=db_path)
    conn = _connect(db_path)
    inserted = 0
    skipped = 0
    try:
        trace_columns = state_table_columns(conn, "position_supervisor_trace")
        archive_capable = {
            "verdict_archive_hash",
            "verdict_raw_sha256",
            "verdict_raw_bytes",
        } <= trace_columns
        rows = _execute(
            conn,
            """
            SELECT *
            FROM decision_ledger
            WHERE event_type IN ('supervisor_close', 'supervisor_reduce', 'supervisor_tighten')
              AND NOT EXISTS (
                  SELECT 1 FROM position_supervisor_trace t
                  WHERE t.decision_id = decision_ledger.decision_id
              )
            ORDER BY decision_ts DESC, created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            action_json = _loads(row["action_json"], {})
            verdict = action_json.get("supervisor_verdict") or action_json
            event_type = str(row["event_type"] or "")
            action = str(verdict.get("action") or event_type.replace("supervisor_", "") or "")
            trace_id = "psvtrace_legacy_" + hashlib.sha1(str(row["decision_id"] or "").encode("utf-8")).hexdigest()[:16]
            integrity = "recovered" if verdict else "partial"
            raw_context = {"legacy_action": action_json, "event_type": event_type}
            raw_verdict = dict(verdict)
            context_json = _dumps(raw_context)
            verdict_json = _dumps(raw_verdict)
            archive = None
            if archive_capable:
                archive = archive_json_payload(
                    conn,
                    source_table="position_supervisor_trace",
                    source_id=trace_id,
                    payload_kind="supervisor_trace",
                    raw_json=supervisor_trace_archive_text(
                        context_json=_dumps(raw_context),
                        verdict_json=_dumps(raw_verdict),
                        risk_verdict_json="{}",
                        execution_json="{}",
                    ),
                )
                if archive:
                    context_json = _dumps(_compact_supervisor_mapping(raw_context))
                    verdict_json = _dumps(
                        _compact_supervisor_mapping(
                            raw_verdict,
                            nested_keys=frozenset(
                                {"evidence", "recommended_controls", "supervisor_template"}
                            ),
                        )
                    )
            cur = _execute(
                conn,
                """
                INSERT INTO position_supervisor_trace
                (trace_id, decision_id, position_id, trade_id, symbol, timeframe,
                 tick, event_ts, action, summary_reason, confidence, template_id,
                 template_version, stage, outcome, risk_action, risk_allowed,
                 risk_reason, execution_status, execution_reason, context_json,
                 verdict_json, risk_verdict_json, execution_json, trace_integrity,
                 config_version, config_hash, evolution_run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'legacy_backfill',
                        'legacy_recovered', '', 0, '', 'unknown', 'legacy decision_ledger backfill',
                        ?, ?, '{}', '{}', ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO NOTHING
                """,
                (
                    trace_id,
                    str(row["decision_id"] or ""),
                    str(row["position_id"] or ""),
                    str(row["trade_id"] or ""),
                    str(row["symbol"] or ""),
                    str(row["timeframe"] or ""),
                    float(row["decision_ts"] or row["created_at"] or 0.0),
                    action,
                    str(verdict.get("summary_reason") or row["action_reason"] or ""),
                    float(verdict.get("confidence", row["action_score"] or 0.0) or 0.0),
                    str((verdict.get("supervisor_template") or {}).get("template_id") or ""),
                    str((verdict.get("supervisor_template") or {}).get("template_version") or ""),
                    context_json,
                    verdict_json,
                    integrity,
                    int(run.get("config_version") or 0),
                    str(run.get("config_hash") or ""),
                    str(run.get("run_id") or ""),
                    time.time(),
                ),
            )
            if archive:
                _execute(
                    conn,
                    """
                    UPDATE position_supervisor_trace
                    SET verdict_archive_hash=?, verdict_raw_sha256=?, verdict_raw_bytes=?
                    WHERE trace_id=?
                    """,
                    (
                        archive["archive_hash"],
                        archive["raw_sha256"],
                        archive["raw_bytes"],
                        trace_id,
                    ),
                )
            if getattr(cur, "rowcount", 0) > 0:
                inserted += 1
            else:
                skipped += 1
        payload = {
            "schema_version": "position_supervisor_trace_backfill.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "inserted": inserted,
            "skipped": skipped,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "position_supervisor_trace_backfill", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="backfill_traces",
            scope_type="position_supervisor_trace",
            action="legacy_backfill",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def mature_position_supervisor_traces(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 500,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="position_supervisor_trace_maturation", trigger_source="counterfactual_review", db_path=db_path)
    run_context = {
        "config_version": int(run.get("config_version") or 0),
        "config_hash": str(run.get("config_hash") or ""),
        "evolution_run_id": str(run.get("run_id") or ""),
    }
    conn = _connect(db_path)
    matured = 0
    pending = 0
    try:
        traces = _execute(
            conn,
            """
            SELECT *
            FROM position_supervisor_trace
            WHERE action IN ('close', 'reduce', 'tighten')
               OR action LIKE 'supervisor_%'
               OR stage LIKE '%execut%'
               OR outcome IN ('executed', 'legacy_recovered')
            ORDER BY event_ts DESC, created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for trace in traces:
            cf = None
            page_offset = 0
            page_limit = max(1, int(limit))
            while cf is None:
                cf_rows = _execute(
                    conn,
                    f"""
                    SELECT cf.*, r.review_id AS source_review_id,
                           r.review_json AS source_review_json{_review_archive_select(conn, output="source_review_archive_hash")}
                    FROM supervisor_counterfactual_review cf
                    JOIN trade_outcome_review r ON r.review_id=cf.review_id
                    WHERE cf.position_id=?
                      AND cf.close_ts >= ?
                    ORDER BY cf.updated_at DESC, cf.close_ts ASC,
                             cf.counterfactual_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (
                        str(trace["position_id"] or ""),
                        float(trace["event_ts"] or 0.0),
                        page_limit,
                        page_offset,
                    ),
                ).fetchall()
                if not cf_rows:
                    break
                page_offset += len(cf_rows)
                cf = next(
                    (
                        candidate
                        for candidate in cf_rows
                        if _counterfactual_source_is_clean(candidate, conn)
                    ),
                    None,
                )
                del cf_rows
            item = _matured_sample_from_supervisor_trace(trace, cf, run_context=run_context, conn=conn)
            if _upsert_sample(conn, item):
                if item["label_status"] == "matured":
                    matured += 1
                else:
                    pending += 1
        payload = {
            "schema_version": "position_supervisor_trace_maturation.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "matured": matured,
            "pending": pending,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "position_supervisor_trace_maturation", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="mature_traces",
            scope_type="supervisor_execution_trace",
            action="materialize_labels",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def materialize_autonomous_learning_samples(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 500,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="autonomous_learning_samples", trigger_source="materialize", db_path=db_path)
    sample_context = {
        "config_version": int(run.get("config_version") or 0),
        "config_hash": str(run.get("config_hash") or ""),
        "evolution_run_id": str(run.get("run_id") or ""),
    }
    conn = _connect(db_path)
    counts = {
        "shadow_open_decision": 0,
        "risk_rejection": 0,
        "entry_supervisor_feedback": 0,
        "supervisor_trajectory": 0,
        "supervisor_execution_trace": 0,
        "trade_review_outcome": 0,
        "post_close_counterfactual": 0,
    }
    try:
        decisions = _execute(
            conn,
            """
            SELECT *
            FROM decision_ledger
            WHERE event_type IN ('open', 'skip') OR event_type LIKE 'supervisor_%'
            ORDER BY decision_ts DESC, created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        for row in decisions:
            event_type = str(row["event_type"] or "")
            if event_type in {"open", "skip"}:
                outcome_review = _review_for_open_decision(conn, row) if event_type == "open" else None
                if _upsert_sample(
                    conn,
                    {
                        **_sample_from_decision(
                            row,
                            "shadow_open_decision",
                            outcome_review=outcome_review,
                            conn=conn,
                        ),
                        **sample_context,
                    },
                ):
                    counts["shadow_open_decision"] += 1
            if event_type == "skip":
                action_json = _loads(row["action_json"], {})
                if str(action_json.get("skip_stage") or "") in {"risk_policy", "market_session", "sizing"}:
                    if _upsert_sample(conn, {**_sample_from_decision(row, "risk_rejection", conn=conn), **sample_context}):
                        counts["risk_rejection"] += 1
            if event_type.startswith("supervisor_"):
                outcome_review = _review_for_open_decision(conn, row)
                if _upsert_sample(conn, {**_sample_from_decision(row, "supervisor_trajectory", outcome_review=outcome_review, conn=conn), **sample_context}):
                    counts["supervisor_trajectory"] += 1
        del decisions
        gc.collect()

        if state_table_exists(conn, "position_supervisor_trace"):
            traces = _execute(
                conn,
                f"""
                SELECT t.*, r.review_id AS source_review_id,
                       r.review_json AS source_review_json{_review_archive_select(conn, output="source_review_archive_hash")}
                FROM position_supervisor_trace t
                LEFT JOIN trade_outcome_review r
                  ON r.review_id = (
                      SELECT r2.review_id
                      FROM trade_outcome_review r2
                      WHERE r2.position_id=t.position_id
                      ORDER BY r2.created_at DESC, r2.review_id DESC
                      LIMIT 1
                  )
                ORDER BY t.event_ts DESC, t.created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            for row in traces:
                if _upsert_sample(conn, {**_sample_from_supervisor_trace(row, conn=conn), **sample_context}):
                    counts["supervisor_execution_trace"] += 1
            del traces
            gc.collect()

        reviews = _execute(
            conn,
            """
            SELECT *
            FROM trade_outcome_review
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        for row in reviews:
            if _upsert_sample(conn, {**_sample_from_review(row, conn=conn), **sample_context}):
                counts["trade_review_outcome"] += 1
            entry_feedback = _sample_from_entry_supervisor_feedback(row, conn=conn)
            if entry_feedback and _upsert_sample(conn, {**entry_feedback, **sample_context}):
                counts["entry_supervisor_feedback"] += 1
        del reviews
        gc.collect()

        if state_table_exists(conn, "supervisor_counterfactual_review"):
            _execute(
                conn,
                """
                DELETE FROM autonomous_learning_sample
                WHERE sample_type='post_close_counterfactual'
                  AND source_table='supervisor_counterfactual_review'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM supervisor_counterfactual_review cf
                      WHERE cf.counterfactual_id=autonomous_learning_sample.source_id
                  )
                """,
            )
            accepted_counterfactuals = 0
            page_offset = 0
            page_limit = max(1, int(limit))
            while accepted_counterfactuals < page_limit:
                cfs = _execute(
                    conn,
                    f"""
                    SELECT cf.*, r.review_id AS source_review_id,
                           r.review_json AS source_review_json{_review_archive_select(conn, output="source_review_archive_hash")}
                    FROM supervisor_counterfactual_review cf
                    JOIN trade_outcome_review r ON r.review_id=cf.review_id
                    ORDER BY cf.updated_at DESC, cf.counterfactual_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (page_limit, page_offset),
                ).fetchall()
                if not cfs:
                    break
                page_offset += len(cfs)
                for row in cfs:
                    if not _counterfactual_source_is_clean(row, conn):
                        continue
                    accepted_counterfactuals += 1
                    if _upsert_sample(conn, {**_sample_from_counterfactual(row, conn=conn), **sample_context}):
                        counts["post_close_counterfactual"] += 1
                    if accepted_counterfactuals >= page_limit:
                        break
                del cfs

        payload = {
            "schema_version": "autonomous_learning_samples.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "config_version": int(run.get("config_version") or 0),
            "config_hash": str(run.get("config_hash") or ""),
            "counts": counts,
            "total_changed": sum(counts.values()),
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "autonomous_learning_samples", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="materialize_samples",
            scope_type="autonomous_learning_sample",
            action="upsert_samples",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def _entry_cluster_bucket_from_features(features: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    action = features.get("action") or {}
    cluster = features.get("entry_cluster") or action.get("entry_cluster") or {}
    same_count = int(float(action.get("same_direction_open_count") or cluster.get("same_direction_open_count_before") or 0))
    depth = int(float(cluster.get("pyramid_depth") or max(0, same_count)))
    recent = cluster.get("recent_same_direction_entries") or action.get("recent_same_direction_entries") or {}
    if same_count >= 3 or depth >= 3:
        return "same_direction_ge_3", {"same_direction_open_count": same_count, "pyramid_depth": depth, "recent_same_direction_entries": recent}
    if same_count >= 2 or depth >= 2:
        return "same_direction_ge_2", {"same_direction_open_count": same_count, "pyramid_depth": depth, "recent_same_direction_entries": recent}
    if same_count >= 1 or depth >= 1:
        return "same_direction_ge_1", {"same_direction_open_count": same_count, "pyramid_depth": depth, "recent_same_direction_entries": recent}
    return "", {"same_direction_open_count": same_count, "pyramid_depth": depth, "recent_same_direction_entries": recent}


def _governance_bucket_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    sample_count = len(items)
    effective_sample_count = sum(float(item.get("governance_weight") or 0.0) for item in items)
    bad_count = sum(1 for item in items if item.get("bad"))
    win_count = sum(1 for item in items if float(item.get("pnl") or 0.0) > 0)
    weighted_bad_count = sum(
        float(item.get("governance_weight") or 0.0)
        for item in items
        if item.get("bad")
    )
    weighted_win_count = sum(
        float(item.get("governance_weight") or 0.0)
        for item in items
        if float(item.get("pnl") or 0.0) > 0
    )
    pnl_sum = sum(float(item.get("pnl") or 0.0) for item in items)
    weighted_reward_sum = sum(
        float(item.get("governance_weight") or 0.0)
        * max(-1.0, min(1.0, float(item.get("pnl") or 0.0) / 50.0))
        for item in items
    )
    weighted_avg_reward = (
        weighted_reward_sum / effective_sample_count
        if effective_sample_count > 0
        else 0.0
    )
    weighted_bad_rate = (
        weighted_bad_count / effective_sample_count
        if effective_sample_count > 0
        else 0.0
    )
    fingerprint_items = sorted(
        (
            str(item.get("sample_id") or ""),
            str(item.get("governance_eligibility_fingerprint") or ""),
            round(float(item.get("governance_weight") or 0.0), 6),
        )
        for item in items
    )
    eligibility_fingerprint = hashlib.sha256(
        _dumps(
            {
                "schema_version": GOVERNANCE_ELIGIBILITY_VERSION,
                "samples": fingerprint_items,
            }
        ).encode("utf-8")
    ).hexdigest()
    return {
        "sample_count": sample_count,
        "effective_sample_count": effective_sample_count,
        "bad_count": bad_count,
        "win_count": win_count,
        "weighted_bad_count": weighted_bad_count,
        "weighted_win_count": weighted_win_count,
        "pnl_sum": pnl_sum,
        "weighted_avg_reward": weighted_avg_reward,
        "weighted_bad_rate": weighted_bad_rate,
        "eligibility_fingerprint": eligibility_fingerprint,
    }


def _weighted_bad_rate(items: list[dict[str, Any]]) -> tuple[float, float]:
    effective_n = sum(
        float(item.get("governance_weight") or 0.0) for item in items
    )
    weighted_bad = sum(
        float(item.get("governance_weight") or 0.0)
        for item in items
        if item.get("bad")
    )
    return (
        weighted_bad / effective_n if effective_n > 0.0 else 0.0,
        effective_n,
    )


def _wilson_lower_bound(rate: float, effective_n: float) -> float:
    if effective_n <= 0.0:
        return 0.0
    z = 1.959963984540054
    denominator = 1.0 + z * z / effective_n
    centre = rate + z * z / (2.0 * effective_n)
    margin = z * math.sqrt(
        max(0.0, rate * (1.0 - rate) / effective_n + z * z / (4.0 * effective_n**2))
    )
    return max(0.0, (centre - margin) / denominator)


def _weak_signal_threshold_scan(
    items: list[dict[str, Any]],
    *,
    base_threshold: float,
    cap_threshold: float,
) -> dict[str, Any]:
    """Choose the smallest threshold with a measurable counterfactual benefit."""

    metrics = _governance_bucket_metrics(items)
    base = max(0.0, float(base_threshold))
    cap = max(base, float(cap_threshold))
    candidates: list[dict[str, Any]] = []
    threshold = base + 0.05
    while threshold <= cap + 1e-9:
        rounded = round(threshold, 4)
        excluded = [
            item
            for item in items
            if base <= float(item.get("entry_score") or 0.0) < rounded
        ]
        retained = [
            item for item in items if float(item.get("entry_score") or 0.0) >= rounded
        ]
        excluded_bad_rate, excluded_n = _weighted_bad_rate(excluded)
        retained_bad_rate, retained_n = _weighted_bad_rate(retained)
        wilson_lower = _wilson_lower_bound(excluded_bad_rate, excluded_n)
        qualifies = bool(
            float(metrics["effective_sample_count"]) >= 20.0
            and int(metrics["bad_count"]) > 0
            and int(metrics["win_count"]) > 0
            and excluded_n >= 8.0
            and retained_n >= 8.0
            and excluded_bad_rate >= 0.65
            and wilson_lower > 0.50
            and retained_bad_rate <= excluded_bad_rate - 0.10
        )
        candidates.append(
            {
                "threshold": rounded,
                "excluded_effective_n": round(excluded_n, 6),
                "retained_effective_n": round(retained_n, 6),
                "excluded_bad_rate": round(excluded_bad_rate, 6),
                "retained_bad_rate": round(retained_bad_rate, 6),
                "excluded_bad_rate_wilson_lower": round(wilson_lower, 6),
                "qualifies": qualifies,
            }
        )
        threshold += 0.05
    selected = next(
        (item for item in candidates if bool(item.get("qualifies"))),
        None,
    )
    valid_scores = [
        float(item.get("entry_score") or 0.0)
        for item in items
        if math.isfinite(float(item.get("entry_score") or 0.0))
    ]
    return {
        "selected_threshold": (
            float(selected["threshold"]) if selected is not None else 0.0
        ),
        "base_threshold": base,
        "cap_threshold": cap,
        "entry_score_min": min(valid_scores) if valid_scores else 0.0,
        "entry_score_max": max(valid_scores) if valid_scores else 0.0,
        "candidates": candidates,
        "metrics": metrics,
    }


def _upsert_governance_pattern_stats(
    conn: Any,
    *,
    scope_type: str,
    scope_key: str,
    metrics: dict[str, Any],
    last_outcome_label: str,
    recommended_action: str,
    now: float,
) -> None:
    _execute(
        conn,
        """
        INSERT INTO experience_pattern_stats
        (scope_type, scope_key, sample_count, win_count, bad_loss_count,
         avg_reward, effective_sample_count, weighted_win_count,
         weighted_bad_loss_count, weighted_avg_reward,
         governance_eligibility_version, governance_eligibility_fingerprint,
         last_outcome_label, recommended_action, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scope_type, scope_key) DO UPDATE SET
            sample_count=excluded.sample_count,
            win_count=excluded.win_count,
            bad_loss_count=excluded.bad_loss_count,
            avg_reward=excluded.avg_reward,
            effective_sample_count=excluded.effective_sample_count,
            weighted_win_count=excluded.weighted_win_count,
            weighted_bad_loss_count=excluded.weighted_bad_loss_count,
            weighted_avg_reward=excluded.weighted_avg_reward,
            governance_eligibility_version=excluded.governance_eligibility_version,
            governance_eligibility_fingerprint=excluded.governance_eligibility_fingerprint,
            last_outcome_label=excluded.last_outcome_label,
            recommended_action=excluded.recommended_action,
            updated_at=excluded.updated_at
        """,
        (
            scope_type,
            scope_key,
            int(metrics["sample_count"]),
            int(metrics["win_count"]),
            int(metrics["bad_count"]),
            round(float(metrics["weighted_avg_reward"]), 6),
            round(float(metrics["effective_sample_count"]), 6),
            round(float(metrics["weighted_win_count"]), 6),
            round(float(metrics["weighted_bad_count"]), 6),
            round(float(metrics["weighted_avg_reward"]), 6),
            GOVERNANCE_ELIGIBILITY_VERSION,
            str(metrics["eligibility_fingerprint"]),
            last_outcome_label,
            recommended_action,
            now,
        ),
    )


def _governance_evidence_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_count": int(metrics["sample_count"]),
        "effective_sample_count": round(float(metrics["effective_sample_count"]), 6),
        "bad_count": int(metrics["bad_count"]),
        "weighted_bad_count": round(float(metrics["weighted_bad_count"]), 6),
        "bad_rate": round(float(metrics["weighted_bad_rate"]), 6),
        "win_count": int(metrics["win_count"]),
        "weighted_win_count": round(float(metrics["weighted_win_count"]), 6),
        "pnl_sum": round(float(metrics["pnl_sum"]), 6),
        "avg_reward": round(float(metrics["weighted_avg_reward"]), 6),
        "weighted_avg_reward": round(float(metrics["weighted_avg_reward"]), 6),
        "governance_eligibility_version": GOVERNANCE_ELIGIBILITY_VERSION,
        "governance_eligibility_fingerprint": str(metrics["eligibility_fingerprint"]),
    }


def materialize_entry_cluster_governance_suggestions(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 1000,
    min_samples: int = 3,
    min_bad_rate: float = 0.5,
) -> dict[str, Any]:
    """Suggest advisory controls when same-direction entry clusters underperform."""
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="entry_cluster_governance", trigger_source="open_outcome_samples", db_path=db_path)
    conn = _connect(db_path)
    buckets: dict[str, list[dict[str, Any]]] = {}
    suggestions = 0
    stats_upserted = 0
    skipped = 0
    try:
        rows = _execute(
            conn,
            """
            SELECT *
            FROM autonomous_learning_sample
            WHERE sample_type='shadow_open_decision'
              AND label_status='matured'
              AND governance_eligible=1
              AND governance_effective_weight>0
              AND governance_eligibility_version=?
              AND governance_eligibility_fingerprint<>''
            ORDER BY event_ts DESC, created_at DESC
            LIMIT ?
            """,
            (GOVERNANCE_ELIGIBILITY_VERSION, max(1, int(limit))),
        ).fetchall()
        for row in rows:
            label = _loads(row["label_json"], {})
            if str(label.get("label") or "") != "open_outcome":
                continue
            features = _loads(row["features_json"], {})
            bucket, cluster = _entry_cluster_bucket_from_features(features)
            if not bucket:
                continue
            pnl = float(label.get("pnl") or 0.0)
            outcome = str(label.get("outcome_label") or "")
            failure_tags = [str(item) for item in label.get("failure_tags") or []]
            bad = outcome == "bad_loss" or "entry_cluster_risk" in failure_tags or pnl < 0
            buckets.setdefault(bucket, []).append(
                {
                    "sample_id": str(row["sample_id"] or ""),
                    "decision_id": str(row["decision_id"] or ""),
                    "position_id": str(row["position_id"] or ""),
                    "pnl": pnl,
                    "outcome_label": outcome,
                    "bad": bad,
                    "cluster": cluster,
                    "governance_weight": float(row["governance_effective_weight"] or 0.0),
                    "governance_eligibility_fingerprint": str(row["governance_eligibility_fingerprint"] or ""),
                }
            )

        now = time.time()
        for bucket, items in sorted(buckets.items()):
            metrics = _governance_bucket_metrics(items)
            sample_count = int(metrics["sample_count"])
            effective_sample_count = float(metrics["effective_sample_count"])
            bad_count = int(metrics["bad_count"])
            win_count = int(metrics["win_count"])
            pnl_sum = float(metrics["pnl_sum"])
            avg_reward = float(metrics["weighted_avg_reward"])
            bad_rate = float(metrics["weighted_bad_rate"])
            action = "watch"
            if effective_sample_count >= float(min_samples) and bad_rate >= float(min_bad_rate):
                action = "increase_same_direction_cooldown"
            elif effective_sample_count >= float(min_samples) and avg_reward <= -0.05:
                action = "raise_pyramid_entry_threshold"
            _upsert_governance_pattern_stats(
                conn,
                scope_type="entry_cluster",
                scope_key=bucket,
                metrics=metrics,
                last_outcome_label=str(items[0].get("outcome_label") or ""),
                recommended_action=action,
                now=now,
            )
            stats_upserted += 1
            if action == "watch":
                skipped += 1
                continue
            existing = _execute(
                conn,
                """
                SELECT suggestion_id
                FROM policy_suggestion
                WHERE scope_type='entry_cluster'
                  AND scope_key=?
                  AND action=?
                  AND status IN ('proposed', 'approved', 'applied')
                LIMIT 1
                """,
                (bucket, action),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            suggestion_id = "psg_entry_cluster_" + hashlib.sha1(f"{bucket}:{action}".encode("utf-8")).hexdigest()[:16]
            confidence = min(0.92, 0.45 + 0.06 * effective_sample_count + 0.20 * bad_rate)
            evidence = {
                "schema_version": "entry_cluster_governance_evidence.v1",
                "bucket": bucket,
                **_governance_evidence_metrics(metrics),
                "sample_ids": [item["sample_id"] for item in items[:20]],
                "position_ids": [item["position_id"] for item in items[:20]],
                "recommended_controls": {
                    "increase_same_direction_cooldown": action == "increase_same_direction_cooldown",
                    "raise_pyramid_entry_threshold": action == "raise_pyramid_entry_threshold",
                    "advisory_only": True,
                },
            }
            evidence = attach_policy_suggestion_agent_context(
                evidence,
                source_agent="autonomous_learning",
                scope_type="entry_cluster",
                action=action,
                requested_writes=["policy_suggestion"],
                status="proposed",
                impact_level="medium",
                db_path=db_path,
            )
            _execute(
                conn,
                """
                INSERT INTO policy_suggestion
                (suggestion_id, scope_type, scope_key, action, confidence,
                 reason, evidence_json, status, governance_eligible,
                 governance_eligibility_version, governance_eligibility_fingerprint,
                 governance_ineligible_reason, created_at)
                VALUES (?, 'entry_cluster', ?, ?, ?, ?, ?, 'proposed', 1, ?, ?, '', ?)
                ON CONFLICT(suggestion_id) DO NOTHING
                """,
                (
                    suggestion_id,
                    bucket,
                    action,
                    round(confidence, 6),
                    f"{bucket} open outcomes show weighted bad_rate={bad_rate:.2f} across effective_n={effective_sample_count:.2f}",
                    _dumps(evidence),
                    GOVERNANCE_ELIGIBILITY_VERSION,
                    str(metrics["eligibility_fingerprint"]),
                    now,
                ),
            )
            suggestions += 1
        payload = {
            "schema_version": "entry_cluster_governance.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "bucket_count": len(buckets),
            "stats_upserted": stats_upserted,
            "suggestions": suggestions,
            "skipped": skipped,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "entry_cluster_governance", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="entry_cluster_governance",
            scope_type="entry_cluster",
            action="materialize_governance_suggestions",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    except Exception as exc:
        conn.rollback()
        finish_evolution_run(str(run.get("run_id") or ""), status="failed", summary={"error": str(exc)[:500]}, db_path=db_path)
        raise
    finally:
        conn.close()


def _event_window_bucket_from_features(features: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(features, dict):
        return "", {}
    event = features.get("event_context") or (features.get("action") or {}).get("event_sizing") or {}
    if not isinstance(event, dict):
        return "", {}
    schema_version = str(event.get("schema_version") or "").strip()
    if schema_version != EVENT_WINDOW_CONTEXT_SCHEMA_VERSION:
        return "", {}
    try:
        multiplier = float(event.get("multiplier") or 1.0)
    except (TypeError, ValueError):
        return "", {}
    event_type = str(event.get("event_type") or event.get("event") or "").strip()
    window_bucket = str(event.get("window_bucket") or "").strip()
    if not window_bucket:
        hours_until = event.get("hours_until_event")
        try:
            h = float(hours_until)
        except Exception:
            h = 999999.0
        if -(5.0 / 60.0) <= h < 0:
            window_bucket = "post_0_5m"
        elif 0 <= h <= 0.25:
            window_bucket = "pre_0_15m"
        elif h <= 0.5:
            window_bucket = "pre_15_30m"
        elif h <= 1.0:
            window_bucket = "pre_30_60m"
    if (
        multiplier >= 1.0
        or multiplier < EVENT_WINDOW_MIN_VALID_MULTIPLIER
        or not event_type
        or window_bucket not in EVENT_WINDOW_ALLOWED_BUCKETS
    ):
        return "", {}
    bucket = f"{event_type}:{window_bucket}"
    return bucket, {
        "schema_version": schema_version,
        "event_type": event_type,
        "event": str(event.get("event") or event_type),
        "event_importance": int(float(event.get("event_importance") or 0)),
        "window_bucket": window_bucket,
        "multiplier": multiplier,
        "hours_until_event": event.get("hours_until_event"),
        "minutes_until_event": event.get("minutes_until_event"),
        "tier_max_hours_before": event.get("tier_max_hours_before"),
    }


def materialize_event_window_governance_suggestions(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 1000,
    min_samples: int = 3,
    min_bad_rate: float = 0.5,
) -> dict[str, Any]:
    """Suggest advisory event-window sizing controls when event windows underperform."""
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="event_window_governance", trigger_source="open_outcome_samples", db_path=db_path)
    conn = _connect(db_path)
    buckets: dict[str, list[dict[str, Any]]] = {}
    suggestions = 0
    stats_upserted = 0
    skipped = 0
    try:
        rows = _execute(
            conn,
            """
            SELECT *
            FROM autonomous_learning_sample
            WHERE sample_type='shadow_open_decision'
              AND label_status='matured'
              AND governance_eligible=1
              AND governance_effective_weight>0
              AND governance_eligibility_version=?
              AND governance_eligibility_fingerprint<>''
            ORDER BY event_ts DESC, created_at DESC
            LIMIT ?
            """,
            (GOVERNANCE_ELIGIBILITY_VERSION, max(1, int(limit))),
        ).fetchall()
        for row in rows:
            label = _loads(row["label_json"], {})
            if str(label.get("label") or "") != "open_outcome":
                continue
            features = _loads(row["features_json"], {})
            bucket, event_window = _event_window_bucket_from_features(features)
            if not bucket:
                continue
            pnl = float(label.get("pnl") or 0.0)
            outcome = str(label.get("outcome_label") or "")
            failure_tags = [str(item) for item in label.get("failure_tags") or []]
            bad = outcome == "bad_loss" or "event_window_bad_entry" in failure_tags or pnl < 0
            buckets.setdefault(bucket, []).append(
                {
                    "sample_id": str(row["sample_id"] or ""),
                    "decision_id": str(row["decision_id"] or ""),
                    "position_id": str(row["position_id"] or ""),
                    "pnl": pnl,
                    "outcome_label": outcome,
                    "bad": bad,
                    "event_window": event_window,
                    "governance_weight": float(row["governance_effective_weight"] or 0.0),
                    "governance_eligibility_fingerprint": str(row["governance_eligibility_fingerprint"] or ""),
                }
            )

        now = time.time()
        for bucket, items in sorted(buckets.items()):
            metrics = _governance_bucket_metrics(items)
            sample_count = int(metrics["sample_count"])
            effective_sample_count = float(metrics["effective_sample_count"])
            bad_count = int(metrics["bad_count"])
            win_count = int(metrics["win_count"])
            pnl_sum = float(metrics["pnl_sum"])
            avg_reward = float(metrics["weighted_avg_reward"])
            bad_rate = float(metrics["weighted_bad_rate"])
            action = "watch"
            if effective_sample_count >= float(min_samples) and bad_rate >= float(min_bad_rate):
                action = "tighten_event_window_sizing"
            elif effective_sample_count >= float(min_samples) and avg_reward <= -0.05:
                action = "extend_event_post_window_review"
            _upsert_governance_pattern_stats(
                conn,
                scope_type="event_window",
                scope_key=bucket,
                metrics=metrics,
                last_outcome_label=str(items[0].get("outcome_label") or ""),
                recommended_action=action,
                now=now,
            )
            stats_upserted += 1
            if action == "watch":
                skipped += 1
                continue
            existing = _execute(
                conn,
                """
                SELECT suggestion_id
                FROM policy_suggestion
                WHERE scope_type='event_window'
                  AND scope_key=?
                  AND action=?
                  AND status IN ('proposed', 'approved', 'applied')
                LIMIT 1
                """,
                (bucket, action),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            suggestion_id = "psg_event_window_" + hashlib.sha1(f"{bucket}:{action}".encode("utf-8")).hexdigest()[:16]
            confidence = min(0.92, 0.45 + 0.06 * effective_sample_count + 0.20 * bad_rate)
            event_window = dict(items[0].get("event_window") or {})
            evidence = {
                "schema_version": "event_window_governance_evidence.v1",
                "bucket": bucket,
                "event_window": event_window,
                **_governance_evidence_metrics(metrics),
                "sample_ids": [item["sample_id"] for item in items[:20]],
                "position_ids": [item["position_id"] for item in items[:20]],
                "recommended_controls": {
                    "tighten_event_window_sizing": action == "tighten_event_window_sizing",
                    "extend_event_post_window_review": action == "extend_event_post_window_review",
                    "advisory_only": True,
                },
            }
            evidence = attach_policy_suggestion_agent_context(
                evidence,
                source_agent="autonomous_learning",
                scope_type="event_window",
                action=action,
                requested_writes=["policy_suggestion"],
                status="proposed",
                impact_level="medium",
                db_path=db_path,
            )
            _execute(
                conn,
                """
                INSERT INTO policy_suggestion
                (suggestion_id, scope_type, scope_key, action, confidence,
                 reason, evidence_json, status, governance_eligible,
                 governance_eligibility_version, governance_eligibility_fingerprint,
                 governance_ineligible_reason, created_at)
                VALUES (?, 'event_window', ?, ?, ?, ?, ?, 'proposed', 1, ?, ?, '', ?)
                ON CONFLICT(suggestion_id) DO NOTHING
                """,
                (
                    suggestion_id,
                    bucket,
                    action,
                    round(confidence, 6),
                    f"{bucket} open outcomes show weighted bad_rate={bad_rate:.2f} across effective_n={effective_sample_count:.2f}",
                    _dumps(evidence),
                    GOVERNANCE_ELIGIBILITY_VERSION,
                    str(metrics["eligibility_fingerprint"]),
                    now,
                ),
            )
            suggestions += 1
        payload = {
            "schema_version": "event_window_governance.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "bucket_count": len(buckets),
            "stats_upserted": stats_upserted,
            "suggestions": suggestions,
            "skipped": skipped,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "event_window_governance", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="event_window_governance",
            scope_type="event_window",
            action="materialize_governance_suggestions",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    except Exception as exc:
        conn.rollback()
        finish_evolution_run(str(run.get("run_id") or ""), status="failed", summary={"error": str(exc)[:500]}, db_path=db_path)
        raise
    finally:
        conn.close()


def materialize_entry_quality_governance_suggestions(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 1000,
    min_samples: int = 5,
    min_bad_rate: float = 0.6,
) -> dict[str, Any]:
    """Suggest entry-quality controls when reviews show weak entries or factor conflict."""
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="entry_quality_governance", trigger_source="trade_review_outcomes", db_path=db_path)
    conn = _connect(db_path)
    buckets: dict[str, list[dict[str, Any]]] = {}
    suggestions = 0
    stats_upserted = 0
    skipped = 0
    invalidated_v1 = 0
    try:
        from config import runtime_config as runtime_config_module

        cfg = runtime_config_module.shared()
        base_signal_threshold = float(
            getattr(cfg, "factor_signal_threshold", 0.30) or 0.30
        )
        balanced_demo = runtime_config_module.bounded_demo_mode_active(cfg)
        weak_signal_cap = 0.55 if balanced_demo else 0.68
        rows = _execute(
            conn,
            """
            SELECT *
            FROM autonomous_learning_sample
            WHERE sample_type='trade_review_outcome'
              AND label_status='matured'
              AND governance_eligible=1
              AND governance_effective_weight>0
              AND governance_eligibility_version=?
              AND governance_eligibility_fingerprint<>''
            ORDER BY event_ts DESC, created_at DESC
            LIMIT ?
            """,
            (GOVERNANCE_ELIGIBILITY_VERSION, max(1, int(limit))),
        ).fetchall()
        legacy_weak_ids: list[str] = []
        page_offset = 0
        page_limit = max(1, int(limit))
        while len(legacy_weak_ids) < page_limit:
            legacy_weak_rows = _execute(
                conn,
                """
                SELECT suggestion_id, evidence_json
                FROM policy_suggestion
                WHERE scope_type='entry_quality'
                  AND scope_key='weak_signal'
                  AND action='raise_weak_signal_threshold'
                  AND status IN ('proposed', 'approved')
                ORDER BY created_at DESC, suggestion_id DESC
                LIMIT ? OFFSET ?
                """,
                (page_limit, page_offset),
            ).fetchall()
            if not legacy_weak_rows:
                break
            page_offset += len(legacy_weak_rows)
            for legacy_row in legacy_weak_rows:
                if str(
                    _loads(legacy_row["evidence_json"], {}).get("schema_version") or ""
                ) == "entry_quality_governance_evidence.v2":
                    continue
                legacy_weak_ids.append(str(legacy_row["suggestion_id"] or ""))
                if len(legacy_weak_ids) >= page_limit:
                    break
            del legacy_weak_rows
        if legacy_weak_ids:
            placeholders = ",".join("?" for _ in legacy_weak_ids)
            _execute(
                conn,
                f"""
                UPDATE policy_suggestion
                SET status='invalidated_evidence', reviewed_at=?,
                    review_note='entry_quality_v1_population_bias'
                WHERE suggestion_id IN ({placeholders})
                  AND status IN ('proposed', 'approved')
                """,
                (time.time(), *legacy_weak_ids),
            )
            invalidated_v1 = len(legacy_weak_ids)
        seen_positions: set[str] = set()
        for row in rows:
            position_id = str(row["position_id"] or "")
            if position_id and position_id in seen_positions:
                continue
            if position_id:
                seen_positions.add(position_id)
            label = _loads(row["label_json"], {})
            features = _loads(row["features_json"], {})
            review = features.get("review") or {}
            failure_tags = {str(item) for item in (label.get("failure_tags") or review.get("failure_tags") or [])}
            pnl = float(label.get("pnl") or 0.0)
            entry_score = abs(float(review.get("entry_score") or 0.0))
            worst_factor = str(review.get("worst_factor") or "").strip()
            taxonomy = review.get("failure_taxonomy") or {}
            primary_responsibility = str(
                review.get("primary_responsibility")
                or (taxonomy.get("primary_responsibility") if isinstance(taxonomy, dict) else "")
                or ""
            ).strip().lower()
            factor_penalty_eligible = bool(
                worst_factor
                and primary_responsibility
                and primary_responsibility != "unclear"
                and primary_responsibility not in FACTOR_PENALTY_BLOCKED_RESPONSIBILITIES
            )
            bad = pnl < 0 or str(label.get("outcome_label") or "") == "bad_loss"
            base_item = {
                "sample_id": str(row["sample_id"] or ""),
                "review_id": str(row["source_id"] or ""),
                "position_id": position_id,
                "pnl": pnl,
                "bad": bad,
                "entry_score": entry_score,
                "worst_factor": worst_factor,
                "primary_responsibility": primary_responsibility,
                "factor_penalty_eligible": factor_penalty_eligible,
                "failure_tags": sorted(failure_tags),
                "governance_weight": float(row["governance_effective_weight"] or 0.0),
                "governance_eligibility_fingerprint": str(row["governance_eligibility_fingerprint"] or ""),
            }
            if math.isfinite(entry_score) and entry_score > 0.0:
                buckets.setdefault("weak_signal", []).append(dict(base_item))
            if bad and failure_tags.intersection({"factor_conflict", "conflicting_factor_entry", "conflict_entry_loss"}):
                buckets.setdefault("factor_conflict", []).append(dict(base_item))
                if factor_penalty_eligible:
                    buckets.setdefault(f"worst_factor:{worst_factor}", []).append(dict(base_item))

        now = time.time()
        for bucket, items in sorted(buckets.items()):
            metrics = _governance_bucket_metrics(items)
            weak_scan = (
                _weak_signal_threshold_scan(
                    items,
                    base_threshold=base_signal_threshold,
                    cap_threshold=weak_signal_cap,
                )
                if bucket == "weak_signal"
                else {}
            )
            eligibility_fingerprint = str(metrics["eligibility_fingerprint"])
            if bucket == "weak_signal":
                eligibility_fingerprint = hashlib.sha256(
                    _dumps(
                        {
                            "schema_version": "entry_quality_governance_evidence.v2",
                            "eligibility_binding_version": (
                                "entry_quality_governance_evidence_binding.v1"
                            ),
                            "sample_fingerprint": eligibility_fingerprint,
                            "threshold_scan": weak_scan,
                        }
                    ).encode("utf-8")
                ).hexdigest()
            sample_count = int(metrics["sample_count"])
            effective_sample_count = float(metrics["effective_sample_count"])
            bad_count = int(metrics["bad_count"])
            win_count = int(metrics["win_count"])
            pnl_sum = float(metrics["pnl_sum"])
            avg_reward = float(metrics["weighted_avg_reward"])
            bad_rate = float(metrics["weighted_bad_rate"])
            avg_entry_score = (
                sum(
                    float(item["entry_score"])
                    * float(item.get("governance_weight") or 0.0)
                    for item in items
                )
                / effective_sample_count
                if effective_sample_count > 0
                else 0.0
            )
            action = "watch"
            scope_key = bucket
            recommended_controls: dict[str, Any] = {"advisory_only": False}
            if bucket == "weak_signal":
                selected_threshold = float(
                    weak_scan.get("selected_threshold") or 0.0
                )
                if selected_threshold > 0.0:
                    action = "raise_weak_signal_threshold"
                    recommended_controls.update(
                        {
                            "min_abs_signal_score": round(selected_threshold, 4),
                            "strong_signal_override": 0.70
                            if balanced_demo
                            else 0.75,
                        }
                    )
            elif effective_sample_count >= float(min_samples) and bad_rate >= float(min_bad_rate):
                if bucket == "factor_conflict":
                    action = "require_factor_agreement"
                    recommended_controls.update(
                        {
                            "max_factor_conflict_ratio": 0.35,
                            "strong_signal_override": 0.78,
                        }
                    )
                elif bucket.startswith("worst_factor:"):
                    scope_key = bucket.split(":", 1)[1]
                    action = "suppress_recent_worst_factor"
                    recommended_controls.update(
                        {
                            "suppressed_factor": scope_key,
                            "strong_signal_override": 0.78,
                        }
                    )
            _upsert_governance_pattern_stats(
                conn,
                scope_type="entry_quality",
                scope_key=scope_key,
                metrics={
                    **metrics,
                    "eligibility_fingerprint": eligibility_fingerprint,
                },
                last_outcome_label="bad_loss" if bad_count else "",
                recommended_action=action,
                now=now,
            )
            stats_upserted += 1
            if action == "watch":
                skipped += 1
                continue
            current = _execute(
                conn,
                """
                SELECT suggestion_id, status
                FROM policy_suggestion
                WHERE scope_type='entry_quality'
                  AND scope_key=?
                  AND action=?
                  AND status IN ('proposed', 'approved', 'applied')
                  AND governance_eligible=1
                  AND governance_eligibility_version=?
                  AND governance_eligibility_fingerprint=?
                ORDER BY created_at DESC, suggestion_id DESC
                LIMIT 1
                """,
                (
                    scope_key,
                    action,
                    GOVERNANCE_ELIGIBILITY_VERSION,
                    eligibility_fingerprint,
                ),
            ).fetchone()
            if current:
                skipped += 1
                continue
            _execute(
                conn,
                """
                UPDATE policy_suggestion
                SET status='invalidated_evidence', reviewed_at=?,
                    review_note='superseded_by_current_governance_eligibility'
                WHERE scope_type='entry_quality'
                  AND scope_key=?
                  AND action=?
                  AND status IN ('proposed', 'approved')
                  AND NOT (
                      COALESCE(governance_eligible, 0)=1
                      AND COALESCE(governance_eligibility_version, '')=?
                      AND COALESCE(governance_eligibility_fingerprint, '')=?
                  )
                """,
                (
                    now,
                    scope_key,
                    action,
                    GOVERNANCE_ELIGIBILITY_VERSION,
                    eligibility_fingerprint,
                ),
            )
            suggestion_id = "psg_entry_quality_" + hashlib.sha1(
                f"{scope_key}:{action}:{eligibility_fingerprint}".encode("utf-8")
            ).hexdigest()[:16]
            confidence = min(0.94, 0.48 + 0.05 * effective_sample_count + 0.20 * bad_rate)
            evidence = {
                "schema_version": (
                    "entry_quality_governance_evidence.v2"
                    if bucket == "weak_signal"
                    else "entry_quality_governance_evidence.v1"
                ),
                "bucket": bucket,
                "scope_key": scope_key,
                **_governance_evidence_metrics(metrics),
                "avg_entry_score": round(avg_entry_score, 6),
                "worst_factor": scope_key if bucket.startswith("worst_factor:") else "",
                "primary_responsibilities": sorted(
                    {
                        str(item.get("primary_responsibility") or "")
                        for item in items
                        if str(item.get("primary_responsibility") or "")
                    }
                ),
                "factor_penalty_eligible": bucket.startswith("worst_factor:"),
                "sample_ids": [item["sample_id"] for item in items[:20]],
                "position_ids": [item["position_id"] for item in items[:20]],
                "recommended_controls": recommended_controls,
            }
            if bucket == "weak_signal":
                evidence.update(
                    {
                        "population_contract": {
                            "all_matured_governance_eligible_positions": True,
                            "deduplicated_by_position": True,
                            "failure_tags_are_explanatory_only": True,
                        },
                        "threshold_scan": weak_scan,
                        "governance_profile": (
                            "balanced_demo" if balanced_demo else "strict_live"
                        ),
                        "governance_eligibility_fingerprint": (
                            eligibility_fingerprint
                        ),
                    }
                )
            evidence = attach_policy_suggestion_agent_context(
                evidence,
                source_agent="autonomous_learning",
                scope_type="entry_quality",
                action=action,
                requested_writes=["policy_suggestion"],
                status="proposed",
                impact_level="medium",
                db_path=db_path,
            )
            _execute(
                conn,
                """
                INSERT INTO policy_suggestion
                (suggestion_id, scope_type, scope_key, action, confidence,
                 reason, evidence_json, status, governance_eligible,
                 governance_eligibility_version, governance_eligibility_fingerprint,
                 governance_ineligible_reason, created_at)
                VALUES (?, 'entry_quality', ?, ?, ?, ?, ?, 'proposed', 1, ?, ?, '', ?)
                ON CONFLICT(suggestion_id) DO UPDATE SET
                    confidence=excluded.confidence,
                    reason=excluded.reason,
                    evidence_json=excluded.evidence_json,
                    status='proposed',
                    governance_eligible=1,
                    governance_eligibility_version=excluded.governance_eligibility_version,
                    governance_eligibility_fingerprint=excluded.governance_eligibility_fingerprint,
                    governance_ineligible_reason='',
                    reviewed_at=0.0,
                    review_note=''
                WHERE policy_suggestion.status='rejected'
                  AND policy_suggestion.governance_ineligible_reason='eligibility_contract_invalid'
                """,
                (
                    suggestion_id,
                    scope_key,
                    action,
                    round(confidence, 6),
                    f"{scope_key} entry outcomes show weighted bad_rate={bad_rate:.2f} across effective_n={effective_sample_count:.2f}",
                    _dumps(evidence),
                    GOVERNANCE_ELIGIBILITY_VERSION,
                    eligibility_fingerprint,
                    now,
                ),
            )
            suggestions += 1
        payload = {
            "schema_version": "entry_quality_governance.v2",
            "evolution_run_id": str(run.get("run_id") or ""),
            "bucket_count": len(buckets),
            "stats_upserted": stats_upserted,
            "suggestions": suggestions,
            "skipped": skipped,
            "invalidated_v1_suggestions": invalidated_v1,
            "weak_signal_base_threshold": base_signal_threshold,
            "weak_signal_cap_threshold": weak_signal_cap,
            "governance_profile": (
                "balanced_demo" if balanced_demo else "strict_live"
            ),
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "entry_quality_governance", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="entry_quality_governance",
            scope_type="entry_quality",
            action="materialize_governance_suggestions",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    except Exception as exc:
        conn.rollback()
        finish_evolution_run(str(run.get("run_id") or ""), status="failed", summary={"error": str(exc)[:500]}, db_path=db_path)
        raise
    finally:
        conn.close()


def list_autonomous_learning_samples(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 100,
    sample_type: str | None = None,
    label_status: str | None = None,
    position_id: str | None = None,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    clauses = []
    params: list[Any] = []
    if sample_type:
        clauses.append("sample_type=?")
        params.append(str(sample_type))
    if label_status:
        clauses.append("label_status=?")
        params.append(str(label_status))
    if position_id:
        clauses.append("position_id=?")
        params.append(str(position_id))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    conn = _connect(db_path)
    try:
        rows = _execute(
            conn,
            f"""
            SELECT *
            FROM autonomous_learning_sample
            {where}
            ORDER BY event_ts DESC, updated_at DESC
            LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()
        items = []
        for row in rows:
            items.append(
                {
                    "sample_id": str(row["sample_id"] or ""),
                    "sample_type": str(row["sample_type"] or ""),
                    "source_table": str(row["source_table"] or ""),
                    "source_id": str(row["source_id"] or ""),
                    "decision_id": str(row["decision_id"] or ""),
                    "trade_id": str(row["trade_id"] or ""),
                    "position_id": str(row["position_id"] or ""),
                    "symbol": str(row["symbol"] or ""),
                    "timeframe": str(row["timeframe"] or ""),
                    "event_ts": float(row["event_ts"] or 0.0),
                    "label_status": str(row["label_status"] or ""),
                    "integrity": str(row["integrity"] or ""),
                    "train_weight": float(row["train_weight"] or 0.0),
                    "config_version": int(row["config_version"] or 0) if "config_version" in row.keys() else 0,
                    "config_hash": str(row["config_hash"] or "") if "config_hash" in row.keys() else "",
                    "evolution_run_id": str(row["evolution_run_id"] or "") if "evolution_run_id" in row.keys() else "",
                    "system_contaminated": bool(row["system_contaminated"]) if "system_contaminated" in row.keys() else False,
                    "governance_eligible": bool(row["governance_eligible"]) if "governance_eligible" in row.keys() else False,
                    "governance_effective_weight": float(row["governance_effective_weight"] or 0.0) if "governance_effective_weight" in row.keys() else 0.0,
                    "governance_eligibility_version": str(row["governance_eligibility_version"] or "") if "governance_eligibility_version" in row.keys() else "",
                    "governance_eligibility_fingerprint": str(row["governance_eligibility_fingerprint"] or "") if "governance_eligibility_fingerprint" in row.keys() else "",
                    "governance_ineligible_reason": str(row["governance_ineligible_reason"] or "") if "governance_ineligible_reason" in row.keys() else "",
                    "features": _loads(row["features_json"], {}),
                    "verdict": _loads(row["verdict_json"], {}),
                    "label": _loads(row["label_json"], {}),
                    "trace": _loads(row["trace_json"], {}),
                    "evidence_contract": _loads(row["evidence_contract_json"], {}),
                    "created_at": float(row["created_at"] or 0.0),
                    "updated_at": float(row["updated_at"] or 0.0),
                }
            )
        return {"items": items, "count": len(items)}
    finally:
        conn.close()


def _rebuilt_evidence_contract_from_sample(row: Any) -> dict[str, Any]:
    item = {
        "sample_id": str(row["sample_id"] or ""),
        "sample_type": str(row["sample_type"] or ""),
        "source_table": str(row["source_table"] or ""),
        "source_id": str(row["source_id"] or ""),
        "decision_id": str(row["decision_id"] or ""),
        "trade_id": str(row["trade_id"] or ""),
        "position_id": str(row["position_id"] or ""),
        "label_status": str(row["label_status"] or "pending"),
        "integrity": row["integrity"] or "missing",
        "train_weight": row["train_weight"] if row["train_weight"] is not None else 0.0,
        "config_version": int(row["config_version"] or 0) if "config_version" in row.keys() else 0,
        "config_hash": str(row["config_hash"] or "") if "config_hash" in row.keys() else "",
        "features": _loads(row["features_json"], {}),
        "verdict": _loads(row["verdict_json"], {}),
        "label": _loads(row["label_json"], {}),
        "trace": _loads(row["trace_json"], {}),
        "evidence_contract": _loads(row["evidence_contract_json"], {}),
    }
    trace = item["trace"] if isinstance(item["trace"], dict) else {}
    item["verified_recovered"] = bool(trace.get("verified_recovered"))
    _, contract, _ = _build_sample_evidence_contract(
        item,
        stored_system_contaminated=(
            row["system_contaminated"]
            if "system_contaminated" in row.keys()
            else None
        ),
    )
    return contract


def validate_evidence_contract_health(*, db_path: str | Path = STATE_DB, limit: int = 10000) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    conn = _connect(db_path, read_only=True)
    counts = {
        "checked": 0,
        "non_matured_allows_supervised_training": 0,
        "model_ready_without_supervised_training": 0,
        "model_ready_non_matured": 0,
        "model_ready_missing_or_incomplete": 0,
        "non_matured_allows_strong_governance": 0,
        "contaminated_allows_strong_governance": 0,
        "contaminated_governance_eligible": 0,
        "contaminated_quality_model_ready": 0,
        "contaminated_quality_executable_governance": 0,
        "open_outcome_missing_target": 0,
        "open_outcome_invalid_or_flat": 0,
        "open_outcome_incomplete_execution": 0,
        "open_outcome_not_trainable": 0,
        "open_outcome_governance_eligible": 0,
        "open_outcome_model_ready": 0,
        "open_consumer_model_ready": 0,
        "open_consumer_not_ready": 0,
        "parse_errors": 0,
    }
    examples: list[dict[str, Any]] = []
    try:
        rows = _execute(
            conn,
            """
            SELECT sample_id, sample_type, label_status, integrity,
                   system_contaminated, governance_eligible,
                   features_json, label_json, trace_json, evidence_contract_json
            FROM autonomous_learning_sample
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            counts["checked"] += 1
            try:
                contract = json.loads(row["evidence_contract_json"] or "{}")
            except Exception:
                contract = {}
                counts["parse_errors"] += 1
            allowed = set(contract.get("allowed_uses") or [])
            quality = contract.get("quality") if isinstance(contract.get("quality"), dict) else {}
            model_ready = bool(contract.get("model_ready"))
            label_status = str(row["label_status"] or "")
            integrity = str(row["integrity"] or "")
            system_contaminated = bool(row["system_contaminated"] or 0)
            governance_eligible = bool(row["governance_eligible"] or 0)
            label_json = _loads(row["label_json"], {})
            complete = bool(_loads(row["features_json"], {})) and bool(label_json) and bool(_loads(row["trace_json"], {}))
            bad_codes = []
            target_blockers = _open_target_blockers(
                {
                    "sample_type": str(row["sample_type"] or ""),
                    "label": label_json,
                }
            )
            if str(row["sample_type"] or "") == "shadow_open_decision" and str(label_json.get("label") or "") == "open_outcome":
                if "missing_open_target_v2" in target_blockers:
                    counts["open_outcome_missing_target"] += 1
                if "flat_or_invalid_open_outcome" in target_blockers:
                    counts["open_outcome_invalid_or_flat"] += 1
                if "incomplete_execution_evidence" in target_blockers:
                    counts["open_outcome_incomplete_execution"] += 1
                if "open_target_not_trainable" in target_blockers:
                    counts["open_outcome_not_trainable"] += 1
                if target_blockers and governance_eligible:
                    counts["open_outcome_governance_eligible"] += 1
                    bad_codes.append("open_outcome_governance_eligible")
                if target_blockers and model_ready:
                    counts["open_outcome_model_ready"] += 1
                    bad_codes.append("open_outcome_model_ready")
                consumer_map = contract.get("consumer_eligibility")
                consumer_map = consumer_map if isinstance(consumer_map, dict) else {}
                open_consumer = consumer_map.get(OPEN_QUALITY_CONSUMER)
                open_consumer = open_consumer if isinstance(open_consumer, dict) else {}
                if bool(open_consumer.get("model_ready")):
                    counts["open_consumer_model_ready"] += 1
                elif open_consumer:
                    counts["open_consumer_not_ready"] += 1
            if label_status != "matured" and "supervised_training" in allowed:
                counts["non_matured_allows_supervised_training"] += 1
                bad_codes.append("non_matured_allows_supervised_training")
            strong_uses = allowed.intersection(
                {"supervised_training", "strong_governance", "executable_governance"}
            )
            if label_status != "matured" and strong_uses:
                counts["non_matured_allows_strong_governance"] += 1
                bad_codes.append("non_matured_allows_strong_governance")
            if system_contaminated and strong_uses:
                counts["contaminated_allows_strong_governance"] += 1
                bad_codes.append("contaminated_allows_strong_governance")
            if system_contaminated and governance_eligible:
                counts["contaminated_governance_eligible"] += 1
                bad_codes.append("contaminated_governance_eligible")
            if system_contaminated and bool(quality.get("model_ready")):
                counts["contaminated_quality_model_ready"] += 1
                bad_codes.append("contaminated_quality_model_ready")
            if system_contaminated and bool(quality.get("executable_governance_allowed")):
                counts["contaminated_quality_executable_governance"] += 1
                bad_codes.append("contaminated_quality_executable_governance")
            if model_ready and "supervised_training" not in allowed:
                counts["model_ready_without_supervised_training"] += 1
                bad_codes.append("model_ready_without_supervised_training")
            if model_ready and label_status != "matured":
                counts["model_ready_non_matured"] += 1
                bad_codes.append("model_ready_non_matured")
            if model_ready and (integrity == "missing" or not complete):
                counts["model_ready_missing_or_incomplete"] += 1
                bad_codes.append("model_ready_missing_or_incomplete")
            if bad_codes and len(examples) < 10:
                examples.append(
                    {
                        "sample_id": str(row["sample_id"] or ""),
                        "sample_type": str(row["sample_type"] or ""),
                        "label_status": label_status,
                        "integrity": integrity,
                        "codes": bad_codes,
                    }
                )
        counts["bad_total"] = sum(
            counts[key]
            for key in (
                "non_matured_allows_supervised_training",
                "model_ready_without_supervised_training",
                "model_ready_non_matured",
                "model_ready_missing_or_incomplete",
                "non_matured_allows_strong_governance",
                "contaminated_allows_strong_governance",
                "contaminated_governance_eligible",
                "contaminated_quality_model_ready",
                "contaminated_quality_executable_governance",
                "open_outcome_governance_eligible",
                "open_outcome_model_ready",
                "parse_errors",
            )
        )
        return {"schema_version": "evidence_contract_health.v1", "counts": counts, "examples": examples}
    finally:
        conn.close()


def entry_context_quality_report(*, db_path: str | Path = STATE_DB, limit: int = 500) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    conn = _connect(db_path, read_only=True)
    fields = [
        "entry_cluster",
        "market_micro_context",
        "bar_context",
        "execution_context",
        "decision_quality_context",
        "event_context",
        "data_quality_context",
        "market_session",
    ]
    coverage = {field: 0 for field in fields}
    missing_examples: list[dict[str, Any]] = []
    open_count = 0
    sample_counts = {
        "matured_open_outcome": 0,
        "model_ready_open_outcome": 0,
        "with_supervised_training": 0,
        "open_consumer_ready_open_outcome": 0,
    }
    try:
        if state_table_exists(conn, "decision_ledger"):
            rows = _execute(
                conn,
                """
                SELECT decision_id, position_id, symbol, decision_ts, action_json
                FROM decision_ledger
                WHERE event_type='open'
                ORDER BY decision_ts DESC, created_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            open_count = len(rows)
            for row in rows:
                action_json = _loads(row["action_json"], {})
                missing: list[str] = []
                for field in fields:
                    present = bool(action_json.get(field))
                    if field == "entry_cluster":
                        present = present or "same_direction_open_count" in action_json
                    if present:
                        coverage[field] += 1
                    else:
                        missing.append(field)
                if missing and len(missing_examples) < 10:
                    missing_examples.append(
                        {
                            "decision_id": str(row["decision_id"] or ""),
                            "position_id": str(row["position_id"] or ""),
                            "symbol": str(row["symbol"] or ""),
                            "decision_ts": float(row["decision_ts"] or 0.0),
                            "missing": missing,
                        }
                    )
        sample_rows = _execute(
            conn,
            """
            SELECT label_json, evidence_contract_json
            FROM autonomous_learning_sample
            WHERE sample_type='shadow_open_decision'
              AND label_status='matured'
            ORDER BY event_ts DESC, created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in sample_rows:
            label = _loads(row["label_json"], {})
            if str(label.get("label") or "") != "open_outcome":
                continue
            sample_counts["matured_open_outcome"] += 1
            contract = _loads(row["evidence_contract_json"], {})
            allowed = set(contract.get("allowed_uses") or [])
            if bool(contract.get("model_ready")):
                sample_counts["model_ready_open_outcome"] += 1
            if "supervised_training" in allowed:
                sample_counts["with_supervised_training"] += 1
            consumer_map = contract.get("consumer_eligibility")
            consumer_map = consumer_map if isinstance(consumer_map, dict) else {}
            open_consumer = consumer_map.get(OPEN_QUALITY_CONSUMER)
            if isinstance(open_consumer, dict) and bool(open_consumer.get("model_ready")):
                sample_counts["open_consumer_ready_open_outcome"] += 1
        ratios = {
            field: round(coverage[field] / max(open_count, 1), 6) if open_count else 0.0
            for field in fields
        }
        missing_total = sum(open_count - coverage[field] for field in fields)
        status = "ok"
        if open_count and any(ratios[field] < 0.95 for field in ("entry_cluster", "bar_context", "execution_context", "market_micro_context")):
            status = "degraded"
        if open_count == 0:
            status = "warming"
        return {
            "schema_version": "entry_context_quality.v1",
            "status": status,
            "limit": int(limit),
            "open_decisions": open_count,
            "coverage": coverage,
            "coverage_ratio": ratios,
            "missing_total": missing_total,
            "samples": sample_counts,
            "missing_examples": missing_examples,
        }
    finally:
        conn.close()


def repair_evidence_contracts(*, db_path: str | Path = STATE_DB, limit: int = 10000) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="evidence_contract_repair", trigger_source="contract_health", db_path=db_path)
    conn = _connect(db_path)
    checked = 0
    repaired = 0
    try:
        rows = _execute(
            conn,
            """
            SELECT *
            FROM autonomous_learning_sample
            ORDER BY
                CASE
                    WHEN governance_eligibility_version<>?
                      OR governance_eligibility_fingerprint=''
                    THEN 0 ELSE 1
                END,
                updated_at DESC,
                created_at DESC
            LIMIT ?
            """,
            (GOVERNANCE_ELIGIBILITY_VERSION, max(1, int(limit))),
        ).fetchall()
        now = time.time()
        for row in rows:
            checked += 1
            rebuilt = _rebuilt_evidence_contract_from_sample(row)
            rebuilt_json = _dumps(rebuilt)
            eligibility_payload = rebuilt.get("governance_eligibility") or {}
            expected = {
                "system_contaminated": 0 if eligibility_payload.get("uncontaminated") else 1,
                "governance_eligible": 1 if eligibility_payload.get("governance_eligible") else 0,
                "governance_effective_weight": float(eligibility_payload.get("governance_effective_weight") or 0.0),
                "governance_eligibility_version": str(eligibility_payload.get("governance_eligibility_version") or ""),
                "governance_eligibility_fingerprint": str(eligibility_payload.get("governance_eligibility_fingerprint") or ""),
                "governance_ineligible_reason": ";".join(eligibility_payload.get("exclusion_reasons") or []),
            }
            current_matches = all(
                (
                    float(row[key] or 0.0) == float(value)
                    if key in {
                        "system_contaminated",
                        "governance_eligible",
                        "governance_effective_weight",
                    }
                    else str(row[key] or "") == str(value)
                )
                for key, value in expected.items()
            )
            if rebuilt_json == str(row["evidence_contract_json"] or "{}") and current_matches:
                continue
            _execute(
                conn,
                """
                UPDATE autonomous_learning_sample
                SET evidence_contract_json=?, system_contaminated=?,
                    governance_eligible=?, governance_effective_weight=?,
                    governance_eligibility_version=?, governance_eligibility_fingerprint=?,
                    governance_ineligible_reason=?, updated_at=?
                WHERE sample_id=?
                """,
                (
                    rebuilt_json,
                    expected["system_contaminated"],
                    expected["governance_eligible"],
                    expected["governance_effective_weight"],
                    expected["governance_eligibility_version"],
                    expected["governance_eligibility_fingerprint"],
                    expected["governance_ineligible_reason"],
                    now,
                    str(row["sample_id"] or ""),
                ),
            )
            repaired += 1
        payload = {
            "schema_version": "evidence_contract_repair.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "checked": checked,
            "repaired": repaired,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "evidence_contract_repair", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="repair_evidence_contracts",
            scope_type="autonomous_learning_sample",
            action="rebuild_contract_json",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def _latest_protection_evidence_before_close(
    conn: Any,
    *,
    position_id: str,
    close_ts: float,
    lookback_sec: float = 3600.0,
) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    lower = float(close_ts or time.time()) - max(1.0, float(lookback_sec or 0.0))
    upper = float(close_ts or time.time())
    try:
        row = _execute(
            conn,
            """
            SELECT decision_id, event_type, action_reason, action_json, risk_state_json, decision_ts
            FROM decision_ledger
            WHERE position_id=?
              AND (
                  event_type LIKE 'supervisor_%'
                  OR event_type IN ('legacy_awe_trailing', 'holding_timeout')
              )
              AND decision_ts <= ?
              AND decision_ts >= ?
            ORDER BY decision_ts DESC
            LIMIT 1
            """,
            (str(position_id), upper, lower),
        ).fetchone()
    except Exception:
        row = None
    if row:
        action_json = _loads(row["action_json"], {})
        risk_state = _loads(row["risk_state_json"], {})
        verdict = action_json.get("supervisor_verdict") or {}
        latest = {
            "decision_id": str(row["decision_id"] or ""),
            "event_type": str(row["event_type"] or ""),
            "action_reason": str(row["action_reason"] or ""),
            "decision_ts": float(row["decision_ts"] or 0.0),
            "seconds_before_close": round(max(0.0, upper - float(row["decision_ts"] or 0.0)), 3),
            "action": str(verdict.get("action") or "").strip(),
            "summary_reason": str(verdict.get("summary_reason") or row["action_reason"] or ""),
            "evidence": _compact_supervisor_mapping(verdict.get("evidence")),
            "recommended_controls": _compact_supervisor_mapping(verdict.get("recommended_controls")),
            "risk_state": _compact_supervisor_mapping(risk_state),
            "source_table": "decision_ledger",
        }
    try:
        trace = _execute(
            conn,
            """
            SELECT trace_id, decision_id, action, summary_reason, event_ts,
                   verdict_json, risk_verdict_json, execution_json, stage, outcome
            FROM position_supervisor_trace
            WHERE position_id=?
              AND event_ts <= ?
              AND event_ts >= ?
              AND action IN ('tighten', 'reduce', 'close')
            ORDER BY event_ts DESC
            LIMIT 1
            """,
            (str(position_id), upper, lower),
        ).fetchone()
    except Exception:
        trace = None
    if trace and (not latest or float(trace["event_ts"] or 0.0) > float(latest.get("decision_ts") or 0.0)):
        verdict = _loads(trace["verdict_json"], {})
        risk_state = _loads(trace["risk_verdict_json"], {})
        execution = _loads(trace["execution_json"], {})
        evidence = _compact_supervisor_mapping(verdict.get("evidence"))
        source = str(evidence.get("protection_source") or "")
        action = str(trace["action"] or "")
        if source == "legacy_awe_trailing":
            event_type = "legacy_awe_trailing"
        elif source == "holding_timeout":
            event_type = "holding_timeout"
        else:
            event_type = f"supervisor_{action}" if action else "position_supervisor_trace"
        latest = {
            "decision_id": str(trace["decision_id"] or ""),
            "trace_id": str(trace["trace_id"] or ""),
            "event_type": event_type,
            "action_reason": str(trace["summary_reason"] or ""),
            "decision_ts": float(trace["event_ts"] or 0.0),
            "seconds_before_close": round(max(0.0, upper - float(trace["event_ts"] or 0.0)), 3),
            "action": action,
            "summary_reason": str(trace["summary_reason"] or ""),
            "evidence": evidence,
            "recommended_controls": _compact_supervisor_mapping(verdict.get("recommended_controls")),
            "risk_state": _compact_supervisor_mapping(risk_state),
            "execution": _compact_supervisor_mapping(
                execution,
                nested_keys=frozenset({"evidence", "controls"}),
            ),
            "stage": str(trace["stage"] or ""),
            "outcome": str(trace["outcome"] or ""),
            "source_table": "position_supervisor_trace",
        }
    return latest


def _classify_review_close_source_from_evidence(close_reason: str, latest: dict[str, Any]) -> str:
    reason = str(close_reason or "")
    if reason == "restart_replay":
        return "restart_replay"
    if latest:
        event_type = str(latest.get("event_type") or "")
        if reason not in {"broker_close", "restart_replay"} and event_type == "supervisor_close":
            return "supervisor_direct_close"
        if reason == "broker_close" and event_type == "supervisor_tighten":
            return "supervisor_tighten_stopout"
        if reason == "broker_close" and event_type == "supervisor_reduce":
            return "supervisor_reduce_partial_or_stopout"
        if reason == "broker_close" and event_type == "supervisor_close":
            return "supervisor_direct_close"
        if reason == "broker_close" and event_type == "legacy_awe_trailing":
            return "legacy_awe_trailing_stopout"
        if reason == "broker_close" and event_type == "holding_timeout":
            return "holding_timeout"
    if reason == "broker_close":
        return "external_broker_close"
    return "unknown_legacy"


def backfill_trade_review_close_sources(*, db_path: str | Path = STATE_DB, limit: int = 10000) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="trade_review_close_source_backfill", trigger_source="review_contract_health", db_path=db_path)
    conn = _connect(db_path)
    checked = 0
    updated = 0
    by_source: dict[str, int] = {}
    try:
        rows = _execute(
            conn,
            """
            SELECT *
            FROM trade_outcome_review
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        now = time.time()
        for row in rows:
            checked += 1
            review = _review_payload_from_row(conn, row)
            if str(review.get("close_reason_source") or "").strip():
                continue
            position_id = str(row["position_id"] or review.get("position_id") or "")
            if not position_id:
                continue
            close_ts = float(review.get("close_ts") or row["created_at"] or 0.0)
            close_reason = str(review.get("close_reason") or "broker_close")
            latest = _latest_protection_evidence_before_close(conn, position_id=position_id, close_ts=close_ts)
            source = _classify_review_close_source_from_evidence(close_reason, latest)
            review["close_reason_source"] = source
            review["inferred_close_supervisor"] = latest
            review["close_reason_source_backfill"] = {
                "schema_version": "close_reason_source_backfill.v1",
                "backfilled_at": now,
                "method": "decision_ledger_or_position_supervisor_trace" if latest else "conservative_no_system_evidence",
            }
            review_id = str(row["review_id"] or "")
            hot_json, archive = _review_storage_parts(
                conn,
                review_id=review_id,
                review=review,
            )
            if archive:
                _execute(
                    conn,
                    """
                    UPDATE trade_outcome_review
                    SET review_json=?, review_archive_hash=?, review_raw_sha256=?, review_raw_bytes=?
                    WHERE review_id=?
                    """,
                    (
                        hot_json,
                        archive["archive_hash"],
                        archive["raw_sha256"],
                        archive["raw_bytes"],
                        review_id,
                    ),
                )
            else:
                _execute(
                    conn,
                    """
                    UPDATE trade_outcome_review
                    SET review_json=?
                    WHERE review_id=?
                    """,
                    (hot_json, review_id),
                )
            updated += 1
            by_source[source] = by_source.get(source, 0) + 1
        payload = {
            "schema_version": "trade_review_close_source_backfill.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "checked": checked,
            "updated": updated,
            "by_source": by_source,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "trade_review_close_source_backfill", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="backfill_close_sources",
            scope_type="trade_outcome_review",
            action="infer_missing_close_reason_source",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def backfill_trade_review_integrity_markers(*, db_path: str | Path = STATE_DB, limit: int = 10000) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="trade_review_integrity_backfill", trigger_source="review_contract_health", db_path=db_path)
    conn = _connect(db_path)
    checked = 0
    updated = 0
    try:
        rows = _execute(
            conn,
            """
            SELECT *
            FROM trade_outcome_review
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, min(max(int(limit) * 10, int(limit)), 50000)),),
        ).fetchall()
        candidate_rows: list[Any] = []
        for row in rows:
            review = _review_payload_from_row(conn, row)
            if (
                not str(review.get("attribution_integrity") or "").strip()
                and not str(review.get("context_integrity") or "").strip()
            ):
                candidate_rows.append(row)
                if len(candidate_rows) >= max(1, int(limit)):
                    break
        rows = candidate_rows
        now = time.time()
        for row in rows:
            checked += 1
            review = _review_payload_from_row(conn, row)
            review["attribution_integrity"] = "missing"
            review["context_integrity"] = "missing"
            review["integrity_backfill"] = {
                "schema_version": "trade_review_integrity_backfill.v1",
                "backfilled_at": now,
                "reason": "legacy_review_missing_integrity_marker",
            }
            review_id = str(row["review_id"] or "")
            hot_json, archive = _review_storage_parts(
                conn,
                review_id=review_id,
                review=review,
            )
            if archive:
                _execute(
                    conn,
                    """
                    UPDATE trade_outcome_review
                    SET review_json=?, review_archive_hash=?, review_raw_sha256=?, review_raw_bytes=?
                    WHERE review_id=?
                    """,
                    (
                        hot_json,
                        archive["archive_hash"],
                        archive["raw_sha256"],
                        archive["raw_bytes"],
                        review_id,
                    ),
                )
            else:
                _execute(
                    conn,
                    """
                    UPDATE trade_outcome_review
                    SET review_json=?
                    WHERE review_id=?
                    """,
                    (hot_json, review_id),
                )
            updated += 1
        payload = {
            "schema_version": "trade_review_integrity_backfill.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "checked": checked,
            "updated": updated,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "trade_review_integrity_backfill", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="backfill_review_integrity",
            scope_type="trade_outcome_review",
            action="mark_missing_legacy_integrity",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def _entry_decision_for_review(conn: Any, review: dict[str, Any], row: Any) -> Any | None:
    if not state_table_exists(conn, "decision_ledger"):
        return None
    entry_decision_id = str(review.get("entry_decision_id") or _row_value(row, "entry_decision_id", "") or "")
    position_id = str(review.get("position_id") or _row_value(row, "position_id", "") or "")
    if entry_decision_id:
        found = _execute(
            conn,
            """
            SELECT *
            FROM decision_ledger
            WHERE decision_id=?
            LIMIT 1
            """,
            (entry_decision_id,),
        ).fetchone()
        if found is not None:
            return found
    if not position_id:
        return None
    return _execute(
        conn,
        """
        SELECT *
        FROM decision_ledger
        WHERE position_id=? AND event_type='open'
        ORDER BY decision_ts DESC
        LIMIT 1
        """,
        (position_id,),
    ).fetchone()


def _order_event_times_for_review(conn: Any, *, decision_id: str, trade_id: str) -> dict[str, float]:
    if not decision_id and not trade_id:
        return {}
    if not state_table_exists(conn, "order_lifecycle_event"):
        return {}
    rows = _execute(
        conn,
        """
        SELECT event_type, event_ts
        FROM order_lifecycle_event
        WHERE (? <> '' AND decision_id=?)
           OR (? <> '' AND trade_id=?)
        ORDER BY event_ts ASC
        """,
        (decision_id, decision_id, trade_id, trade_id),
    ).fetchall()
    out: dict[str, float] = {}
    for event in rows:
        event_type = str(_row_value(event, "event_type", "") or "")
        if event_type and event_type not in out:
            out[event_type] = float(_row_value(event, "event_ts", 0.0) or 0.0)
    return out


def _summary_from_review(review: dict[str, Any], *, pnl: float, outcome_label: str) -> str:
    system_issue = review.get("system_issue_context") if isinstance(review.get("system_issue_context"), dict) else {}
    labels = list((system_issue or {}).get("labels") or [])
    parts = [
        f"trade {review.get('position_id') or ''} closed pnl={float(pnl):.2f}",
        f"outcome={outcome_label}",
    ]
    primary = str(review.get("primary_responsibility") or (review.get("failure_taxonomy") or {}).get("primary_responsibility") or "")
    if primary:
        parts.append(f"primary_responsibility={primary}")
    if labels:
        parts.append(f"system_issue={','.join(labels[:4])}")
    largest_contribution_factor = str(
        review.get("largest_contribution_factor")
        or review.get("top_factor")
        or review.get("top_weight_factor")
        or ""
    )
    if largest_contribution_factor:
        parts.append(f"largest_contribution_factor={largest_contribution_factor}")
    worst = str(review.get("worst_factor") or "")
    if worst:
        parts.append(f"worst_factor={worst}")
    return "; ".join(parts)


def _merge_review_labels(existing: Any, *groups: Any) -> list[str]:
    labels: list[str] = []
    for source in (existing, *groups):
        if isinstance(source, str):
            source = _loads(source, [])
        if not isinstance(source, list):
            continue
        for label in source:
            text = str(label or "")
            if text and text not in labels:
                labels.append(text)
    return labels


def _review_payload_from_row(conn: Any, row: Any) -> dict[str, Any]:
    """Load a review from its verified archive when the hot row is projected."""
    return _review_payload_value(conn, row)


def _review_storage_parts(
    conn: Any,
    *,
    review_id: str,
    review: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Prepare one bounded hot review and its optional lossless archive."""

    full_review = dict(review)
    normalized = normalize_trade_review_contract(full_review)
    archive = None
    archive_capable = {
        "review_archive_hash",
        "review_raw_sha256",
        "review_raw_bytes",
    } <= state_table_columns(conn, "trade_outcome_review")
    if archive_capable:
        archive = archive_json_payload(
            conn,
            source_table="trade_outcome_review",
            source_id=str(review_id),
            payload_kind="review_json",
            raw_json=_dumps(full_review),
        )
    if archive:
        return _dumps(normalized), archive

    # Pre-15 fixtures/legacy SQLite stores have no archive destination. Keep
    # their previous complete behavior rather than silently dropping recursive
    # branches; production PostgreSQL must use the archive branch above.
    legacy = dict(normalized)
    for key in ("inferred_close_supervisor", "responsibility_domains"):
        if key in full_review:
            legacy[key] = full_review[key]
    return _dumps(legacy), None


def _update_factor_contribution_system_notes(
    conn: Any,
    *,
    review_id: str,
    system_issue: dict[str, Any],
) -> int:
    if not bool((system_issue or {}).get("contaminates_learning")):
        return 0
    if not state_table_exists(conn, "factor_contribution_review"):
        return 0
    rows = _execute(
        conn,
        """
        SELECT id, confidence, notes
        FROM factor_contribution_review
        WHERE review_id=?
        """,
        (review_id,),
    ).fetchall()
    updated = 0
    for item in rows:
        notes = _loads(_row_value(item, "notes", ""), {})
        if not isinstance(notes, dict):
            notes = {}
        notes["system_contaminated"] = True
        notes["system_issue_labels"] = list((system_issue or {}).get("labels") or [])
        notes["factor_training_allowed"] = False
        current_confidence = float(_row_value(item, "confidence", 0.0) or 0.0)
        next_confidence = round(max(0.0, min(current_confidence, current_confidence * 0.2)), 6)
        _execute(
            conn,
            """
            UPDATE factor_contribution_review
            SET confidence=?, notes=?
            WHERE id=?
            """,
            (next_confidence, _dumps(notes), _row_value(item, "id", 0)),
        )
        updated += 1
    return updated


def backfill_trade_review_timing_and_system_markers(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 10000,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(
        run_type="trade_review_timing_system_backfill",
        trigger_source="review_contract_health",
        db_path=db_path,
    )
    conn = _connect(db_path)
    checked = 0
    updated = 0
    factor_rows_updated = 0
    contaminated = 0
    try:
        rows = _execute(
            conn,
            """
            SELECT *
            FROM trade_outcome_review
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        now = time.time()
        for row in rows:
            checked += 1
            review = _review_payload_from_row(conn, row)
            if not isinstance(review, dict):
                continue
            before = _dumps(review)
            entry = _entry_decision_for_review(conn, review, row)
            entry_action = _loads(_row_value(entry, "action_json", "{}"), {}) if entry is not None else {}
            entry_risk_state = _loads(_row_value(entry, "risk_state_json", "{}"), {}) if entry is not None else {}
            entry_decision_id = str(_row_value(entry, "decision_id", "") or review.get("entry_decision_id") or "")
            trade_id = str(_row_value(entry, "trade_id", "") or review.get("trade_id") or _row_value(row, "trade_id", "") or "")
            signal_bar_ts = float(
                _row_value(entry, "decision_ts", 0.0)
                or review.get("signal_bar_ts")
                or review.get("entry_decision_ts")
                or review.get("entry_ts")
                or 0.0
            )
            risk_verdict = entry_action.get("risk_verdict") if isinstance(entry_action, dict) else {}
            temporal_context = (
                ((risk_verdict or {}).get("audit_payload") or {}).get("temporal_context") or {}
                if isinstance(risk_verdict, dict)
                else {}
            )
            order_times = _order_event_times_for_review(
                conn,
                decision_id=entry_decision_id,
                trade_id=trade_id,
            )
            close_ts = float(review.get("close_ts") or _row_value(row, "created_at", 0.0) or 0.0)
            timeframe = str(review.get("timeframe") or _row_value(entry, "timeframe", "") or "")
            entry_timing = build_entry_timing_context(
                signal_bar_ts=signal_bar_ts,
                decision_evaluated_at=temporal_context.get("evaluated_at") or signal_bar_ts,
                order_submitted_at=order_times.get("submitted", 0.0),
                fill_ts=order_times.get("filled", 0.0),
                close_ts=close_ts,
                timeframe=timeframe,
                source="trade_review_backfill",
            )
            decision_freshness = extract_decision_freshness_context(
                entry_action=entry_action if isinstance(entry_action, dict) else {},
                entry_risk_state=entry_risk_state if isinstance(entry_risk_state, dict) else {},
                review_payload=review,
            )

            review["entry_decision_id"] = entry_decision_id or str(review.get("entry_decision_id") or "")
            review["trade_id"] = trade_id or str(review.get("trade_id") or "")
            review["entry_decision_ts"] = signal_bar_ts
            review["signal_bar_ts"] = signal_bar_ts
            review["entry_timing_context"] = entry_timing
            review["decision_freshness_context"] = decision_freshness
            actual_entry_ts = float(entry_timing.get("actual_entry_ts") or 0.0)
            if actual_entry_ts > 0:
                review["entry_ts"] = actual_entry_ts
            actual_holding = float(entry_timing.get("actual_holding_seconds") or 0.0)
            if actual_holding > 0:
                review["holding_seconds"] = round(actual_holding, 3)
                review["holding_minutes"] = round(actual_holding / 60.0, 3)
            review["timing_system_backfill"] = {
                "schema_version": "trade_review_timing_system_backfill.v1",
                "backfilled_at": now,
                "method": "decision_ledger_order_events_runtime_health",
            }
            full_review = dict(review)
            review = normalize_trade_review_contract(
                review,
                entry_quality=review.get("entry_quality", _row_value(row, "entry_quality", 0.0)),
                hold_quality=review.get("hold_quality", _row_value(row, "hold_quality", 0.0)),
                exit_quality=review.get("exit_quality", _row_value(row, "exit_quality", 0.0)),
                regime_fit_score=review.get("regime_fit_score", _row_value(row, "regime_fit_score", 0.0)),
                execution_quality=review.get("execution_quality", _row_value(row, "execution_quality", 0.0)),
            )
            taxonomy = build_failure_taxonomy({**review, "pnl": float(_row_value(row, "pnl", 0.0) or 0.0)})
            review["failure_taxonomy"] = taxonomy
            review["primary_responsibility"] = taxonomy["primary_responsibility"]
            review["responsibility_labels"] = list(taxonomy["responsibility_labels"] or [])
            failure_tags = _merge_review_labels(
                _row_value(row, "failure_tags_json", "[]"),
                review.get("failure_tags") or [],
                taxonomy.get("responsibility_labels") or [],
            )
            review["failure_tags"] = failure_tags
            archive_review = dict(review)
            for key in ("inferred_close_supervisor", "responsibility_domains"):
                if key in full_review:
                    archive_review[key] = full_review[key]
            system_issue = review.get("system_issue_context") if isinstance(review.get("system_issue_context"), dict) else {}
            if bool((system_issue or {}).get("contaminates_learning")):
                contaminated += 1
            summary = _summary_from_review(
                review,
                pnl=float(_row_value(row, "pnl", 0.0) or 0.0),
                outcome_label=str(_row_value(row, "outcome_label", "") or review.get("outcome_label") or ""),
            )
            if _dumps(review) == before:
                continue
            review_id = str(_row_value(row, "review_id", "") or "")
            after, archive = _review_storage_parts(
                conn,
                review_id=review_id,
                review=archive_review,
            )
            if archive:
                _execute(
                    conn,
                    """
                    UPDATE trade_outcome_review
                    SET failure_tags_json=?, summary_text=?, review_json=?,
                        review_archive_hash=?, review_raw_sha256=?, review_raw_bytes=?
                    WHERE review_id=?
                    """,
                    (
                        _dumps(failure_tags), summary, after, archive["archive_hash"],
                        archive["raw_sha256"], archive["raw_bytes"], review_id,
                    ),
                )
            else:
                _execute(
                    conn,
                    """
                    UPDATE trade_outcome_review
                    SET failure_tags_json=?, summary_text=?, review_json=?
                    WHERE review_id=?
                    """,
                    (_dumps(failure_tags), summary, after, review_id),
                )
            factor_rows_updated += _update_factor_contribution_system_notes(
                conn,
                review_id=review_id,
                system_issue=system_issue,
            )
            updated += 1

        payload = {
            "schema_version": "trade_review_timing_system_backfill.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "checked": checked,
            "updated": updated,
            "contaminated": contaminated,
            "factor_contribution_rows_updated": factor_rows_updated,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "trade_review_timing_system_backfill", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="backfill_trade_review_timing_system",
            scope_type="trade_outcome_review",
            action="add_timing_and_system_issue_markers",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def _recommendation_already_materialized(conn, recommendation_id: str) -> bool:
    needle = f"%{recommendation_id}%"
    checks = [
        (
            """
            SELECT 1 FROM policy_suggestion
            WHERE evidence_json LIKE ?
            LIMIT 1
            """,
            (needle,),
        ),
        (
            """
            SELECT 1 FROM parameter_template_release_candidate
            WHERE validation_summary_json LIKE ?
            LIMIT 1
            """,
            (needle,),
        ),
        (
            """
            SELECT 1 FROM jobs
            WHERE kind='parameter_template_validation'
              AND params_json LIKE ?
              AND status IN ('pending','running','done')
            LIMIT 1
            """,
            (needle,),
        ),
    ]
    for sql, params in checks:
        try:
            if _execute(conn, sql, params).fetchone():
                return True
        except Exception:
            continue
    return False


def _offline_deep_auto_submit_allowed(*, db_path: str | Path = STATE_DB) -> tuple[bool, str]:
    try:
        from backend.services.learning_research_jobs import offmarket_high_load_allowed
        from backend.services.runtime_health_projection import RuntimeHealthProjectionService

        projection = RuntimeHealthProjectionService(db_path).latest(max_age_seconds=300.0)
        session = dict(projection.get("market_session") or {}) if projection.get("ok") else {}
        allowed, reason = offmarket_high_load_allowed(session)
        return bool(allowed), str(reason or "")
    except Exception as exc:
        return False, f"market_session_unavailable:{exc}"


def materialize_parameter_template_recommendations(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 20,
    submit_offline_deep: bool = True,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    from backend.jobs import get_job_manager
    from backend.services.parameter_template_validation import run_parameter_template_offline_validation
    from backend.services.parameter_templates import ParameterTemplateService

    service = ParameterTemplateService(str(db_path))
    recommendations = service.list_recommendations(limit=limit)
    conn = _connect(db_path)
    counts = {"suggested": 0, "offline_jobs": 0, "skipped_existing": 0, "skipped_offmarket": 0, "errors": 0}
    items: list[dict[str, Any]] = []
    try:
        for recommendation in recommendations:
            recommendation_id = str(recommendation.get("recommendation_id") or "")
            if not recommendation_id:
                continue
            if _recommendation_already_materialized(conn, recommendation_id):
                counts["skipped_existing"] += 1
                continue
            try:
                action = str(recommendation.get("recommended_action") or "")
                if action == "offline_validate":
                    if not submit_offline_deep:
                        counts["skipped_offmarket"] += 1
                        items.append({"recommendation_id": recommendation_id, "mode": "offline_validate", "skipped": "offline_deep_disabled"})
                        continue
                    allowed, reason = _offline_deep_auto_submit_allowed(db_path=db_path)
                    if not allowed:
                        counts["skipped_offmarket"] += 1
                        items.append({"recommendation_id": recommendation_id, "mode": "offline_validate", "skipped": reason})
                        continue
                    boundary = dict(recommendation.get("boundary") or {})
                    params = {
                        "factor_id": str(recommendation.get("factor_id") or ""),
                        "template_id": str(recommendation.get("target_template_id") or ""),
                        "regime_key": str(recommendation.get("regime_key") or ""),
                        "recommended_scope": boundary.get("recommended_scope"),
                        "boundary_reasons": list(boundary.get("reasons") or []),
                        "recommendation_context": {
                            "source": "autonomous_learning",
                            "recommendation_id": recommendation_id,
                            "reason": recommendation.get("reason", ""),
                            "responsibility": dict(recommendation.get("responsibility") or {}),
                            "approval_path": recommendation.get("approval_path", ""),
                        },
                    }
                    fn = lambda cb, _params=params: run_parameter_template_offline_validation(_params, cb)
                    js = get_job_manager().submit("parameter_template_validation", params, fn)
                    from backend.core.static_feature_flags import (
                        shared_static_feature_flags,
                    )

                    if not shared_static_feature_flags().pg_job_queue_v2_enabled:
                        # Compatibility jobs still need the historical query
                        # projection.  The durable queue already committed its
                        # row atomically in submit(); rewriting it here could
                        # race a worker claim and turn running back to pending.
                        _execute(
                            conn,
                            """
                            INSERT INTO jobs
                            (id, kind, status, params_json, result_json, progress, error, created_at, updated_at)
                            VALUES (?, 'parameter_template_validation', 'pending', ?, '{}', 0.0, '', ?, ?)
                            ON CONFLICT(id) DO UPDATE SET
                                kind=excluded.kind,
                                status=excluded.status,
                                params_json=excluded.params_json,
                                result_json=excluded.result_json,
                                progress=excluded.progress,
                                error=excluded.error,
                                created_at=excluded.created_at,
                                updated_at=excluded.updated_at
                            """,
                            (js.id, _dumps(params), time.time(), time.time()),
                        )
                    counts["offline_jobs"] += 1
                    items.append({"recommendation_id": recommendation_id, "mode": "offline_validate", "job_id": js.id})
                else:
                    result = service.create_suggestion_from_recommendation(
                        recommendation_id=recommendation_id,
                        note="autonomous materialize from parameter template recommendation",
                    )
                    counts["suggested"] += 1
                    items.append(
                        {
                            "recommendation_id": recommendation_id,
                            "mode": "suggest_switch",
                            "suggestion_id": ((result.get("item") or {}).get("suggestion_id") or ""),
                        }
                    )
            except Exception as exc:
                counts["errors"] += 1
                items.append({"recommendation_id": recommendation_id, "error": str(exc)})
        payload = {
            "schema_version": "parameter_template_auto_materialize.v1",
            "counts": counts,
            "items": items,
        }
        _insert_evolution_event(conn, "parameter_template_auto_materialize", payload)
        conn.commit()
        return payload
    finally:
        conn.close()


def _approve_demo_policy_suggestions(
    conn,
    *,
    experiment_id: str,
    limit: int = 200,
    db_path: str | Path = STATE_DB,
    run_id: str = "",
) -> dict[str, Any]:
    from backend.services.brain_governance_candidates import (
        is_v16_candidate_bridge_evidence,
    )

    allowed_scopes = {
        "factor",
        "parameter_template",
        "position_supervisor_template",
        "entry_cluster",
        "event_window",
        "entry_quality",
    }
    allowed_actions = {
        "boost_small",
        "downweight",
        "switch_parameter_template",
        "relax_thesis_break",
        "tighten_profit_protection",
        "increase_min_hold_window",
        "switch_position_supervisor_template",
        "increase_same_direction_cooldown",
        "raise_pyramid_entry_threshold",
        "tighten_event_window_sizing",
        "extend_event_post_window_review",
        "raise_weak_signal_threshold",
        "require_factor_agreement",
        "suppress_recent_worst_factor",
    }
    rows = _execute(
        conn,
        """
        SELECT *
        FROM policy_suggestion
        WHERE status='proposed'
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    candidate_ids = []
    skipped = []
    now = time.time()
    for row in rows:
        scope_type = str(row["scope_type"] or "")
        action = str(row["action"] or "")
        suggestion_id = str(row["suggestion_id"] or "")
        if scope_type not in allowed_scopes or action not in allowed_actions:
            _execute(
                conn,
                """
                UPDATE policy_suggestion
                SET status='rejected', reviewed_at=?, review_note=?
                WHERE suggestion_id=? AND status='proposed'
                """,
                (
                    now,
                    "system rejected by demo_autonomous: no autonomous execution rule",
                    suggestion_id,
                ),
            )
            conn.commit()
            record_evolution_decision(
                run_id=run_id,
                decision_type="demo_auto_reject",
                scope_type=scope_type,
                scope_key=str(row["scope_key"] or ""),
                action=action,
                status="rejected",
                evidence=_loads(row["evidence_json"], {}),
                before={"status": "proposed", "suggestion_id": suggestion_id},
                after={"status": "rejected", "suggestion_id": suggestion_id},
                result={"experiment_id": experiment_id, "reason": "not_demo_autonomy_whitelisted"},
                db_path=db_path,
            )
            skipped.append({"suggestion_id": suggestion_id, "reason": "system_rejected_not_whitelisted"})
            continue
        evidence = _loads(row["evidence_json"], {})
        if scope_type == "position_supervisor_template":
            if not is_v16_candidate_bridge_evidence(evidence):
                _execute(
                    conn,
                    """
                    UPDATE policy_suggestion
                    SET status='superseded', reviewed_at=?, review_note=?
                    WHERE suggestion_id=? AND status='proposed'
                    """,
                    (
                        now,
                        "superseded: position supervisor advisory is observation-only; V16 candidate bridge is required",
                        suggestion_id,
                    ),
                )
                conn.commit()
                record_evolution_decision(
                    run_id=run_id,
                    decision_type="demo_auto_supersede",
                    scope_type=scope_type,
                    scope_key=str(row["scope_key"] or ""),
                    action=action,
                    status="superseded",
                    evidence=evidence,
                    before={"status": "proposed", "suggestion_id": suggestion_id},
                    after={"status": "superseded", "suggestion_id": suggestion_id},
                    result={"experiment_id": experiment_id, "reason": "v16_candidate_bridge_required"},
                    db_path=db_path,
                )
                skipped.append({
                    "suggestion_id": suggestion_id,
                    "reason": "superseded_non_v16_supervisor_suggestion",
                })
                continue
            has_replay = bool(evidence.get("replay_summary") or evidence.get("replay") or evidence.get("day"))
            has_counterfactual = bool(evidence.get("counterfactual_summary") or evidence.get("counterfactual"))
            if not (has_replay and has_counterfactual):
                _execute(
                    conn,
                    """
                    UPDATE policy_suggestion
                    SET status='rejected', reviewed_at=?, review_note=?
                    WHERE suggestion_id=? AND status='proposed'
                    """,
                    (
                        now,
                        "system rejected by demo_autonomous: missing supervisor evidence",
                        suggestion_id,
                    ),
                )
                conn.commit()
                record_evolution_decision(
                    run_id=run_id,
                    decision_type="demo_auto_reject",
                    scope_type=scope_type,
                    scope_key=str(row["scope_key"] or ""),
                    action=action,
                    status="rejected",
                    evidence=evidence,
                    before={"status": "proposed", "suggestion_id": suggestion_id},
                    after={"status": "rejected", "suggestion_id": suggestion_id},
                    result={"experiment_id": experiment_id, "reason": "missing_supervisor_switch_evidence"},
                    db_path=db_path,
                )
                skipped.append({"suggestion_id": suggestion_id, "reason": "system_rejected_missing_supervisor_evidence"})
                continue
        candidate_ids.append(suggestion_id)
    conn.commit()

    review_result = {"approved": 0, "rejected": 0, "unchanged": 0, "superseded": 0}
    conflict_result = {"winners": 0, "superseded": 0, "items": []}
    if candidate_ids:
        from research.learning.governor import RuleEvolutionGovernor

        governor = RuleEvolutionGovernor(str(db_path))
        review_result = governor.review_pending()
        conflict_result = governor.resolve_conflicts()

    approved = []
    if candidate_ids:
        placeholders = ",".join("?" for _ in candidate_ids)
        reviewed_rows = _execute(
            conn,
            f"""
            SELECT suggestion_id, scope_type, scope_key, action, status, review_note
            FROM policy_suggestion
            WHERE suggestion_id IN ({placeholders})
            """,
            tuple(candidate_ids),
        ).fetchall()
        for reviewed in reviewed_rows:
            status = str(reviewed["status"] or "")
            item = {
                "suggestion_id": str(reviewed["suggestion_id"] or ""),
                "scope_type": str(reviewed["scope_type"] or ""),
                "scope_key": str(reviewed["scope_key"] or ""),
                "action": str(reviewed["action"] or ""),
                "status": status,
                "review_note": str(reviewed["review_note"] or ""),
            }
            if status == "approved":
                approved.append(item)
            elif status in {"rejected", "superseded"}:
                skipped.append({**item, "reason": status})
    return {"approved": approved, "skipped": skipped, "review": review_result, "conflicts": conflict_result}


def _apply_approved_factor_suggestions_for_demo(*, experiment_id: str, limit: int = 1) -> dict[str, Any]:
    from alpha.decision_policy import DecisionPolicy
    from backend.services.factor_weight_change import FactorWeightChangeService
    from config.runtime_config import DEMO_AUTONOMY_MODES, shared as runtime_config
    from research.learning.governor import RuleEvolutionGovernor

    cfg = runtime_config()
    mode = str(getattr(cfg, "autonomy_mode", "") or "").strip().lower()
    if mode not in DEMO_AUTONOMY_MODES:
        return {"attempted": 0, "applied": False, "reason": "demo_mode_required", "items": []}
    conn = _connect(STATE_DB, read_only=True)
    try:
        rows = _execute(
            conn,
            """
            SELECT suggestion_id, scope_key, action, evidence_json
            FROM policy_suggestion
            WHERE scope_type='factor'
              AND status='approved'
              AND action IN ('downweight', 'boost_small')
              AND governance_eligible=1
              AND governance_eligibility_version=?
              AND COALESCE(governance_eligibility_fingerprint, '') <> ''
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (
                GOVERNANCE_ELIGIBILITY_VERSION,
                max(20, min(max(1, int(limit or 1)) * 20, 200)),
            ),
        ).fetchall()
    finally:
        conn.close()
    current_weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
    items: list[dict[str, Any]] = []
    actionable_attempted = 0
    action_limit = max(1, min(int(limit or 1), 20))
    governor = RuleEvolutionGovernor()
    for row in rows:
        suggestion_id = str(row["suggestion_id"] or "")
        factor = str(row["scope_key"] or "")
        action = str(row["action"] or "")
        evidence = _loads(row["evidence_json"], {})
        expected = dict(evidence.get("expected_effect") or {})
        if action == "downweight" and factor not in current_weights:
            note = (
                "superseded before apply: factor is absent from the current "
                "RuntimeConfig weight source of truth; restoring its historical "
                "weight would be a risk expansion"
            )
            governor.set_status(suggestion_id, "superseded", note)
            items.append(
                {
                    "suggestion_id": suggestion_id,
                    "factor": factor,
                    "status": "superseded_stale_runtime_target",
                    "reason": "factor_absent_from_current_runtime_weights",
                }
            )
            continue
        if actionable_attempted >= action_limit:
            break
        actionable_attempted += 1
        old_weight = float(current_weights.get(factor) or 0.0)
        target_weight = float(expected.get("suggested_target_weight") or 0.0)
        if action == "downweight":
            if target_weight <= 0.0 or target_weight >= old_weight:
                target_weight = old_weight * 0.89
            target_weight = max(0.0, min(target_weight, old_weight * 0.95))
        else:
            if target_weight <= old_weight:
                target_weight = old_weight * 1.05
        if old_weight <= 0.0 or abs(target_weight - old_weight) < 1e-9:
            items.append(
                {
                    "suggestion_id": suggestion_id,
                    "factor": factor,
                    "status": "skipped_non_actionable_weight",
                    "old_weight": old_weight,
                    "target_weight": target_weight,
                }
            )
            continue
        result = FactorWeightChangeService().execute(
            source="demo_approved_factor_suggestion",
            producer="autonomous_demo_apply_stepper",
            run_id=f"{experiment_id}:{suggestion_id}",
            actor="system:autonomous_demo_apply_stepper.sync_factor_weights",
            reason="apply approved factor suggestion through governed weight service",
            awe_patches={factor: {"weight": target_weight, "reason": f"approved_{action}"}},
            factor_configs=dict(getattr(cfg, "factor_signal_config", {}) or {}),
            current_weights=current_weights,
            fast=True,
            bypass_for_risk_reduction=action == "downweight",
            decision_policy=DecisionPolicy(min_weight=0.0),
            suggestion_ids_by_factor={factor: [suggestion_id]},
            evidence_by_factor={
                factor: {
                    "approved_factor_suggestion": True,
                    "suggestion_id": suggestion_id,
                    "action": action,
                    "target_weight": target_weight,
                    "governance_evidence": evidence,
                }
            },
            source_agent="factor_governance",
        )
        items.append(
            {
                "suggestion_id": suggestion_id,
                "factor": factor,
                "status": str(result.get("status") or ""),
                "old_weight": old_weight,
                "target_weight": target_weight,
                "applications": result.get("applications") or {},
                "mutation": result.get("mutation") or {},
                "reason": result.get("reason") or "",
            }
        )
        if str(result.get("status") or "") == "applied":
            current_weights.update(dict(result.get("proposed_weights") or {}))
    return {
        "attempted": len(items),
        "actionable_attempted": actionable_attempted,
        "superseded": sum(
            item.get("status") == "superseded_stale_runtime_target" for item in items
        ),
        "applied": any(item.get("status") == "applied" for item in items),
        "items": items,
    }


def _sync_factor_weights_for_demo(*, experiment_id: str) -> dict[str, Any]:
    try:
        verdict = __import__("risk.policy_service", fromlist=["RiskPolicyService"]).RiskPolicyService.shared().evaluate(
            "update_weight",
            {
                "required_mode": "governed",
                "governance": {
                    "experiment_id": experiment_id,
                    "autonomy_mode": _autonomy_mode(),
                },
            },
        ).to_dict()
        if not verdict.get("allowed", False):
            return {"synced": False, "blocked": True, "risk_verdict": verdict}
        approved = _apply_approved_factor_suggestions_for_demo(
            experiment_id=experiment_id,
            limit=1,
        )
        if approved.get("applied"):
            return {
                "synced": True,
                "blocked": False,
                "risk_verdict": verdict,
                "approved_factor_suggestions": approved,
            }
        if int(approved.get("actionable_attempted") or 0) > 0:
            return {
                "synced": False,
                "blocked": True,
                "reason": "approved_factor_suggestion_not_applied",
                "risk_verdict": verdict,
                "approved_factor_suggestions": approved,
            }
        from backend.runtime.evolution_orchestrator import _update_weights

        return {
            "synced": bool(_update_weights()),
            "blocked": False,
            "risk_verdict": verdict,
            "approved_factor_suggestions": approved,
        }
    except Exception as exc:
        return {"synced": False, "blocked": False, "error": str(exc)}


def _auto_apply_parameter_template_suggestions(
    *,
    db_path: str | Path,
    experiment_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    from backend.services.parameter_templates import ParameterTemplateService

    service = ParameterTemplateService(str(db_path))
    conn = _connect(db_path, read_only=True)
    applied = []
    skipped = []
    try:
        rows = _execute(
            conn,
            """
            SELECT *
            FROM policy_suggestion
            WHERE status='approved'
              AND scope_type='parameter_template'
              AND action='switch_parameter_template'
            ORDER BY reviewed_at DESC, created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        suggestion_id = str(row["suggestion_id"] or "")
        evidence = _loads(row["evidence_json"], {})
        target_template_id = str(evidence.get("target_template_id") or "")
        factor_id = str(evidence.get("factor_id") or "")
        regime_key = str(evidence.get("regime_key") or "")
        boundary = evidence.get("boundary") or {}
        if not target_template_id or not factor_id:
            skipped.append({"suggestion_id": suggestion_id, "reason": "missing_target_template"})
            continue
        if str(boundary.get("recommended_scope") or "") != "online_light":
            skipped.append({"suggestion_id": suggestion_id, "reason": "offline_deep_requires_candidate_release"})
            continue
        current = service.get_active_template(factor_id=factor_id, regime_key=regime_key) or {}
        if str(current.get("template_id") or "") == target_template_id:
            skipped.append({"suggestion_id": suggestion_id, "reason": "already_active"})
            continue
        result = service.activate_template(
            factor_id=factor_id,
            template_id=target_template_id,
            regime_key=regime_key,
            suggestion_id=suggestion_id,
            note=f"demo_autonomous apply experiment {experiment_id}",
        )
        if result.get("blocked"):
            skipped.append({"suggestion_id": suggestion_id, "reason": "risk_blocked", "result": result})
        else:
            applied.append({"suggestion_id": suggestion_id, "result": result})
    return {"applied": applied, "skipped": skipped}


def _auto_release_parameter_template_candidates(
    *,
    db_path: str | Path,
    experiment_id: str,
    limit: int = 50,
) -> dict[str, Any]:
    from backend.services.parameter_template_validation import ParameterTemplateValidationService
    from backend.services.parameter_templates import ParameterTemplateService

    service = ParameterTemplateValidationService(str(db_path))
    template_service = ParameterTemplateService(str(db_path))
    candidates = service.list_release_candidates(limit=limit)
    approved = []
    released = []
    rejected = []
    skipped = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        status = str(candidate.get("status") or "")
        summary = candidate.get("validation_summary") or {}
        factor_id = str(candidate.get("factor_id") or "")
        regime_key = str(candidate.get("regime_key") or "")
        template_id = str(candidate.get("template_id") or "")
        if template_id and not template_service.get_template(template_id=template_id):
            try:
                service.ensure_candidate_template_materialized(
                    candidate,
                    template_service=template_service,
                )
            except Exception as exc:
                skipped.append({"candidate_id": candidate_id, "reason": f"template_materialize_failed:{exc}"})
                continue
        if template_id and not template_service.get_template(template_id=template_id):
            if status in {"pending_review", "approved"}:
                try:
                    service.review_release_candidate(
                        candidate_id=candidate_id,
                        status="rejected",
                        note=f"demo_autonomous rejected orphan candidate experiment {experiment_id}",
                    )
                    rejected.append({"candidate_id": candidate_id, "reason": "orphan_template"})
                except Exception as exc:
                    skipped.append({"candidate_id": candidate_id, "reason": f"orphan_reject_failed:{exc}"})
            else:
                skipped.append({"candidate_id": candidate_id, "reason": "orphan_template"})
            continue
        active = template_service.get_active_template(factor_id=factor_id, regime_key=regime_key) or {}
        if template_id and str(active.get("template_id") or "") == template_id:
            skipped.append({"candidate_id": candidate_id, "reason": "already_active"})
            continue
        if status == "pending_review":
            if not bool(summary.get("walk_forward_passed", False)):
                skipped.append({"candidate_id": candidate_id, "reason": "walk_forward_not_passed"})
                continue
            candidate = service.review_release_candidate(
                candidate_id=candidate_id,
                status="approved",
                note=f"auto-approved by demo_autonomous experiment {experiment_id}",
            )
            approved.append(candidate_id)
            status = str(candidate.get("status") or "")
        if status == "approved":
            try:
                result = service.deploy_release_candidate(
                    candidate_id=candidate_id,
                    note=f"demo_autonomous release experiment {experiment_id}",
                )
                if result.get("blocked"):
                    skipped.append({"candidate_id": candidate_id, "reason": "risk_blocked", "result": result})
                else:
                    released.append({"candidate_id": candidate_id, "result": result})
            except Exception as exc:
                skipped.append({"candidate_id": candidate_id, "reason": str(exc)})
    return {"approved": approved, "released": released, "rejected": rejected, "skipped": skipped}


def _auto_apply_position_supervisor_template_suggestions(
    *,
    db_path: str | Path,
    experiment_id: str,
    limit: int = 50,
    run_id: str = "",
) -> dict[str, Any]:
    from backend.services.brain_governance_candidates import (
        is_v16_candidate_bridge_evidence,
    )
    from backend.services.position_supervisor_templates import list_position_supervisor_templates
    from config.runtime_config import (
        DEMO_AUTONOMY_MODES,
        autonomy_expansion_freeze_applies,
        shared as runtime_config,
    )
    from risk.policy_service import RiskPolicyService
    from backend.services.position_supervisor_governance import (
        PositionSupervisorGovernanceMutationService,
        _single_control_candidate_contract,
        materialize_position_supervisor_candidate_observations,
    )
    from backend.services.v16_command_gate import V16CommandGate
    from research.learning.governor import RuleEvolutionGovernor

    cfg = runtime_config()
    candidate_observations = materialize_position_supervisor_candidate_observations(
        db_path=db_path,
        limit=max(1, int(limit) * 20),
        run_id=run_id,
    )
    if autonomy_expansion_freeze_applies(cfg):
        return {
            "applied": [],
            "skipped": [{"reason": "autonomy_expansion_frozen"}],
            "status": "observation_only",
            "candidate_observations": candidate_observations,
        }

    def _template_switch_priority(row) -> tuple[int, float, float]:
        action = str(row["action"] or "")
        target_template_id = str(row["scope_key"] or "")
        priority = {
            "tighten_mfe_capture_protection": 100,
            "tighten_profit_protection": 80,
            "relax_thesis_break": 50,
            "increase_min_hold_window": 45,
        }.get(action, 10)
        if target_template_id == "position_supervisor:profit_protection.v1":
            priority += 10
        return (
            priority,
            float(row["confidence"] or 0.0),
            float(row["created_at"] or 0.0),
        )

    RuleEvolutionGovernor(str(db_path)).resolve_conflicts()
    valid_templates = {
        str(item.get("template_id") or "")
        for item in list_position_supervisor_templates(db_path=db_path)
    }
    previous_template_id = str(getattr(cfg, "position_supervisor_template_id", "") or "position_supervisor:default.v1")
    conn = _connect(db_path)
    applied = []
    skipped = []
    try:
        rows = _execute(
            conn,
            """
            SELECT *
            FROM policy_suggestion
            WHERE status='approved'
              AND scope_type='position_supervisor_template'
            ORDER BY reviewed_at DESC, created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        switch_claimed = False
        for row in sorted(rows, key=_template_switch_priority, reverse=True):
            suggestion_id = str(row["suggestion_id"] or "")
            target_template_id = str(row["scope_key"] or "")
            evidence = _loads(row["evidence_json"], {})
            candidate_id = str(evidence.get("candidate_id") or "")
            if not is_v16_candidate_bridge_evidence(evidence):
                _execute(
                    conn,
                    """
                    UPDATE policy_suggestion
                    SET status='superseded', reviewed_at=?, review_note=?
                    WHERE suggestion_id=? AND status='approved'
                    """,
                    (
                        time.time(),
                        "superseded: position supervisor template changes require a V16 candidate bridge",
                        suggestion_id,
                    ),
                )
                conn.commit()
                skipped.append({
                    "suggestion_id": suggestion_id,
                    "reason": "superseded_non_v16_supervisor_suggestion",
                })
                continue
            if target_template_id.startswith("position_supervisor:auto_"):
                candidate_contract = _single_control_candidate_contract(
                    evidence.get("candidate_template")
                )
                if not candidate_contract.get("ok") or not evidence.get(
                    "generation_context"
                ):
                    _execute(
                        conn,
                        """
                        UPDATE policy_suggestion
                        SET status='superseded', reviewed_at=?, review_note=?
                        WHERE suggestion_id=? AND status='approved'
                        """,
                        (
                            time.time(),
                            "superseded: supervisor candidate missing single-control generation contract",
                            suggestion_id,
                        ),
                    )
                    conn.commit()
                    skipped.append(
                        {
                            "suggestion_id": suggestion_id,
                            "reason": "superseded_invalid_supervisor_candidate_contract",
                            "candidate_contract": candidate_contract,
                        }
                    )
                    continue
            canary_required = max(1, int(getattr(cfg, "supervisor_canary_mature_trade_count", 50) or 50))
            mature_positions: set[str] = set()
            mature_sessions: set[str] = set()
            mature_regimes: set[str] = set()
            accepted_counterfactuals = 0
            page_offset = 0
            page_limit = 2000
            while accepted_counterfactuals < page_limit:
                cf_rows = _execute(
                    conn,
                    f"""
                    SELECT cf.position_id, cf.close_ts, cf.evidence_json,
                           r.review_id AS source_review_id,
                           r.review_json AS source_review_json{_review_archive_select(conn, output="source_review_archive_hash")}
                    FROM supervisor_counterfactual_review cf
                    JOIN trade_outcome_review r ON r.review_id=cf.review_id
                    WHERE cf.close_ts>=?
                      AND EXISTS (
                          SELECT 1
                          FROM position_supervisor_trace t
                          WHERE t.position_id=cf.position_id
                            AND t.template_id=?
                            AND t.stage='learning_shadow'
                            AND t.execution_status='observation_only'
                            AND t.trace_integrity='recovered'
                            AND t.execution_reason=?
                            AND t.event_ts>=?
                      )
                    ORDER BY cf.close_ts DESC, cf.counterfactual_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (
                        float(row["created_at"] or 0.0),
                        target_template_id,
                        f"learning_worker_candidate_replay:{suggestion_id}",
                        float(row["created_at"] or 0.0),
                        page_limit,
                        page_offset,
                    ),
                ).fetchall()
                if not cf_rows:
                    break
                page_offset += len(cf_rows)
                for cf_row in cf_rows:
                    if not _counterfactual_source_is_clean(cf_row, conn):
                        continue
                    cf_evidence = _loads(cf_row["evidence_json"], {})
                    if not bool((cf_evidence.get("maturity") or {}).get("governance_eligible")):
                        continue
                    position_id = str(cf_row["position_id"] or "")
                    if not position_id or position_id in mature_positions:
                        continue
                    mature_positions.add(position_id)
                    accepted_counterfactuals += 1
                    close_hour = time.gmtime(float(cf_row["close_ts"] or 0.0)).tm_hour
                    mature_sessions.add("asia" if close_hour < 7 else "europe" if close_hour < 13 else "us")
                    regime = str(cf_evidence.get("regime") or "")
                    if regime and regime != "unknown":
                        mature_regimes.add(regime)
                    if accepted_counterfactuals >= page_limit:
                        break
                del cf_rows
            evidence_ready = (
                len(mature_positions) >= canary_required
                and len(mature_sessions) >= 2
                and len(mature_regimes) >= 2
            )
            aggressive_demo = str(getattr(cfg, "autonomy_mode", "") or "").lower() in DEMO_AUTONOMY_MODES
            if not evidence_ready and not aggressive_demo:
                skipped.append({
                    "suggestion_id": suggestion_id,
                    "reason": "supervisor_canary_not_ready",
                    "mature_trade_count": len(mature_positions),
                    "required_trade_count": canary_required,
                    "session_count": len(mature_sessions),
                    "regime_count": len(mature_regimes),
                })
                continue
            if switch_claimed:
                _execute(
                    conn,
                    """
                    UPDATE policy_suggestion
                    SET status='superseded', reviewed_at=?, review_note=?
                    WHERE suggestion_id=? AND status='approved'
                    """,
                    (
                        time.time(),
                        "superseded by higher priority position supervisor template switch in same cycle",
                        suggestion_id,
                    ),
                )
                conn.commit()
                skipped.append({"suggestion_id": suggestion_id, "reason": "lower_priority_template_switch"})
                continue
            if target_template_id == previous_template_id:
                skipped.append({"suggestion_id": suggestion_id, "reason": "already_active"})
                switch_claimed = True
                continue
            if target_template_id not in valid_templates:
                _execute(
                    conn,
                    """
                    UPDATE policy_suggestion
                    SET status='rejected', reviewed_at=?, review_note=?
                    WHERE suggestion_id=? AND status='approved'
                    """,
                    (
                        time.time(),
                        "system rejected by demo_autonomous: invalid position supervisor template",
                        suggestion_id,
                    ),
                )
                conn.commit()
                skipped.append({"suggestion_id": suggestion_id, "reason": "invalid_template"})
                continue
            verdict = RiskPolicyService.shared().evaluate(
                "switch_position_supervisor_template",
                {
                    "suggestion_id": suggestion_id,
                    "suggestion_status": "approved",
                    "target_template_id": target_template_id,
                    "previous_template_id": previous_template_id,
                    "evidence": evidence,
                    "experiment_id": experiment_id,
                    "autonomous_apply": True,
                    "autonomy_mode": "demo_autonomous",
                },
            ).to_dict()
            if not verdict.get("allowed", False):
                record_evolution_decision(
                    run_id=run_id,
                    decision_type="apply_switch",
                    scope_type="position_supervisor_template",
                    scope_key=target_template_id,
                    action="switch_position_supervisor_template",
                    status="blocked",
                    evidence=evidence,
                    risk_verdict=verdict,
                    before={"template_id": previous_template_id},
                    after={"template_id": target_template_id},
                    result={"suggestion_id": suggestion_id},
                    db_path=db_path,
                )
                skipped.append({"suggestion_id": suggestion_id, "reason": "risk_blocked", "risk_verdict": verdict})
                continue
            v16_claim = V16CommandGate.claim(
                db_path,
                target_agent="position_supervisor_governance",
                scope_type="supervisor_template",
                scope_key="position_supervisor",
                action="switch_position_supervisor_template",
                candidate_id=candidate_id,
            )
            if not v16_claim.get("allowed", False):
                skipped.append({
                    "suggestion_id": suggestion_id,
                    "reason": str(v16_claim.get("status") or "v16_command_required"),
                    "v16_claim": v16_claim,
                })
                continue
            from backend.services.learning_experiment_admission import LearningExperimentAdmissionService

            experiment_admission = LearningExperimentAdmissionService(db_path).reserve_scope(
                scope_type="position_supervisor_template",
                scope_key=target_template_id,
                action="switch_position_supervisor_template",
                allow_active_replacement=True,
            )
            if not experiment_admission.get("allowed"):
                V16CommandGate.release(
                    db_path,
                    command_id=str(v16_claim.get("command_id") or ""),
                    claim_token=str(v16_claim.get("claim_token") or ""),
                    reason="supervisor_experiment_admission_blocked",
                )
                skipped.append({
                    "suggestion_id": suggestion_id,
                    "reason": str(experiment_admission.get("status") or "experiment_admission_blocked"),
                    "experiment_admission": experiment_admission,
                })
                continue
            reservation_id = str(experiment_admission.get("reservation_id") or "")
            conn.commit()
            governed = PositionSupervisorGovernanceMutationService(
                db_path
            ).switch_template(
                suggestion_id=suggestion_id,
                previous_template_id=previous_template_id,
                target_template_id=target_template_id,
                actor="system:autonomous_learning",
                source="position_supervisor_template_switch",
                run_id=run_id,
                reason=f"demo_autonomous applied suggestion {suggestion_id}",
                evidence=evidence,
                risk_verdict=verdict,
                reservation_id=reservation_id,
                application_details={
                    "experiment_id": experiment_id,
                    "autonomy_mode": str(
                        getattr(cfg, "autonomy_mode", "") or "demo_autonomous"
                    ),
                    "demo_aggressive_governance": aggressive_demo,
                    "canary_evidence_ready": evidence_ready,
                    "canary_evidence": {
                        "mature_trade_count": len(mature_positions),
                        "required_trade_count": canary_required,
                        "session_count": len(mature_sessions),
                        "regime_count": len(mature_regimes),
                    },
                    "v16_command_id": str(v16_claim.get("command_id") or ""),
                },
                v16_command_id=str(v16_claim.get("command_id") or ""),
                v16_claim_token=str(v16_claim.get("claim_token") or ""),
                evidence_fingerprint=str(
                    v16_claim.get("evidence_fingerprint") or ""
                ),
            )
            mutation = dict(governed.get("mutation") or {})
            if not governed.get("committed"):
                V16CommandGate.release(
                    db_path,
                    command_id=str(v16_claim.get("command_id") or ""),
                    claim_token=str(v16_claim.get("claim_token") or ""),
                    reason="supervisor_governance_mutation_blocked",
                )
                skipped.append({
                    "suggestion_id": suggestion_id,
                    "reason": str(
                        mutation.get("status") or "governance_mutation_blocked"
                    ),
                    "mutation": mutation,
                })
                continue
            snapshot = dict(mutation.get("snapshot") or {})
            application_id = str(governed.get("application_id") or "")
            record_evolution_decision(
                run_id=run_id,
                decision_type="apply_switch",
                scope_type="position_supervisor_template",
                scope_key=target_template_id,
                action="switch_position_supervisor_template",
                status="applied",
                evidence=evidence,
                risk_verdict=verdict,
                before={"template_id": previous_template_id},
                after={"template_id": target_template_id},
                result={"suggestion_id": suggestion_id, "application_id": application_id},
                rollback={"previous_template_id": previous_template_id},
                config_version=int(snapshot.get("config_version") or 0),
                config_hash=str(snapshot.get("config_hash") or ""),
                db_path=db_path,
            )
            applied.append(
                {
                    "suggestion_id": suggestion_id,
                    "previous_template_id": previous_template_id,
                    "target_template_id": target_template_id,
                    "application_id": application_id,
                    "mutation_id": str(governed.get("mutation_id") or ""),
                    "projection_ready": bool(governed.get("projection_ready")),
                }
            )
            previous_template_id = target_template_id
            switch_claimed = True
        conn.commit()
        return {
            "applied": applied,
            "skipped": skipped,
            "candidate_observations": candidate_observations,
        }
    finally:
        conn.close()


def _auto_rollback_position_supervisor_template(
    *,
    db_path: str | Path,
    experiment_id: str,
    run_id: str = "",
    min_observed_trades: int = 3,
    max_delta_avg_reward: float = -0.005,
) -> dict[str, Any]:
    from config.runtime_config import shared as runtime_config
    from backend.services.position_supervisor_governance import (
        PositionSupervisorGovernanceMutationService,
    )

    conn = _connect(db_path)
    rolled_back = []
    skipped = []
    try:
        try:
            rows = _execute(
                conn,
                """
                SELECT l.*, e.observed_trade_count, e.delta_avg_reward, e.status AS effect_status
                FROM learning_application_log l
                JOIN learning_application_effect e ON e.application_id = l.application_id
                WHERE l.scope_type='position_supervisor_template'
                  AND l.action='switch_position_supervisor_template'
                  AND l.status IN ('applied', 'observing', 'ineffective')
                  AND e.status IN ('observing', 'ineffective')
                ORDER BY l.created_at DESC
                LIMIT 20
                """
            ).fetchall()
        except Exception as exc:
            return {"rolled_back": [], "skipped": [{"reason": "effect_schema_unavailable", "error": str(exc)}]}
        for row in rows:
            application_id = str(row["application_id"] or "")
            observed = int(row["observed_trade_count"] or 0)
            delta = float(row["delta_avg_reward"] or 0.0)
            details = _loads(row["details_json"], {})
            previous_template_id = str(details.get("previous_template_id") or "")
            target_template_id = str(details.get("target_template_id") or row["scope_key"] or "")
            current_template_id = str(getattr(runtime_config(), "position_supervisor_template_id", "") or "")
            if observed < int(min_observed_trades):
                skipped.append({"application_id": application_id, "reason": "insufficient_observations", "observed": observed})
                continue
            if delta > float(max_delta_avg_reward):
                skipped.append({"application_id": application_id, "reason": "effect_not_negative_enough", "delta_avg_reward": delta})
                continue
            if not previous_template_id:
                skipped.append({"application_id": application_id, "reason": "missing_previous_template"})
                continue
            if current_template_id != target_template_id:
                skipped.append({"application_id": application_id, "reason": "target_not_current", "current_template_id": current_template_id})
                continue
            rollback_evidence = {
                "experiment_id": experiment_id,
                "application_id": application_id,
                "previous_template_id": previous_template_id,
                "rolled_back_from": target_template_id,
                "observed_trade_count": observed,
                "delta_avg_reward": delta,
            }
            conn.commit()
            governed = PositionSupervisorGovernanceMutationService(
                db_path
            ).rollback_template(
                application_id=application_id,
                current_template_id=target_template_id,
                previous_template_id=previous_template_id,
                actor="system:autonomous_learning",
                source="position_supervisor_template_auto_rollback",
                run_id=run_id,
                reason=f"rollback ineffective supervisor template {application_id}",
                evidence=rollback_evidence,
                rollback_details={
                    "schema_version": "position_supervisor_template_rollback.v2",
                    **rollback_evidence,
                },
            )
            mutation = dict(governed.get("mutation") or {})
            if not governed.get("committed"):
                skipped.append({
                    "application_id": application_id,
                    "reason": "runtime_mutation_blocked",
                    "mutation": mutation,
                })
                continue
            snapshot = dict(mutation.get("snapshot") or {})
            rollback = {
                "schema_version": "position_supervisor_template_rollback.v2",
                **rollback_evidence,
                "mutation": mutation,
                "mutation_id": str(governed.get("mutation_id") or ""),
                "config_version": int(snapshot.get("config_version") or 0),
                "config_hash": str(snapshot.get("config_hash") or ""),
            }
            record_evolution_decision(
                run_id=run_id,
                decision_type="auto_rollback",
                scope_type="position_supervisor_template",
                scope_key=target_template_id,
                action="rollback_position_supervisor_template",
                status="rolled_back",
                evidence={"observed_trade_count": observed, "delta_avg_reward": delta},
                before={"template_id": target_template_id},
                after={"template_id": previous_template_id},
                result=rollback,
                rollback={"previous_template_id": previous_template_id},
                config_version=int(snapshot.get("config_version") or 0),
                config_hash=str(snapshot.get("config_hash") or ""),
                db_path=db_path,
            )
            rolled_back.append(rollback)
        conn.commit()
        return {"rolled_back": rolled_back, "skipped": skipped}
    finally:
        conn.close()


def _run_demo_nursery_factor_pruning_governance(*, db_path: str | Path, bridge_limit: int = 5) -> dict[str, Any]:
    mode = _autonomy_mode()
    from config.runtime_config import DEMO_AUTONOMY_MODES

    if mode not in DEMO_AUTONOMY_MODES:
        return {
            "schema_version": "demo_nursery_factor_pruning_governance.v1",
            "enabled": False,
            "mode": mode,
        }
    try:
        from research.factor_governance_lightgbm import FactorGovernanceLightGBMService

        model_advisories = FactorGovernanceLightGBMService(db_path=db_path).materialize_demo_governance_advisories(
            limit=5000,
            min_weakness_score=0.85,
            min_weak_sample_count=2,
            max_factors=10,
        )
    except Exception as exc:
        model_advisories = {
            "schema_version": "factor_governance_demo_bridge.v1",
            "enabled": True,
            "materialized": False,
            "count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    from backend.services.factor_pruning_governance import FactorPruningGovernanceService

    service = FactorPruningGovernanceService(db_path)
    materialize = service.materialize_latest(limit=50, min_priority=0.75, persist=True)
    promote = service.promote_ready(limit=50, min_evidence_score=0.9, require_weak_health=True)
    bridge = service.bridge_ready_candidates(
        limit=max(1, min(int(bridge_limit), 20)),
        require_demo_nursery=True,
        actor="system:autonomous_learning.demo_nursery_factor_pruning",
    )
    return {
        "schema_version": "demo_nursery_factor_pruning_governance.v1",
        "enabled": True,
        "mode": mode,
        "model_advisories": model_advisories,
        "materialize": materialize,
        "promote": promote,
        "bridge": bridge,
        "bridge_limit": max(1, min(int(bridge_limit), 20)),
    }


def apply_demo_autonomy(
    *,
    db_path: str | Path = STATE_DB,
    suggestion_limit: int = 200,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    experiment_id = _new_experiment_id()
    run = start_evolution_run(
        run_type="demo_autonomy_apply",
        trigger_source="autonomous_learning_cycle",
        db_path=db_path,
        run_id=experiment_id,
    )
    if not _demo_autonomous_enabled():
        payload = {
            "schema_version": "demo_autonomy_apply.v1",
            "enabled": False,
            "mode": _autonomy_mode(),
            "experiment_id": experiment_id,
        }
        finish_evolution_run(str(run.get("run_id") or experiment_id), status="skipped", summary=payload, db_path=db_path)
        return payload
    demo_effect_reconcile = {}
    from config.runtime_config import DEMO_AUTONOMY_MODES

    if _autonomy_mode() in DEMO_AUTONOMY_MODES:
        from research.learning.governor import RuleEvolutionGovernor

        # Demo nursery must not let an old observation-only window occupy a
        # scope indefinitely.  One day is long enough to collect a live demo
        # observation, while an absent/insufficient baseline is explicitly
        # closed as inconclusive and retried through a new governed application.
        demo_effect_reconcile = RuleEvolutionGovernor(str(db_path)).reconcile_application_effects(
            application_limit=2000,
            mixed_recheck_after_seconds=0.0,
            max_observation_age_seconds=86400.0,
            terminalize_mixed_after_recheck=True,
        )
    factor_pruning_governance = _run_demo_nursery_factor_pruning_governance(db_path=db_path, bridge_limit=5)
    conn = _connect(db_path)
    try:
        approvals = _approve_demo_policy_suggestions(
            conn,
            experiment_id=experiment_id,
            limit=suggestion_limit,
            db_path=db_path,
            run_id=str(run.get("run_id") or experiment_id),
        )
        _insert_evolution_event(
            conn,
            "demo_autonomy_governor_review",
            {"experiment_id": experiment_id, "factor_pruning_governance": factor_pruning_governance, **approvals},
        )
        conn.commit()
    finally:
        conn.close()

    from research.learning.governor import RuleEvolutionGovernor

    governance_conflicts = RuleEvolutionGovernor(str(db_path)).resolve_conflicts()
    factor_weights = _sync_factor_weights_for_demo(experiment_id=experiment_id)
    parameter_suggestions = _auto_apply_parameter_template_suggestions(
        db_path=db_path,
        experiment_id=experiment_id,
    )
    parameter_candidates = _auto_release_parameter_template_candidates(
        db_path=db_path,
        experiment_id=experiment_id,
    )
    supervisor_templates = _auto_apply_position_supervisor_template_suggestions(
        db_path=db_path,
        experiment_id=experiment_id,
        run_id=str(run.get("run_id") or experiment_id),
    )
    supervisor_rollbacks = _auto_rollback_position_supervisor_template(
        db_path=db_path,
        experiment_id=experiment_id,
        run_id=str(run.get("run_id") or experiment_id),
    )
    payload = {
        "schema_version": "demo_autonomy_apply.v1",
        "enabled": True,
        "mode": _autonomy_mode(),
        "experiment_id": experiment_id,
        "demo_effect_reconcile": demo_effect_reconcile,
        "factor_pruning_governance": factor_pruning_governance,
        "approvals": approvals,
        "governance_conflicts": governance_conflicts,
        "factor_weights": factor_weights,
        "parameter_suggestions": parameter_suggestions,
        "parameter_candidates": parameter_candidates,
        "supervisor_templates": supervisor_templates,
        "supervisor_rollbacks": supervisor_rollbacks,
    }
    conn = _connect(db_path)
    try:
        _insert_evolution_event(conn, "demo_autonomy_apply", payload)
        conn.commit()
    finally:
        conn.close()
    finish_evolution_run(str(run.get("run_id") or experiment_id), status="completed", summary=payload, db_path=db_path)
    return payload


def materialize_portfolio_shadow_trades(
    *, db_path: str | Path = STATE_DB, limit: int = 1000,
) -> dict[str, Any]:
    """Build a combination-level shadow ledger from matured open outcomes."""
    conn = _connect(db_path)
    inserted = 0
    try:
        rows = _execute(
            conn,
            """
            SELECT sample_id, symbol, timeframe, event_ts, features_json, label_json
            FROM autonomous_learning_sample
            WHERE sample_type='shadow_open_decision' AND label_status='matured'
            ORDER BY event_ts DESC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        for row in rows:
            features = _loads(row["features_json"], {})
            label = _loads(row["label_json"], {})
            action = features.get("action") or {}
            score = float(action.get("score") or action.get("action_score") or features.get("action_score") or 0.0)
            direction = int(action.get("direction") or (1 if score > 0 else -1 if score < 0 else 0))
            stable_id = int(hashlib.sha1(f"portfolio:{row['sample_id']}".encode("utf-8")).hexdigest()[:15], 16)
            exists = _execute(conn, "SELECT 1 FROM shadow_trades WHERE id=?", (stable_id,)).fetchone()
            if exists:
                continue
            _execute(
                conn,
                """
                INSERT INTO shadow_trades
                (id, factor, symbol, timeframe, ts, signal, position, pnl, created_at)
                VALUES (?, '__portfolio_shadow__', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id, str(row["symbol"] or ""), str(row["timeframe"] or ""),
                    float(row["event_ts"] or 0.0), score, direction,
                    float(label.get("pnl") or 0.0), time.time(),
                ),
            )
            inserted += 1
        conn.commit()
        return {"schema_version": "portfolio_shadow_ledger.v1", "inserted": inserted, "scanned": len(rows)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def maybe_auto_unfreeze_learning_repair(*, db_path: str | Path = STATE_DB) -> dict[str, Any]:
    """Atomically release the repair freeze only after every safety gate passes."""
    from backend.services.backend_readiness import BackendReadinessService
    from backend.services.release_control import ReleaseControlService
    from backend.services.governance_control_plans import AutonomyControlPlan
    from config.runtime_config import (
        autonomy_expansion_freeze_applies,
        shared as runtime_config,
    )

    cfg = runtime_config()
    if not autonomy_expansion_freeze_applies(cfg):
        if bool(getattr(cfg, "autonomy_expansion_frozen", True)):
            return {
                "status": "demo_governance_not_frozen",
                "ok": True,
                "autonomy_mode": str(getattr(cfg, "autonomy_mode", "") or ""),
                "configured_freeze_retained_for_non_demo": True,
            }
        return {"status": "already_unfrozen", "ok": True}
    readiness = BackendReadinessService(db_path=db_path).build()
    repair = dict(readiness.get("learning_repair") or {})
    replay = dict(readiness.get("replay") or {})
    drift = dict(readiness.get("config_runtime_drift") or {})
    execution = dict(readiness.get("execution_semantics") or {})
    conn = _connect(db_path, read_only=True)
    try:
        verification = _execute(
            conn,
            "SELECT payload_json, timestamp FROM evolution_events WHERE event_type='learning_closure_verification_passed' ORDER BY timestamp DESC LIMIT 1",
        ).fetchone()
        conflict_count = int(_execute(
            conn,
            "SELECT COUNT(*) AS n FROM policy_suggestion WHERE scope_type='position_supervisor_template' AND status IN ('approved','applied')",
        ).fetchone()["n"] or 0)
    finally:
        conn.close()
    checks = {
        "learning_repair": bool(repair.get("ok")),
        "replay": bool(replay.get("ok")),
        "config_drift": not bool(drift.get("drift")) and not bool(drift.get("semantic_drift")),
        "proposal_conflicts": conflict_count <= 1,
        "broker_alignment": not bool(execution.get("blocking_components")),
        "verification": verification is not None,
    }
    if not all(checks.values()):
        return {"ok": False, "status": "freeze_retained", "checks": checks, "learning_repair": repair}

    plan = AutonomyControlPlan(
        patch={"autonomy_expansion_frozen": False},
        source="learning_repair_auto_unfreeze",
        actor="system:learning_repair_release",
        action="auto_unfreeze_expansionary_autonomy",
        run_id=f"learning_repair_unfreeze_{int(time.time())}",
        reason="all learning repair, replay, canary, drift and broker-alignment gates passed",
        scope_type="autonomy_control",
        scope_key="autonomy_expansion_frozen",
        target_agent="governance_control",
        rollback={"autonomy_expansion_frozen": True},
        evidence_refs={
            "checks": checks,
            "learning_repair": repair,
            "verification_timestamp": (
                float(verification["timestamp"] or 0.0) if verification is not None else 0.0
            ),
        },
        current_mode=str(getattr(cfg, "autonomy_mode", "") or "manual"),
        target_mode=str(getattr(cfg, "autonomy_mode", "") or "manual"),
    )
    try:
        mutation = plan.execute(db_path)
    except Exception as exc:
        mutation = {
            "ok": False,
            "status": "governance_mutation_unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    if not mutation.get("ok"):
        return {"ok": False, "status": "freeze_retained_mutation_failed", "checks": checks, "mutation": mutation}
    release_service = ReleaseControlService(db_path)
    release = release_service.start_release(
        release_class="learning_closure_repair",
        summary={"checks": checks, "learning_repair": repair, "mutation": mutation},
        tests=[{"name": "learning_closure_verification", "status": "passed"}],
        rollback_ref={"runtime_config_snapshot": mutation.get("snapshot") or mutation.get("config_snapshot") or {}},
        created_by="system:learning_repair_release",
        readiness=readiness,
    )
    release = release_service.finish_release(
        str(release.get("run_id") or ""),
        status="completed",
        summary={"checks": checks, "learning_repair": repair, "mutation": mutation},
        readiness=readiness,
    )
    conn = _connect(db_path)
    try:
        _insert_evolution_event(conn, "learning_repair_auto_unfrozen", {"checks": checks, "mutation": mutation, "release": release})
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "status": "auto_unfrozen", "checks": checks, "mutation": mutation, "release": release}


def run_autonomous_learning_cycle(
    *,
    db_path: str | Path = STATE_DB,
    sample_limit: int = 500,
    recommendation_limit: int = 20,
    submit_offline_deep: bool = True,
    apply_demo: bool = False,
    mutation_capability: bool = True,
) -> dict[str, Any]:
    from research.learning.governor import RuleEvolutionGovernor
    from backend.services.supervisor_counterfactual import evaluate_counterfactuals

    from config.runtime_config import governance_expansion_is_paused

    started_at = time.time()
    memory_profile: list[dict[str, Any]] = []
    stages: dict[str, dict[str, Any]] = {}
    operator_paused = bool(governance_expansion_is_paused())
    mutation_allowed = bool(mutation_capability and not operator_paused)
    mutation_block = {
        "status": "observation_only" if operator_paused else "mutation_circuit_open",
        "skipped": True,
        "reason": (
            "governance_expansion_paused"
            if operator_paused
            else "worker_observation_continues_without_runtime_mutation"
        ),
    }

    stages["counterfactuals"] = _run_compact_learning_stage(
        "counterfactuals",
        lambda: evaluate_counterfactuals(
            db_path=db_path,
            limit=sample_limit,
            materialize=True,
        ),
        memory_profile,
    )

    def _materialize_supervisor_candidate_observations() -> dict[str, Any]:
        try:
            from backend.services.position_supervisor_governance import (
                materialize_position_supervisor_candidate_observations,
            )

            return materialize_position_supervisor_candidate_observations(
                db_path=db_path,
                limit=sample_limit,
                run_id=f"learning_observation_{int(time.time())}",
            )
        except Exception as exc:
            return {
                "schema_version": "position_supervisor_candidate_observation.v1",
                "status": "unavailable",
                "reason": f"{type(exc).__name__}: {exc}",
                "broker_mutation_allowed": False,
                "inserted": 0,
                "existing": 0,
                "evaluated": 0,
            }

    stages["supervisor_candidate_observations"] = _run_compact_learning_stage(
        "supervisor_candidate_observations",
        _materialize_supervisor_candidate_observations,
        memory_profile,
    )
    stage_operations: tuple[tuple[str, Callable[[], Any]], ...] = (
        (
            "trace_maturation",
            lambda: mature_position_supervisor_traces(db_path=db_path, limit=sample_limit),
        ),
        (
            "review_integrity_backfill",
            lambda: backfill_trade_review_integrity_markers(db_path=db_path, limit=sample_limit),
        ),
        (
            "close_source_backfill",
            lambda: backfill_trade_review_close_sources(db_path=db_path, limit=sample_limit),
        ),
        (
            "samples",
            lambda: materialize_autonomous_learning_samples(db_path=db_path, limit=sample_limit),
        ),
        (
            "portfolio_shadow",
            lambda: materialize_portfolio_shadow_trades(db_path=db_path, limit=sample_limit),
        ),
        (
            "entry_quality_governance",
            lambda: materialize_entry_quality_governance_suggestions(db_path=db_path, limit=sample_limit),
        ),
        (
            "entry_cluster_governance",
            lambda: materialize_entry_cluster_governance_suggestions(db_path=db_path, limit=sample_limit),
        ),
        (
            "event_window_governance",
            lambda: materialize_event_window_governance_suggestions(db_path=db_path, limit=sample_limit),
        ),
        (
            "evidence_contract_repair",
            lambda: repair_evidence_contracts(
                db_path=db_path,
                limit=max(sample_limit, sample_limit * 4),
            ),
        ),
    )
    for stage_name, operation in stage_operations:
        stages[stage_name] = _run_compact_learning_stage(
            stage_name,
            operation,
            memory_profile,
        )

    gov = RuleEvolutionGovernor(str(db_path))
    # Demo autonomy must not let an old observation-only window occupy a
    # scope indefinitely.  demo_nursery already terminalizes via
    # apply_demo_autonomy; demo_autonomous runs the same reconcile here so
    # mixed/observing windows are closed as inconclusive (evidence quality
    # preserved, retry_via_new_application=True) instead of blocking AWE
    # weight adaptation forever.
    _effect_reconcile_kwargs = (
        {
            "mixed_recheck_after_seconds": 0.0,
            "max_observation_age_seconds": 86400.0,
            "terminalize_mixed_after_recheck": True,
        }
        if _autonomy_mode() in {"demo_autonomous", "demo_nursery"}
        else {}
    )
    governance = {
        "review_pending": _run_compact_learning_stage(
            "governance.review_pending",
            gov.review_pending if mutation_allowed else lambda: dict(mutation_block),
            memory_profile,
        ),
        "reconcile_active": _run_compact_learning_stage(
            "governance.reconcile_active",
            gov.reconcile_active if mutation_allowed else lambda: dict(mutation_block),
            memory_profile,
        ),
        "reconcile_application_effects": _run_compact_learning_stage(
            "governance.reconcile_application_effects",
            (
                (lambda: gov.reconcile_application_effects(**_effect_reconcile_kwargs))
                if mutation_allowed
                else (lambda: dict(mutation_block))
            ),
            memory_profile,
        ),
    }
    stages["parameter_template_recommendations"] = _run_compact_learning_stage(
        "parameter_template_recommendations",
        lambda: materialize_parameter_template_recommendations(
            db_path=db_path,
            limit=recommendation_limit,
            submit_offline_deep=submit_offline_deep,
        ),
        memory_profile,
    )
    demo_apply = _run_compact_learning_stage(
        "demo_autonomy",
        (
            (lambda: apply_demo_autonomy(db_path=db_path))
            if apply_demo and mutation_allowed
            else (lambda: {
                "schema_version": "demo_autonomy_apply.v1",
                "enabled": False,
                "mode": _autonomy_mode(),
                "status": (
                    "skipped_explicit_apply_required"
                    if mutation_allowed
                    else str(mutation_block["status"])
                ),
                "reason": (
                    "explicit_apply_not_requested"
                    if mutation_allowed
                    else str(mutation_block["reason"])
                ),
            })
        ),
        memory_profile,
    )
    auto_unfreeze = _run_compact_learning_stage(
        "learning_repair_auto_unfreeze",
        (
            (lambda: maybe_auto_unfreeze_learning_repair(db_path=db_path))
            if mutation_allowed
            else (lambda: {
                "ok": False,
                "status": str(mutation_block["status"]),
                "reason": str(mutation_block["reason"]),
            })
        ),
        memory_profile,
    )
    finished_at = time.time()
    payload = {
        "schema_version": "autonomous_learning_cycle.v2",
        "status": "completed",
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_sec": max(0.0, finished_at - started_at),
        "stages": stages,
        "governance": governance,
        "demo_autonomy": demo_apply,
        "learning_repair_auto_unfreeze": auto_unfreeze,
        "memory_profile": memory_profile,
    }
    conn = _connect(db_path)
    try:
        _insert_evolution_event(conn, "autonomous_learning_cycle", payload)
        conn.commit()
        return payload
    finally:
        conn.close()


def run_watermark_gated_autonomous_learning_cycle(
    *,
    db_path: str | Path = STATE_DB,
    sample_limit: int = 500,
    recommendation_limit: int = 20,
    submit_offline_deep: bool = True,
    mutation_capability: bool = True,
) -> dict[str, Any]:
    """Run one fixed-schedule cycle only when source facts advanced."""

    from backend.services.learning_cycle_watermark import (
        LearningCycleWatermarkService,
    )

    watermark_service = LearningCycleWatermarkService(db_path=db_path)
    gate = watermark_service.evaluate()
    if not gate.get("should_run"):
        return {
            "ok": True,
            "status": "skipped_no_new_facts",
            "watermark": gate,
        }
    result = run_autonomous_learning_cycle(
        db_path=db_path,
        sample_limit=sample_limit,
        recommendation_limit=recommendation_limit,
        submit_offline_deep=submit_offline_deep,
        mutation_capability=mutation_capability,
    )
    watermark_service.mark_completed(gate["current"])
    return {
        **dict(result or {}),
        "ok": True,
        "status": str((result or {}).get("status") or "completed"),
        "watermark": gate,
    }


def schedule_autonomous_learning(
    *,
    delay_sec: float = 420.0,
    interval_sec: float = 1800.0,
    sample_limit: int = 500,
    recommendation_limit: int = 20,
    submit_offline_deep: bool = True,
    mutation_capability: Callable[[], bool] | None = None,
) -> bool:
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return False
    _stop_event.clear()

    def _log_summary(result: dict) -> dict:
        return {
            "schema_version": result.get("schema_version"),
            "status": result.get("status"),
            "duration_sec": result.get("duration_sec"),
            "stages": result.get("stages") or {},
            "governance": result.get("governance") or {},
            "demo_autonomy": result.get("demo_autonomy") or {},
            "memory_profile": result.get("memory_profile") or [],
        }

    def _worker() -> None:
        if _stop_event.wait(max(0.0, delay_sec)):
            return
        while not _stop_event.is_set():
            try:
                result = run_watermark_gated_autonomous_learning_cycle(
                    sample_limit=sample_limit,
                    recommendation_limit=recommendation_limit,
                    submit_offline_deep=submit_offline_deep,
                    mutation_capability=(
                        bool(mutation_capability())
                        if mutation_capability is not None
                        else True
                    ),
                )
                if result.get("status") == "skipped_no_new_facts":
                    logger.info("[autonomous_learning] scheduled run skipped: no new source facts")
                else:
                    logger.info("[autonomous_learning] scheduled run completed: {}", _log_summary(result))
            except Exception as exc:
                logger.warning("[autonomous_learning] scheduled run failed: {}", exc)
            if _stop_event.wait(max(60.0, interval_sec)):
                return

    _scheduler_thread = threading.Thread(
        target=_worker,
        name="autonomous_learning_scheduler",
        daemon=True,
    )
    _scheduler_thread.start()
    return True


def stop_autonomous_learning() -> None:
    _stop_event.set()
