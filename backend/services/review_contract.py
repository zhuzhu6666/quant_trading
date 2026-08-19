from __future__ import annotations

import json
import math
from typing import Any

from backend.services.supervisor_payload_contract import (
    bounded_review_projection,
    supervisor_payload_sha256,
)


SYSTEM_CONTAMINATION_LABELS = {
    "bar_data_degraded",
    "broker_close_price_unknown",
    "data_quality_issue",
    "decision_bar_stale",
    "market_data_stale",
    "signal_execution_delay",
    # 外部/回放终止的持仓不是策略自然生命周期，禁止进入学习
    "restart_replay",
    "manual_close",
}

ADVISORY_ONLY_HEALTH_COMPONENTS: set[str] = set()

# B2: canonical responsibility-domain vocabulary.  system-issue override
# domains (operator_intervention / execution_timing / data_quality) and the
# failure-taxonomy domains share one enumeration so no consumer can hold a
# different responsibility vocabulary.
RESPONSIBILITY_DOMAINS = frozenset(
    {
        # system-issue override domains
        "operator_intervention",
        "execution_timing",
        "data_quality",
        # failure-taxonomy domains
        "timing",
        "event_risk",
        "execution",
        "exit",
        "signal_quality",
        "factor_conflict",
        "reward_risk",
        "regime",
        "parameter",
        "thesis",
        "holding",
        "unclear",
        # catch-all used by the governance mutation / audit surfaces
        "system",
    }
)

# B1: responsibilities that must never downweight or penalize a factor.  A bad
# loss caused by these domains is a system/process defect, not evidence against
# the alpha factor itself.  Shared by the failure taxonomy, counter-evidence,
# and experience_builder so the exclusion list cannot drift.
NON_FACTOR_RESPONSIBILITIES = frozenset(
    {
        "exit",
        "holding",
        "execution",
        "execution_timing",
        "operator_intervention",
        "data_quality",
        "system",
        "parameter",
    }
)


def trusted_broker_close_price(payload: dict[str, Any] | None) -> float | None:
    value = payload or {}
    if str(value.get("price_quality") or "").strip().lower() not in {
        "broker_reported",
        "broker_reconciled",
    }:
        return None
    try:
        price = float(value.get("exec_price") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return None
    return price if math.isfinite(price) and price > 0.0 else None


def classify_4label_outcome(
    *,
    pnl: float,
    entry_score: float = 0.0,
    positive_share: float | None = None,
    has_entry_context: bool = False,
    has_attribution: bool = False,
    pos_mc: float = 0.0,
    neg_mc: float = 0.0,
    factor_conflict_ratio: float | None = None,
    effective_alpha_factor_count: int = 0,
) -> tuple[str, bool, bool, bool]:
    """Single authoritative 4-label outcome classifier (A2, reviewer 口径).

    Returns ``(outcome_label, conflict, weak_entry, avoidable_entry)``.

    Profit requires attribution proof: ``positive_share >= 0.55`` is a
    ``good_win``; any profit without that evidence is a ``lucky_win``.  Losses
    are ``bad_loss`` when entry conviction ``>= 0.55`` or the entry was
    avoidable, otherwise ``good_loss``.  Every producer of outcome labels for
    canonical ``trade_review`` must go through this rule so labels do not
    drift across the live, backfill, and replay paths.
    """
    conviction = abs(float(entry_score or 0.0))
    conflict = bool(
        has_attribution
        and pos_mc > 0
        and neg_mc < 0
        and float(factor_conflict_ratio or 0.0) >= 0.4
        and int(effective_alpha_factor_count or 0) >= 3
    )
    weak_entry = bool(has_entry_context and conviction < 0.55)
    share = 0.0 if positive_share is None else float(positive_share or 0.0)
    if pnl > 0:
        return ("good_win" if share >= 0.55 else "lucky_win"), conflict, weak_entry, False

    avoidable_entry = bool(weak_entry and (conflict or (has_attribution and share < 0.45)))
    label = "bad_loss" if conviction >= 0.55 or avoidable_entry else "good_loss"
    return label, conflict, weak_entry, avoidable_entry


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value not in (None, "") else default)
    except Exception:
        return float(default)


def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nested_dict(payload: dict[str, Any], *path: str) -> dict[str, Any]:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _first_float(*values: Any, default: float = 0.0) -> float:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return float(default)


def _first_nonempty_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict) and value:
            return dict(value)
    return {}


