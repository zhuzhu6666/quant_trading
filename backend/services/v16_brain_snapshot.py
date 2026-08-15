"""V16 Brain State & Memory — merged read-only snapshot layer.

Combines BrainStateService (world model builder from readiness) and
BrainMemoryService (experience/memory retrieval from source tables).

Both are read-only aggregation: they translate existing V15 facts into
display-friendly snapshots. They never mutate runtime config, weights,
orders, positions, learning samples, or broker state.

Previously: brain_state.py (579 lines) + brain_memory.py (592 lines) = 1,171 lines
Now:        ~650 lines (shared helpers extracted to _brain_helpers.py)
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, state_table_columns, state_table_exists
from backend.services._brain_helpers import (
    connect,
    dumps,
    execute,
    loads,
    safe_float,
    text,
)
from backend.services.review_contract import review_has_system_contamination
from backend.services.state_payload_archive import load_json_payload
from backend.services.supervisor_payload_contract import (
    compact_supervisor_mapping as _compact_supervisor_mapping,
)


# ---------------------------------------------------------------------------
# Table helpers
# ---------------------------------------------------------------------------

def _memory_id(source_table: str, source_id: str) -> str:
    raw = f"{source_table}:{source_id}".encode("utf-8")
    return f"mem_{hashlib.sha256(raw).hexdigest()[:24]}"


def _status_from_component(component: dict[str, Any], default: str = "unknown") -> str:
    return str(component.get("status") or component.get("overall") or component.get("mode") or default)


def _review_archive_select(conn: Any, *, alias: str = "r", output: str = "source_review_archive_hash") -> str:
    """Select the authoritative review archive reference when the schema has it."""

    if "review_archive_hash" not in state_table_columns(conn, "trade_outcome_review"):
        return ""
    return f", {alias}.review_archive_hash AS {output}"


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        value = row[key]
    except Exception:
        return default
    return default if value is None else value


def _review_payload_from_row(
    conn: Any,
    row: Any,
    *,
    source_id_key: str,
    inline_key: str,
    archive_key: str = "source_review_archive_hash",
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


_MEMORY_PERSISTED_MAX_KEYS = 64
_MEMORY_PERSISTED_MAX_LIST_ITEMS = 32
_MEMORY_PERSISTED_MAX_STRING = 512
_MEMORY_PERSISTED_NESTED_KEYS = frozenset(
    {"context", "decision_context", "evidence", "lesson", "posterior_reconciliation", "review"}
)
_UNSUPPORTED_PERSISTED_VALUE = object()


def _persisted_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MEMORY_PERSISTED_MAX_STRING]
    return _UNSUPPORTED_PERSISTED_VALUE


def _bounded_persisted_mapping(
    value: Any,
    *,
    nested_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Keep only bounded scalar metadata for rebuildable brain projections."""

    if not isinstance(value, dict):
        return {}
    projected: dict[str, Any] = {}
    for raw_key, raw_value in sorted(value.items(), key=lambda item: str(item[0])):
        key = str(raw_key)[:128]
        scalar = _persisted_scalar(raw_value)
        if scalar is not _UNSUPPORTED_PERSISTED_VALUE:
            projected[key] = scalar
        elif isinstance(raw_value, list):
            values = []
            for item in raw_value[:_MEMORY_PERSISTED_MAX_LIST_ITEMS]:
                item_scalar = _persisted_scalar(item)
                if item_scalar is not _UNSUPPORTED_PERSISTED_VALUE:
                    values.append(item_scalar)
            if values or not raw_value:
                projected[key] = values
        elif key in nested_keys and isinstance(raw_value, dict):
            nested = _bounded_persisted_mapping(raw_value)
            if nested:
                projected[key] = nested
        if len(projected) >= _MEMORY_PERSISTED_MAX_KEYS:
            break
    return projected


def _persisted_memory_item(item: Any) -> dict[str, Any]:
    """Persist identity and bounded metadata, never the source evidence tree."""

    if not isinstance(item, dict):
        return {}
    fields = (
        "memory_id",
        "schema_version",
        "memory_type",
        "source_table",
        "source_id",
        "symbol",
        "timeframe",
        "regime",
        "text_summary",
        "evidence_score",
        "similarity_score",
        "polarity",
        "created_at",
        "evidence_eligible",
    )
    projected: dict[str, Any] = {}
    for key in fields:
        scalar = _persisted_scalar(item.get(key))
        if scalar is not _UNSUPPORTED_PERSISTED_VALUE:
            projected[key] = scalar
    projected["structured"] = _bounded_persisted_mapping(
        item.get("structured"),
        nested_keys=_MEMORY_PERSISTED_NESTED_KEYS,
    )
    sources = item.get("evidence_sources")
    if isinstance(sources, list):
        projected["evidence_sources"] = [
            {
                "source_table": str(source.get("source_table") or "")[:128],
                "source_id": str(source.get("source_id") or "")[:_MEMORY_PERSISTED_MAX_STRING],
            }
            for source in sources[:_MEMORY_PERSISTED_MAX_LIST_ITEMS]
            if isinstance(source, dict)
        ]
    return projected


def _persisted_memory_reference(item: Any) -> dict[str, Any]:
    """Persist only the reference fields used by evidence consumers."""

    if not isinstance(item, dict):
        return {}
    fields = (
        "memory_id",
        "schema_version",
        "memory_type",
        "source_table",
        "source_id",
        "evidence_score",
        "similarity_score",
        "polarity",
        "created_at",
        "evidence_eligible",
    )
    projected: dict[str, Any] = {}
    for key in fields:
        scalar = _persisted_scalar(item.get(key))
        if scalar is not _UNSUPPORTED_PERSISTED_VALUE:
            projected[key] = scalar
    return projected


