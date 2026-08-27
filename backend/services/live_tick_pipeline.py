"""Pure helpers for the live tick factor pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import math
import time
from typing import Any

from backend.services.review_contract import build_execution_quality_event_details


def _normalize_close_source(close_source: Mapping[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(close_source, Mapping):
        return dict(close_source)
    if close_source:
        return {"close_reason_source": str(close_source), "inferred_close_supervisor": {}}
    return {"close_reason_source": "", "inferred_close_supervisor": {}}


def build_factor_bar(last_bar: Any, df_new: Any, timeframe: str) -> dict[str, Any]:
    index = getattr(last_bar, "index", ())
    ts_value = getattr(df_new, "index", [None])[-1]
    return {
        "open": float(last_bar["open"]),
        "high": float(last_bar["high"]),
        "low": float(last_bar["low"]),
        "close": float(last_bar["close"]),
        "volume": float(last_bar["volume"]) if "volume" in index else 0.0,
        "time": float(ts_value.timestamp()) if hasattr(ts_value, "timestamp") else 0.0,
        "timeframe": timeframe,
        "complete": True,
    }


def build_factor_votes(
    signals: Mapping[str, Any],
    factor_values: Mapping[str, Any],
    factor_roles: Mapping[str, Any] | None = None,
    active_weights: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    votes: dict[str, dict[str, Any]] = {}
    roles = factor_roles or {}
    weights = active_weights or {}
    for name, sig in (signals or {}).items():
        raw_val = factor_values.get(name)
        signal_value = sig if isinstance(sig, (int, float)) else None
        raw_value = raw_val if isinstance(raw_val, (int, float)) else None
        role = str(roles.get(name) or "alpha")
        try:
            used_in_score = role == "alpha" and abs(float(weights.get(name, 0.0) or 0.0)) > 0
        except (TypeError, ValueError):
            used_in_score = False
        votes[str(name)] = {
            "signal": round(signal_value, 4) if signal_value is not None else None,
            "raw": round(raw_value, 4) if raw_value is not None else None,
            "direction": (
                1 if signal_value > 0 else -1 if signal_value < 0 else 0
            ) if used_in_score and signal_value is not None else 0,
            "role": role,
            "used_in_score": used_in_score,
            "available": signal_value is not None,
            "abstained": signal_value is None,
        }
    return votes


def build_factor_snapshot_summary(
    composite: Any,
    gate_result: Any,
    *,
    now: float,
    decision_bar_ts: float | None = None,
) -> dict[str, Any]:
    summary = {
        "direction": composite.direction,
        "score": round(composite.score, 4),
        "tactical_score": round(composite.tactical_score, 4),
        "macro_score": round(composite.macro_score, 4),
        "alpha_score": round(getattr(composite, "alpha_score", composite.score), 4),
        "n_active": composite.n_active_factors,
        "n_available": int(
            getattr(composite, "n_available_factors", composite.n_active_factors)
            or 0
        ),
        "n_scoring": int(
            getattr(
                composite,
                "n_scoring_factors",
                getattr(composite, "n_active_alpha_factors", 0),
            )
            or 0
        ),
        "n_contributing": int(getattr(composite, "n_contributing_factors", 0) or 0),
        "n_active_alpha": int(
            getattr(
                composite,
                "n_active_alpha_factors",
                composite.n_active_factors,
            )
            or 0
        ),
        "effective_alpha_factor_count": int(getattr(
            composite,
            "effective_alpha_factor_count",
            getattr(composite, "n_active_alpha_factors", composite.n_active_factors),
        ) or 0),
        "n_abstain": composite.n_abstain_factors,
        "composer_version": str(getattr(composite, "composer_version", "")),
        "context_state": dict(getattr(composite, "context_state", {}) or {}),
        "context_policy": dict(getattr(composite, "context_policy", {}) or {}),
        "calibrated_confidence": dict(getattr(composite, "calibrated_confidence", {}) or {}),
        "redundancy_groups": dict(getattr(composite, "redundancy_groups", {}) or {}),
        "gate_passed": gate_result.passed,
        "gate_reason": gate_result.reason,
        "ts": now,
    }
    if decision_bar_ts is not None:
        summary["decision_bar_ts"] = float(decision_bar_ts or 0.0)
    return summary


def build_signal_log_suffix(composite: Any, gate_result: Any) -> str:
    if int(getattr(composite, "direction", 0) or 0) == 0:
        return ""
    direction_name = {1: "LONG", -1: "SHORT"}.get(composite.direction, "?")
    available = getattr(
        composite, "n_available_factors", composite.n_active_factors
    )
    scoring = getattr(
        composite,
        "n_scoring_factors",
        getattr(composite, "n_active_alpha_factors", 0),
    )
    contributing = getattr(composite, "n_contributing_factors", 0)
    return (
        f" signal={direction_name} score={composite.score:.4f}"
        f" tactical={composite.tactical_score:.4f}"
        f" macro={composite.macro_score:.4f}"
        f" available={available}"
        f" scoring={scoring}"
        f" contributing={contributing}"
        f" gate={gate_result.reason}"
    )


def normalize_live_positions_payload(
    positions_payload: Any,
    *,
    position_to_dict: Callable[[Any], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    positions = positions_payload or []
    if isinstance(positions, dict):
        positions = positions.get("positions", []) or []
    if not positions:
        return []
    if isinstance(positions[0], dict):
        return list(positions)
    if position_to_dict is None:
        return [dict(p) for p in positions]
    return [position_to_dict(p) for p in positions]


def collect_position_ids(positions: Iterable[Mapping[str, Any]]) -> set[int]:
    current_pids: set[int] = set()
    for position in positions or []:
        pid = position.get("position_id") or position.get("ticket")
        if pid is not None:
            current_pids.add(int(pid))
    return current_pids


def resolve_closed_position_ids(
    *,
    previous_position_ids: set[int],
    current_position_ids: set[int],
    positions_snapshot_ready: bool,
    tracked_position_ids: set[int] | None = None,
) -> tuple[set[int], set[int], bool]:
    expected_position_ids = {
        int(item)
        for item in (
            set(previous_position_ids) | set(tracked_position_ids or set())
        )
        if int(item or 0) > 0
    }
    if not expected_position_ids:
        return set(), current_position_ids.copy(), False
    if not current_position_ids and not positions_snapshot_ready:
        return set(), expected_position_ids, True
    return expected_position_ids - current_position_ids, current_position_ids, False


def select_close_total_pnl(
    *,
    real_pnl: Mapping[str, Any] | None,
    factor_contributions: Mapping[str, float] | None,
    fallback_pnl: float,
) -> float:
    if real_pnl and real_pnl.get("net") is not None:
        return float(real_pnl["net"])
    if factor_contributions:
        return float(sum(factor_contributions.values()))
    return float(fallback_pnl or 0.0)


def build_close_decision_audit_meta(
    *,
    position_id: int,
    total_pnl: float,
    current_price: float,
    tick: int,
) -> dict[str, Any]:
    return {
        "position_id": position_id,
        "pnl": round(float(total_pnl or 0.0), 2),
        "price": round(float(current_price or 0.0), 2),
        "tick": tick,
    }


def build_close_ledger_payloads(
    *,
    position_id: int,
    timeframe: str,
    decision_ts: float,
    close_ts: float,
    account: Mapping[str, Any],
    session_pnl: float,
    risk_state: Mapping[str, Any],
    total_pnl: float,
    current_price: float,
    tick: int,
    close_reason: str,
    close_source: Mapping[str, Any] | str | None,
    attribution_integrity: str,
    close_verdict: Mapping[str, Any],
    factor_contributions: Mapping[str, float] | None,
    real_pnl: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    pid = int(position_id)
    close_source = _normalize_close_source(close_source)
    close_source_name = str(close_source.get("close_reason_source") or "")
    inferred_supervisor = close_source.get("inferred_close_supervisor") or {}
    action_json = {
        "position_id": pid,
        "pnl": round(float(total_pnl or 0.0), 2),
        "price": round(float(current_price or 0.0), 2),
        "tick": tick,
        "close_reason": close_reason,
        "close_reason_source": close_source_name,
        "inferred_close_supervisor": inferred_supervisor,
        "attribution_integrity": attribution_integrity,
        "risk_verdict": dict(close_verdict or {}),
        "factor_contributions": dict(factor_contributions or {}),
        "real_pnl": dict(real_pnl or {}),
    }
    position_details = {
        "tick": tick,
        "real_pnl": dict(real_pnl or {}),
        "factor_contributions": dict(factor_contributions or {}),
        "close_reason": close_reason,
        "close_reason_source": close_source_name,
        "inferred_close_supervisor": inferred_supervisor,
        "attribution_integrity": attribution_integrity,
        "risk_verdict": dict(close_verdict or {}),
    }
    return {
        "decision": {
            "event_type": "close",
            "symbol": "XAUUSD+",
            "timeframe": timeframe,
            "decision_ts": decision_ts,
            "trade_id": str(pid),
            "position_id": str(pid),
            "portfolio_state": {
                "balance": account.get("balance", 0),
                "equity": account.get("equity", 0),
                "session_pnl": session_pnl,
            },
            "risk_state": dict(risk_state or {}),
            "action_score": float(total_pnl),
            "action_reason": close_reason,
            "action_json": action_json,
        },
        "position_event": {
            "position_id": str(pid),
            "trade_id": str(pid),
            "symbol": "XAUUSD+",
            "event_type": "closed",
            "avg_price": float(current_price),
            "realized_pnl": float(total_pnl),
            "details": position_details,
            "event_ts": close_ts,
        },
    }


def build_trade_review_payload(
    *,
    position_id: int,
    total_pnl: float,
    current_price: float,
    close_ts: float,
    factor_contributions: Mapping[str, float] | None,
    exit_decision_id: str,
    real_pnl: Mapping[str, Any] | None,
    close_reason: str,
    context_integrity: str,
    attribution_integrity: str,
    close_source: Mapping[str, Any] | str | None,
) -> dict[str, Any]:
    close_source = _normalize_close_source(close_source)
    return {
        "position_id": str(int(position_id)),
        "pnl": float(total_pnl),
        "close_price": float(current_price),
        "close_ts": close_ts,
        "contributions": dict(factor_contributions or {}),
        "exit_decision_id": exit_decision_id,
        "real_pnl": real_pnl,
        "close_reason": close_reason,
        "context_integrity": context_integrity,
        "attribution_integrity": attribution_integrity,
        "close_reason_source": str(close_source.get("close_reason_source") or ""),
        "inferred_close_supervisor": close_source.get("inferred_close_supervisor") or {},
    }


def build_effective_event_sizing_payload(
    *,
    base_volume: float,
    adjusted_volume: float,
    sizing_trace: Mapping[str, Any],
    sizing_block_reason: str,
    event_sizing_context: Mapping[str, Any],
) -> dict[str, Any]:
    trace = dict(sizing_trace or {})
    volume = float(adjusted_volume or 0.0)
    if sizing_block_reason and base_volume > 0:
        volume = float(base_volume)
        trace["event_policy_candidate_api_volume"] = volume
    context = {
        **dict(event_sizing_context or {}),
        "base_api_volume": float(base_volume or 0.0),
        "raw_api_volume": float(trace.get("event_raw_api_volume", base_volume) or 0.0),
        "adjusted_api_volume": float(adjusted_volume or 0.0),
        "effective_requested_api_volume": volume,
        "blocked_reason": str(sizing_block_reason or ""),
    }
    return {"volume": volume, "sizing_trace": trace, "event_sizing_context": context}


def build_open_order_preflight(
    *,
    direction: int,
    current_price: float,
    atr_price: float,
    strategy_sl_atr: float,
    strategy_tp_atr: float,
    bridge_meta: Mapping[str, Any] | None,
    protection_prices: Callable[[int, float, float, float, int], tuple[float, float]],
) -> dict[str, Any]:
    direction_name = {1: "LONG", -1: "SHORT"}.get(int(direction or 0), "?")
    price = float(current_price or 0.0)
    atr = float(atr_price or 0.0)
    sl_dist = atr * float(strategy_sl_atr or 0.0) if atr > 0 else price * 0.02
    tp_dist = atr * float(strategy_tp_atr or 0.0) if atr > 0 else price * 0.03
    digits = int((bridge_meta or {}).get("digits", 2) or 2)
    sl_price, tp_price = protection_prices(
        int(direction or 0),
        price,
        sl_dist,
        tp_dist,
        digits,
    )
    return {
        "direction_name": direction_name,
        "sl_dist": sl_dist,
        "tp_dist": tp_dist,
        "digits": digits,
        "sl_price": float(sl_price or 0.0),
        "tp_price": float(tp_price or 0.0),
    }


def guard_current_price_with_spot_quote(
    *,
    current_price: float,
    get_spot_quote: Callable[[], Mapping[str, Any] | None],
    quote_is_fresh: Callable[[Mapping[str, Any]], bool],
) -> dict[str, Any]:
    price = float(current_price or 0.0)
    try:
        quote = get_spot_quote() or {}
        spot = float((quote or {}).get("mid") or 0.0) if quote_is_fresh(quote) else 0.0
        if spot and spot > 0 and price > 0 and abs(spot - price) / price < 0.20:
            price = spot
        return {"current_price": price, "error": None}
    except Exception as exc:
        return {"current_price": price, "error": exc}


def resolve_order_fill_price(result: Any, *, current_price: float) -> float:
    try:
        price = float(getattr(result, "price", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return price if math.isfinite(price) and price > 0.0 else 0.0


def _position_id_from_payload(position: Any) -> int:
    if position is None:
        return 0
    if hasattr(position, "get"):
        return int(position.get("position_id") or position.get("ticket") or 0)
    return int(getattr(position, "position_id", 0) or getattr(position, "ticket", 0) or 0)


def resolve_order_position_id(result: Any, *, positions_before: list[Any] | None) -> int:
    """Return only the broker-confirmed position ID.

    ``positions_before[0]`` was an unsafe historical guess: it could attach
    protection/audit records to an unrelated existing position after a timeout
    or unknown protobuf.  The v2 execution contract resolves IDs from broker
    order/deal/position differentials before producing the result.
    """
    pid = int(getattr(result, "position_id", 0) or 0)
    if pid > 0:
        return pid
    return 0


def resolve_open_protection_prices(
    *,
    direction: int,
    fill_price: float,
    current_price: float,
    sl_dist: float,
    tp_dist: float,
    digits: int,
    position_id: int,
    refreshed_positions: Iterable[Any] | None,
    position_open_price: Callable[[Any], float],
    protection_prices: Callable[[int, float, float, float, int], tuple[float, float]],
) -> dict[str, Any]:
    reference_price = float(fill_price or 0.0)
    if reference_price > 0:
        sl_price, tp_price = protection_prices(
            int(direction or 0),
            reference_price,
            float(sl_dist or 0.0),
            float(tp_dist or 0.0),
            int(digits or 2),
        )
    else:
        sl_price, tp_price = 0.0, 0.0
    for position in refreshed_positions or []:
        if _position_id_from_payload(position) == int(position_id):
            protection_ref = float(position_open_price(position) or 0.0)
            if protection_ref > 0:
                sl_price, tp_price = protection_prices(
                    int(direction or 0),
                    protection_ref,
                    float(sl_dist or 0.0),
                    float(tp_dist or 0.0),
                    int(digits or 2),
                )
                reference_price = protection_ref
            break
    return {
        "reference_price": reference_price,
        "sl_price": float(sl_price or 0.0),
        "tp_price": float(tp_price or 0.0),
    }


def build_market_order_block(
    *,
    market_session: Mapping[str, Any],
    risk_verdict: Any,
) -> dict[str, Any]:
    market_block_reason = ""
    if not bool((market_session or {}).get("can_open_positions", False)):
        market_block_reason = (
            f"market_session:{(market_session or {}).get('status') or 'unknown'}:"
            f"{(market_session or {}).get('reason') or 'unknown'}"
        )
    risk_allowed = bool(getattr(risk_verdict, "allowed", False))
    risk_reason = str(getattr(risk_verdict, "reason", "") or "")
    return {
        "market_block_reason": market_block_reason,
        "order_blocked": bool(market_block_reason) or not risk_allowed,
        "block_reason": market_block_reason or risk_reason,
        "skip_stage": "market_session" if market_block_reason else "risk_policy",
    }


def build_skip_ledger_payload(
    *,
    composite: Any,
    gate_result: Any,
    cfg: Any,
    bar: Mapping[str, Any],
    account: Mapping[str, Any],
    positions_before: list[Any] | None,
    risk_state: Mapping[str, Any],
    risk_verdict: Any,
    block_reason: str,
    skip_stage: str,
    tick: int,
    sizing_trace: Mapping[str, Any],
    market_session: Mapping[str, Any],
    event_sizing_context: Mapping[str, Any],
    learning_context: Mapping[str, Any],
    decision_ts_fallback: float,
) -> dict[str, Any]:
    risk_payload = risk_verdict.to_dict() if hasattr(risk_verdict, "to_dict") else dict(risk_verdict or {})
    action_json = {
        "tick": tick,
        "skip_stage": skip_stage,
        "sizing_trace": dict(sizing_trace or {}),
        "market_session": dict(market_session or {}),
        "event_sizing": dict(event_sizing_context or {}),
        **dict(learning_context or {}),
    }
    # A pre-candidate admission blocker is not a RiskPolicy verdict.  Keep
    # the existing ledger shape for evaluated risk decisions, while making an
    # unevaluated risk stage explicit instead of persisting an empty/fake
    # verdict that downstream readers could interpret as a policy result.
    if risk_payload:
        action_json["risk_verdict"] = risk_payload
    else:
        action_json["risk_stage"] = "not_reached"
        action_json["risk_policy_reached"] = False
    return {
        "event_type": "skip",
        "composite": composite,
        "gate_result": gate_result,
        "symbol": "XAUUSD+",
        "timeframe": str(getattr(cfg, "timeframe", "") or ""),
        "decision_ts": (bar or {}).get("time", decision_ts_fallback),
        "portfolio_state": {
            "balance": (account or {}).get("balance", 0),
            "equity": (account or {}).get("equity", 0),
            "n_positions": len(positions_before or []),
        },
        "risk_state": dict(risk_state or {}),
        "action_reason": block_reason,
        "action_json": action_json,
    }


def build_open_ledger_payloads(
    *,
    composite: Any,
    gate_result: Any,
    cfg: Any,
    bar: Mapping[str, Any],
    account: Mapping[str, Any],
    positions_before: list[Any] | None,
    session_pnl: float,
    risk_state: Mapping[str, Any],
    risk_verdict: Any,
    pid: int,
    requested_volume: float,
    base_requested_volume: float,
    actual_api_volume: float,
    current_price: float,
    fill_price: float,
    sl_price: float,
    tp_price: float,
    tick: int,
    event_sizing_context: Mapping[str, Any],
    sizing_trace: Mapping[str, Any],
    learning_context: Mapping[str, Any],
    decision_ts_fallback: float,
    event_ts: float,
) -> dict[str, dict[str, Any]]:
    pid_str = str(int(pid))
    direction = int(getattr(composite, "direction", 0) or 0)
    risk_payload = risk_verdict.to_dict() if hasattr(risk_verdict, "to_dict") else dict(risk_verdict or {})
    return {
        "decision": {
            "event_type": "open",
            "composite": composite,
            "gate_result": gate_result,
            "symbol": "XAUUSD+",
            "timeframe": str(getattr(cfg, "timeframe", "") or ""),
            "decision_ts": (bar or {}).get("time", decision_ts_fallback),
            "trade_id": pid_str,
            "position_id": pid_str,
            "portfolio_state": {
                "balance": (account or {}).get("balance", 0),
                "equity": (account or {}).get("equity", 0),
                "n_positions": len(positions_before or []),
                "session_pnl": float(session_pnl or 0.0),
            },
            "risk_state": dict(risk_state or {}),
            "action_reason": "executed",
            "action_json": {
                "position_id": int(pid),
                "volume": actual_api_volume,
                "requested_volume": requested_volume,
                "base_requested_volume": base_requested_volume,
                "event_sizing": dict(event_sizing_context or {}),
                "sizing_trace": dict(sizing_trace or {}),
                "price": round(float(current_price or 0.0), 2),
                "sl": round(float(sl_price or 0.0), 2),
                "tp": round(float(tp_price or 0.0), 2),
                "tick": int(tick or 0),
                "risk_verdict": risk_payload,
                **dict(learning_context or {}),
            },
        },
        "submitted_order": {
            "event_type": "submitted",
            "trade_id": pid_str,
            "order_id": pid_str,
            "broker_order_id": pid_str,
            "price": float(current_price or 0.0),
            "volume": float(actual_api_volume or 0.0),
            "status": "submitted",
            "details": build_execution_quality_event_details(
                tick=tick,
                direction=direction,
                requested_price=current_price,
                fill_price=0.0,
                learning_context=learning_context,
            ),
        },
        "filled_order": {
            "event_type": "filled",
            "trade_id": pid_str,
            "order_id": pid_str,
            "broker_order_id": pid_str,
            "price": float(fill_price or 0.0),
            "volume": float(actual_api_volume or 0.0),
            "status": "filled",
            "details": {
                **build_execution_quality_event_details(
                    tick=tick,
                    direction=direction,
                    requested_price=current_price,
                    fill_price=fill_price,
                    learning_context=learning_context,
                ),
                "event_sizing": dict(event_sizing_context or {}),
            },
        },
        "position_event": {
            "position_id": pid_str,
            "trade_id": pid_str,
            "symbol": "XAUUSD+",
            "event_type": "opened",
            "net_volume": float(actual_api_volume or 0.0),
            "avg_price": float(fill_price or 0.0),
            "details": {
                "tick": int(tick or 0),
                "direction": direction,
                "sl": round(float(sl_price or 0.0), 2),
                "tp": round(float(tp_price or 0.0), 2),
                "event_sizing": dict(event_sizing_context or {}),
                "sizing_trace": dict(sizing_trace or {}),
            },
            "event_ts": float(event_ts or 0.0),
        },
    }


def build_open_decision_audit_meta(
    *,
    position_id: int,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    event_sizing_context: Mapping[str, Any],
    sizing_trace: Mapping[str, Any],
    current_price: float,
    sl_price: float,
    tp_price: float,
    tick: int,
) -> dict[str, Any]:
    return {
        "position_id": int(position_id),
        "volume": actual_api_volume,
        "requested_volume": requested_volume,
        "base_requested_volume": base_requested_volume,
        "event_sizing": dict(event_sizing_context or {}),
        "sizing_trace": dict(sizing_trace or {}),
        "price": round(float(current_price or 0.0), 2),
        "sl": round(float(sl_price or 0.0), 2),
        "tp": round(float(tp_price or 0.0), 2),
        "tick": int(tick or 0),
    }


def build_amend_failed_ledger_payloads(
    *,
    composite: Any,
    gate_result: Any,
    cfg: Any,
    bar: Mapping[str, Any],
    account: Mapping[str, Any],
    positions_before: list[Any] | None,
    risk_state: Mapping[str, Any],
    pid: int,
    requested_volume: float,
    fill_price: float,
    sl_price: float,
    tp_price: float,
    actual_api_volume: float,
    tick: int,
    action_reason: str,
    comment: str = "",
    error: str = "",
    decision_ts_fallback: float = 0.0,
) -> dict[str, dict[str, Any]]:
    pid_str = str(int(pid))
    action_json = {
        "tick": int(tick or 0),
        "skip_stage": "amend_sltp",
        "position_id": int(pid),
        "requested_volume": requested_volume,
        "fill_price": fill_price,
        "sl": sl_price,
        "tp": tp_price,
    }
    details = {"tick": int(tick or 0), "direction": int(getattr(composite, "direction", 0) or 0)}
    if error:
        action_json["error"] = str(error)[:300]
        details["error"] = str(error)[:300]
    else:
        details["comment"] = str(comment or "")
    return {
        "decision": {
            "event_type": "amend_failed",
            "composite": composite,
            "gate_result": gate_result,
            "symbol": "XAUUSD+",
            "timeframe": str(getattr(cfg, "timeframe", "") or ""),
            "decision_ts": (bar or {}).get("time", decision_ts_fallback),
            "trade_id": pid_str,
            "position_id": pid_str,
            "portfolio_state": {
                "balance": (account or {}).get("balance", 0),
                "equity": (account or {}).get("equity", 0),
                "n_positions": len(positions_before or []),
            },
            "risk_state": dict(risk_state or {}),
            "action_reason": str(action_reason or "amend_failed"),
            "action_json": action_json,
        },
        "order_event": {
            "event_type": "amend_failed",
            "trade_id": pid_str,
            "order_id": pid_str,
            "broker_order_id": pid_str,
            "price": float(fill_price or 0.0),
            "volume": float(actual_api_volume or 0.0),
            "status": "failed",
            "details": details,
        },
    }


def build_order_failed_ledger_payloads(
    *,
    composite: Any,
    gate_result: Any,
    cfg: Any,
    bar: Mapping[str, Any],
    account: Mapping[str, Any],
    positions_before: list[Any] | None,
    risk_state: Mapping[str, Any],
    requested_volume: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    tick: int,
    error_code: str,
    comment: str,
    decision_ts_fallback: float = 0.0,
) -> dict[str, dict[str, Any]]:
    reason = f"{error_code or '?'} {comment or ''}".strip()
    return {
        "decision": {
            "event_type": "order_failed",
            "composite": composite,
            "gate_result": gate_result,
            "symbol": "XAUUSD+",
            "timeframe": str(getattr(cfg, "timeframe", "") or ""),
            "decision_ts": (bar or {}).get("time", decision_ts_fallback),
            "portfolio_state": {
                "balance": (account or {}).get("balance", 0),
                "equity": (account or {}).get("equity", 0),
                "n_positions": len(positions_before or []),
            },
            "risk_state": dict(risk_state or {}),
            "action_reason": reason or "order_failed",
            "action_json": {
                "tick": int(tick or 0),
                "skip_stage": "broker_order_failed",
                "requested_volume": requested_volume,
                "price": round(float(current_price or 0.0), 2),
                "sl": round(float(sl_price or 0.0), 2),
                "tp": round(float(tp_price or 0.0), 2),
                "error_code": str(error_code or ""),
                "comment": str(comment or ""),
            },
        },
        "order_event": {
            "event_type": "order_failed",
            "price": float(current_price or 0.0),
            "volume": float(requested_volume or 0.0),
            "status": "failed",
            "details": {
                "tick": int(tick or 0),
                "direction": int(getattr(composite, "direction", 0) or 0),
                "error_code": str(error_code or ""),
                "comment": str(comment or ""),
            },
        },
    }
