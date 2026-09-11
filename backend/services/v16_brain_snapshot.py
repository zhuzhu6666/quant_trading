"""V16 bounded memory retrieval and trade-review posterior reconciliation.

``BrainMemoryService`` translates existing canonical facts into a bounded,
display/advisor-friendly memory index and reconciles one trade review against
its counterfactuals (used by agent briefings).  Read-only aggregation: it never
mutates runtime config, weights, orders, positions, learning samples or broker
state.  Posterior arbitration itself lives in
``backend.services.v16_posterior_arbitration``.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    is_state_db_path,
    state_table_columns,
    state_table_exists,
)
from backend.services._brain_helpers import (
    connect,
    dumps,
    execute,
    loads,
    safe_float,
    text,
)
from backend.services.canonical_v2_reader import (
    canonical_ready,
    iter_counterfactual_rows,
    iter_review_rows,
    review_row,
)
from backend.services.review_contract import review_has_system_contamination
from backend.services.v16_posterior_arbitration import (
    _SUPERVISOR_COUNTERFACTUAL_ACTIONS,
    _position_supervisor_binding_reference,
    _review_fact_projection,
    build_posterior_arbitration,
)
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
) -> dict[str, Any]:
    payload = _row_value(row, inline_key, {})
    if isinstance(payload, str):
        payload = loads(payload, {})
    return payload if isinstance(payload, dict) else {}


def _memory_similarity_projection(value: Any) -> Any:
    """Remove lineage-only fields from semantic memory matching input."""

    if isinstance(value, dict):
        return {
            str(key): _memory_similarity_projection(item)
            for key, item in value.items()
            if not str(key).startswith("position_supervisor_binding")
        }
    if isinstance(value, list):
        return [_memory_similarity_projection(item) for item in value]
    return value


_MEMORY_PERSISTED_MAX_KEYS = 64
_MEMORY_PERSISTED_MAX_LIST_ITEMS = 32
_MEMORY_PERSISTED_MAX_STRING = 512
_MEMORY_PERSISTED_NESTED_KEYS = frozenset(
    {
        "context",
        "decision_context",
        "evidence",
        "lesson",
        "posterior_reconciliation",
        "review",
        "position_supervisor_binding",
    }
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


_NON_ACTIONABLE_POLICY_STATUSES = {
    "superseded",
    "rejected",
    "failed",
    "blocked_by_evidence",
}



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
            if source_table == "canonical_v2.trade_review" and source_id:
                payload = dict(structured)
                payload.setdefault("review_id", source_id)
                payload.setdefault("source_id", source_id)
                priority = 2 if str(item.get("source_table") or "") == "canonical_v2.trade_review" else 1
                if priority >= review_priority.get(source_id, -1):
                    reviews[source_id] = payload
                    review_priority[source_id] = priority
            elif source_table == "canonical_v2.counterfactual_review" and source_id:
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
            "source_table": "canonical_v2.trade_review",
            "source_id": review_id,
            "structured": {
                "source_table": "canonical_v2.trade_review",
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

        if source_table == "canonical_v2.trade_review" and source_id:
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
        elif source_table == "canonical_v2.counterfactual_review" and source_id:
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
        if not fingerprint:
            return None

        # A4: positive entry memory — when there is no actionable correction
        # (loss or counterfactual), but there IS a positive entry reinforcement,
        # generate a memory item so the system can learn "how to win".
        positive_entry = dict(arbitration.get("positive_entry_conclusion") or {})
        if not selected and positive_entry:
            return self._item(
                source_table="posterior_arbitration",
                source_id=fingerprint,
                memory_type="positive_entry",
                text_summary=f"entry positive_entry_reinforcement {str(positive_entry.get('primary_responsibility') or '')}",
                structured={
                    "final_memory": True,
                    "posterior_arbitration": arbitration,
                    "positive_entry": positive_entry,
                    "action_owner": "autonomous_learning",
                    "allowed_uses": ["memory_retrieval", "critic_context", "positive_reinforcement"],
                },
                evidence_score=max(0.0, min(1.0, safe_float(positive_entry.get("evidence_score", 0.6)))),
                polarity="positive",
                created_at=time.time(),
                terms=set(),
            )

        if not selected:
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
        if source_table == "canonical_v2.trade_review" and source_id:
            return f"trade_review:{source_id}"
        if item_source == "canonical_v2.trade_review" and source_id:
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
                "DELETE FROM brain_memory WHERE source_table IN ('canonical_v2.counterfactual_review', 'posterior_arbitration')",
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
            if canonical_ready(conn):
                review_ids = {
                    str(r.get("review_id") or "") for r in iter_review_rows(conn, limit=0)
                }
                review_ids.discard("")
                if review_ids:
                    placeholders = ",".join("?" for _ in review_ids)
                    execute(
                        conn,
                        f"""DELETE FROM brain_memory
                           WHERE source_table='canonical_v2.trade_review'
                             AND source_id NOT IN ({placeholders})""",
                        tuple(review_ids),
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
        memory_rows = execute(conn, """SELECT e.experience_id, e.trade_id, e.source_table, e.source_id,
            e.regime_id, e.decision_context_json, e.outcome_label, e.reward_score,
            e.failure_tags_json, e.recommended_action, e.evidence_strength,
            e.append_source, e.artifact_version, e.created_at
            FROM experience_memory e
            WHERE e.append_source='trade_lesson_memory.v1'
            ORDER BY e.created_at DESC""").fetchall()
        built: list[dict[str, Any]] = []
        for item in memory_rows:
            value = dict(item)
            source_id = str(value.get("source_id") or "")
            review = review_row(conn, source_id) if source_id else None
            value["source_review_id"] = source_id if review is not None else ""
            value["source_review_json"] = (review or {}).get("review_json") or {}
            built.append(value)
        rows = built
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
            binding_ref = _position_supervisor_binding_reference(
                source_review,
                context_projection,
            )
            binding = (
                context_projection.get("position_supervisor_binding")
                if isinstance(context_projection.get("position_supervisor_binding"), dict)
                else source_review.get("position_supervisor_binding")
                if isinstance(source_review.get("position_supervisor_binding"), dict)
                else {}
            )
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
                            "position_supervisor_binding_status": binding_ref["status"],
                            "position_supervisor_binding_reason": binding_ref["reason"],
                            "position_supervisor_binding_template_id": binding_ref["template_id"],
                            "position_supervisor_binding_template_version": binding_ref["template_version"],
                            "position_supervisor_binding_template_hash": binding_ref["template_hash"],
                            "position_supervisor_binding_source": binding_ref["binding_source"],
                            "position_supervisor_binding": binding,
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
        if not canonical_ready(conn):
            gaps.append("canonical_v2.trade_review")
            return []
        rows = iter_review_rows(conn, limit=0)
        rows.sort(key=lambda r: float(r.get("created_at") or 0.0), reverse=True)
        items = []
        for row in rows:
            tags = row.get("failure_tags") or loads(row.get("failure_tags_json"), [])
            review = _review_payload_from_row(
                conn,
                row,
                source_id_key="review_id",
                inline_key="review_json",
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
            binding_ref = _position_supervisor_binding_reference(review)
            binding = (
                review.get("position_supervisor_binding")
                if isinstance(review.get("position_supervisor_binding"), dict)
                else {}
            )
            items.append(self._item(
                source_table="canonical_v2.trade_review", source_id=str(row["review_id"] or ""),
                memory_type="negative" if polarity == "negative" else "episodic",
                text_summary=summary or "trade outcome review",
                structured={"review_id": str(row["review_id"] or ""),
                            "trade_id": row["trade_id"], "position_id": row["position_id"],
                            "entry_decision_id": row["entry_decision_id"], "pnl": pnl,
                            "outcome_label": outcome_label, "failure_tags": tags,
                            "position_supervisor_binding_status": binding_ref["status"],
                            "position_supervisor_binding_reason": binding_ref["reason"],
                            "position_supervisor_binding_template_id": binding_ref["template_id"],
                            "position_supervisor_binding_template_version": binding_ref["template_version"],
                            "position_supervisor_binding_template_hash": binding_ref["template_hash"],
                            "position_supervisor_binding_source": binding_ref["binding_source"],
                            "position_supervisor_binding": binding,
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
        if not canonical_ready(conn):
            gaps.append("canonical_v2.counterfactual_review")
            return []
        rows = iter_counterfactual_rows(conn, limit=0, reverse=True)
        review_map = {
            str(item.get("review_id") or ""): item
            for item in iter_review_rows(conn, limit=0)
        }
        built: list[dict[str, Any]] = []
        for item in rows:
            value = dict(item)
            review_id = str(value.get("review_id") or "")
            review = review_map.get(review_id)
            value["source_review_id"] = review_id if review is not None else ""
            value["source_review_json"] = (review or {}).get("review_json") or {}
            built.append(value)
        rows = built
        items = []
        for row in rows:
            if (
                not str(row.get("source_review_id") or "")
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
            horizons = row.get("horizons") or loads(row.get("horizons_json"), [])
            evidence = row.get("evidence") or loads(row.get("evidence_json"), {})
            if bool(evidence.get("evidence_invalidated")):
                continue
            label = str(row.get("label") or "")
            confidence = safe_float(row.get("confidence"))
            mapped = _SUPERVISOR_COUNTERFACTUAL_ACTIONS.get(label)
            binding_ref = _position_supervisor_binding_reference(
                _review_payload_from_row(
                    conn,
                    row,
                    source_id_key="source_review_id",
                    inline_key="source_review_json",
                ),
                evidence,
            )
            binding = (
                evidence.get("position_supervisor_binding")
                if isinstance(evidence.get("position_supervisor_binding"), dict)
                else {}
            )
            summary = " ".join(
                str(part or "")
                for part in [
                    label,
                    row.get("supervisor_reason"),
                    row.get("supervisor_event_type"),
                    " ".join(evidence.get("tags") or []),
                ]
            ).strip()
            structured = {
                "counterfactual_id": str(row.get("counterfactual_id") or ""),
                "review_id": str(row.get("review_id") or ""),
                "trade_id": str(row.get("trade_id") or ""),
                "position_id": str(row.get("position_id") or ""),
                "close_ts": safe_float(row.get("close_ts")),
                "close_reason": str(row.get("close_reason") or ""),
                "supervisor_event_type": str(row.get("supervisor_event_type") or ""),
                "supervisor_reason": str(row.get("supervisor_reason") or ""),
                "label": label,
                "confidence": confidence,
                "horizons": horizons,
                "evidence": evidence,
                "causal_scope": "supervisor",
                "position_supervisor_binding_status": binding_ref["status"],
                "position_supervisor_binding_reason": binding_ref["reason"],
                "position_supervisor_binding_template_id": binding_ref["template_id"],
                "position_supervisor_binding_template_version": binding_ref["template_version"],
                "position_supervisor_binding_template_hash": binding_ref["template_hash"],
                "position_supervisor_binding_source": binding_ref["binding_source"],
                "position_supervisor_binding": binding,
                "posterior_verdict": mapped[0] if mapped else "inconclusive",
                "recommended_action": mapped[1] if mapped else "hold",
            }
            # A matured counterfactual is positive evidence for the affected
            # intervention even when the realized trade itself was a loss.
            polarity = "positive" if mapped and confidence >= 0.5 and horizons else "neutral"
            items.append(self._item(
                source_table="canonical_v2.counterfactual_review",
                source_id=str(row.get("counterfactual_id") or ""),
                memory_type="counterfactual",
                text_summary=summary or "supervisor counterfactual review",
                structured=structured,
                evidence_score=max(0.0, min(1.0, confidence)),
                polarity=polarity,
                created_at=safe_float(row.get("updated_at") or row.get("created_at")),
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
        similarity = self._similarity(
            " ".join([
                text_summary,
                dumps(_memory_similarity_projection(structured)),
                regime,
            ]),
            terms,
        )
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
