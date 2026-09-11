"""V16 posterior arbitration: the canonical-evidence judgement layer.

Owns the arbitration fact that V16 publishes every cycle and that the
factor-expansion handoff carries: which causal scope holds the strongest
post-close conclusion, its correction contract, and the stable fingerprint
used as the expansion-authority carrier.  Pure reads over canonical
review/counterfactual evidence; no cognition ledger is involved.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, is_state_db_path
from backend.services._brain_helpers import connect, loads, safe_float
from backend.services.canonical_v2_reader import (
    canonical_ready,
    iter_counterfactual_rows,
    iter_review_rows,
)
from backend.services.position_supervisor_templates import (
    resolve_position_supervisor_binding_lineage,
)


def _position_supervisor_binding_reference(*sources: Any) -> dict[str, str]:
    lineage = resolve_position_supervisor_binding_lineage(*sources)
    binding = lineage.get("binding") if isinstance(lineage.get("binding"), dict) else {}
    return {
        "status": str(lineage.get("state") or "unknown"),
        "reason": str(lineage.get("reason") or "binding_missing"),
        "template_id": str(binding.get("template_id") or ""),
        "template_version": str(binding.get("template_version") or ""),
        "template_hash": str(binding.get("template_hash") or ""),
        "binding_source": str(binding.get("binding_source") or ""),
    }



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
    binding_ref = _position_supervisor_binding_reference(raw)
    projected.update(
        {
            "position_supervisor_binding_status": binding_ref["status"],
            "position_supervisor_binding_reason": binding_ref["reason"],
            "position_supervisor_binding_template_id": binding_ref["template_id"],
            "position_supervisor_binding_template_version": binding_ref["template_version"],
            "position_supervisor_binding_template_hash": binding_ref["template_hash"],
            "position_supervisor_binding_source": binding_ref["binding_source"],
        }
    )
    return projected



def _build_correction_contract(
    *,
    fingerprint: str,
    selected: dict[str, Any],
) -> dict[str, Any]:
    """Project posterior evidence into the existing V16 policy JSON boundary.

    The contract is deliberately non-executable: every dimension defaults to
    ``no_change`` because none of the v16 callers supply governed dimension
    facts (the former ``dimension_evidence`` projection was never reached).
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



_SUPERVISOR_WEAK_CONFIDENCE_THRESHOLD = 0.3

