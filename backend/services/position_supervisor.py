from __future__ import annotations

from typing import Any

from backend.services.position_supervisor_templates import (
    DEFAULT_TEMPLATE_ID,
    normalize_position_supervisor_template,
)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _component_state(position: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = position.get(key)
        if value not in (None, ""):
            return str(value).strip().lower()
    return ""


def _component_known(position: dict[str, Any], *keys: str) -> bool:
    # Canonical broker reconciliation must publish an explicit component fact;
    # missing state is unknown and cannot authorize numeric supervision.
    return _component_state(position, *keys) == "known"


def _side_name(direction: int) -> str:
    return "long" if int(direction or 0) >= 0 else "short"


def _tightened_sl(
    *,
    direction: int,
    entry_price: float,
    current_price: float,
    current_sl: float,
    profit_capture_ratio: float,
    sl_policy: dict[str, Any] | None = None,
) -> float:
    if entry_price <= 0 or current_price <= 0:
        return 0.0
    sl_policy = sl_policy or {}
    breakeven_lock_ratio = _safe_float(sl_policy.get("breakeven_lock_ratio"), 0.25)
    profit_lock_multiplier = _safe_float(sl_policy.get("profit_lock_multiplier"), 0.60)
    lock_ratio = max(breakeven_lock_ratio, profit_capture_ratio * profit_lock_multiplier)
    if direction >= 0:
        breakeven = entry_price
        lock_price = entry_price + max(0.0, (current_price - entry_price) * lock_ratio)
        target = max(current_sl, breakeven, lock_price)
    else:
        breakeven = entry_price
        lock_price = entry_price - max(0.0, (entry_price - current_price) * lock_ratio)
        target = min(current_sl if current_sl > 0 else breakeven, breakeven, lock_price)
    return round(target, 2)


def build_model_tighten_controls(context: dict[str, Any]) -> dict[str, Any]:
    """Reuse the authoritative supervisor SL policy for model fusion."""
    position = dict(context.get("position") or {})
    if not _component_known(position, "current_price_state", "price_state"):
        return {}
    risk = dict(context.get("risk") or {})
    template = normalize_position_supervisor_template(context.get("position_supervisor_template") or {})
    sl_policy = dict(template.get("sl_policy") or {})
    direction = int(position.get("direction") or risk.get("direction") or 0)
    entry_price = _safe_float(position.get("entry_price") or risk.get("entry_price"))
    current_price = _safe_float(position.get("current_price") or risk.get("current_price"))
    current_sl = _safe_float(position.get("sl") or position.get("stop_loss") or risk.get("current_sl"))
    target = _tightened_sl(
        direction=direction,
        entry_price=entry_price,
        current_price=current_price,
        current_sl=current_sl,
        profit_capture_ratio=_safe_float(risk.get("profit_capture_ratio")),
        sl_policy=sl_policy,
    )
    if target <= 0:
        return {}
    return {
        "target_stop_loss": target,
        "target_take_profit": _safe_float(position.get("tp") or position.get("take_profit")),
        "reduce_fraction": 0.0,
    }


def _extended_tp(
    *,
    direction: int,
    entry_price: float,
    current_tp: float,
    tp_policy: dict[str, Any] | None = None,
) -> float:
    if entry_price <= 0 or current_tp <= 0:
        return current_tp
    tp_policy = tp_policy or {}
    extension_factor = _safe_float(tp_policy.get("extension_factor"), 0.0)
    max_extension_factor = _safe_float(tp_policy.get("max_extension_factor"), 0.0)
    extension_factor = _clamp(extension_factor, 0.0, max(0.0, max_extension_factor))
    if extension_factor <= 0:
        return current_tp
    distance = abs(current_tp - entry_price)
    if distance <= 0:
        return current_tp
    if direction >= 0:
        return round(current_tp + distance * extension_factor, 2)
    return round(current_tp - distance * extension_factor, 2)


def _target_changed(current: float, target: float, *, direction: int, target_kind: str, min_delta: float) -> bool:
    if target <= 0:
        return False
    if current <= 0:
        return True
    if target_kind == "sl":
        return target > current + min_delta if direction >= 0 else target < current - min_delta
    if target_kind == "tp":
        return target > current + min_delta if direction >= 0 else target < current - min_delta
    return abs(target - current) >= min_delta


def _target_progress(
    *,
    direction: int,
    entry_price: float,
    current_price: float,
    target_price: float,
    target_kind: str,
) -> float:
    if entry_price <= 0 or current_price <= 0 or target_price <= 0:
        return 0.0
    if target_kind == "tp":
        denom = (target_price - entry_price) if direction >= 0 else (entry_price - target_price)
        move = (current_price - entry_price) if direction >= 0 else (entry_price - current_price)
    else:
        denom = (entry_price - target_price) if direction >= 0 else (target_price - entry_price)
        move = (entry_price - current_price) if direction >= 0 else (current_price - entry_price)
    if denom <= 0:
        return 0.0
    return _clamp(move / denom, 0.0, 10.0)


_KNOWN_MARKET_STATES = frozenset({
    "low",
    "normal",
    "high",
    "weak",
    "strong",
})


def _market_state(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return "" if normalized in {"", "unknown", "none", "null", "n/a"} else normalized


def resolve_supervisor_posture(
    *,
    market: dict[str, Any] | None,
    risk: dict[str, Any] | None,
    thesis_status: str,
    regime_shift: str,
    hard_risk_active: bool,
    signal_reversal: bool,
    thesis_break_ready: bool,
    thesis_break_confirmed: bool,
    closed_bar_window_ready: bool,
) -> dict[str, Any]:
    """Resolve one explainable management posture from existing market facts.

    This is deliberately a pure, low-cardinality state resolver.  It does not
    calculate indicators or invent a second market regime authority.  Missing
    trend/volatility dimensions stay observational so a stale/partial market
    payload cannot turn into an aggressive protection action.
    """

    market = dict(market or {})
    risk = dict(risk or {})
    trend_state = _market_state(
        market.get("trend_strength_state")
        or (market.get("regime_dimensions") or {}).get("trend")
    )
    volatility_state = _market_state(
        market.get("volatility_state")
        or (market.get("regime_dimensions") or {}).get("volatility")
    )
    regime_source = str(market.get("regime_source") or "").strip().lower()
    regime_confidence = _safe_float(market.get("regime_confidence"), 0.0)
    dimensions_known = (
        trend_state in _KNOWN_MARKET_STATES
        and volatility_state in _KNOWN_MARKET_STATES
        and not regime_source.endswith("session_fallback")
    )
    hard_exit = bool(
        hard_risk_active
        or signal_reversal
        or (
            thesis_break_ready
            and thesis_break_confirmed
            and closed_bar_window_ready
        )
    )
    if hard_exit:
        posture = "exit_commit"
        reason = "confirmed_exit_evidence"
    elif not dimensions_known:
        posture = "unknown_observe"
        reason = "market_context_unknown"
    elif (
        regime_shift in {"confirmed", "transition", "detected"}
        or thesis_status in {"weakening", "broken", "confirmed_broken"}
    ):
        posture = "transition_confirming"
        reason = "market_or_thesis_transition"
    elif trend_state == "strong" and volatility_state in _KNOWN_MARKET_STATES:
        posture = "trend_hold"
        reason = "strong_trend_thesis_hold"
    else:
        posture = "range_capture"
        reason = "range_or_normal_market_capture"
    return {
        "posture": posture,
        "reason": reason,
        "trend_strength_state": trend_state or "unknown",
        "volatility_state": volatility_state or "unknown",
        "regime_source": regime_source or "unavailable",
        "regime_confidence": round(_clamp(regime_confidence), 4),
        "market_dimensions_known": bool(dimensions_known),
        "hard_exit": bool(hard_exit),
    }


def adaptive_execution_mode(template: dict[str, Any] | None) -> str:
    """Return the template boundary used by the single live supervisor path.

    Historical observation rows remain readable, but an active template is
    never allowed to advertise ``observation_only`` as a new execution
    authority.
    """

    template = template or {}
    boundary = dict(template.get("risk_boundary") or {})
    mode = str(boundary.get("adaptive_execution_mode") or "observation_only")
    if str(template.get("status") or "").strip().lower() == "active":
        return "governed_execute"
    return mode if mode in {"observation_only", "governed_execute"} else "observation_only"


def is_hard_supervisor_action(
    *,
    action: str,
    summary_reason: str,
    evidence: dict[str, Any] | None = None,
) -> bool:
    """Identify hard risk-reduction actions for policy classification.

    Execution authorization is decided by the governed runtime and RiskPolicy;
    this predicate does not grant an observation-only or Demo-specific path.
    """

    evidence = dict(evidence or {})
    if str(action or "").strip().lower() == "hold":
        return False
    if bool(evidence.get("hard_risk_active")):
        return True
    return str(summary_reason or "") in {
        "hard_risk_active",
        "holding_timeout_exceeded",
        "near_stop_loss_preemptive_exit",
        "thesis_broken",
        "regime_shift_detected",
    }


def humanize_supervisor_reason(action: str, reason: str, evidence: dict[str, Any] | None = None) -> str:
    evidence = evidence or {}
    if reason == "holding_timeout_exceeded":
        return "这笔仓位已经超过持仓时长上限，系统建议主动收口，不再继续等待原始止盈止损。"
    if reason == "thesis_broken":
        return "仓位 thesis 已经被判定为失效，继续占用风险预算的价值很低，系统建议直接退出。"
    if reason == "thesis_break_unconfirmed":
        return "交易假设出现弱化迹象，但独立证据尚未确认失效，系统保留原始风险保护并继续观察。"
    if reason == "thesis_break_pending_window":
        return "交易假设已有失效证据，但最小观察窗口尚未完成，系统保留原始风险保护并继续观察。"
    if reason == "profit_giveback_after_mfe":
        return "仓位曾经浮盈明显，但已经出现较大回吐，系统建议先收紧保护，避免继续把已证明的利润吐回去。"
    if reason == "profit_protection_evidence_pending":
        return "仓位出现了回吐或弱化信号，但有效盈利证据或完整观察窗口尚未形成，系统暂不主动收紧保护。"
    if reason == "trend_hold_preserve_profit":
        return "当前市场仍处于有效趋势，仓位没有确认失效；系统暂不因普通回吐或接近止盈而提前磨损利润。"
    if reason == "market_context_unknown":
        return "市场状态上下文不完整，系统保留硬风险保护并暂不执行主动持仓管理。"
    if reason == "hard_risk_active":
        return "检测到硬风险保护条件，系统优先执行风险收口。"
    if reason == "time_decay_and_low_efficiency":
        return "这笔仓位已经拿得偏久，但收益效率没有跟上，系统判断继续硬拿的性价比在下降。"
    if reason == "regime_shift_detected":
        return "当前市场状态与入场时不再一致，系统怀疑这笔仓位的适用环境已经发生切换。"
    if reason == "near_take_profit_capture":
        return "仓位已经非常接近原始止盈目标，系统建议直接兑现利润，避免临门回吐。"
    if reason == "near_take_profit_protect":
        return "仓位已经接近原始止盈目标，但持仓证据仍然较强，系统建议先收紧保护并按模板决定是否延展止盈。"
    if reason == "near_stop_loss_preemptive_exit":
        return "仓位已经非常接近止损，且持仓证据偏弱，系统建议提前止损离场，不再等到被动打掉。"
    if action == "reduce":
        return "系统判断这笔仓位仍有逻辑，但不值得继续满仓承受同样风险，建议先降一部分。"
    if action == "tighten":
        return "系统判断仓位还没到必须退出，但保护应该更紧，不适合继续裸拿。"
    return "系统判断这笔仓位暂时仍可继续持有，没有看到足够强的主动收口信号。"


def evaluate_position_supervisor(position_context: dict[str, Any]) -> dict[str, Any]:
    template = normalize_position_supervisor_template(
        position_context.get("position_supervisor_template")
        or position_context.get("supervisor_template")
        or position_context.get("template")
    )
    thresholds = template.get("thresholds") or {}
    sl_policy = template.get("sl_policy") or {}
    tp_policy = template.get("tp_policy") or {}
    capture_policy = template.get("capture_policy") or {}
    position = position_context.get("position") or {}
    risk = position_context.get("risk") or {}
    temporal = position_context.get("temporal_context") or {}
    market = position_context.get("market") or {}
    market_space = position_context.get("market_space_context") or {}
    entry_context = position_context.get("entry_context") or {}

    position_id = str(position.get("position_id") or position.get("ticket") or "")
    direction = _safe_int(position.get("direction") or (1 if position.get("type") == "buy" else -1 if position.get("type") == "sell" else 0))
    entry_price = _safe_float(position.get("entry_price") or position.get("open_price") or position.get("price_open"))
    current_price = _safe_float(position.get("current_price") or position.get("price_current"))
    current_sl = _safe_float(position.get("sl") or position.get("stop_loss"))
    current_tp = _safe_float(position.get("tp") or position.get("take_profit"))
    current_pnl = _safe_float(position.get("unrealized_pnl", position.get("profit", position.get("pnl"))))
    volume = _safe_float(position.get("volume") or position.get("api_volume"))
    price_component_state = _component_state(position, "current_price_state", "price_state")
    pnl_component_state = _component_state(position, "pnl_state", "unrealized_pnl_state")
    price_known = _component_known(position, "current_price_state", "price_state")
    pnl_known = _component_known(position, "pnl_state", "unrealized_pnl_state")
    path_metrics_state = str(position.get("position_path_metrics_state") or "").strip().lower()
    path_metrics_known = path_metrics_state in {"", "known"}

    holding_seconds = _safe_float(temporal.get("holding_seconds"))
    max_holding_seconds = _safe_float(risk.get("max_holding_seconds"))
    timeout_ratio = _safe_float(risk.get("holding_timeout_ratio"))
    if timeout_ratio <= 0 and max_holding_seconds > 0:
        timeout_ratio = holding_seconds / max_holding_seconds
    timeout_exceeded_value = risk.get("holding_timeout_exceeded")
    timeout_exceeded = (
        bool(timeout_exceeded_value)
        if timeout_exceeded_value is not None
        else max_holding_seconds > 0 and holding_seconds >= max_holding_seconds
    )

    mfe = _safe_float(risk.get("mfe"))
    mae = _safe_float(risk.get("mae"))
    giveback_ratio = _safe_float(risk.get("giveback_ratio"))
    profit_capture_ratio = _safe_float(risk.get("profit_capture_ratio"))
    time_in_profit = _safe_float(risk.get("time_in_profit", risk.get("time_in_profit_seconds")))
    holding_efficiency = _safe_float(risk.get("holding_efficiency"))
    time_decay_score = _safe_float(risk.get("time_decay_score"))
    thesis_status = str(risk.get("thesis_status") or "intact")
    regime_shift = str(risk.get("regime_shift") or "none")
    supervisor_state = dict(risk.get("supervisor_state") or {})
    original_sl = _safe_float(risk.get("original_stop_loss"))
    risk_boundary_sl = original_sl if original_sl > 0 else current_sl

    trigger_tags: list[str] = []
    action = "hold"
    summary_reason = "position_healthy"
    severity = "info"
    min_thesis_break_seconds = _safe_float(thresholds.get("min_thesis_break_seconds"))
    min_closed_bars_fast = max(1, _safe_int(thresholds.get("min_closed_bars_high_vol_or_weak_trend"), 1))
    min_closed_bars_default = max(min_closed_bars_fast, _safe_int(thresholds.get("min_closed_bars_default"), 2))
    hard_risk_bypass = bool(thresholds.get("hard_risk_bypass", True))
    min_independent_evidence = max(1, _safe_int(thresholds.get("min_independent_thesis_break_evidence"), 2))
    broken_holding_efficiency_threshold = _safe_float(thresholds.get("broken_holding_efficiency_threshold"), 0.20)
    giveback_reduce_threshold = _safe_float(thresholds.get("giveback_reduce_threshold"), 0.70)
    giveback_tighten_threshold = _safe_float(thresholds.get("giveback_tighten_threshold"), 0.35)
    profit_capture_min_threshold = _safe_float(thresholds.get("profit_capture_min_threshold"), 0.35)
    time_decay_reduce_threshold = _safe_float(thresholds.get("time_decay_reduce_threshold"), 0.35)
    timeout_tighten_ratio = _safe_float(thresholds.get("timeout_tighten_ratio"), 0.80)
    timeout_reduce_ratio = _safe_float(thresholds.get("timeout_reduce_ratio"), 0.80)
    weakening_efficiency_threshold = _safe_float(thresholds.get("weakening_holding_efficiency_threshold"), 0.45)
    near_tp_progress_threshold = _safe_float(thresholds.get("near_take_profit_progress"), 0.92)
    near_sl_progress_threshold = _safe_float(thresholds.get("near_stop_loss_progress"), 0.85)
    near_sl_efficiency_threshold = _safe_float(thresholds.get("near_stop_loss_efficiency_threshold"), 0.25)
    take_profit_progress = _target_progress(
        direction=direction,
        entry_price=entry_price,
        current_price=current_price,
        target_price=current_tp,
        target_kind="tp",
    ) if price_known else 0.0
    stop_loss_progress = _target_progress(
        direction=direction,
        entry_price=entry_price,
        current_price=current_price,
        target_price=risk_boundary_sl,
        target_kind="sl",
    ) if price_known else 0.0
    current_stop_loss_progress = _target_progress(
        direction=direction,
        entry_price=entry_price,
        current_price=current_price,
        target_price=current_sl,
        target_kind="sl",
    ) if price_known else 0.0

    near_tp_action = str(tp_policy.get("near_take_profit_action") or "close")
    tp_extension_enabled = bool(tp_policy.get("extension_enabled", False))
    tp_extension_progress_threshold = _safe_float(tp_policy.get("extension_progress_threshold"), 0.80)
    tp_extension_efficiency_threshold = _safe_float(tp_policy.get("extension_efficiency_threshold"), 0.70)
    tp_extension_profit_capture_min = _safe_float(tp_policy.get("extension_profit_capture_min"), 0.65)
    can_extend_tp = (
        price_known
        and pnl_known
        and current_pnl > 0
        and current_tp > 0
        and tp_extension_enabled
        and take_profit_progress >= tp_extension_progress_threshold
        and holding_efficiency >= tp_extension_efficiency_threshold
        and profit_capture_ratio >= tp_extension_profit_capture_min
        and thesis_status in {"intact", "strong", "healthy"}
        and regime_shift in {"none", "", "aligned"}
    )
    try:
        thesis_broken_confirmations = int(
            risk.get("thesis_broken_confirmations")
            or risk.get("consecutive_thesis_broken_count")
            or risk.get("consecutive_thesis_broken")
            or 0
        )
    except (TypeError, ValueError):
        thesis_broken_confirmations = 0
    signal_reversal = bool(
        risk.get("signal_reversal")
        or risk.get("direction_reversal")
        or risk.get("entry_signal_reversed")
    )
    completed_bars_after_entry = max(
        0,
        _safe_int(temporal.get("completed_bars_after_entry"), int(holding_seconds // 300.0)),
    )
    closed_bar_key = str(
        temporal.get("closed_bar_key")
        or temporal.get("closed_bar_ts")
        or temporal.get("last_closed_bar_ts")
        or f"bars:{completed_bars_after_entry}"
    )
    current_regime_text = str(
        risk.get("current_regime")
        or market.get("regime_id")
        or ""
    ).lower()
    fast_window = (
        "volatility=high" in current_regime_text
        or "trend=weak" in current_regime_text
        or str(market.get("volatility_state") or "").lower() == "high"
        or str(market.get("trend_strength_state") or "").lower() == "weak"
    )
    required_closed_bars = min_closed_bars_fast if fast_window else min_closed_bars_default
    closed_bar_window_ready = completed_bars_after_entry >= required_closed_bars
    # Discretionary profit protection needs one completed bar to distinguish
    # a real path from quote noise.  Thesis-break decisions retain the
    # template-specific (possibly longer) observation window above.
    management_required_closed_bars = max(1, min_closed_bars_fast)
    management_closed_bar_window_ready = (
        completed_bars_after_entry >= management_required_closed_bars
    )
    # Reuse the existing built-in baseline capture-policy floor as the
    # agent-level validity boundary.  Governance templates may raise this
    # floor, but may not create a second lower authority for micro-MFE.
    baseline_capture_policy = dict(
        normalize_position_supervisor_template(DEFAULT_TEMPLATE_ID).get(
            "capture_policy"
        )
        or {}
    )
    baseline_capture_mfe_floor = _safe_float(
        baseline_capture_policy.get("mfe_capture_failure_threshold"),
        0.0,
    )
    capture_mfe_floor = max(
        baseline_capture_mfe_floor,
        _safe_float(
            capture_policy.get("mfe_capture_failure_threshold"),
            baseline_capture_mfe_floor,
        ),
    )
    management_evidence_ready = bool(
        price_known
        and pnl_known
        and path_metrics_known
        and management_closed_bar_window_ready
    )
    profit_protection_window_ready = bool(
        management_evidence_ready and mfe >= capture_mfe_floor
    )
    thesis_break_evidence_families: list[str] = []
    if signal_reversal:
        thesis_break_evidence_families.append("signal_reversal")
    if regime_shift == "confirmed":
        thesis_break_evidence_families.append("regime_shift")
    if price_known and stop_loss_progress >= near_sl_progress_threshold:
        thesis_break_evidence_families.append("market_structure_risk")
    if path_metrics_known and (
        time_decay_score <= time_decay_reduce_threshold
        or timeout_ratio >= timeout_reduce_ratio
    ):
        thesis_break_evidence_families.append("time_decay")
    if thesis_broken_confirmations >= 2:
        thesis_break_evidence_families.append("persistent_price_path")
    thesis_break_evidence_families = list(dict.fromkeys(thesis_break_evidence_families))
    hard_risk_active = bool(
        risk.get("hard_risk_active")
        or risk.get("circuit_breaker_active")
        or risk.get("connection_risk_active")
        or (price_known and stop_loss_progress >= 1.0)
    )
    time_decay_window_ready = bool(
        management_evidence_ready
        or timeout_ratio >= 1.0
        or hard_risk_active
    )
    thesis_break_ready = (
        thesis_status in {"broken", "confirmed_broken"}
        and holding_seconds >= min_thesis_break_seconds
        and (closed_bar_window_ready or (hard_risk_bypass and hard_risk_active))
        and path_metrics_known
        and holding_efficiency <= broken_holding_efficiency_threshold
    )
    thesis_break_confirmed = (
        (hard_risk_bypass and hard_risk_active)
        or len(thesis_break_evidence_families) >= min_independent_evidence
    )

    posture_info = resolve_supervisor_posture(
        market=market,
        risk=risk,
        thesis_status=thesis_status,
        regime_shift=regime_shift,
        hard_risk_active=hard_risk_active,
        signal_reversal=signal_reversal,
        thesis_break_ready=thesis_break_ready,
        thesis_break_confirmed=thesis_break_confirmed,
        closed_bar_window_ready=closed_bar_window_ready,
    )
    supervisor_posture = str(posture_info.get("posture") or "unknown_observe")
    if supervisor_posture != "range_capture":
        # The existing thresholds remain valid, but they cannot authorize a
        # discretionary action outside the posture that owns that control.
        can_extend_tp = False

    near_stop_loss_strong = bool(
        price_known
        and pnl_known
        and stop_loss_progress >= near_sl_progress_threshold
        and current_pnl <= 0
        and (
            thesis_status == "broken"
            or holding_efficiency <= near_sl_efficiency_threshold
            or time_decay_score <= time_decay_reduce_threshold
            or regime_shift == "confirmed"
        )
    )

    if hard_risk_active:
        trigger_tags.append("hard_risk_active")
        action = "close"
        summary_reason = "hard_risk_active"
        severity = "error"
    elif timeout_exceeded:
        trigger_tags.append("holding_timeout_exceeded")
        action = "close"
        summary_reason = "holding_timeout_exceeded"
        severity = "warn"
    elif near_stop_loss_strong:
        trigger_tags.append("near_stop_loss")
        action = "close"
        summary_reason = "near_stop_loss_preemptive_exit"
        severity = "warn"
    elif supervisor_posture == "exit_commit":
        if thesis_break_ready and thesis_break_confirmed:
            trigger_tags.append("thesis_broken")
            action = "close"
            summary_reason = "thesis_broken"
            severity = "error"
        elif signal_reversal or regime_shift == "confirmed":
            trigger_tags.append("regime_shift_detected")
            action = "close"
            summary_reason = "regime_shift_detected"
            severity = "warn"
    elif supervisor_posture == "trend_hold":
        if (
            take_profit_progress >= near_tp_progress_threshold
            or giveback_ratio >= giveback_tighten_threshold
            or timeout_ratio >= timeout_tighten_ratio
            or thesis_status == "weakening"
        ):
            trigger_tags.append("trend_hold_preserve_profit")
            summary_reason = "trend_hold_preserve_profit"
            severity = "info"
    elif supervisor_posture in {"unknown_observe", "transition_confirming"}:
        trigger_tags.append(supervisor_posture)
        if supervisor_posture == "transition_confirming":
            summary_reason = "transition_confirming"
            severity = "info"
        if thesis_status in {"broken", "confirmed_broken"}:
            trigger_tags.append("thesis_broken_delayed")
            trigger_tags.append("thesis_broken_unconfirmed")
            summary_reason = "thesis_break_unconfirmed"
            severity = "warn"
    elif price_known and pnl_known and current_pnl > 0 and take_profit_progress >= near_tp_progress_threshold and near_tp_action == "protect":
        trigger_tags.append("near_take_profit")
        action = "tighten"
        summary_reason = "near_take_profit_protect"
        severity = "info"
    elif price_known and pnl_known and current_pnl > 0 and take_profit_progress >= near_tp_progress_threshold:
        trigger_tags.append("near_take_profit")
        action = "close"
        summary_reason = "near_take_profit_capture"
        severity = "info"
    elif (
        supervisor_posture == "range_capture"
        and
        path_metrics_known
        and pnl_known
        and giveback_ratio >= giveback_reduce_threshold
        and mfe > 0
        and profit_capture_ratio <= profit_capture_min_threshold
        and profit_protection_window_ready
    ):
        trigger_tags.append("profit_giveback_after_mfe")
        action = "reduce"
        summary_reason = "profit_giveback_after_mfe"
        severity = "warn"
    elif thesis_status in {"broken", "confirmed_broken"}:
        trigger_tags.append("thesis_broken_delayed")
        if not thesis_break_confirmed:
            trigger_tags.append("thesis_broken_unconfirmed")
        if not thesis_break_ready:
            trigger_tags.append("thesis_break_window_pending")
        action = "hold"
        summary_reason = (
            "thesis_break_unconfirmed"
            if not thesis_break_confirmed
            else "thesis_break_pending_window"
        )
        severity = "warn"
    elif supervisor_posture == "range_capture" and regime_shift == "confirmed" and pnl_known and current_pnl <= 0:
        trigger_tags.append("regime_shift_detected")
        action = "close"
        summary_reason = "regime_shift_detected"
        severity = "warn"
    elif supervisor_posture == "range_capture" and (
        path_metrics_known
        and pnl_known
        and time_decay_window_ready
        and (
            time_decay_score <= time_decay_reduce_threshold
            or (
                timeout_ratio >= timeout_reduce_ratio
                and holding_efficiency <= weakening_efficiency_threshold
            )
        )
    ):
        trigger_tags.append("time_decay_and_low_efficiency")
        action = "reduce" if current_pnl > 0 else "close"
        summary_reason = "time_decay_and_low_efficiency"
        severity = "warn"
    elif supervisor_posture == "range_capture" and (
        (
            profit_protection_window_ready
            and giveback_ratio >= giveback_tighten_threshold
        )
        or (management_evidence_ready and thesis_status == "weakening")
        or (time_decay_window_ready and timeout_ratio >= timeout_tighten_ratio)
    ):
        if profit_protection_window_ready and giveback_ratio >= giveback_tighten_threshold:
            trigger_tags.append("profit_giveback_after_mfe")
            summary_reason = "profit_giveback_after_mfe"
        elif timeout_ratio >= timeout_tighten_ratio:
            trigger_tags.append("time_decay_and_low_efficiency")
            summary_reason = "time_decay_and_low_efficiency"
        else:
            trigger_tags.append("thesis_weakening")
            summary_reason = "thesis_weakening"
        action = "tighten"
        severity = "warn"

    if (
        action == "hold"
        and summary_reason == "position_healthy"
        and not profit_protection_window_ready
        and (
            giveback_ratio >= giveback_tighten_threshold
            or thesis_status == "weakening"
            or timeout_ratio >= timeout_tighten_ratio
        )
    ):
        trigger_tags.append("profit_protection_window_pending")
        if giveback_ratio >= giveback_tighten_threshold:
            trigger_tags.append("profit_giveback_after_mfe_pending")
        summary_reason = "profit_protection_evidence_pending"
        severity = "info"

    confidence = 0.35
    if action == "hold":
        confidence = 0.55 + _clamp(holding_efficiency * 0.35 + profit_capture_ratio * 0.1, 0.0, 0.35)
    elif action == "tighten":
        confidence = 0.60 + _clamp(giveback_ratio * 0.2 + max(0.0, timeout_ratio - 0.5) * 0.2, 0.0, 0.25)
    elif action == "reduce":
        confidence = 0.70 + _clamp(giveback_ratio * 0.2 + (0.5 - time_decay_score) * 0.25, 0.0, 0.2)
    elif action == "close":
        confidence = 0.82 + _clamp((1.0 - time_decay_score) * 0.15 + (0.15 if thesis_status == "broken" else 0.0), 0.0, 0.15)
    confidence = round(_clamp(confidence), 4)

    recommended_controls = {
        "target_stop_loss": 0.0,
        "target_take_profit": current_tp,
        "reduce_fraction": 0.0,
        "close_reason": "",
        "protection_mode": "",
    }
    if action == "tighten":
        recommended_controls["target_stop_loss"] = _tightened_sl(
            direction=direction,
            entry_price=entry_price,
            current_price=current_price,
            current_sl=current_sl,
            profit_capture_ratio=profit_capture_ratio,
            sl_policy=sl_policy,
        )
        if can_extend_tp:
            recommended_controls["target_take_profit"] = _extended_tp(
                direction=direction,
                entry_price=entry_price,
                current_tp=current_tp,
                tp_policy=tp_policy,
            )
        recommended_controls["close_reason"] = "supervisor_tighten"
        recommended_controls["protection_mode"] = "dynamic_tpsl" if can_extend_tp else "tightened_stop"
    elif action == "reduce":
        recommended_controls["reduce_fraction"] = 0.5
        recommended_controls["close_reason"] = "supervisor_reduce"
        recommended_controls["protection_mode"] = "partial_de_risk"
        if current_pnl > 0:
            recommended_controls["target_stop_loss"] = _tightened_sl(
                direction=direction,
                entry_price=entry_price,
                current_price=current_price,
                current_sl=current_sl,
                profit_capture_ratio=max(profit_capture_ratio, 0.5),
                sl_policy=sl_policy,
            )
    elif action == "close":
        recommended_controls["close_reason"] = summary_reason
        recommended_controls["protection_mode"] = "full_exit"

    evidence = {
        "holding_seconds": round(holding_seconds, 6),
        "holding_timeout_ratio": round(max(0.0, timeout_ratio), 6),
        "mfe": round(mfe, 6),
        "mae": round(mae, 6),
        "giveback_ratio": round(giveback_ratio, 6),
        "profit_capture_ratio": round(profit_capture_ratio, 6),
        "time_in_profit": round(time_in_profit, 6),
        "holding_efficiency": round(holding_efficiency, 6),
        "time_decay_score": round(time_decay_score, 6),
        "thesis_status": thesis_status,
        "thesis_break_ready": bool(thesis_break_ready),
        "thesis_break_confirmed": bool(thesis_break_confirmed),
        "completed_bars_after_entry": int(completed_bars_after_entry),
        "closed_bar_key": closed_bar_key,
        "required_closed_bars": int(required_closed_bars),
        "closed_bar_window_ready": bool(closed_bar_window_ready),
        "management_required_closed_bars": int(management_required_closed_bars),
        "management_closed_bar_window_ready": bool(
            management_closed_bar_window_ready
        ),
        "capture_mfe_floor": round(capture_mfe_floor, 6),
        "mfe_is_meaningful": bool(mfe >= capture_mfe_floor),
        "management_evidence_ready": bool(management_evidence_ready),
        "profit_protection_window_ready": bool(profit_protection_window_ready),
        # Model influence is advisory and may only act after the same
        # meaningful-profit evidence window used by discretionary protection.
        "model_action_boundary_ready": bool(profit_protection_window_ready),
        "thesis_break_evidence_families": thesis_break_evidence_families,
        "min_independent_thesis_break_evidence": int(min_independent_evidence),
        "hard_risk_active": bool(hard_risk_active),
        "thesis_broken_confirmations": int(thesis_broken_confirmations),
        "signal_reversal": bool(signal_reversal),
        "regime_shift": regime_shift,
        "supervisor_posture": supervisor_posture,
        "supervisor_posture_reason": str(posture_info.get("reason") or ""),
        "market_context_state": str(market.get("market_context_state") or "unknown"),
        "market_regime_id": str(market.get("regime_id") or ""),
        "market_regime_confidence": round(_safe_float(market.get("regime_confidence")), 6),
        "market_regime_source": str(market.get("regime_source") or "unavailable"),
        "market_regime_dimensions": dict(market.get("regime_dimensions") or {}),
        "trend_strength_state": str(market.get("trend_strength_state") or "unknown"),
        "volatility_state": str(market.get("volatility_state") or "unknown"),
        "event_window_state": str(market.get("event_window_state") or "unknown"),
        "session_state": str(market.get("session_state") or "unknown"),
        "market_dimensions_known": bool(posture_info.get("market_dimensions_known")),
        "supervisor_state": supervisor_state,
        "current_pnl": round(current_pnl, 6),
        "current_price_component_state": price_component_state or "unknown",
        "pnl_component_state": pnl_component_state or "unknown",
        "position_path_metrics_state": path_metrics_state or "unknown",
        "volume": round(volume, 6),
        "distance_to_sl": _safe_float(market_space.get("distance_to_sl")),
        "distance_to_tp": _safe_float(market_space.get("distance_to_tp")),
        "market_space_context_state": str(market_space.get("state") or "unknown"),
        "atr_multiple_from_entry": market_space.get("atr_multiple_from_entry"),
        "range_location": market_space.get("range_location"),
        "structure_bias": market_space.get("structure_bias"),
        "take_profit_progress": round(take_profit_progress, 6),
        "stop_loss_progress": round(stop_loss_progress, 6),
        "current_stop_loss_progress": round(current_stop_loss_progress, 6),
        "original_stop_loss": round(original_sl, 6),
        "stop_loss_progress_source": (
            "original_entry_protection" if original_sl > 0 else "current_broker_stop"
        ),
        "entry_regime": str(entry_context.get("entry_regime") or risk.get("entry_regime") or ""),
        "current_regime": str(risk.get("current_regime") or market.get("regime_id") or ""),
        "trigger_tags": trigger_tags,
        "supervisor_template_id": str(template.get("template_id") or ""),
        "supervisor_template_version": str(template.get("template_version") or ""),
        "tp_extension_candidate": bool(can_extend_tp),
        "near_take_profit_action": near_tp_action,
    }
    min_delta = _safe_float(sl_policy.get("min_stop_tighten_points"), 0.01)
    protection_candidates: list[dict[str, Any]] = []
    target_sl = _safe_float(recommended_controls.get("target_stop_loss"))
    target_tp = _safe_float(recommended_controls.get("target_take_profit"))
    if action in {"tighten", "reduce"} and (
        _target_changed(current_sl, target_sl, direction=direction, target_kind="sl", min_delta=min_delta)
        or _target_changed(current_tp, target_tp, direction=direction, target_kind="tp", min_delta=min_delta)
    ):
        protection_candidates.append(
            {
                "schema_version": "position_supervisor_protection_candidate.v1",
                "source": "supervisor_dynamic_tpsl",
                "action": "dynamic_tpsl" if target_tp != current_tp and target_tp > 0 else "tighten_sl",
                "risk_action": "tighten_position",
                "priority": 30 if action == "tighten" else 35,
                "target_stop_loss": round(target_sl, 2) if target_sl > 0 else 0.0,
                "target_take_profit": round(target_tp, 2) if target_tp > 0 else 0.0,
                "current_stop_loss": round(current_sl, 2) if current_sl > 0 else 0.0,
                "current_take_profit": round(current_tp, 2) if current_tp > 0 else 0.0,
                "close_reason": recommended_controls.get("close_reason") or action,
                "protection_mode": recommended_controls.get("protection_mode") or "dynamic_tpsl",
                "reason": summary_reason,
                "confidence": confidence,
                "ttl_seconds": 90,
                "template_id": str(template.get("template_id") or ""),
                "template_version": str(template.get("template_version") or ""),
            }
        )
    human_summary = humanize_supervisor_reason(action, summary_reason, evidence)
    required_components: list[str] = []
    if action == "tighten":
        required_components.append("price")
    if summary_reason in {
        "near_take_profit_capture",
        "near_take_profit_protect",
        "near_stop_loss_preemptive_exit",
        "regime_shift_detected",
        "profit_giveback_after_mfe",
        "profit_protection_evidence_pending",
    }:
        required_components.append("pnl")
    required_components = list(dict.fromkeys(required_components))
    return {
        "position_id": position_id,
        "decision_ts": _safe_float(temporal.get("decision_ts")),
        "action": action,
        "recommended_action": action,
        "requested_action": action,
        "effective_action": action,
        "adaptive_execution_mode": adaptive_execution_mode(template),
        "confidence": confidence,
        "severity": severity,
        "thesis_status": thesis_status,
        "regime_shift": regime_shift,
        "summary_reason": summary_reason,
        "human_summary": human_summary,
        "evidence": evidence,
        "recommended_controls": recommended_controls,
        "protection_candidates": protection_candidates,
        "required_components": required_components,
        "supervisor_template": {
            "schema_version": str(template.get("schema_version") or ""),
            "template_id": str(template.get("template_id") or ""),
            "template_version": str(template.get("template_version") or ""),
            "template_role": str(template.get("template_role") or ""),
            "thresholds": thresholds,
            "sl_policy": sl_policy,
            "tp_policy": tp_policy,
            "capture_policy": capture_policy,
            "learning_bounds": template.get("learning_bounds") or {},
            "risk_boundary": template.get("risk_boundary") or {},
        },
        "requires_risk_verdict": action in {"tighten", "reduce", "close"},
        "action_label": {
            "hold": "继续持有",
            "tighten": "收紧保护",
            "reduce": "先降仓位",
            "close": "主动平仓",
        }.get(action, action),
        "position_side": _side_name(direction),
    }
