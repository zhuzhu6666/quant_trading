from __future__ import annotations

from typing import Any


SYSTEM_CONTAMINATION_LABELS = {
    "bar_data_degraded",
    "data_quality_issue",
    "decision_bar_stale",
    "market_data_stale",
    "signal_execution_delay",
}

ADVISORY_ONLY_HEALTH_COMPONENTS = {"tick_data"}


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
    timing = _as_dict(review.get("entry_timing_context"))
    freshness = _as_dict(review.get("decision_freshness_context"))
    data_quality = _as_dict(review.get("data_quality_context"))
    market_session = _as_dict(review.get("market_session"))
    runtime_health = _as_dict(data_quality.get("runtime_health"))
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
    if any(label in labels for label in ("decision_bar_stale", "market_data_stale", "data_quality_issue", "bar_data_degraded")):
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
    normalized = dict(review)

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
    return normalized
