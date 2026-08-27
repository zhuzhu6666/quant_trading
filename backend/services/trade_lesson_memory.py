from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
    state_table_exists,
)
from backend.core.state_store import validate_runtime_state_schema
from backend.services.canonical_v2_reader import canonical_ready, iter_review_rows
from backend.services.position_supervisor_templates import (
    resolve_position_supervisor_binding_lineage,
)


APPEND_SOURCE = "trade_lesson_memory.v1"
ARTIFACT_VERSION = "trade_lesson.v1"


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        value = row[key]
    except Exception:
        value = default
    return default if value is None else value


from backend.core.db_helpers import (
    conn_is_pg as _conn_is_pg,
    execute as _execute,
    load_json as _loads,
    pg_sql as _sql,
)


def _review_payload(conn: Any, row: Any) -> dict[str, Any]:
    payload = _row_get(row, "review_json", {})
    if isinstance(payload, str):
        payload = _loads(payload, {})
    return payload if isinstance(payload, dict) else {}


def trade_review_payload_from_row(row: Any, *, conn: Any | None = None) -> dict[str, Any]:
    """Normalize a review table row for the canonical rich lesson builder.

    The low-level upsert still accepts a row for compatibility with focused
    callers/tests, but production rebuild paths must feed the same parsed
    payload to ``ExperienceBuilder`` so a refresh cannot replace a rich lesson
    with the old compact fallback shape.
    """
    review_json = _review_payload(conn, row) if conn is not None else _loads(_row_get(row, "review_json", "{}"), {})
    if not isinstance(review_json, dict):
        review_json = {}
    failure_tags = _loads(_row_get(row, "failure_tags_json", "[]"), [])
    if not isinstance(failure_tags, list):
        failure_tags = []
    regime_id = _row_get(row, "regime_id", "")
    if not regime_id:
        regime_id = review_json.get("regime_id") or review_json.get("regime") or ""
    return {
        "review_id": str(_row_get(row, "review_id", "") or ""),
        "trade_id": str(_row_get(row, "trade_id", "") or ""),
        "position_id": str(_row_get(row, "position_id", "") or ""),
        "entry_decision_id": str(_row_get(row, "entry_decision_id", "") or ""),
        "exit_decision_id": str(_row_get(row, "exit_decision_id", "") or ""),
        "regime_id": str(regime_id or ""),
        "pnl": _safe_float(_row_get(row, "pnl", 0.0)),
        "mae": _safe_float(_row_get(row, "mae", 0.0)),
        "mfe": _safe_float(_row_get(row, "mfe", 0.0)),
        "outcome_label": str(_row_get(row, "outcome_label", "") or ""),
        "failure_tags": [str(tag) for tag in failure_tags],
        "summary_text": str(_row_get(row, "summary_text", "") or ""),
        "review_json": review_json,
        "created_at": _safe_float(_row_get(row, "created_at", 0.0)),
    }


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def ensure_trade_lesson_memory_schema(conn: Any) -> None:
    table_declaration = """
        CREATE TABLE IF NOT EXISTS experience_memory (
                experience_id TEXT PRIMARY KEY,
                trade_id TEXT DEFAULT '',
                source_table TEXT DEFAULT '',
                source_id TEXT DEFAULT '',
                append_source TEXT DEFAULT '',
                regime_id TEXT DEFAULT '',
                setup_hash TEXT DEFAULT '',
                decision_context_json TEXT DEFAULT '{}',
                outcome_label TEXT DEFAULT '',
                reward_score REAL DEFAULT 0.0,
                failure_tags_json TEXT DEFAULT '[]',
                recommended_action TEXT DEFAULT '',
                evidence_strength REAL DEFAULT 0.0,
                artifact_version TEXT DEFAULT 'v1',
                evolution_run_id TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
        )
    """
    index_declaration = """
        CREATE INDEX IF NOT EXISTS idx_experience_memory_source_append
        ON experience_memory(source_table, source_id, append_source)
    """
    if _conn_is_pg(conn):
        validate_runtime_state_schema(
            conn,
            (table_declaration, index_declaration),
        )
        return

    if not state_table_exists(conn, "experience_memory"):
        _execute(conn, table_declaration)
    cols = state_table_columns(conn, "experience_memory")
    migrations = {
        "source_table": "ALTER TABLE experience_memory ADD COLUMN source_table TEXT DEFAULT ''",
        "source_id": "ALTER TABLE experience_memory ADD COLUMN source_id TEXT DEFAULT ''",
        "append_source": "ALTER TABLE experience_memory ADD COLUMN append_source TEXT DEFAULT ''",
        "evolution_run_id": "ALTER TABLE experience_memory ADD COLUMN evolution_run_id TEXT DEFAULT ''",
    }
    for name, ddl in migrations.items():
        if name not in cols:
            _execute(conn, ddl)
    _execute(
        conn,
        index_declaration,
    )