def build_execution_quality_event_details(
    *,
    tick: int,
    direction: Any,
    requested_price: Any,
    fill_price: Any = 0.0,
    learning_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the execution inputs beside each submitted/filled event.

    The decision ledger remains the primary learning-context source.  These
    event details are a durable fallback for execution reconstruction when a
    consumer has to replay only the order lifecycle chain.
    """

    context = _as_dict(learning_context)
    direction_value = _safe_float(direction)
    details: dict[str, Any] = {
        "tick": int(tick or 0),
        "direction": 1 if direction_value > 0 else -1 if direction_value < 0 else 0,
        "requested_price": _safe_float(requested_price),
        "fill_price": _safe_float(fill_price),
        "capture_schema": "execution_quality_event.v1",
    }
    for key in (
        "market_micro_context",
        "execution_context",
        "data_quality_context",
    ):
        value = context.get(key)
        if isinstance(value, dict):
            details[key] = dict(value)
    return details


def build_execution_quality_evidence(
    *,
    order_events: list[dict[str, Any]] | None = None,
    entry_action: dict[str, Any] | None = None,
    broker_deal: dict[str, Any] | None = None,
    direction: Any = 0,
) -> dict[str, Any]:
    """Calculate entry execution quality from recorded execution evidence.

    The numeric score is deliberately secondary to ``evidence_state``.  A
    historical row without a complete request/fill/spread/broker chain is
    retained for audit, but it must not be treated as model-ready evidence.
    ``full`` requires a submitted event, a filled event, a broker-reported
    open deal, a direction, and a recorded spread.  The broker deal price is
    the authoritative fill; a different lifecycle-event price is recorded as
    observed slippage rather than treated as a missing-evidence error.
    """

    action = _as_dict(entry_action)
    events = [dict(item) for item in (order_events or []) if isinstance(item, dict)]
    broker = _as_dict(broker_deal)

    def _event(event_type: str) -> dict[str, Any]:
        for item in events:
            if str(item.get("event_type") or "").strip().lower() != event_type:
                continue
            details = item.get("details_json") or item.get("details") or {}
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except Exception:
                    details = {}
            details = details if isinstance(details, dict) else {}
            return {**item, "details": details}
        return {}

    submitted = _event("submitted")
    filled = _event("filled")
    submitted_details = _as_dict(submitted.get("details"))
    filled_details = _as_dict(filled.get("details"))
    details = _as_dict(filled_details or submitted_details)
    execution = _first_nonempty_dict(
        action.get("execution_context"),
        submitted_details.get("execution_context"),
        filled_details.get("execution_context"),
    )
    market = _first_nonempty_dict(
        action.get("market_micro_context"),
        submitted_details.get("market_micro_context"),
        filled_details.get("market_micro_context"),
    )
    direction_value = _safe_float(
        direction,
        _safe_float(action.get("direction"), _safe_float(details.get("direction"))),
    )
    direction_sign = 1 if direction_value > 0 else -1 if direction_value < 0 else 0

    requested_price = _first_float(
        submitted.get("price"),
        submitted_details.get("requested_price"),
        execution.get("requested_price"),
        execution.get("signal_price"),
        action.get("requested_price"),
        default=0.0,
    )
    lifecycle_fill_price = _first_float(
        filled.get("price"),
        filled_details.get("fill_price"),
        filled_details.get("avg_price"),
        default=0.0,
    )
    broker_price = _first_float(
        broker.get("exec_price"),
        broker.get("raw_execution_price"),
        default=0.0,
    )
    broker_price_quality = str(broker.get("price_quality") or "").strip().lower()
    broker_price_trusted = broker_price > 0.0 and broker_price_quality in {
        "broker_reported",
        "broker_reconciled",
    }
    fill_price = broker_price if broker_price_trusted else lifecycle_fill_price

    bid = _first_float(market.get("bid"), action.get("bid"), default=0.0)
    ask = _first_float(market.get("ask"), action.get("ask"), default=0.0)
    spread_points = _first_float(
        market.get("spread"),
        action.get("spread"),
        ask - bid if ask > 0.0 and bid > 0.0 else None,
        default=0.0,
    )
    spread_points = max(0.0, spread_points)

    issues: list[str] = []
    if not submitted:
        issues.append("missing_submitted_event")
    if not filled:
        issues.append("missing_filled_event")
    if requested_price <= 0.0:
        issues.append("missing_requested_price")
    if fill_price <= 0.0:
        issues.append("missing_fill_price")
    if direction_sign == 0:
        issues.append("missing_direction")
    if not broker_price_trusted:
        issues.append("missing_broker_open_deal")
    if spread_points <= 0.0:
        issues.append("missing_spread")

    broker_match = None
    observations: list[str] = []
    lifecycle_broker_fill_delta = 0.0
    if broker_price_trusted and lifecycle_fill_price > 0.0:
        broker_match = abs(broker_price - lifecycle_fill_price) <= max(1e-6, abs(broker_price) * 1e-8)
        if not broker_match:
            # The order lifecycle price is the request/observed quote in the
            # current open path.  cTrader's deal exec_price is the actual
            # execution fact and is intentionally allowed to differ.
            lifecycle_broker_fill_delta = broker_price - lifecycle_fill_price
            observations.append("lifecycle_fill_differs_from_broker_fill")

    signed_slippage = 0.0
    adverse_slippage = 0.0
    if requested_price > 0.0 and fill_price > 0.0 and direction_sign:
        signed_slippage = (fill_price - requested_price) * direction_sign
        adverse_slippage = max(0.0, signed_slippage)

    observed = bool(submitted or filled or requested_price > 0.0 or fill_price > 0.0)
    evidence_state = "full" if not issues else ("partial" if observed else "unknown")
    score = 0.0
    if requested_price > 0.0 and fill_price > 0.0 and direction_sign:
        # Half-spread plus adverse slippage is the observed entry cost.  The
        # denominator is two spreads, with a one-basis-point fallback only
        # when the recorded spread is zero; no constant quality is injected.
        fair_cost = max(spread_points * 2.0, abs(requested_price) * 0.0001, 1e-9)
        observed_cost = adverse_slippage + spread_points / 2.0
        score = max(0.0, min(1.0, 1.0 - observed_cost / fair_cost))

    return {
        "schema_version": "execution_quality_evidence.v2",
        "evidence_state": evidence_state,
        "issues": list(dict.fromkeys(issues)),
        "observations": list(dict.fromkeys(observations)),
        "submitted_event": bool(submitted),
        "filled_event": bool(filled),
        "broker_deal_id": _safe_float(broker.get("deal_id"), 0.0),
        "broker_price_quality": broker_price_quality,
        "broker_price_trusted": broker_price_trusted,
        "broker_deal_fill_match": broker_match,
        "fill_price_source": "broker_deal" if broker_price_trusted else "lifecycle_filled_event",
        "requested_price": round(requested_price, 8),
        "lifecycle_fill_price": round(lifecycle_fill_price, 8),
        "broker_fill_price": round(broker_price, 8),
        "lifecycle_broker_fill_delta_points": round(lifecycle_broker_fill_delta, 8),
        "fill_price": round(fill_price, 8),
        "direction": direction_sign,
        "spread_points": round(spread_points, 8),
        "slippage_points": round(signed_slippage, 8),
        "adverse_slippage_points": round(adverse_slippage, 8),
        "score_formula": "clamp(1-(max(adverse_slippage,0)+spread/2)/max(2*spread,abs(requested)*0.0001,1e-9),0,1)",
        "score": round(score, 6),
    }


def _append_label(labels: list[str], label: str) -> None:
    if label and label not in labels:
        labels.append(label)


def timeframe_seconds(timeframe: Any) -> float:
    value = str(timeframe or "").strip().upper()
    if not value:
        return 0.0
    try:
        if value.startswith("M"):
            return float(value[1:]) * 60.0
        if value.startswith("H"):
            return float(value[1:]) * 3600.0
        if value.startswith("D"):
            return float(value[1:]) * 86400.0
    except Exception:
        return 0.0
    return 0.0


def build_entry_timing_context(
    *,
    signal_bar_ts: Any = 0.0,
    decision_evaluated_at: Any = 0.0,
    order_submitted_at: Any = 0.0,
    fill_ts: Any = 0.0,
    close_ts: Any = 0.0,
    timeframe: Any = "",
    source: str = "",
) -> dict[str, Any]:
    signal_ts = _safe_float(signal_bar_ts)
    evaluated_at = _safe_float(decision_evaluated_at)
    submitted_at = _safe_float(order_submitted_at)
    actual_fill_ts = _safe_float(fill_ts)
    actual_close_ts = _safe_float(close_ts)
    tf_seconds = timeframe_seconds(timeframe)
    actual_entry_ts = actual_fill_ts or submitted_at or evaluated_at or signal_ts

    def _delta(start: float, end: float) -> float:
        return round(max(0.0, end - start), 6) if start > 0 and end > 0 else 0.0

    return {
        "schema_version": "entry_timing_context.v1",
        "source": str(source or "review_contract"),
        "timeframe": str(timeframe or ""),
        "timeframe_seconds": tf_seconds,
        "signal_bar_ts": signal_ts,
        "decision_evaluated_at": evaluated_at,
        "order_submitted_at": submitted_at,
        "fill_ts": actual_fill_ts,
        "actual_entry_ts": actual_entry_ts,
        "close_ts": actual_close_ts,
        "signal_to_decision_delay_seconds": _delta(signal_ts, evaluated_at),
        "decision_to_order_delay_seconds": _delta(evaluated_at, submitted_at),
        "order_to_fill_delay_seconds": _delta(submitted_at, actual_fill_ts),
        "decision_to_fill_delay_seconds": _delta(evaluated_at, actual_fill_ts),
        "signal_to_fill_delay_seconds": _delta(signal_ts, actual_fill_ts or submitted_at),
        "actual_holding_seconds": _delta(actual_entry_ts, actual_close_ts),
    }


def extract_decision_freshness_context(
    *,
    entry_action: dict[str, Any] | None = None,
    entry_risk_state: dict[str, Any] | None = None,
    review_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action = _as_dict(entry_action)
    risk_state = _as_dict(entry_risk_state)
    review = _as_dict(review_payload)
    data_quality = _as_dict(action.get("data_quality_context") or review.get("data_quality_context"))
    risk_verdict = _first_nonempty_dict(action.get("risk_verdict"), risk_state.get("policy_verdict"))
    audit_payload = _nested_dict(risk_verdict, "audit_payload")
    state = _nested_dict(audit_payload, "state")
    runtime_snapshot = _nested_dict(state, "runtime_health_snapshot")
    raw_runtime = _nested_dict(runtime_snapshot, "raw")
    sync_health = _first_nonempty_dict(
        _nested_dict(data_quality, "runtime_health", "sync_health"),
        _nested_dict(raw_runtime, "sync_health"),
        _nested_dict(review, "runtime_health", "sync_health"),
    )

    direct = _first_nonempty_dict(
        action.get("decision_freshness"),
        data_quality.get("decision_freshness"),
        audit_payload.get("decision_freshness"),
        review.get("decision_freshness_context"),
    )
    if direct:
        payload = dict(direct)
        payload.setdefault("schema_version", "decision_bar_freshness.v1")
        payload.setdefault("source", "decision_freshness")
        if sync_health and "sync_health" not in payload:
            payload["sync_health"] = sync_health
        return payload

    data_lag = _first_float(
        runtime_snapshot.get("data_lag_seconds"),
        _nested_dict(action, "market_session").get("market_data_age_seconds"),
        _nested_dict(review, "market_session").get("market_data_age_seconds"),
        default=0.0,
    )
    if not sync_health and data_lag <= 0:
        return {}

    return {
        "schema_version": "review_decision_freshness_context.v1",
        "source": "risk_runtime_health_snapshot",
        "fresh": not _safe_bool(sync_health.get("stale"), False)
        and not _safe_bool(sync_health.get("degraded"), False),
        "stale": _safe_bool(sync_health.get("stale"), False),
        "degraded": _safe_bool(sync_health.get("degraded"), False),
        "sync_health": sync_health,
        "data_lag_seconds": data_lag,
        "last_bar_ts_by_tf": dict(sync_health.get("last_bar_ts_by_tf") or {}),
    }


def build_system_issue_context(review_payload: dict[str, Any] | None) -> dict[str, Any]:
    review = _as_dict(review_payload)
    labels: list[str] = []
    evidence: dict[str, Any] = {}
    # 外部/回放终止的持仓（operator 手动平仓、重启回放 close）不是策略
    # 自然生命周期产物，持有期与退出价格不代表策略演化，禁止进入学习。
    close_reason = str(
        review.get("close_reason")
        or review.get("close_reason_source")
        or ""
    ).strip().lower()
    if close_reason in {"restart_replay", "manual_close"}:
        _append_label(labels, close_reason)
        evidence["close_reason"] = str(review.get("close_reason") or "")
    timing = _as_dict(review.get("entry_timing_context"))
    freshness = _as_dict(review.get("decision_freshness_context"))
    data_quality = _as_dict(review.get("data_quality_context"))
    market_session = _as_dict(review.get("market_session"))
    runtime_health = _as_dict(data_quality.get("runtime_health"))
    real_pnl = _as_dict(review.get("real_pnl"))
    system_health = _as_dict(runtime_health.get("system_health"))
    sync_health = _first_nonempty_dict(
        freshness.get("sync_health"),
        _as_dict(runtime_health.get("sync_health")),
    )
    timeframe_sec = _safe_float(
        timing.get("timeframe_seconds"),
        timeframe_seconds(review.get("timeframe")),
    )
    stale_threshold = max(180.0, timeframe_sec * 1.5 if timeframe_sec > 0 else 0.0)

    price_quality = str(real_pnl.get("price_quality") or "").strip().lower()
    if price_quality == "unknown":
        _append_label(labels, "broker_close_price_unknown")
        evidence["broker_close_price"] = {
            "price_contract": str(real_pnl.get("price_contract") or "legacy_unknown"),
            "price_quality": "unknown",
        }

    if data_quality and not _safe_bool(data_quality.get("quote_fresh"), True):
        _append_label(labels, "data_quality_issue")
        evidence["quote_fresh"] = False
        evidence["quote_age_seconds"] = _safe_float(data_quality.get("quote_age_seconds"))

    decision_fresh = _safe_bool(freshness.get("fresh"), True)
    missing_bars = freshness.get("missing_closed_bars_by_tf") or {}
    if freshness and (not decision_fresh or bool(missing_bars)):
        _append_label(labels, "decision_bar_stale")
        _append_label(labels, "data_quality_issue")
        evidence["decision_freshness"] = freshness

    data_lag = _first_float(
        freshness.get("data_lag_seconds"),
        _nested_dict(freshness, "runtime_health_snapshot").get("data_lag_seconds"),
        default=0.0,
    )
    if data_lag > stale_threshold > 0:
        _append_label(labels, "market_data_stale")
        _append_label(labels, "data_quality_issue")
        evidence["data_lag_seconds"] = data_lag
        evidence["data_lag_threshold_seconds"] = stale_threshold

    market_data_age = _safe_float(market_session.get("market_data_age_seconds"))
    if market_data_age > stale_threshold > 0:
        _append_label(labels, "market_data_stale")
        _append_label(labels, "data_quality_issue")
        evidence["market_data_age_seconds"] = market_data_age
        evidence["market_data_age_threshold_seconds"] = stale_threshold

    if sync_health and (
        _safe_bool(sync_health.get("stale"), False)
        or _safe_bool(sync_health.get("degraded"), False)
    ):
        _append_label(labels, "bar_data_degraded")
        _append_label(labels, "data_quality_issue")
        evidence["sync_health"] = sync_health

    components = _as_dict(system_health.get("component_status"))
    advisory_component_status = {
        name: status
        for name, status in components.items()
        if name in ADVISORY_ONLY_HEALTH_COMPONENTS
        and str(status or "").lower() in {"critical", "degraded"}
    }
    if advisory_component_status:
        evidence["advisory_component_status"] = advisory_component_status

    signal_to_decision = _safe_float(timing.get("signal_to_decision_delay_seconds"))
    signal_to_fill = _safe_float(timing.get("signal_to_fill_delay_seconds"))
    if max(signal_to_decision, signal_to_fill) > stale_threshold > 0:
        _append_label(labels, "signal_execution_delay")
        evidence["signal_to_decision_delay_seconds"] = signal_to_decision
        evidence["signal_to_fill_delay_seconds"] = signal_to_fill
        evidence["signal_delay_threshold_seconds"] = stale_threshold

    primary = ""
    if "restart_replay" in labels or "manual_close" in labels:
        primary = "operator_intervention"
    elif any(label in labels for label in ("decision_bar_stale", "market_data_stale", "data_quality_issue", "bar_data_degraded")):
        primary = "data_quality"
    elif "signal_execution_delay" in labels:
        primary = "execution_timing"

    return {
        "schema_version": "trade_review_system_issue.v1",
        "system_contaminated": bool(labels),
        "contaminates_learning": any(label in SYSTEM_CONTAMINATION_LABELS for label in labels),
        "primary_responsibility": primary,
        "labels": labels,
        "evidence": evidence,
    }


def review_has_system_contamination(review_payload: dict[str, Any] | None) -> bool:
    review = _as_dict(review_payload)
    system_context = _as_dict(review.get("system_issue_context"))
    if system_context:
        return _safe_bool(system_context.get("contaminates_learning"), False)
    labels = set(review.get("responsibility_labels") or []) | set(review.get("failure_tags") or [])
    return bool(labels & SYSTEM_CONTAMINATION_LABELS)


def review_execution_evidence_is_trainable(review_payload: dict[str, Any] | None) -> bool:
    """Return whether a matured review has a complete execution chain.

    Consumers share the state written by ``build_execution_quality_evidence``;
    they must not infer training permission from the numeric quality score or
    from a legacy review label.
    """
    review = _as_dict(review_payload)
    evidence = _as_dict(review.get("execution_quality_evidence"))
    state = str(
        review.get("execution_quality_state")
        or evidence.get("evidence_state")
        or "unknown"
    ).strip().lower()
    evidence_state = str(evidence.get("evidence_state") or "").strip().lower()
    return (
        str(evidence.get("schema_version") or "") == "execution_quality_evidence.v2"
        and state in {"full", "replay_verified"}
        and evidence_state == state
        and not review_has_system_contamination(review)
    )


def normalize_trade_review_contract(
    review_payload: dict[str, Any] | None,
    *,
    entry_quality: Any = 0.0,
    hold_quality: Any = 0.0,
    exit_quality: Any = 0.0,
    regime_fit_score: Any = 0.0,
    execution_quality: Any = 0.0,
) -> dict[str, Any]:
    review = dict(review_payload or {})
    # The hot row is a bounded projection.  The complete recursive branches
    # are retained by the writer in the archive payload and are addressed by
    # supervisor_payload_sha256; keeping them inline would recreate the
    # historical write-amplification path even after schema 15 is applied.
    normalized = bounded_review_projection(review)

    recursive_supervisor_payload = {
        key: review[key]
        for key in ("inferred_close_supervisor", "responsibility_domains")
        if isinstance(review.get(key), dict)
    }
    if recursive_supervisor_payload:
        # The digest is a stable reference for the complete pre-projection
        # object. The archive writer attaches the compressed original when a
        # database connection is available; the hot review stays bounded.
        normalized["supervisor_payload_sha256"] = supervisor_payload_sha256(
            recursive_supervisor_payload
        )

    normalized["contract_version"] = str(
        review.get("contract_version")
        or review.get("review_contract_version")
        or "phase_d.v1"
    )
    normalized["entry_quality"] = round(
        _safe_float(review.get("entry_quality"), _safe_float(entry_quality)), 6
    )
    normalized["hold_quality"] = round(
        _safe_float(review.get("hold_quality"), _safe_float(hold_quality)), 6
    )
    normalized["exit_quality"] = round(
        _safe_float(review.get("exit_quality"), _safe_float(exit_quality)), 6
    )
    normalized["regime_fit_score"] = round(
        _safe_float(review.get("regime_fit_score"), _safe_float(regime_fit_score)), 6
    )
    normalized["regime_fit"] = round(
        _safe_float(review.get("regime_fit"), normalized["regime_fit_score"]), 6
    )
    normalized["execution_quality"] = round(
        _safe_float(review.get("execution_quality"), _safe_float(execution_quality)), 6
    )
    normalized["holding_efficiency"] = round(
        _safe_float(review.get("holding_efficiency")), 6
    )
    normalized["profit_capture_ratio"] = round(
        _safe_float(review.get("profit_capture_ratio")), 6
    )
    normalized["giveback_ratio"] = round(
        _safe_float(review.get("giveback_ratio")), 6
    )
    normalized["time_in_profit"] = round(
        _safe_float(
            review.get("time_in_profit"),
            _safe_float(review.get("time_in_profit_seconds")),
        ),
        6,
    )
    normalized["time_in_profit_seconds"] = round(
        _safe_float(
            review.get("time_in_profit_seconds"),
            normalized["time_in_profit"],
        ),
        6,
    )
    normalized["time_in_profit_ratio"] = round(
        _safe_float(review.get("time_in_profit_ratio")), 6
    )
    normalized["thesis_status_at_exit"] = str(
        review.get("thesis_status_at_exit")
        or review.get("thesis_status")
        or ""
    )
    normalized["regime_shift_at_exit"] = str(
        review.get("regime_shift_at_exit")
        or review.get("regime_shift")
        or ""
    )
    if isinstance(review.get("entry_timing_context"), dict):
        normalized["entry_timing_context"] = dict(review["entry_timing_context"])
    if isinstance(review.get("decision_freshness_context"), dict):
        normalized["decision_freshness_context"] = dict(review["decision_freshness_context"])
    system_issue = (
        dict(review["system_issue_context"])
        if isinstance(review.get("system_issue_context"), dict)
        else build_system_issue_context(normalized)
    )
    normalized["system_issue_context"] = system_issue
    return bounded_review_projection(normalized)