def build_posterior_arbitration(
    *,
    trade_reviews: list[dict[str, Any]] | None = None,
    counterfactuals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Select the strongest post-close conclusion by causal scope.

    A negative realized PnL can identify an entry/thesis problem while the
    future path independently proves that a supervisor intervention was too
    early.  Those are not competing labels.  V16 must keep both facts and
    dispatch each conclusion to the agent that owns that surface.

    A3 fix: Lower the supervisor confidence threshold from 0.5 to 0.3 so
    that weak-but-real counterfactuals can still generate posterior memory
    items (with proportionally lower evidence_score).  This allows the
    brain to accumulate posterior evidence even when individual
    counterfactuals are not yet high-confidence.
    """
    reviews = [dict(item) for item in (trade_reviews or []) if isinstance(item, dict)]
    cfs = [dict(item) for item in (counterfactuals or []) if isinstance(item, dict)]
    supervisor_items: list[dict[str, Any]] = []
    weak_supervisor_items: list[dict[str, Any]] = []
    for item in cfs:
        label = str(item.get("label") or "")
        mapped = _SUPERVISOR_COUNTERFACTUAL_ACTIONS.get(label)
        horizons = item.get("horizons") or []
        confidence = max(0.0, min(1.0, safe_float(item.get("confidence"))))
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        tags = list(evidence.get("tags") or [])
        if not mapped:
            continue
        # A3: accept confidence >= 0.3 for weak posterior channel (down from 0.5)
        if confidence < _SUPERVISOR_WEAK_CONFIDENCE_THRESHOLD or not horizons:
            continue
        is_weak = confidence < 0.5
        item_data = {
            "causal_scope": "supervisor",
            "conclusion": mapped[0],
            "recommended_action": mapped[1],
            "counterfactual_label": label,
            "confidence": confidence,
            "evidence_score": round(confidence * (0.6 if is_weak else 1.0) * (1.0 if "no_future_bars" not in tags else 0.5), 6),
            "source_ref_type": "canonical_v2.counterfactual_review",
            "source_ref_id": str(item.get("counterfactual_id") or ""),
            "position_id": str(item.get("position_id") or ""),
            "review_id": str(item.get("review_id") or ""),
            "trade_id": str(item.get("trade_id") or ""),
            "evidence_tags": tags,
            "weak_posterior": is_weak,
        }
        if is_weak:
            weak_supervisor_items.append(item_data)
        else:
            supervisor_items.append(item_data)
    all_supervisor_items = supervisor_items + weak_supervisor_items
    supervisor = {}
    if all_supervisor_items:
        weighted_counts: dict[str, float] = {}
        weighted_action: dict[str, float] = {}
        conclusion_to_action: dict[str, str] = {}
        for item in all_supervisor_items:
            conclusion = str(item.get("conclusion") or "")
            action = str(item.get("recommended_action") or "")
            score = float(item.get("evidence_score") or 0.0)
            weighted_counts[conclusion] = weighted_counts.get(conclusion, 0.0) + score
            weighted_action[action] = weighted_action.get(action, 0.0) + score
            if conclusion and action:
                conclusion_to_action[conclusion] = action
        dominant_conclusion = max(weighted_counts, key=lambda key: weighted_counts[key]) if weighted_counts else ""
        dominant_weight = float(weighted_counts.get(dominant_conclusion, 0.0))
        weighted_total = sum(weighted_counts.values())
        sorted_weights = sorted(weighted_counts.values(), reverse=True)
        second_weight = float(sorted_weights[1]) if len(sorted_weights) > 1 else 0.0
        margin = round(dominant_weight - second_weight, 6)
        # Evidence is inconclusive when the dominant signal is narrow or
        # the total weighted evidence is still sparse.  Keep the aggregated
        # signal but mark it inconclusive so governors do not trigger live
        # template changes without a canary/application.
        causal_state = "strong"
        if weighted_total < 1.5 or dominant_weight < 0.8 or margin < 0.8:
            # Single strong item (e.g. 0.8) with no competitor has margin
            # == dominant_weight (0.8) which meets the threshold, so it
            # remains strong.  A 3-vs-2 split (≈2.1 vs 1.4) has margin 0.7
            # and stays inconclusive.
            if not (len(all_supervisor_items) == 1 and dominant_weight >= 0.8):
                causal_state = "inconclusive"
        # Pick the strongest item among the dominant conclusion for source
        # lineage; the aggregated view still carries the weighted totals.
        dominant_items = [item for item in all_supervisor_items if str(item.get("conclusion") or "") == dominant_conclusion]
        representative = max(dominant_items, key=lambda item: float(item.get("evidence_score") or 0.0)) if dominant_items else max(all_supervisor_items, key=lambda item: float(item.get("evidence_score") or 0.0))
        supervisor = {
            "causal_scope": "supervisor",
            "conclusion": dominant_conclusion,
            "recommended_action": str(representative.get("recommended_action") or conclusion_to_action.get(dominant_conclusion) or ""),
            "counterfactual_label": str(representative.get("counterfactual_label") or ""),
            "confidence": float(representative.get("confidence") or 0.0),
            "evidence_score": round(dominant_weight, 6),
            "weighted_total": round(weighted_total, 6),
            "weighted_label_counts": {key: round(value, 6) for key, value in weighted_counts.items()},
            "weighted_action_counts": {key: round(value, 6) for key, value in weighted_action.items()},
            "dominant_conclusion": dominant_conclusion,
            "dominant_weight": round(dominant_weight, 6),
            "margin": margin,
            "causal_state": causal_state,
            "evidence_count": len(all_supervisor_items),
            "strong_count": len(supervisor_items),
            "weak_count": len(weak_supervisor_items),
            "source_ref_type": str(representative.get("source_ref_type") or ""),
            "source_ref_id": str(representative.get("source_ref_id") or ""),
            "position_id": str(representative.get("position_id") or ""),
            "review_id": str(representative.get("review_id") or ""),
            "trade_id": str(representative.get("trade_id") or ""),
            "evidence_tags": list(representative.get("evidence_tags") or []),
            "weak_posterior": bool(causal_state == "inconclusive" and dominant_weight < 1.2),
        }
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
        pnl = safe_float(review.get("pnl", review_json.get("pnl")))
        if primary or outcome or tags:
            entry = {
                "causal_scope": "entry",
                "conclusion": "entry_or_thesis_failure" if pnl < 0 else "entry_supported",
                "primary_responsibility": primary,
                "outcome_label": outcome,
                "failure_tags": tags,
                "confidence": 0.75 if primary else 0.55,
                "evidence_score": 0.75,
                "source_ref_type": "canonical_v2.trade_review",
                "source_ref_id": str(review.get("review_id") or ""),
                "position_id": str(review.get("position_id") or ""),
                "trade_id": str(review.get("trade_id") or ""),
                "pnl": pnl,
            }

    # A successful/neutral trade review is useful context but is not an entry
    # correction command.  Only a realized loss is actionable for the entry
    # agent; a mature counterfactual can still independently select the
    # supervisor agent for the same position.
    entry_actionable = entry if entry.get("conclusion") == "entry_or_thesis_failure" else {}
    # A4: positive entry memory — wins with good attribution produce
    # positive reinforcement so the system can learn "how to win", not
    # only "how to lose".  A positive entry is not an actionable
    # correction but becomes a positive_entry memory item downstream.
    positive_entry = {}
    if (
        entry
        and entry.get("conclusion") == "entry_supported"
        and safe_float(entry.get("pnl")) > 0
    ):
        # Positive trades are useful when they have a clear primary factor
        # responsibility (not execution_timing / operator_intervention /
        # data_quality which are system noise, not strategy signal).
        positive_primary = str(entry.get("primary_responsibility") or "")
        _POSITIVE_ENTRY_BLOCKED_RESPONSIBILITIES = frozenset({
            "execution_timing", "operator_intervention", "data_quality",
            "system", "exit", "holding",
        })
        if positive_primary and positive_primary not in _POSITIVE_ENTRY_BLOCKED_RESPONSIBILITIES:
            positive_entry = {
                "causal_scope": "entry",
                "conclusion": "positive_entry_reinforcement",
                "primary_responsibility": positive_primary,
                "outcome_label": str(entry.get("outcome_label") or ""),
                "failure_tags": [],
                "confidence": 0.65,
                "evidence_score": 0.60,
                "source_ref_type": "canonical_v2.trade_review",
                "source_ref_id": str(entry.get("source_ref_id") or ""),
                "position_id": str(entry.get("position_id") or ""),
                "trade_id": str(entry.get("trade_id") or ""),
                "pnl": safe_float(entry.get("pnl")),
            }
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
        "positive_entry": positive_entry,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    # Determine selection reason including positive entry channel
    if supervisor:
        selection_reason = "mature_counterfactual_has_highest_causal_evidence"
    elif entry_actionable:
        selection_reason = "canonical_v2_trade_review_is_current_best_source"
    elif positive_entry:
        selection_reason = "positive_trade_reinforcement"
    else:
        selection_reason = "no_actionable_posterior"
    return {
        "schema_version": "posterior_arbitration.v1",
        "status": "actionable" if selected else "positive" if positive_entry else "needs_evidence",
        "selected_scope": selected_scope,
        "selected_conclusion": selected,
        "entry_conclusion": entry,
        "supervisor_conclusion": supervisor,
        "positive_entry_conclusion": positive_entry,
        "conflicts": conflicts,
        "selection_reason": selection_reason,
        "fingerprint": fingerprint,
        "correction_contract": _build_correction_contract(
            fingerprint=fingerprint,
            selected=selected,
        ),
        "authority": {
            "v16_role": "judge_and_dispatch_only",
            "entry_agent": "autonomous_learning",
            "supervisor_agent": "position_supervisor_governance",
            "runtime_mutation_agent": "downstream_governor_only",
        },
    }



def load_posterior_arbitration(db_path: str | Path = STATE_DB) -> dict[str, Any]:
    """Arbitrate the posterior conclusion from canonical evidence alone.

    Pure read, no cognition ledger involved: this is the V16 decision fact
    that a factor-expansion handoff carries, and ``fingerprint`` is the token
    ``delegate_factor_governance_cycle`` re-derives before authorising a
    cycle.  Inputs are the newest mature-or-newer canonical rows; unlike the
    memory retrieval path this reads review/counterfactual facts directly so
    the arbitration does not depend on the snapshot projection.
    """
    try:
        conn = connect(db_path, read_only=True)
    except Exception:
        # No store at all (isolated fixture): there are no posterior facts to
        # arbitrate.  Production state must fail closed instead, so the
        # expansion handoff cannot be minted without the canonical store.
        if is_state_db_path(db_path):
            raise
        return build_posterior_arbitration()
    try:
        if not canonical_ready(conn):
            return build_posterior_arbitration()
        reviews = [
            {
                **dict(row),
                "created_at": safe_float(row.get("created_at")),
                "pnl": safe_float(row.get("pnl")),
            }
            for row in iter_review_rows(conn, limit=0)
        ]
        counterfactuals = [
            {
                **dict(row),
                "confidence": safe_float(row.get("confidence")),
                "evidence": row.get("evidence")
                if isinstance(row.get("evidence"), dict)
                else loads(row.get("evidence_json"), {}),
                "horizons": row.get("horizons")
                if isinstance(row.get("horizons"), list)
                else loads(row.get("horizons_json"), []),
            }
            for row in iter_counterfactual_rows(conn, limit=0, reverse=True)
        ]
    finally:
        conn.close()
    return build_posterior_arbitration(
        trade_reviews=reviews,
        counterfactuals=counterfactuals,
    )