def _reward_score(pnl: float) -> float:
    if pnl > 0:
        return min(1.0, pnl / max(abs(pnl), 50.0))
    if pnl < 0:
        return -min(1.0, abs(pnl) / max(abs(pnl), 50.0))
    return 0.0


def _recommended_action(outcome_label: str, failure_tags: list[Any], pnl: float) -> str:
    tags = {str(tag) for tag in failure_tags}
    if pnl < 0 or outcome_label in {"bad_loss", "loss"}:
        if "weak_entry_signal" in tags or "entry_quality" in tags:
            return "tighten_entry_review"
        if "event_window" in tags or "event_risk" in tags:
            return "review_event_window"
        return "observe_and_compare"
    if pnl > 0:
        return "reuse_when_context_matches"
    return "watch"



def build_trade_lesson(row: Any, *, conn: Any | None = None) -> dict[str, Any]:
    review_id = str(_row_get(row, "review_id", "") or "")
    review = _review_payload(conn, row) if conn is not None else _loads(_row_get(row, "review_json", "{}"), {})
    binding_lineage = resolve_position_supervisor_binding_lineage(review)
    supervisor_binding = dict(binding_lineage.get("binding") or {})
    supervisor_binding_state = str(binding_lineage.get("state") or "unknown")
    supervisor_binding_reason = str(
        binding_lineage.get("reason") or "binding_missing"
    )
    failure_tags = _loads(_row_get(row, "failure_tags_json", "[]"), [])
    if not isinstance(failure_tags, list):
        failure_tags = []
    pnl = _safe_float(_row_get(row, "pnl", 0.0))
    outcome_label = str(_row_get(row, "outcome_label", "") or "")
    recommended_action = _recommended_action(outcome_label, failure_tags, pnl)
    evidence_strength = max(0.15, min(1.0, abs(_reward_score(pnl)) + 0.12 * len(failure_tags) + 0.25))
    allowed_uses = ["memory_retrieval", "critic_context", "demo_learning_review"]
    confidence = round(evidence_strength, 6)
    result = {
        "pnl": pnl,
        "mae": _safe_float(_row_get(row, "mae", 0.0)),
        "mfe": _safe_float(_row_get(row, "mfe", 0.0)),
        "outcome_label": outcome_label,
    }
    reusable_lesson = str(_row_get(row, "summary_text", "") or outcome_label or "trade lesson")
    context = {
        "schema_version": ARTIFACT_VERSION,
        "market_state": {
            "regime": review.get("regime") or review.get("regime_id") or "",
            "market_session": review.get("market_session") or {},
            "temporal_context": review.get("temporal_context") or {},
            "event_sizing": review.get("event_sizing") or {},
            "decision_freshness_context": review.get("decision_freshness_context") or {},
        },
        "entry_reason": {
            "entry_decision_id": str(_row_get(row, "entry_decision_id", "") or ""),
            "top_factor": review.get("top_factor") or "",
            "largest_contribution_factor": (
                review.get("largest_contribution_factor")
                or review.get("top_factor")
                or ""
            ),
            "top_weight_factor": review.get("top_weight_factor") or "",
            "signal_score": review.get("signal_score"),
            "summary_text": str(_row_get(row, "summary_text", "") or ""),
        },
        "risk_observations": review.get("demo_nursery_observations")
        or review.get("risk_observations")
        or (review.get("risk_verdict") or {}).get("demo_nursery_observations")
        or [],
        "execution_action": {
            "position_id": str(_row_get(row, "position_id", "") or ""),
            "trade_id": str(_row_get(row, "trade_id", "") or ""),
            "exit_decision_id": str(_row_get(row, "exit_decision_id", "") or ""),
            "close_reason": review.get("close_reason") or "",
        },
        "result": result,
        "outcome": result,
        "attribution_tags": [str(tag) for tag in failure_tags],
        "reusable_lesson": reusable_lesson,
        "allowed_uses": allowed_uses,
        "confidence": confidence,
        "recommended_action": recommended_action,
        "position_supervisor_binding_status": supervisor_binding_state,
        "position_supervisor_binding_reason": supervisor_binding_reason,
        "lesson": {
            "recommended_action": recommended_action,
            "summary": reusable_lesson,
            "allowed_uses": allowed_uses,
            "confidence": confidence,
        },
    }
    if supervisor_binding:
        context["position_supervisor_binding"] = supervisor_binding
        context["position_supervisor_binding_template_id"] = str(
            supervisor_binding.get("template_id") or ""
        )
        context["position_supervisor_binding_template_version"] = str(
            supervisor_binding.get("template_version") or ""
        )
        context["position_supervisor_binding_template_hash"] = str(
            supervisor_binding.get("template_hash") or ""
        )
        context["position_supervisor_binding_source"] = str(
            supervisor_binding.get("binding_source") or ""
        )
    setup_hash = hashlib.sha1(
        _dumps(
            {
                "regime": context["market_state"]["regime"],
                "top_factor": context["entry_reason"]["top_factor"],
                "outcome_label": outcome_label,
            }
        ).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "experience_id": f"trade_lesson:{review_id}",
        "trade_id": str(_row_get(row, "trade_id", "") or ""),
        "source_table": "canonical_v2.trade_review",
        "source_id": review_id,
        "append_source": APPEND_SOURCE,
        "regime_id": str(context["market_state"]["regime"] or ""),
        "setup_hash": setup_hash,
        "decision_context_json": _dumps(context),
        "outcome_label": outcome_label,
        "reward_score": round(_reward_score(pnl), 6),
        "failure_tags_json": _dumps(failure_tags),
        "recommended_action": recommended_action,
        "evidence_strength": round(evidence_strength, 6),
        "artifact_version": ARTIFACT_VERSION,
        "evolution_run_id": "",
        "created_at": _safe_float(_row_get(row, "created_at", 0.0), time.time()),
        "position_supervisor_binding": supervisor_binding,
        "position_supervisor_binding_status": supervisor_binding_state,
        "position_supervisor_binding_reason": supervisor_binding_reason,
    }