def _bounded_persisted_memory(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    projected: dict[str, Any] = {}
    for key, raw_value in value.items():
        if key in {"items", "counter_evidence"}:
            projected[key] = [
                _persisted_memory_item(item)
                for item in (raw_value or [])[:_MEMORY_PERSISTED_MAX_LIST_ITEMS]
                if isinstance(item, dict)
            ]
            continue
        if key == "negative_matches":
            projected[key] = [
                _persisted_memory_reference(item)
                for item in (raw_value or [])[:_MEMORY_PERSISTED_MAX_LIST_ITEMS]
                if isinstance(item, dict)
            ]
            continue
        if key == "posterior_memory":
            projected[key] = _persisted_memory_item(raw_value)
            continue
        scalar = _persisted_scalar(raw_value)
        if scalar is not _UNSUPPORTED_PERSISTED_VALUE:
            projected[key] = scalar
        elif isinstance(raw_value, list):
            projected[key] = [
                item_scalar
                for item in raw_value[:_MEMORY_PERSISTED_MAX_LIST_ITEMS]
                if (item_scalar := _persisted_scalar(item)) is not _UNSUPPORTED_PERSISTED_VALUE
            ]
        elif isinstance(raw_value, dict):
            projected[key] = _bounded_persisted_mapping(raw_value)
    return projected


_SUPERVISOR_COUNTERFACTUAL_ACTIONS = {
    "protection_too_tight": ("over_protected", "less_tighten"),
    "premature_tighten": ("over_protected", "less_tighten"),
    "noise_stopout": ("over_protected", "less_tighten"),
    "sl_too_tight": ("over_protected", "less_tighten"),
    "tp_too_near": ("over_protected", "less_tighten"),
    "missed_extension": ("over_protected", "less_tighten"),
    "correct_stop": ("correct_action", "keep"),
    "profit_protected": ("correct_action", "keep"),
    "missed_protection": ("under_protected", "tighten"),
    "sl_too_loose": ("under_protected", "tighten"),
    "tp_too_far": ("under_protected", "tighten"),
    "mfe_capture_failed": ("under_protected", "tighten"),
}

_NON_ACTIONABLE_POLICY_STATUSES = {
    "superseded",
    "rejected",
    "failed",
    "blocked_by_evidence",
}

_POSTERIOR_DIMENSIONS = (
    "signal",
    "factor",
    "entry_threshold",
    "position_sizing",
    "execution",
    "supervision",
    "data",
    "market",
)


def _review_fact_projection(
    review: Any,
    *,
    review_id: Any = "",
    trade_id: Any = "",
    position_id: Any = "",
    pnl: Any = None,
    outcome_label: Any = "",
    failure_tags: Any = None,
) -> dict[str, Any]:
    """Project review facts without carrying the full close-time payload."""
    raw = review if isinstance(review, dict) else {}
    tags = failure_tags if isinstance(failure_tags, list) else raw.get("failure_tags")
    if not isinstance(tags, list):
        tags = []
    projected: dict[str, Any] = {
        "review_id": str(review_id or raw.get("review_id") or ""),
        "trade_id": str(trade_id or raw.get("trade_id") or ""),
        "position_id": str(position_id or raw.get("position_id") or ""),
        "pnl": safe_float(raw.get("pnl") if pnl is None else pnl),
        "outcome_label": str(outcome_label or raw.get("outcome_label") or ""),
        "failure_tags": [str(tag) for tag in tags],
    }
    for key in ("primary_responsibility", "close_reason", "thesis_status"):
        value = raw.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            projected[key] = value
    taxonomy = raw.get("failure_taxonomy")
    if isinstance(taxonomy, dict):
        primary = taxonomy.get("primary_responsibility")
        if primary is not None and not isinstance(primary, (dict, list)):
            projected["failure_taxonomy"] = {"primary_responsibility": primary}
    inferred = raw.get("inferred_close_supervisor")
    if isinstance(inferred, dict):
        projected["inferred_close_supervisor"] = _compact_supervisor_mapping(
            inferred,
            nested_keys=frozenset({"evidence", "recommended_controls", "execution", "risk_state"}),
        )
    return projected


def _build_correction_contract(
    *,
    fingerprint: str,
    selected: dict[str, Any],
    dimension_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project posterior evidence into the existing V16 policy JSON boundary.

    The default is deliberately non-executable.  A caller may provide an
    already governed dimension fact, but it must explicitly prove that the
    downstream effect is executable; source coverage alone never becomes a
    production patch here.
    """
    refs: list[str] = []
    for key in ("source_ref_id", "review_id", "trade_id", "position_id"):
        value = str(selected.get(key) or "")
        if value:
            refs.append(f"{key}:{value}")
    dimensions: dict[str, dict[str, Any]] = {}
    for name in _POSTERIOR_DIMENSIONS:
        dimensions[name] = {
            "evidence_status": "missing",
            "causal_state": "unobservable",
            "action": "no_change",
            "confidence": None,
            "applicable_generation": None,
            "applicable_regime": None,
            "evidence_refs": [],
            "expected_effect": None,
            "rollback_plan": None,
            "reason": "no_canonical_dimension_evidence",
        }

    selected_scope = str(selected.get("causal_scope") or "")
    if selected_scope in {"entry", "supervisor"}:
        dimension_name = "signal" if selected_scope == "entry" else "supervision"
        dimensions[dimension_name].update(
            {
                "evidence_status": "observed",
                "causal_state": "inconclusive",
                "confidence": safe_float(selected.get("confidence")) or None,
                "evidence_refs": list(refs),
                "expected_effect": {
                    "recommended_action": str(selected.get("recommended_action") or "")
                },
                "reason": "single_review_or_counterfactual_is_a_lead_only",
            }
        )

    for name, raw in dict(dimension_evidence or {}).items():
        if name not in dimensions or not isinstance(raw, dict):
            continue
        item = dict(raw)
        evidence_status = str(item.get("evidence_status") or "missing")
        causal_state = str(item.get("causal_state") or "unobservable")
        requested_action = str(item.get("action") or "no_change")
        executable_allowed = bool(item.get("executable_allowed"))
        action = requested_action if executable_allowed else "no_change"
        reason = str(item.get("reason") or "")
        if requested_action != "no_change" and not executable_allowed:
            reason = reason or "dimension_evidence_not_governed_for_execution"
        dimensions[name] = {
            "evidence_status": evidence_status,
            "causal_state": causal_state,
            "action": action,
            "confidence": item.get("confidence"),
            "applicable_generation": item.get("applicable_generation"),
            "applicable_regime": item.get("applicable_regime"),
            "evidence_refs": list(item.get("evidence_refs") or []),
            "expected_effect": item.get("expected_effect"),
            "rollback_plan": item.get("rollback_plan"),
            "reason": reason or "dimension_fact_projected_without_direct_mutation",
        }

    policy_decision_id = "pd_" + hashlib.sha256(
        f"{fingerprint}:v16_brain_policy_decision.v1".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "schema": "v16_brain_policy_decision.v1",
        "policy_decision_id": policy_decision_id,
        "posterior_fingerprint": str(fingerprint or ""),
        "evidence_refs": refs,
        "dimensions": dimensions,
    }


def build_posterior_arbitration(
    *,
    trade_reviews: list[dict[str, Any]] | None = None,
    counterfactuals: list[dict[str, Any]] | None = None,
    dimension_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select the strongest post-close conclusion by causal scope.

    A negative realized PnL can identify an entry/thesis problem while the
    future path independently proves that a supervisor intervention was too
    early.  Those are not competing labels.  V16 must keep both facts and
    dispatch each conclusion to the agent that owns that surface.
    """
    reviews = [dict(item) for item in (trade_reviews or []) if isinstance(item, dict)]
    cfs = [dict(item) for item in (counterfactuals or []) if isinstance(item, dict)]
    supervisor_items: list[dict[str, Any]] = []
    for item in cfs:
        label = str(item.get("label") or "")
        mapped = _SUPERVISOR_COUNTERFACTUAL_ACTIONS.get(label)
        horizons = item.get("horizons") or []
        confidence = max(0.0, min(1.0, safe_float(item.get("confidence"))))
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        tags = list(evidence.get("tags") or [])
        if not mapped or confidence < 0.5 or not horizons:
            continue
        supervisor_items.append({
            "causal_scope": "supervisor",
            "conclusion": mapped[0],
            "recommended_action": mapped[1],
            "counterfactual_label": label,
            "confidence": confidence,
            "evidence_score": round(confidence * (1.0 if "no_future_bars" not in tags else 0.5), 6),
            "source_ref_type": "supervisor_counterfactual_review",
            "source_ref_id": str(item.get("counterfactual_id") or ""),
            "position_id": str(item.get("position_id") or ""),
            "review_id": str(item.get("review_id") or ""),
            "trade_id": str(item.get("trade_id") or ""),
            "evidence_tags": tags,
        })
    supervisor = max(supervisor_items, key=lambda item: item["evidence_score"]) if supervisor_items else {}

    entry = {}
    related_reviews = reviews
    if supervisor:
        review_id = str(supervisor.get("review_id") or "")
        trade_id = str(supervisor.get("trade_id") or "")
        position_id = str(supervisor.get("position_id") or "")
        if review_id:
            related_reviews = [
                item for item in reviews
                if str(item.get("review_id") or item.get("source_id") or "") == review_id
            ]
        elif trade_id:
            related_reviews = [
                item for item in reviews
                if str(item.get("trade_id") or "") == trade_id
            ]
        elif position_id:
            related_reviews = [
                item for item in reviews
                if str(item.get("position_id") or "") == position_id
            ]
        else:
            related_reviews = []
    if related_reviews:
        review = max(related_reviews, key=lambda item: safe_float(item.get("created_at")))
        review_json = review.get("review") if isinstance(review.get("review"), dict) else review.get("review_json")
        if isinstance(review_json, str):
            try:
                review_json = json.loads(review_json)
            except Exception:
                review_json = {}
        if not isinstance(review_json, dict):
            review_json = {}
        failure_taxonomy = review_json.get("failure_taxonomy")
        if not isinstance(failure_taxonomy, dict):
            failure_taxonomy = {}
        failure_tags = review.get("failure_tags") or review.get("failure_tags_json") or review_json.get("failure_tags")
        if isinstance(failure_tags, str):
            try:
                failure_tags = json.loads(failure_tags)
            except Exception:
                failure_tags = [failure_tags] if failure_tags else []
        if not isinstance(failure_tags, list):
            failure_tags = []
        primary = str(
            review_json.get("primary_responsibility")
            or failure_taxonomy.get("primary_responsibility")
            or ""
        )
        outcome = str(review.get("outcome_label") or review_json.get("outcome_label") or "")
        tags = list(failure_tags)
        if primary or outcome or tags:
            entry = {
                "causal_scope": "entry",
                "conclusion": "entry_or_thesis_failure" if safe_float(review.get("pnl", review_json.get("pnl"))) < 0 else "entry_supported",
                "primary_responsibility": primary,
                "outcome_label": outcome,
                "failure_tags": tags,
                "confidence": 0.75 if primary else 0.55,
                "evidence_score": 0.75,
                "source_ref_type": "trade_outcome_review",
                "source_ref_id": str(review.get("review_id") or ""),
                "position_id": str(review.get("position_id") or ""),
                "trade_id": str(review.get("trade_id") or ""),
            }

    # A successful/neutral trade review is useful context but is not an entry
    # correction command.  Only a realized loss is actionable for the entry
    # agent; a mature counterfactual can still independently select the
    # supervisor agent for the same position.
    entry_actionable = entry if entry.get("conclusion") == "entry_or_thesis_failure" else {}
    selected = supervisor or entry_actionable
    selected_scope = str(selected.get("causal_scope") or "")
    conflicts = []
    if supervisor and entry_actionable:
        conflicts.append({
            "type": "causal_scope_overlap_reviewed",
            "status": "separated",
            "scopes": ["entry", "supervisor"],
            "reason": "realized_outcome_judges_entry_thesis; future_path_judges_supervisor_intervention",
        })
    fingerprint_payload = {
        "selected": selected,
        "entry": entry,
        "supervisor": supervisor,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "schema_version": "posterior_arbitration.v1",
        "status": "actionable" if selected else "needs_evidence",
        "selected_scope": selected_scope,
        "selected_conclusion": selected,
        "entry_conclusion": entry,
        "supervisor_conclusion": supervisor,
        "conflicts": conflicts,
        "selection_reason": (
            "mature_counterfactual_has_highest_causal_evidence"
            if supervisor else "trade_outcome_review_is_current_best_source" if entry_actionable else "no_actionable_posterior"
        ),
        "fingerprint": fingerprint,
        "correction_contract": _build_correction_contract(
            fingerprint=fingerprint,
            selected=selected,
            dimension_evidence=dimension_evidence,
        ),
        "authority": {
            "v16_role": "judge_and_dispatch_only",
            "entry_agent": "autonomous_learning",
            "supervisor_agent": "position_supervisor_governance",
            "runtime_mutation_agent": "downstream_governor_only",
        },
    }


def ensure_brain_state_snapshot_table(db_path: str | Path = STATE_DB) -> None:
    conn = connect(db_path)
    try:
        execute(
            conn,
            """CREATE TABLE IF NOT EXISTS brain_state_snapshot (
                snapshot_id TEXT PRIMARY KEY,
                schema_version TEXT DEFAULT 'brain_state_snapshot.v1',
                source TEXT DEFAULT '',
                status TEXT DEFAULT 'computed',
                world_model_json TEXT NOT NULL DEFAULT '{}',
                perceptions_json TEXT NOT NULL DEFAULT '{}',
                memory_json TEXT NOT NULL DEFAULT '{}',
                hypotheses_json TEXT NOT NULL DEFAULT '[]',
                critic_json TEXT NOT NULL DEFAULT '{}',
                evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )""",
        )
        if "memory_json" not in state_table_columns(conn, "brain_state_snapshot"):
            execute(conn, "ALTER TABLE brain_state_snapshot ADD COLUMN memory_json TEXT NOT NULL DEFAULT '{}'")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_state_snapshot_created ON brain_state_snapshot(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_state_snapshot_status ON brain_state_snapshot(status, created_at)")
        conn.commit()
    finally:
        conn.close()


def ensure_brain_memory_table(db_path: str | Path = STATE_DB) -> None:
    conn = connect(db_path)
    try:
        execute(
            conn,
            """CREATE TABLE IF NOT EXISTS brain_memory (
                memory_id TEXT PRIMARY KEY,
                memory_type TEXT DEFAULT '',
                source_table TEXT DEFAULT '',
                source_id TEXT DEFAULT '',
                symbol TEXT DEFAULT '',
                timeframe TEXT DEFAULT '',
                regime TEXT DEFAULT '',
                text_summary TEXT DEFAULT '',
                structured_json TEXT NOT NULL DEFAULT '{}',
                evidence_score REAL NOT NULL DEFAULT 0.0,
                similarity_score REAL NOT NULL DEFAULT 0.0,
                polarity TEXT DEFAULT 'neutral',
                created_at REAL NOT NULL DEFAULT 0.0,
                last_used_at REAL NOT NULL DEFAULT 0.0
            )""",
        )
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_memory_source ON brain_memory(source_table, source_id)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_memory_type ON brain_memory(memory_type, created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_memory_score ON brain_memory(evidence_score, similarity_score)")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# BrainStateService
# ---------------------------------------------------------------------------

class BrainStateService:
    """Read-only V16 brain state snapshot builder.

    Translates existing readiness/autonomy/replay/incident data into
    a world-model, hypotheses, critic verdict, and evidence refs.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "phase": "v16_phase1_read_only_brain",
            "read_only": True,
            "affects_trading": False,
            "does_not_execute_action_plan": True,
            "does_not_mutate_runtime_overlay": True,
            "does_not_change_factor_weights": True,
            "does_not_write_learning_samples": True,
            "risk_policy_service_required_for_future_actions": True,
            "decision_policy_required_for_future_weight_writes": True,
        }

    def build(
        self,
        *,
        readiness: dict[str, Any],
        persist: bool = True,
        source: str = "brain_state_service",
    ) -> dict[str, Any]:
        now = time.time()
        snapshot_id = f"brain_{uuid.uuid4().hex[:16]}"
        perceptions = self._perceptions(readiness, now=now)
        world_model = self._world_model(perceptions)
        hypotheses = self._hypotheses(perceptions, world_model, now=now)
        memory = self._memory(world_model=world_model, hypotheses=hypotheses)
        hypotheses = self._attach_memory_evidence(hypotheses, memory)
        critic = self._critic(hypotheses, world_model, memory)
        evidence_refs = self._evidence_refs(perceptions, memory)
        snapshot = {
            "ok": True,
            "schema_version": "brain_state_snapshot.v1",
            "snapshot_id": snapshot_id,
            "status": "computed",
            "phase": "v16_phase1_read_only_brain",
            "source": str(source or ""),
            "world_model": world_model,
            "perceptions": perceptions,
            "memory": memory,
            "hypotheses": hypotheses,
            "critic": critic,
            "evidence_refs": evidence_refs,
            "boundary": self.boundary(),
            "created_at": now,
            "read_only": True,
            "affects_trading": False,
        }
        if persist:
            self._persist(snapshot)
        return snapshot

    def latest_snapshot(self) -> dict[str, Any]:
        ensure_brain_state_snapshot_table(self.db_path)
        conn = connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_state_snapshot"):
                return self._missing_status("missing_table")
            row = execute(
                conn,
                """SELECT snapshot_id, schema_version, source, status, world_model_json,
                   perceptions_json, memory_json, hypotheses_json, critic_json,
                   evidence_refs_json, boundary_json, created_at
                FROM brain_state_snapshot ORDER BY created_at DESC LIMIT 1""",
            ).fetchone()
            if not row:
                return self._missing_status("missing_snapshot")
            return self._row_to_snapshot(row)
        finally:
            conn.close()

    def status(self) -> dict[str, Any]:
        latest = self.latest_snapshot()
        if not latest.get("snapshot_id"):
            return latest
        age_sec = max(0.0, time.time() - safe_float(latest.get("created_at")))
        posture = latest.get("world_model", {}).get("strategy_posture", "unknown")
        return {
            "ok": True,
            "schema_version": "brain_state_readiness.v1",
            "status": "available",
            "snapshot_id": latest.get("snapshot_id"),
            "age_seconds": round(age_sec, 3),
            "strategy_posture": posture,
            "hypothesis_count": len(latest.get("hypotheses") or []),
            "critic_verdict": latest.get("critic", {}).get("verdict", "unknown"),
            "read_only": True,
            "affects_trading": False,
            "latest_snapshot": latest,
        }

    # -- internal helpers ---------------------------------------------------

    def _persist(self, snapshot: dict[str, Any]) -> None:
        ensure_brain_state_snapshot_table(self.db_path)
        conn = connect(self.db_path)
        try:
            persisted_memory = _bounded_persisted_memory(snapshot["memory"])
            execute(
                conn,
                """INSERT INTO brain_state_snapshot
                (snapshot_id, schema_version, source, status, world_model_json,
                 perceptions_json, memory_json, hypotheses_json, critic_json,
                 evidence_refs_json, boundary_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot["snapshot_id"], snapshot["schema_version"],
                    snapshot["source"], snapshot["status"],
                    dumps(snapshot["world_model"]), dumps(snapshot["perceptions"]),
                    dumps(persisted_memory), dumps(snapshot["hypotheses"]),
                    dumps(snapshot["critic"]), dumps(snapshot["evidence_refs"]),
                    dumps(snapshot["boundary"]), safe_float(snapshot["created_at"]),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _perceptions(readiness: dict[str, Any], *, now: float) -> dict[str, Any]:
        replay = dict(readiness.get("replay") or {})
        autonomy = dict(readiness.get("autonomy_health") or {})
        incident = dict(readiness.get("incident_control") or {})
        governance = dict(readiness.get("governance") or {})
        freshness = dict(readiness.get("governance_freshness") or {})
        live = dict(readiness.get("live") or {})
        system = dict(readiness.get("system_health") or {})
        release = dict(readiness.get("release") or {})
        return {
            "schema_version": "brain_perception_snapshot.v1",
            "generated_at": now,
            "market": {
                "source": "backend_readiness.market_session",
                "status": _status_from_component(dict(readiness.get("market_session") or {})),
                "freshness": "readiness_snapshot",
            },
            "runtime": {
                "source": "backend_readiness.live",
                "ctrader": dict(live.get("ctrader") or {}),
                "loop": dict(live.get("loop") or {}),
                "system_health": system,
                "freshness": "readiness_snapshot",
            },
            "governance": {
                "source": "backend_readiness.governance",
                "status": _status_from_component(governance, "unknown"),
                "freshness": freshness,
            },
            "replay": {
                "source": "backend_readiness.replay",
                "ok": bool(replay.get("ok")),
                "status": _status_from_component(replay, "unknown"),
                "latest_report": dict(replay.get("latest_report") or replay.get("report") or {}),
            },
            "incident_control": {
                "source": "backend_readiness.incident_control",
                "mode": str(incident.get("mode") or "normal"),
                "readiness_effect": dict(incident.get("readiness_effect") or {}),
            },
            "release": {
                "source": "backend_readiness.release",
                "ok": bool(release.get("ok")),
                "latest_release": dict(release.get("latest_release") or {}),
            },
            "autonomy_health": {
                "source": "backend_readiness.autonomy_health",
                "score": safe_float(autonomy.get("score")),
                "posture": str(autonomy.get("posture") or "unknown"),
                "blockers": list(autonomy.get("blockers") or []),
            },
            "readiness": {
                "source": "backend_readiness",
                "ready_for_frontend": bool(readiness.get("ready_for_frontend")),
                "blocker_count": len(readiness.get("blockers") or []),
                "known_observation_count": len(readiness.get("known_observations") or []),
            },
        }

    @staticmethod
    def _world_model(perceptions: dict[str, Any]) -> dict[str, Any]:
        incident_mode = str(perceptions.get("incident_control", {}).get("mode") or "normal")
        autonomy_posture = str(perceptions.get("autonomy_health", {}).get("posture") or "unknown")
        replay_ok = bool(perceptions.get("replay", {}).get("ok"))
        runtime_system = perceptions.get("runtime", {}).get("system_health") or {}
        runtime_status = _status_from_component(runtime_system, "unknown")
        blocker_count = int(perceptions.get("readiness", {}).get("blocker_count") or 0)
        if incident_mode in {"frozen", "only_close"} or autonomy_posture == "frozen":
            strategy_posture = "no_new_risk"
        elif incident_mode in {"shadow_only", "no_new_risk"} or autonomy_posture == "shadow_only":
            strategy_posture = "observation_only"
        elif autonomy_posture == "constrained" or blocker_count > 0 or not replay_ok:
            strategy_posture = "defensive"
        else:
            strategy_posture = "normal"
        if incident_mode in {"frozen", "only_close"}:
            execution_posture = "unsafe"
        elif blocker_count > 0 or runtime_status in {"critical", "degraded"}:
            execution_posture = "degraded"
        elif not runtime_system:
            execution_posture = "unknown"
        else:
            execution_posture = "broker_ok"
        governance_freshness = perceptions.get("governance", {}).get("freshness") or {}
        stale_tables = [
            name for name, item in dict(governance_freshness.get("tables") or {}).items()
            if str((item or {}).get("status") or "") not in {"fresh", "ok"}
        ]
        return {
            "schema_version": "brain_world_model.v1",
            "market_regime": "event_window" if "event" in str(perceptions.get("market", {}).get("status") or "") else "unknown",
            "strategy_posture": strategy_posture,
            "factor_posture": "healthy" if not stale_tables else "unstable",
            "factor_governance_posture": "stale" if stale_tables else "fresh",
            "factor_performance_posture": "not_assessed",
            "execution_posture": execution_posture,
            "execution_evidence_posture": "complete" if runtime_system else "incomplete",
            "replay_evidence_posture": "validated" if replay_ok else "missing",
            "performance_evidence_posture": "not_assessed",
            "learning_posture": "replay_validated" if replay_ok else "warming_up",
            "autonomy_posture": autonomy_posture,
            "incident_mode": incident_mode,
            "stale_governance_tables": stale_tables[:10],
            "read_only": True,
        }

    @staticmethod
    def _hypotheses(perceptions: dict[str, Any], world_model: dict[str, Any], *, now: float) -> list[dict[str, Any]]:
        hypotheses: list[dict[str, Any]] = []
        incident_mode = str(world_model.get("incident_mode") or "normal")
        if incident_mode != "normal":
            hypotheses.append(BrainStateService._hypothesis(
                scope="incident",
                claim=f"runtime incident mode is {incident_mode}; brain should observe only",
                confidence=0.85, evidence_score=0.78, risk_class="high",
                evidence_refs={"incident_control": "backend_readiness.incident_control"}, now=now,
            ))
        if str(world_model.get("autonomy_posture") or "") in {"constrained", "shadow_only", "frozen"}:
            hypotheses.append(BrainStateService._hypothesis(
                scope="autonomy",
                claim="autonomy health limits current brain action scope",
                confidence=0.8,
                evidence_score=max(0.1, safe_float(perceptions.get("autonomy_health", {}).get("score"))),
                risk_class="medium",
                evidence_refs={"autonomy_health": "backend_readiness.autonomy_health"}, now=now,
            ))
        if not bool(perceptions.get("replay", {}).get("ok")):
            hypotheses.append(BrainStateService._hypothesis(
                scope="simulation",
                claim="latest replay evidence is missing or unhealthy; high-impact actions must stay blocked",
                confidence=0.75, evidence_score=0.45, risk_class="medium",
                evidence_refs={"replay": "backend_readiness.replay"}, now=now,
            ))
        stale_tables = list(world_model.get("stale_governance_tables") or [])
        if stale_tables:
            hypotheses.append(BrainStateService._hypothesis(
                scope="factor",
                claim="governance freshness has stale inputs; factor posture should remain cautious",
                confidence=0.65, evidence_score=0.5, risk_class="low",
                evidence_refs={"governance_freshness": "backend_readiness.governance_freshness"}, now=now,
            ))
        if not hypotheses:
            hypotheses.append(BrainStateService._hypothesis(
                scope="runtime",
                claim="no immediate V16 brain objection found; continue read-only observation",
                confidence=0.55, evidence_score=0.6, risk_class="low",
                evidence_refs={"readiness": "backend_readiness"}, now=now,
            ))
        return hypotheses

    @staticmethod
    def _hypothesis(*, scope: str, claim: str, confidence: float, evidence_score: float,
                    risk_class: str, evidence_refs: dict[str, Any], now: float) -> dict[str, Any]:
        return {
            "hypothesis_id": f"hyp_{uuid.uuid4().hex[:12]}",
            "schema_version": "brain_hypothesis.v1",
            "scope": scope, "claim": claim,
            "expected_effect": "read_only_operator_explanation",
            "evidence_refs": evidence_refs, "counter_evidence_refs": {},
            "confidence": round(max(0.0, min(float(confidence), 1.0)), 4),
            "evidence_score": round(max(0.0, min(float(evidence_score), 1.0)), 4),
            "risk_class": risk_class,
            "required_validation": ["continue_read_only_observation"],
            "action_scope": "observe_only",
            "expires_at": now + 900.0,
        }

    @staticmethod
    def _attach_memory_evidence(hypotheses: list[dict[str, Any]], memory: dict[str, Any]) -> list[dict[str, Any]]:
        negative_matches = list(memory.get("negative_matches") or [])
        counter_evidence = list(memory.get("counter_evidence") or [])
        source_gaps = list(memory.get("source_gaps") or [])
        enriched = []
        for hypothesis in hypotheses:
            item = dict(hypothesis)
            if counter_evidence:
                refs = dict(item.get("counter_evidence_refs") or {})
                refs["memory"] = [{"memory_id": m.get("memory_id"), "source_table": m.get("source_table"),
                                    "source_id": m.get("source_id"), "evidence_score": m.get("evidence_score"),
                                    "similarity_score": m.get("similarity_score")} for m in counter_evidence[:3]]
                item["counter_evidence_refs"] = refs
            if negative_matches:
                refs = dict(item.get("evidence_refs") or {})
                refs["negative_memory"] = [{"memory_id": m.get("memory_id"), "source_table": m.get("source_table"),
                                             "source_id": m.get("source_id"), "evidence_score": m.get("evidence_score"),
                                             "similarity_score": m.get("similarity_score")} for m in negative_matches[:3]]
                item["evidence_refs"] = refs
            if source_gaps:
                validation = list(item.get("required_validation") or [])
                validation.append("memory_source_gap_review")
                item["required_validation"] = sorted(set(validation))
            enriched.append(item)
        return enriched

    def _memory(self, *, world_model: dict[str, Any], hypotheses: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            return BrainMemoryService(self.db_path).retrieve(
                world_model=world_model, hypotheses=hypotheses, limit=12, persist=True,
            )
        except Exception as exc:
            return {
                "ok": False, "schema_version": "brain_memory_retrieval.v1", "items": [],
                "negative_matches": [], "counter_evidence": [], "source_gaps": ["brain_memory_error"],
                "error": f"{type(exc).__name__}: {exc}", "read_only": True, "affects_trading": False,
            }

    @staticmethod
    def _critic(hypotheses: list[dict[str, Any]], world_model: dict[str, Any],
                memory: dict[str, Any]) -> dict[str, Any]:
        objections = []
        verdict = "pass"
        if str(world_model.get("strategy_posture") or "") in {"defensive", "observation_only", "no_new_risk"}:
            verdict = "shadow_only"
            objections.append("strategy_posture_limits_action_scope")
        if any(safe_float(h.get("evidence_score")) < 0.5 for h in hypotheses):
            verdict = "shadow_only"
            objections.append("evidence_score_below_action_threshold")
        balance = memory.get("evidence_balance") or {}
        if balance.get("dominant") == "negative":
            verdict = "shadow_only"
            objections.append("negative_memory_dominates")
        elif memory.get("negative_matches") or memory.get("counter_evidence"):
            objections.append("mixed_or_insufficient_memory_requires_observation")
        if memory.get("source_gaps"):
            objections.append("memory_sources_incomplete")
        return {
            "schema_version": "brain_critic.v1", "verdict": verdict,
            "objections": sorted(set(objections)),
            "missing_evidence": ["v16_counter_evidence_search"] if not memory.get("counter_evidence") else [],
            "required_replay": ["required_before_any_non_observe_action"],
            "max_allowed_action_scope": "observe_only",
            "read_only": True,
        }

    @staticmethod
    def _evidence_refs(perceptions: dict[str, Any], memory: dict[str, Any]) -> dict[str, Any]:
        latest_report = perceptions.get("replay", {}).get("latest_report") or {}
        latest_release = perceptions.get("release", {}).get("latest_release") or {}
        return {
            "backend_readiness": {"schema": "backend_readiness.v1"},
            "replay_report": {"replay_run_id": str(latest_report.get("replay_run_id") or ""),
                              "artifact_hash": str(latest_report.get("artifact_hash") or "")},
            "release_run": {"run_id": str(latest_release.get("run_id") or "")},
            "incident_control": {"mode": str(perceptions.get("incident_control", {}).get("mode") or "normal")},
            "autonomy_health": {"posture": str(perceptions.get("autonomy_health", {}).get("posture") or ""),
                                "score": safe_float(perceptions.get("autonomy_health", {}).get("score"))},
            "memory": {"item_count": len(memory.get("items") or []),
                       "raw_item_count": int(memory.get("raw_item_count") or 0),
                       "evidence_unit_count": int(memory.get("evidence_unit_count") or 0),
                       "negative_match_count": len(memory.get("negative_matches") or []),
                       "counter_evidence_count": len(memory.get("counter_evidence") or []),
                       "evidence_balance": memory.get("evidence_balance") or {},
                       "posterior_arbitration": memory.get("posterior_arbitration") or {},
                       "source_gaps": memory.get("source_gaps") or []},
        }

    @staticmethod
    def _missing_status(status: str) -> dict[str, Any]:
        return {"ok": False, "schema_version": "brain_state_readiness.v1", "status": status,
                "read_only": True, "affects_trading": False, "boundary": BrainStateService.boundary()}

    @staticmethod
    def _row_to_snapshot(row: Any) -> dict[str, Any]:
        return {
            "ok": True, "schema_version": str(row["schema_version"] or "brain_state_snapshot.v1"),
            "snapshot_id": str(row["snapshot_id"] or ""), "status": str(row["status"] or ""),
            "phase": "v16_phase1_read_only_brain", "source": str(row["source"] or ""),
            "world_model": loads(row["world_model_json"], {}),
            "perceptions": loads(row["perceptions_json"], {}),
            "memory": loads(row["memory_json"], {}),
            "hypotheses": loads(row["hypotheses_json"], []),
            "critic": loads(row["critic_json"], {}),
            "evidence_refs": loads(row["evidence_refs_json"], {}),
            "boundary": loads(row["boundary_json"], BrainStateService.boundary()),
            "created_at": safe_float(row["created_at"]),
            "read_only": True, "affects_trading": False,
        }


# ---------------------------------------------------------------------------
# BrainMemoryService
# ---------------------------------------------------------------------------

class BrainMemoryService:
    """Read-only V16 memory retrieval over existing audit facts.

    Materializes lightweight memory metadata for display. Does not create
    learning labels, mutate runtime config, or authorize actions.
    """

    SHADOW_TABLES = {
        "open_quality_shadow_audit": {"id": "inference_id", "score": "quality_score",
                                       "risk": "risk_score", "summary": "open quality shadow audit"},
        "position_quality_shadow_audit": {"id": "inference_id", "score": "hold_score",
                                           "risk": "exit_risk_score", "summary": "position quality shadow audit"},
        "factor_governance_shadow_audit": {"id": "inference_id", "score": "positive_score",
                                            "risk": "weakness_score", "summary": "factor governance shadow audit"},
    }

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {"phase": "v16_phase1_read_only_memory", "read_only": True, "affects_trading": False,
                "does_not_write_learning_samples": True, "does_not_authorize_actions": True,
                "source_facts_remain_authoritative": True}

    def retrieve(self, *, world_model: dict[str, Any] | None = None,
                 hypotheses: list[dict[str, Any]] | None = None,
                 limit: int = 12, persist: bool = True) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        terms = self._query_terms(world_model or {}, hypotheses or [])
        source_gaps: list[str] = []
        items: list[dict[str, Any]] = []
        conn = connect(self.db_path, read_only=True)
        try:
            items.extend(self._experience_memories(conn, terms, source_gaps))
            items.extend(self._trade_outcome_memories(conn, terms, source_gaps))
            items.extend(self._counterfactual_memories(conn, terms, source_gaps))
            items.extend(self._policy_suggestion_memories(conn, terms, source_gaps))
            items.extend(self._model_permission_memories(conn, terms, source_gaps))
            items.extend(self._shadow_audit_memories(conn, terms, source_gaps))
        finally:
            conn.close()
        raw_item_count = len(items)
        posterior_sources = self._posterior_source_facts(items)
        items = self._deduplicate_items(items)
        posterior_arbitration = build_posterior_arbitration(
            trade_reviews=posterior_sources["trade_reviews"],
            counterfactuals=posterior_sources["counterfactuals"],
        )
        items = [
            self._apply_posterior_reconciliation(
                item,
                trade_reviews=posterior_sources["trade_reviews"],
                counterfactuals=posterior_sources["counterfactuals"],
            )
            for item in items
        ]
        scored_items = sorted(items, key=lambda item: (
            safe_float(item.get("similarity_score")), safe_float(item.get("evidence_score")),
            safe_float(item.get("created_at")),
        ), reverse=True)
        ranked = scored_items[:limit]
        posterior_memory = self._posterior_memory_item(posterior_arbitration)
        if persist and ranked:
            self._persist_items(ranked + ([posterior_memory] if posterior_memory else []))
        # The display window is intentionally bounded, but evidence balance
        # must include every matched unit so a cluster of negative rows cannot
        # hide positive counter-evidence merely by ranking ahead of it.
        matched = [
            item for item in scored_items
            if safe_float(item.get("similarity_score")) >= 0.1
            and item.get("evidence_eligible", True)
        ]
        negative_matches = sorted(
            (item for item in matched if item.get("polarity") == "negative"),
            key=self._evidence_weight,
            reverse=True,
        )
        counter_evidence = sorted(
            (item for item in matched if item.get("polarity") == "positive"),
            key=self._evidence_weight,
            reverse=True,
        )
        evidence_balance = self._evidence_balance(matched)
        return {
            "ok": True, "schema_version": "brain_memory_retrieval.v1",
            "items": ranked, "negative_matches": negative_matches[:5],
            "counter_evidence": counter_evidence[:5], "source_gaps": sorted(set(source_gaps)),
            "raw_item_count": raw_item_count,
            "evidence_unit_count": len(items),
            "evidence_balance": evidence_balance,
            "posterior_arbitration": posterior_arbitration,
            "posterior_memory": posterior_memory or {},
            "query_terms": sorted(terms), "boundary": self.boundary(),
            "read_only": True, "affects_trading": False, "generated_at": time.time(),
        }

    @staticmethod
    def _source_identity(item: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        structured = item.get("structured") if isinstance(item.get("structured"), dict) else {}
        source_table = str(structured.get("source_table") or item.get("source_table") or "")
        source_id = str(structured.get("source_id") or item.get("source_id") or "")
        return source_table, source_id, structured

    @classmethod
    def _posterior_source_facts(cls, items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Build posterior inputs before evidence deduplication.

        ``experience_memory`` rows carry the original review reference in
        ``structured``.  If deduplication selects that row over the raw review,
        filtering only on the top-level source table silently drops the review
        from arbitration.  Prefer the raw review when both representations
        exist, while still allowing a source-backed experience row to fill a
        missing review.
        """
        reviews: dict[str, dict[str, Any]] = {}
        review_priority: dict[str, int] = {}
        counterfactuals: dict[str, dict[str, Any]] = {}
        for item in items:
            source_table, source_id, structured = cls._source_identity(item)
            if source_table == "trade_outcome_review" and source_id:
                payload = dict(structured)
                payload.setdefault("review_id", source_id)
                payload.setdefault("source_id", source_id)
                priority = 2 if str(item.get("source_table") or "") == "trade_outcome_review" else 1
                if priority >= review_priority.get(source_id, -1):
                    reviews[source_id] = payload
                    review_priority[source_id] = priority
            elif source_table == "supervisor_counterfactual_review" and source_id:
                payload = dict(structured)
                payload.setdefault("counterfactual_id", source_id)
                counterfactuals[source_id] = payload
        return {
            "trade_reviews": list(reviews.values()),
            "counterfactuals": list(counterfactuals.values()),
        }

    @classmethod
    def reconcile_trade_review(
        cls,
        review: dict[str, Any],
        *,
        counterfactuals: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Expose the same posterior reconciliation used by memory retrieval.

        Generation-context readers need the causal owner too.  Keeping this
        small adapter here prevents them from reimplementing, or bypassing,
        V16's entry-versus-supervisor arbitration rules.
        """
        payload = dict(review or {})
        review_id = str(payload.get("review_id") or payload.get("source_id") or "")
        payload.setdefault("review_id", review_id)
        payload.setdefault("source_id", review_id)
        item = {
            "source_table": "trade_outcome_review",
            "source_id": review_id,
            "structured": {
                "source_table": "trade_outcome_review",
                "source_id": review_id,
            },
        }
        return cls._apply_posterior_reconciliation(
            item,
            trade_reviews=[payload] if review_id else [],
            counterfactuals=[dict(item) for item in (counterfactuals or []) if isinstance(item, dict)],
        )

    @classmethod
    def _apply_posterior_reconciliation(
        cls,
        item: dict[str, Any],
        *,
        trade_reviews: list[dict[str, Any]],
        counterfactuals: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Attach causal-scope status without rewriting source facts.

        A raw entry review and a later supervisor counterfactual can both be
        true.  The memory index must therefore preserve the source text but
        prevent the raw entry recommendation from being treated as a global
        action after the supervisor posterior wins.
        """
        result = dict(item)
        structured = dict(item.get("structured") or {})
        source_table, source_id, _ = cls._source_identity(item)
        status = "source_observation"
        causal_scope = ""
        action_owner = ""
        actionable = True
        local_arbitration: dict[str, Any] = {}

        if source_table == "trade_outcome_review" and source_id:
            review = next((row for row in trade_reviews if str(row.get("review_id") or row.get("source_id") or "") == source_id), {})
            review_id = source_id
            related_cfs = [
                row for row in counterfactuals
                if str(row.get("review_id") or "") == review_id
                or (
                    str(row.get("position_id") or "")
                    and str(row.get("position_id") or "") == str(review.get("position_id") or "")
                )
            ]
            local_arbitration = build_posterior_arbitration(
                trade_reviews=[review] if review else [],
                counterfactuals=related_cfs,
            )
            selected_scope = str(local_arbitration.get("selected_scope") or "")
            causal_scope = "entry"
            action_owner = "autonomous_learning"
            if selected_scope == "supervisor":
                status = "entry_conclusion_retained"
                actionable = False
                # The entry observation remains available in the final
                # posterior record, but it must not become global negative
                # evidence when the supervisor counterfactual is stronger.
                result["polarity"] = "neutral"
                result["memory_type"] = "episodic"
            elif selected_scope == "entry":
                status = "selected_entry_conclusion"
            elif review:
                status = "entry_observation_pending_supervisor_posterior"
        elif source_table == "supervisor_counterfactual_review" and source_id:
            related = next((row for row in counterfactuals if str(row.get("counterfactual_id") or "") == source_id), {})
            review_id = str(related.get("review_id") or structured.get("review_id") or "")
            related_reviews = [
                row for row in trade_reviews
                if str(row.get("review_id") or row.get("source_id") or "") == review_id
            ]
            local_arbitration = build_posterior_arbitration(
                trade_reviews=related_reviews,
                counterfactuals=[related] if related else [],
            )
            selected = local_arbitration.get("selected_conclusion") or {}
            causal_scope = "supervisor"
            action_owner = "position_supervisor_governance"
            if str(selected.get("source_ref_id") or "") == source_id:
                status = "selected_supervisor_conclusion"
            else:
                status = "supervisor_observation"
        elif source_table == "policy_suggestion":
            policy_status = str(structured.get("status") or "")
            causal_scope = "governance"
            action_owner = "downstream_specialist_and_governor"
            if policy_status in _NON_ACTIONABLE_POLICY_STATUSES:
                status = "historical_non_actionable"
                actionable = False
                result["polarity"] = "neutral"
                result["memory_type"] = "historical"

        structured["posterior_reconciliation"] = {
            "schema_version": "memory_posterior_reconciliation.v1",
            "status": status,
            "causal_scope": causal_scope,
            "action_owner": action_owner,
            "evidence_eligible": actionable,
            "local_arbitration": local_arbitration,
        }
        result["structured"] = structured
        result["evidence_eligible"] = actionable
        return result

    def _posterior_memory_item(self, arbitration: dict[str, Any]) -> dict[str, Any] | None:
        selected = dict(arbitration.get("selected_conclusion") or {})
        fingerprint = str(arbitration.get("fingerprint") or "")
        if not selected or not fingerprint:
            return None
        scope = str(selected.get("causal_scope") or arbitration.get("selected_scope") or "")
        conclusion = str(selected.get("conclusion") or "")
        action = str(selected.get("recommended_action") or "hold")
        owner = (
            "position_supervisor_governance"
            if scope == "supervisor" else "autonomous_learning"
        )
        return self._item(
            source_table="posterior_arbitration",
            source_id=fingerprint,
            memory_type="posterior",
            text_summary=f"{scope} {conclusion} {action}".strip(),
            structured={
                "final_memory": True,
                "posterior_arbitration": arbitration,
                "action_owner": owner,
                "allowed_uses": ["memory_retrieval", "critic_context", "v16_dispatch"],
            },
            evidence_score=max(0.0, min(1.0, safe_float(selected.get("evidence_score")))),
            polarity="positive" if scope == "supervisor" else "negative",
            created_at=time.time(),
            terms=set(),
        )

    @staticmethod
    def _evidence_identity(item: dict[str, Any]) -> str | None:
        structured = item.get("structured") if isinstance(item.get("structured"), dict) else {}
        item_source = str(item.get("source_table") or "")
        source_table = str(structured.get("source_table") or item_source)
        source_id = str(structured.get("source_id") or item.get("source_id") or "")
        trade_id = str(structured.get("trade_id") or "")
        if source_table == "trade_outcome_review" and source_id:
            return f"trade_review:{source_id}"
        if item_source == "trade_outcome_review" and source_id:
            return f"trade_review:{source_id}"
        if trade_id and item_source == "experience_memory":
            return f"trade:{trade_id}"
        return None

    @classmethod
    def _deduplicate_items(cls, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        standalone: list[dict[str, Any]] = []
        for item in items:
            identity = cls._evidence_identity(item)
            if identity is None:
                standalone.append(item)
            else:
                groups.setdefault(identity, []).append(item)

        deduped = list(standalone)
        for identity, group in groups.items():
            representative = dict(max(
                group,
                key=lambda item: (
                    1 if str(item.get("source_table") or "") == "experience_memory" else 0,
                    safe_float(item.get("evidence_score")),
                    safe_float(item.get("similarity_score")),
                    safe_float(item.get("created_at")),
                ),
            ))
            if len(group) > 1:
                representative["evidence_unit_id"] = identity
                representative["evidence_sources"] = [
                    {
                        "source_table": str(item.get("source_table") or ""),
                        "source_id": str(item.get("source_id") or ""),
                    }
                    for item in group
                ]
            deduped.append(representative)
        return deduped

    @staticmethod
    def _evidence_weight(item: dict[str, Any]) -> float:
        evidence_score = max(0.0, min(1.0, safe_float(item.get("evidence_score"))))
        similarity = max(0.1, min(1.0, safe_float(item.get("similarity_score"))))
        return evidence_score * similarity

    @classmethod
    def _evidence_balance(cls, items: list[dict[str, Any]]) -> dict[str, Any]:
        negative = [item for item in items if item.get("polarity") == "negative"]
        positive = [item for item in items if item.get("polarity") == "positive"]
        negative_score = sum(cls._evidence_weight(item) for item in negative)
        positive_score = sum(cls._evidence_weight(item) for item in positive)
        dominant = "mixed"
        if len(negative) >= 2 and negative_score > positive_score * 1.25:
            dominant = "negative"
        elif len(positive) >= 2 and positive_score > negative_score * 1.25:
            dominant = "positive"
        return {
            "schema_version": "memory_evidence_balance.v1",
            "negative_count": len(negative),
            "positive_count": len(positive),
            "negative_score": round(negative_score, 6),
            "positive_score": round(positive_score, 6),
            "dominant": dominant,
        }

    def latest_indexed(self, *, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        conn = connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_memory"):
                return {"ok": False, "schema_version": "brain_memory_index.v1",
                        "status": "missing_table", "items": [], "read_only": True, "affects_trading": False}
            rows = execute(
                conn,
                """SELECT memory_id, memory_type, source_table, source_id, symbol, timeframe,
                   regime, text_summary, structured_json, evidence_score,
                   similarity_score, polarity, created_at, last_used_at
                FROM brain_memory ORDER BY last_used_at DESC, created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return {"ok": True, "schema_version": "brain_memory_index.v1", "status": "available",
                    "items": [self._row_to_item(row) for row in rows], "read_only": True, "affects_trading": False}
        finally:
            conn.close()

    # -- internal helpers ---------------------------------------------------

    def _persist_items(self, items: list[dict[str, Any]]) -> None:
        ensure_brain_memory_table(self.db_path)
        now = time.time()
        conn = connect(self.db_path)
        try:
            # These are derived projections.  Rebuild their current window on
            # every refresh so a later posterior cannot leave an older
            # counterfactual/final-arbitration record looking current.
            execute(
                conn,
                "DELETE FROM brain_memory WHERE source_table IN ('supervisor_counterfactual_review', 'posterior_arbitration')",
            )
            if state_table_exists(conn, "policy_suggestion"):
                execute(
                    conn,
                    """DELETE FROM brain_memory
                       WHERE source_table='policy_suggestion'
                         AND source_id IN (
                             SELECT suggestion_id FROM policy_suggestion
                             WHERE status IN ('superseded', 'rejected', 'failed', 'blocked_by_evidence')
                       )""",
                )
            # ``brain_memory`` is a rebuildable retrieval index, not an
            # archive.  P3 canonical lesson consolidation can retire old
            # experience IDs while their derived index rows survive a normal
            # upsert refresh.  Remove only references whose authoritative
            # source no longer exists; raw evidence and current index rows
            # remain untouched.
            if state_table_exists(conn, "experience_memory"):
                execute(
                    conn,
                    """DELETE FROM brain_memory
                       WHERE source_table='experience_memory'
                         AND source_id NOT IN (
                             SELECT experience_id FROM experience_memory
                         )""",
                )
            if state_table_exists(conn, "trade_outcome_review"):
                execute(
                    conn,
                    """DELETE FROM brain_memory
                       WHERE source_table='trade_outcome_review'
                         AND source_id NOT IN (
                             SELECT review_id FROM trade_outcome_review
                       )""",
                )
            for item in items:
                persisted_item = _persisted_memory_item(item)
                execute(
                    conn,
                    """INSERT INTO brain_memory
                    (memory_id, memory_type, source_table, source_id, symbol, timeframe,
                     regime, text_summary, structured_json, evidence_score,
                     similarity_score, polarity, created_at, last_used_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id) DO UPDATE SET
                        memory_type=excluded.memory_type, symbol=excluded.symbol,
                        timeframe=excluded.timeframe, regime=excluded.regime,
                        text_summary=excluded.text_summary,
                        structured_json=excluded.structured_json,
                        evidence_score=excluded.evidence_score,
                        similarity_score=excluded.similarity_score,
                        polarity=excluded.polarity, last_used_at=excluded.last_used_at""",
                    (persisted_item["memory_id"], persisted_item.get("memory_type", ""),
                     persisted_item.get("source_table", ""), persisted_item.get("source_id", ""),
                     persisted_item.get("symbol", ""), persisted_item.get("timeframe", ""),
                     persisted_item.get("regime", ""), persisted_item.get("text_summary", ""),
                     dumps(persisted_item.get("structured", {})),
                     safe_float(persisted_item.get("evidence_score")),
                     safe_float(persisted_item.get("similarity_score")),
                     persisted_item.get("polarity", "neutral"),
                     safe_float(persisted_item.get("created_at")), now),
                )
            conn.commit()
        finally:
            conn.close()

    def _experience_memories(self, conn, terms: set[str], gaps: list[str]) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "experience_memory"):
            gaps.append("experience_memory")
            return []
        archive_select = _review_archive_select(conn)
        rows = execute(conn, f"""SELECT e.experience_id, e.trade_id, e.source_table, e.source_id,
            e.regime_id, e.decision_context_json, e.outcome_label, e.reward_score,
            e.failure_tags_json, e.recommended_action, e.evidence_strength,
            e.append_source, e.artifact_version, e.created_at,
            r.review_id AS source_review_id, r.review_json AS source_review_json{archive_select}
            FROM experience_memory e
            LEFT JOIN trade_outcome_review r ON r.review_id=e.source_id
            WHERE e.append_source='trade_lesson_memory.v1'
            ORDER BY e.created_at DESC""").fetchall()
        items = []
        for row in rows:
            source_review = _review_payload_from_row(
                conn,
                row,
                source_id_key="source_review_id",
                inline_key="source_review_json",
            )
            if not str(row["source_review_id"] or "") or review_has_system_contamination(source_review):
                continue
            tags = loads(row["failure_tags_json"], [])
            context = loads(row["decision_context_json"], {})
            context_projection = dict(context) if isinstance(context, dict) else {}
            # Older lessons stored the complete review here.  Keep the
            # derived lesson fields, but never rehydrate that recursive
            # source payload into the brain/readiness projection.
            context_projection.pop("review_json", None)
            context_projection.pop("review", None)
            lesson = context_projection.get("lesson")
            if not isinstance(lesson, dict):
                lesson = {}
            summary = " ".join(str(part or "") for part in [row["outcome_label"],
                row["recommended_action"], lesson.get("summary"), row["regime_id"],
                " ".join(str(t) for t in tags)]).strip()
            reward = safe_float(row["reward_score"])
            outcome_label = str(row["outcome_label"] or "")
            # ``good_loss`` is a controlled/acceptable loss label, not proof
            # that the entry factor was wrong.  Keep it neutral until the
            # per-trade posterior arbitration assigns causal ownership.
            if outcome_label == "good_loss":
                polarity = "neutral"
            else:
                polarity = "negative" if reward < 0 or tags else ("positive" if reward > 0 else "neutral")
            items.append(self._item(
                source_table="experience_memory", source_id=str(row["experience_id"] or ""),
                memory_type="negative" if polarity == "negative" else "episodic",
                text_summary=summary or "experience memory",
                structured={"trade_id": row["trade_id"], "source_table": row["source_table"],
                            "source_id": row["source_id"], "append_source": row["append_source"],
                            "artifact_version": row["artifact_version"],
                            "outcome_label": outcome_label, "reward_score": reward,
                            "failure_tags": tags, "recommended_action": row["recommended_action"],
                            "lesson": lesson, "decision_context": context_projection,
                            "review": _review_fact_projection(
                                source_review,
                                review_id=row["source_review_id"],
                                trade_id=row["trade_id"],
                                failure_tags=tags,
                                outcome_label=outcome_label,
                            )},
                evidence_score=max(0.0, min(safe_float(row["evidence_strength"]), 1.0)),
                polarity=polarity, created_at=safe_float(row["created_at"]), terms=terms,
                regime=str(row["regime_id"] or ""),
            ))
            if len(items) >= 50:
                break
        return items

    def _trade_outcome_memories(self, conn, terms: set[str], gaps: list[str]) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "trade_outcome_review"):
            gaps.append("trade_outcome_review")
            return []
        archive_select = _review_archive_select(conn, output="review_archive_hash")
        rows = execute(conn, f"""SELECT review_id, trade_id, position_id, entry_decision_id, pnl,
            outcome_label, failure_tags_json, summary_text, review_json, created_at{archive_select}
            FROM trade_outcome_review AS r ORDER BY created_at DESC""").fetchall()
        items = []
        for row in rows:
            tags = loads(row["failure_tags_json"], [])
            review = _review_payload_from_row(
                conn,
                row,
                source_id_key="review_id",
                inline_key="review_json",
                archive_key="review_archive_hash",
            )
            if review_has_system_contamination(review):
                continue
            pnl = safe_float(row["pnl"])
            outcome_label = str(row["outcome_label"] or "")
            polarity = (
                "neutral" if outcome_label == "good_loss"
                else "negative" if pnl < 0 or tags
                else "positive" if pnl > 0 else "neutral"
            )
            summary = " ".join(str(part or "") for part in [row["outcome_label"],
                row["summary_text"], " ".join(str(t) for t in tags)]).strip()
            items.append(self._item(
                source_table="trade_outcome_review", source_id=str(row["review_id"] or ""),
                memory_type="negative" if polarity == "negative" else "episodic",
                text_summary=summary or "trade outcome review",
                structured={"review_id": str(row["review_id"] or ""),
                            "trade_id": row["trade_id"], "position_id": row["position_id"],
                            "entry_decision_id": row["entry_decision_id"], "pnl": pnl,
                            "outcome_label": outcome_label, "failure_tags": tags,
                            "review": _review_fact_projection(
                                review,
                                review_id=row["review_id"],
                                trade_id=row["trade_id"],
                                position_id=row["position_id"],
                                pnl=pnl,
                                outcome_label=outcome_label,
                                failure_tags=tags,
                            ),
                            "created_at": safe_float(row["created_at"])},
                evidence_score=0.75, polarity=polarity,
                created_at=safe_float(row["created_at"]), terms=terms,
            ))
            if len(items) >= 50:
                break
        return items

    def _counterfactual_memories(self, conn, terms: set[str], gaps: list[str]) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "supervisor_counterfactual_review"):
            gaps.append("supervisor_counterfactual_review")
            return []
        archive_select = _review_archive_select(conn)
        rows = execute(conn, f"""SELECT c.counterfactual_id, c.review_id, c.trade_id, c.position_id,
            c.close_ts, c.close_reason, c.supervisor_event_type, c.supervisor_reason, c.label,
            c.confidence, c.horizons_json, c.evidence_json, c.created_at, c.updated_at,
            r.review_id AS source_review_id, r.review_json AS source_review_json{archive_select}
            -- close_ts is the event-time ordering.  updated_at is only the
            -- batch/recompute timestamp and must not decide which posterior
            -- enters the brain's bounded evidence window.
            FROM supervisor_counterfactual_review c
            LEFT JOIN trade_outcome_review r ON r.review_id=c.review_id
            ORDER BY c.close_ts DESC, c.updated_at DESC""").fetchall()
        items = []
        for row in rows:
            if (
                not str(row["source_review_id"] or "")
                or review_has_system_contamination(
                    _review_payload_from_row(
                        conn,
                        row,
                        source_id_key="source_review_id",
                        inline_key="source_review_json",
                    )
                )
            ):
                continue
            horizons = loads(row["horizons_json"], [])
            evidence = loads(row["evidence_json"], {})
            if bool(evidence.get("evidence_invalidated")):
                continue
            label = str(row["label"] or "")
            confidence = safe_float(row["confidence"])
            mapped = _SUPERVISOR_COUNTERFACTUAL_ACTIONS.get(label)
            summary = " ".join(
                str(part or "")
                for part in [label, row["supervisor_reason"], row["supervisor_event_type"], " ".join(evidence.get("tags") or [])]
            ).strip()
            structured = {
                "counterfactual_id": str(row["counterfactual_id"] or ""),
                "review_id": str(row["review_id"] or ""),
                "trade_id": str(row["trade_id"] or ""),
                "position_id": str(row["position_id"] or ""),
                "close_ts": safe_float(row["close_ts"]),
                "close_reason": str(row["close_reason"] or ""),
                "supervisor_event_type": str(row["supervisor_event_type"] or ""),
                "supervisor_reason": str(row["supervisor_reason"] or ""),
                "label": label,
                "confidence": confidence,
                "horizons": horizons,
                "evidence": evidence,
                "causal_scope": "supervisor",
                "posterior_verdict": mapped[0] if mapped else "inconclusive",
                "recommended_action": mapped[1] if mapped else "hold",
            }
            # A matured counterfactual is positive evidence for the affected
            # intervention even when the realized trade itself was a loss.
            polarity = "positive" if mapped and confidence >= 0.5 and horizons else "neutral"
            items.append(self._item(
                source_table="supervisor_counterfactual_review",
                source_id=str(row["counterfactual_id"] or ""),
                memory_type="counterfactual",
                text_summary=summary or "supervisor counterfactual review",
                structured=structured,
                evidence_score=max(0.0, min(1.0, confidence)),
                polarity=polarity,
                created_at=safe_float(row["updated_at"] or row["created_at"]),
                terms=terms,
            ))
            if len(items) >= 50:
                break
        return items

    def _policy_suggestion_memories(self, conn, terms: set[str], gaps: list[str]) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "policy_suggestion"):
            gaps.append("policy_suggestion")
            return []
        rows = execute(conn, """SELECT suggestion_id, scope_type, scope_key, action, confidence,
            reason, evidence_json, status, created_at
            FROM policy_suggestion
            WHERE status NOT IN (
                'superseded', 'rejected', 'failed', 'blocked_by_evidence',
                'invalidated_evidence'
            )
            ORDER BY created_at DESC LIMIT 50""").fetchall()
        items = []
        for row in rows:
            status = str(row["status"] or "")
            action = str(row["action"] or "")
            polarity = "negative" if status in {"rolled_back", "blocked_by_risk"} else "neutral"
            summary = f"{row['scope_type']} {row['scope_key']} {action} {status} {row['reason']}"
            items.append(self._item(
                source_table="policy_suggestion", source_id=str(row["suggestion_id"] or ""),
                memory_type="procedural", text_summary=summary,
                structured={"scope_type": row["scope_type"], "scope_key": row["scope_key"],
                            "action": action, "status": status,
                            "evidence": loads(row["evidence_json"], {})},
                evidence_score=max(0.0, min(safe_float(row["confidence"]), 1.0)),
                polarity=polarity, created_at=safe_float(row["created_at"]), terms=terms,
            ))
        return items

    def _model_permission_memories(self, conn, terms: set[str], gaps: list[str]) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "model_permission_audit"):
            gaps.append("model_permission_audit")
            return []
        rows = execute(conn, """SELECT audit_id, model_type, status, reason, capabilities_json,
            violations_json, context_json, created_at
            FROM model_permission_audit ORDER BY created_at DESC LIMIT 30""").fetchall()
        items = []
        for row in rows:
            status = str(row["status"] or "")
            polarity = "negative" if status == "blocked" else "neutral"
            summary = f"{row['model_type']} permission {status} {row['reason']}"
            items.append(self._item(
                source_table="model_permission_audit", source_id=str(row["audit_id"] or ""),
                memory_type="semantic" if polarity != "negative" else "negative",
                text_summary=summary,
                structured={"model_type": row["model_type"], "status": status,
                            "reason": row["reason"],
                            "capabilities": loads(row["capabilities_json"], {}),
                            "violations": loads(row["violations_json"], []),
                            "context": loads(row["context_json"], {})},
                evidence_score=0.9 if polarity == "negative" else 0.65,
                polarity=polarity, created_at=safe_float(row["created_at"]), terms=terms,
            ))
        return items

    def _shadow_audit_memories(self, conn, terms: set[str], gaps: list[str]) -> list[dict[str, Any]]:
        items = []
        for table, spec in self.SHADOW_TABLES.items():
            if not state_table_exists(conn, table):
                gaps.append(table)
                continue
            rows = execute(conn, f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT 20").fetchall()
            for row in rows:
                score = safe_float(row[spec["score"]]) if spec["score"] in row.keys() else 0.0
                risk = safe_float(row[spec["risk"]]) if spec["risk"] in row.keys() else 0.0
                source_id = str(row[spec["id"]] or "")
                summary = f"{spec['summary']} score={score:.3f} risk={risk:.3f}"
                polarity = "negative" if risk >= 0.65 else ("positive" if score >= 0.65 else "neutral")
                items.append(self._item(
                    source_table=table, source_id=source_id, memory_type="semantic",
                    text_summary=summary,
                    structured={key: row[key] for key in row.keys()
                                if key.endswith("_id") or key in {"model_type", "factor", "mode"}},
                    evidence_score=max(score, risk, 0.25), polarity=polarity,
                    created_at=safe_float(row["created_at"]), terms=terms,
                ))
        return items

    @staticmethod
    def _query_terms(world_model: dict[str, Any], hypotheses: list[dict[str, Any]]) -> set[str]:
        tokens = {str(world_model.get(k) or "") for k in
                  ("market_regime", "strategy_posture", "factor_posture", "execution_posture",
                   "learning_posture", "autonomy_posture", "incident_mode")}
        tokens.update(str(item) for item in world_model.get("stale_governance_tables") or [])
        tokens.update({"supervisor", "counterfactual", "posterior", "thesis"})
        for hypothesis in hypotheses:
            tokens.add(str(hypothesis.get("scope") or ""))
            tokens.update(str(hypothesis.get("claim") or "").lower().replace(";", " ").split())
        return {token.lower() for token in tokens if token and len(token) >= 3}

    @staticmethod
    def _similarity(text_val: str, terms: set[str]) -> float:
        if not terms:
            return 0.0
        import re

        tokens = set(re.findall(r"[a-z0-9_]+", text_val.lower()))
        hits = sum(1 for term in terms if str(term).lower() in tokens)
        return round(min(1.0, hits / max(3, min(len(terms), 12))), 4)

    def _item(self, *, source_table: str, source_id: str, memory_type: str,
              text_summary: str, structured: dict[str, Any], evidence_score: float,
              polarity: str, created_at: float, terms: set[str],
              symbol: str = "", timeframe: str = "", regime: str = "") -> dict[str, Any]:
        similarity = self._similarity(" ".join([text_summary, dumps(structured), regime]), terms)
        return {
            "memory_id": _memory_id(source_table, source_id),
            "schema_version": "brain_memory_item.v1",
            "memory_type": memory_type, "source_table": source_table,
            "source_id": source_id, "symbol": symbol, "timeframe": timeframe,
            "regime": regime, "text_summary": text_summary,
            "structured": structured,
            "evidence_score": round(max(0.0, min(float(evidence_score), 1.0)), 4),
            "similarity_score": similarity, "polarity": polarity,
            "created_at": created_at,
        }

    @staticmethod
    def _row_to_item(row: Any) -> dict[str, Any]:
        return {
            "memory_id": str(row["memory_id"] or ""), "schema_version": "brain_memory_item.v1",
            "memory_type": str(row["memory_type"] or ""), "source_table": str(row["source_table"] or ""),
            "source_id": str(row["source_id"] or ""), "symbol": str(row["symbol"] or ""),
            "timeframe": str(row["timeframe"] or ""), "regime": str(row["regime"] or ""),
            "text_summary": str(row["text_summary"] or ""),
            "structured": loads(row["structured_json"], {}),
            "evidence_score": safe_float(row["evidence_score"]),
            "similarity_score": safe_float(row["similarity_score"]),
            "polarity": str(row["polarity"] or "neutral"),
            "created_at": safe_float(row["created_at"]),
            "last_used_at": safe_float(row["last_used_at"]),
        }