def upsert_trade_lesson_memory(
    conn: Any,
    row: Any,
    *,
    lesson: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_trade_lesson_memory_schema(conn)
    lesson = dict(lesson or build_trade_lesson(row, conn=conn))
    if not lesson["source_id"]:
        raise ValueError("trade lesson requires review_id")
    context = _loads(lesson["decision_context_json"], {})
    if isinstance(context, dict):
        attribution = _agent_attribution_for_review(conn, row)
        context["agent_attribution"] = attribution
        context["feedback_agents"] = attribution.get("feedback_targets") or []
        lesson["decision_context_json"] = _dumps(context)
    _execute(
        conn,
        """
        INSERT INTO experience_memory
        (experience_id, trade_id, source_table, source_id, append_source,
         regime_id, setup_hash, decision_context_json, outcome_label,
         reward_score, failure_tags_json, recommended_action,
         evidence_strength, artifact_version, evolution_run_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(experience_id) DO UPDATE SET
            trade_id=excluded.trade_id,
            source_table=excluded.source_table,
            source_id=excluded.source_id,
            append_source=excluded.append_source,
            regime_id=excluded.regime_id,
            setup_hash=excluded.setup_hash,
            decision_context_json=excluded.decision_context_json,
            outcome_label=excluded.outcome_label,
            reward_score=excluded.reward_score,
            failure_tags_json=excluded.failure_tags_json,
            recommended_action=excluded.recommended_action,
            evidence_strength=excluded.evidence_strength,
            artifact_version=excluded.artifact_version,
            evolution_run_id=excluded.evolution_run_id,
            created_at=excluded.created_at
        """,
        (
            lesson["experience_id"],
            lesson["trade_id"],
            lesson["source_table"],
            lesson["source_id"],
            lesson["append_source"],
            lesson["regime_id"],
            lesson["setup_hash"],
            lesson["decision_context_json"],
            lesson["outcome_label"],
            lesson["reward_score"],
            lesson["failure_tags_json"],
            lesson["recommended_action"],
            lesson["evidence_strength"],
            lesson["artifact_version"],
            lesson["evolution_run_id"],
            lesson["created_at"],
        ),
    )
    return lesson


def _agent_attribution_for_review(conn: Any, row: Any) -> dict[str, Any]:
    review_id = str(_row_get(row, "review_id", "") or "")
    trade_id = str(_row_get(row, "trade_id", "") or "")
    position_id = str(_row_get(row, "position_id", "") or "")
    keys = [item for item in [review_id, trade_id, position_id] if item]
    participants: list[dict[str, Any]] = []
    shadow_specs = [
        ("open_quality_shadow_audit", "inference_id", [("trade_id", trade_id), ("position_id", position_id)]),
        ("position_quality_shadow_audit", "inference_id", [("review_id", review_id), ("trade_id", trade_id), ("position_id", position_id)]),
        ("factor_governance_shadow_audit", "inference_id", [("review_id", review_id), ("trade_id", trade_id), ("position_id", position_id)]),
    ]
    for table, id_col, clauses in shadow_specs:
        if not state_table_exists(conn, table):
            continue
        where = []
        params = []
        for col, value in clauses:
            if value:
                where.append(f"{col}=?")
                params.append(value)
        if not where:
            continue
        try:
            rows = _execute(
                conn,
                f"SELECT {id_col}, created_at FROM {table} WHERE {' OR '.join(where)} ORDER BY created_at DESC LIMIT 10",
                tuple(params),
            ).fetchall()
        except Exception:
            rows = []
        for item in rows:
            participants.append(
                {
                    "source_agent": "lightgbm_shadow_models",
                    "source_ref_type": table,
                    "source_ref_id": str(_row_get(item, id_col, "") or ""),
                    "role": "shadow_advisory",
                }
            )
    if state_table_exists(conn, "llm_advisory_audit"):
        for key in keys[:3]:
            rows = _execute(
                conn,
                """
                SELECT audit_id
                FROM llm_advisory_audit
                WHERE target_id=?
                ORDER BY created_at DESC
                LIMIT 5
                """,
                (key,),
            ).fetchall()
            for item in rows:
                participants.append(
                    {
                        "source_agent": "llm_advisory",
                        "source_ref_type": "llm_advisory_audit",
                        "source_ref_id": str(_row_get(item, "audit_id", "") or ""),
                        "role": "llm_review",
                    }
                )
    if state_table_exists(conn, "proposal_registry"):
        for key in keys[:3]:
            rows = _execute(
                conn,
                """
                SELECT proposal_id, source_agent, source_ref_type, source_ref_id
                FROM proposal_registry
                WHERE source_ref_id=? OR evidence_refs_json LIKE ?
                ORDER BY updated_at DESC
                LIMIT 10
                """,
                (key, f"%{key}%"),
            ).fetchall()
            for item in rows:
                participants.append(
                    {
                        "source_agent": str(_row_get(item, "source_agent", "") or ""),
                        "source_ref_type": str(_row_get(item, "source_ref_type", "") or "proposal_registry"),
                        "source_ref_id": str(_row_get(item, "source_ref_id", "") or _row_get(item, "proposal_id", "") or ""),
                        "role": "proposal_or_evidence",
                    }
                )
    deduped = []
    seen = set()
    for item in participants:
        source = str(item.get("source_agent") or "")
        if not source:
            continue
        key = (source, item.get("source_ref_type"), item.get("source_ref_id"), item.get("role"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    if not deduped:
        deduped.append(
            {
                "source_agent": "autonomous_learning",
                "source_ref_type": "experience_memory",
                "source_ref_id": f"trade_lesson:{review_id}",
                "role": "lesson_consumer",
            }
        )
    return {
        "schema_version": "trade_lesson_agent_attribution.v1",
        "participants": deduped,
        "feedback_targets": sorted({item["source_agent"] for item in deduped}),
        "linked": bool(deduped),
    }


def rebuild_trade_lesson_memory(db_path: str | Path = STATE_DB, *, limit: int = 500) -> dict[str, Any]:
    from research.learning.experience_builder import ExperienceBuilder

    conn = _connect(db_path)
    try:
        if not canonical_ready(conn):
            return {"ok": False, "status": "missing_canonical_review_source", "upserted": 0}
        rows = iter_review_rows(conn, limit=max(1, int(limit)))
        builder = ExperienceBuilder(db_path=db_path, ensure_schema=False)
        count = 0
        for row in rows:
            builder.build_from_review(trade_review_payload_from_row(row, conn=None), conn=conn)
            count += 1
        conn.commit()
        return {"ok": True, "status": "available", "upserted": count, "append_source": APPEND_SOURCE}
    finally:
        conn.close()
