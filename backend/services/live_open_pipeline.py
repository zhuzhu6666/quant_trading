"""Open-trade pipeline: candidate preparation, context builders, risk adjudication wiring, intent/order submission, admission gates, and pending-open retry.

Extracted from backend.services.live_service (2026-09-12 structural repair).
Shared loop-owned state resolves lazily via _live_service().
"""
from __future__ import annotations

_ls_module = None


def _live_service():
    """Lazy handle to the live loop module (import-order-safe)."""
    global _ls_module
    if _ls_module is None:
        from backend.services import live_service as _module
        _ls_module = _module
    return _ls_module


from backend.services import live_close_settlement, live_open_processing
from backend.services.incident_controls import RuntimeIncidentControlService
from backend.services.live_open_admission import (
    evaluate_final_open_admission as _evaluate_final_open_admission,
    probe_postgres_authority as _probe_postgres_authority,
)
from backend.services.live_open_protection import (
    OpenProtectionRequest,
    OpenProtectionRuntime,
    attach_open_trade_protection as _runtime_attach_open_trade_protection,
)
from backend.services.live_open_submission import (
    OpenSubmissionRuntime,
    finalize_nursery_reservation as _runtime_finalize_nursery_reservation,
    submit_open_trade_candidate as _runtime_submit_open_trade_candidate,
)
from backend.services.live_position_lifecycle import (
    build_open_decision_replay_payload as _lifecycle_build_open_decision_replay_payload,
    validate_open_learning_context as _lifecycle_validate_open_learning_context,
)
from backend.services.live_reconciliation import (
    evaluate_reconciliation_snapshot as _evaluate_reconciliation_snapshot,
    verify_position_protection_projection as _verify_position_protection_projection,
)
from backend.services.live_safety_state import no_new_risk_latch_status
from backend.services.live_tick_pipeline import (
    build_effective_event_sizing_payload as _tick_build_effective_event_sizing_payload,
    build_factor_bar as _tick_build_factor_bar,
    build_market_order_block as _tick_build_market_order_block,
    build_open_order_preflight as _tick_build_open_order_preflight,
    build_order_failed_ledger_payloads as _tick_build_order_failed_ledger_payloads,
    build_skip_ledger_payload as _tick_build_skip_ledger_payload,
    guard_current_price_with_spot_quote as _tick_guard_current_price_with_spot_quote,
)
from risk.policy_service import INCIDENT_MODE_RANK
from typing import Any
import time
from backend.services import live_safety_watchdog
from backend.services import live_supervision_runtime


def _resolve_open_trade_bridge_meta(bridge: Any) -> dict[str, Any]:
    meta = getattr(bridge, "_symbol_meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    if not meta.get("api_min_volume") and bridge is not None and hasattr(bridge, "_resolve_symbol_id"):
        try:
            bridge._resolve_symbol_id()
            meta = getattr(bridge, "_symbol_meta", None) or {}
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            pass
    return dict(meta or {})


def _apply_context_position_sizing(
    *,
    volume: float,
    sizing_trace: dict[str, Any],
    composite: Any,
    bridge_meta: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    context_policy = dict(getattr(composite, "context_policy", {}) or {})
    input_volume = float(volume or 0.0)
    next_trace = dict(sizing_trace)
    try:
        context_mult = float(context_policy.get("position_multiplier", 1.0) or 1.0)
    except (TypeError, ValueError):
        context_mult = 1.0
    if not context_policy or abs(context_mult - 1.0) <= 1e-9:
        return input_volume, sizing_trace

    upstream_blocked_reason = str(next_trace.get("blocked_reason") or "")
    context_raw_volume = input_volume * context_mult
    min_volume = float((bridge_meta or {}).get("api_min_volume") or 1.0)
    blocked_reason = ""
    if input_volume <= 0:
        adjusted_volume = 0.0
        blocked_reason = upstream_blocked_reason or "non_positive_upstream_sizing"
    elif (
        context_mult < 1.0
        and context_raw_volume < min_volume
        and bool(
            next_trace.get("demo_exploration")
            or next_trace.get("demo_nursery_exploration")
        )
    ):
        adjusted_volume = input_volume
        next_trace["context_policy_demo_nursery_min_preserved"] = True
    elif context_mult < 1.0 and context_raw_volume < min_volume:
        adjusted_volume = 0.0
        blocked_reason = (
            f"context_sizing_below_min: {input_volume:.0f}*{context_mult:.2f}="
            f"{context_raw_volume:.0f}<{min_volume:.0f}"
        )
    else:
        if context_mult < 1.0:
            context_volume = _live_service()._round_api_volume_to_step(context_raw_volume, bridge_meta)
        else:
            context_volume = _live_service()._floor_api_volume_to_step(context_raw_volume, bridge_meta)
        adjusted_volume = context_volume if context_volume > 0 else input_volume
    next_trace["context_policy"] = {
        **context_policy,
        "raw_api_volume": context_raw_volume,
        "adjusted_api_volume": adjusted_volume,
        "blocked_reason": blocked_reason,
    }
    if blocked_reason:
        next_trace["blocked_reason"] = blocked_reason
        next_trace["context_policy_candidate_api_volume"] = input_volume
    return adjusted_volume, next_trace


def _prepare_open_trade_candidate(
    *,
    bridge: Any,
    pipeline: dict,
    cfg: Any,
    bar: dict[str, Any],
    factor_values: dict[str, Any],
    composite: Any,
    positions: list,
    current_price: float,
    atr_price: float,
    tick: int,
    log,
) -> _live_service()._OpenTradeCandidate:
    bridge_meta = _resolve_open_trade_bridge_meta(bridge)
    preflight = _tick_build_open_order_preflight(
        direction=int(composite.direction or 0),
        current_price=float(current_price or 0.0),
        atr_price=float(atr_price or 0.0),
        strategy_sl_atr=float(getattr(cfg, "strategy_sl_atr", 0.0) or 0.0),
        strategy_tp_atr=float(getattr(cfg, "strategy_tp_atr", 0.0) or 0.0),
        bridge_meta=bridge_meta,
        protection_prices=_live_service()._protection_prices_from_reference,
    )
    direction_name = str(preflight["direction_name"])
    sl_dist = float(preflight["sl_dist"])
    tp_dist = float(preflight["tp_dist"])
    digits = int(preflight["digits"])
    sl_price = float(preflight["sl_price"])
    tp_price = float(preflight["tp_price"])

    account_for_risk = _live_service().live_state_get("account", {}, clone=True) or {}
    sizing_result = _live_service()._risk_kelly_sizing(
        cfg,
        composite.direction,
        current_price,
        sl_price,
        bridge_meta,
        account_for_risk,
    )
    base_volume = float(sizing_result.get("volume") or 0.0)
    sizing_trace = dict(sizing_result.get("trace") or {})
    event_sizing_context = _live_service()._event_sizing_context(
        pipeline.get("event_sizing"),
        float(bar.get("time", time.time()) or time.time()),
    )
    try:
        event_multiplier = float(event_sizing_context.get("multiplier", 1.0))
    except (TypeError, ValueError):
        event_multiplier = 1.0
    event_sizing_result = _live_service()._apply_entry_event_sizing(
        base_volume=base_volume,
        event_multiplier=event_multiplier,
        bridge_meta=bridge_meta,
        sizing_trace=sizing_trace,
    )
    adjusted_volume = float(event_sizing_result.get("volume") or 0.0)
    sizing_trace = dict(event_sizing_result.get("trace") or {})
    sizing_block_reason = str(event_sizing_result.get("blocked_reason") or "")
    effective_sizing = _tick_build_effective_event_sizing_payload(
        base_volume=base_volume,
        adjusted_volume=adjusted_volume,
        sizing_trace=sizing_trace,
        sizing_block_reason=sizing_block_reason,
        event_sizing_context=event_sizing_context,
    )
    volume = float(effective_sizing["volume"])
    sizing_trace = dict(effective_sizing["sizing_trace"])
    event_sizing_context = dict(effective_sizing["event_sizing_context"])
    volume, sizing_trace = _apply_context_position_sizing(
        volume=volume,
        sizing_trace=sizing_trace,
        composite=composite,
        bridge_meta=bridge_meta,
    )

    log(
        f"tick {tick}: v4 {direction_name} req_api_volume={volume:.0f} "
        f"(Kelly enabled={getattr(cfg, 'kelly_enabled', False)} "
        f"event_mult={event_multiplier:.2f} base_api_volume={base_volume:.0f})"
    )

    event_filter_context = _live_service()._event_filter_context_for_risk_policy(
        cfg=cfg,
        direction=int(composite.direction or 0),
        bar=bar,
        factor_values=factor_values,
    )
    risk_context = _live_service()._build_open_trade_risk_context(
        cfg=cfg,
        bridge=bridge,
        acct=account_for_risk,
        positions=positions,
        requested_api_volume=volume,
        signal_score=float(composite.score or 0.0),
        symbol="XAUUSD",
        direction=int(composite.direction or 0),
        current_price=float(current_price or 0.0),
        atr_price=float(atr_price or 0.0),
        event_sizing_context=event_sizing_context,
        event_filter_context=event_filter_context,
        decision_quality_context=_live_service()._decision_quality_context(composite),
        decision_ts=float(bar.get("time", time.time()) or time.time()),
    )
    risk_verdict = _live_service()._RISK_POLICY.evaluate("open_trade", risk_context)
    market_session = _live_service().live_state_get("market_session", {}, clone=True) or {}
    order_block = _tick_build_market_order_block(
        market_session=market_session,
        risk_verdict=risk_verdict,
    )
    quote = _live_service().live_state_get("spot_quote", {}, clone=True) or {}
    bid = float(quote.get("bid") or 0.0)
    ask = float(quote.get("ask") or 0.0)
    spread_points = max(0.0, ask - bid) if bid > 0 and ask > 0 else 0.0
    try:
        from config import load_config

        settings = load_config()
    except Exception:
        settings = {}
    commission_per_lot = float(((settings.get("commission") or {}).get("value") or 0.0))
    slippage_points = float(((settings.get("execution") or {}).get("slippage_value") or 0.0))
    lots = max(0.0, volume) / 10000.0
    ounces = lots * float(bridge_meta.get("lot_size") or 0.0)
    uncertainty_points = max(0.0, atr_price) * float(
        getattr(cfg, "entry_edge_uncertainty_atr_ratio", 0.10) or 0.10
    )
    gross_edge = max(0.0, tp_dist) * ounces
    estimated_cost = (
        (spread_points + slippage_points + uncertainty_points) * ounces
        + commission_per_lot * lots
    )
    edge_evidence = {
        "schema_version": "entry_cost_edge.v1",
        "gross_edge": gross_edge,
        "estimated_cost": estimated_cost,
        "net_edge": gross_edge - estimated_cost,
        "spread_points": spread_points,
        "expected_slippage_points": slippage_points,
        "uncertainty_points": uncertainty_points,
        "commission_per_lot": commission_per_lot,
        "lots": lots,
        "lot_size": float(bridge_meta.get("lot_size") or 0.0),
    }
    audit_payload = dict(getattr(risk_verdict, "audit_payload", {}) or {})
    audit_payload["entry_cost_edge"] = edge_evidence
    risk_verdict.audit_payload = audit_payload
    if not bool(order_block.get("order_blocked")) and (ounces <= 0 or gross_edge <= estimated_cost):
        order_block = {
            **order_block,
            "order_blocked": True,
            "block_reason": "no_positive_edge_after_costs",
            "skip_stage": "entry_cost_edge",
        }
    position_supervisor_binding = live_supervision_runtime.select_position_supervisor_binding_for_open(
        cfg=cfg,
        composite=composite,
    )
    if not bool(order_block.get("order_blocked")):
        try:
            model_veto = _live_service()._evaluate_open_quality_model_veto(
                cfg=cfg,
                bridge=bridge,
                bar=bar,
                composite=composite,
                positions=positions,
                current_price=current_price,
                event_context=event_sizing_context,
                rule_decision={
                    "passed": True,
                    "risk_reason": str(getattr(risk_verdict, "reason", "") or ""),
                    "entry_cost_edge": edge_evidence,
                },
            )
        except Exception as exc:
            model_veto = {
                "passed": True,
                "reason": f"model_open_veto_unavailable:{type(exc).__name__}",
            }
        audit_payload = dict(getattr(risk_verdict, "audit_payload", {}) or {})
        audit_payload["model_open_quality"] = model_veto
        risk_verdict.audit_payload = audit_payload
        if not bool(model_veto.get("passed", True)):
            order_block = {
                **order_block,
                "order_blocked": True,
                "block_reason": "model_open_quality_veto",
                "skip_stage": "model_influence",
            }
    if not bool(order_block.get("order_blocked")):
        # Build the same canonical context that the filled-open ledger will
        # persist.  The broker fill is intentionally allowed to be unknown at
        # this point, but every other training input must already be present;
        # otherwise the order is rejected before broker mutation instead of
        # creating another permanently untrainable open sample.
        try:
            pre_open_context = _live_service()._open_learning_context_payload(
                bridge=bridge,
                bar=bar,
                positions_before=positions,
                composite=composite,
                symbol="XAUUSD+",
                pid=0,
                actual_api_volume=volume,
                requested_volume=volume,
                base_requested_volume=base_volume,
                current_price=current_price,
                fill_price=0.0,
                sl_price=sl_price,
                tp_price=tp_price,
                sl_dist=sl_dist,
                tp_dist=tp_dist,
                event_sizing_context=event_sizing_context,
                sizing_trace=sizing_trace,
                risk_verdict=risk_verdict,
                market_session=market_session,
                position_supervisor_binding=position_supervisor_binding,
            )
            context_quality = _lifecycle_validate_open_learning_context(
                pre_open_context,
                require_fill=False,
            )
        except Exception as exc:
            context_quality = {
                "schema_version": "open_learning_context.v2",
                "ready": False,
                "missing_fields": [],
                "invalid_fields": [f"capture_exception:{type(exc).__name__}"],
                "require_fill": False,
            }
        audit_payload = dict(getattr(risk_verdict, "audit_payload", {}) or {})
        audit_payload["open_learning_context_quality"] = context_quality
        risk_verdict.audit_payload = audit_payload
        if not bool(context_quality.get("ready")):
            order_block = {
                **order_block,
                "order_blocked": True,
                "block_reason": "open_learning_context_incomplete",
                "skip_stage": "learning_context",
                "learning_context_quality": context_quality,
            }
    nursery_reservation_id = ""
    audit_payload = dict(getattr(risk_verdict, "audit_payload", {}) or {})
    observations = list(audit_payload.get("demo_nursery_observations") or [])
    if observations and not bool(order_block.get("order_blocked")):
        try:
            from backend.services.nursery_exploration_budget import (
                NurseryExplorationBudgetService,
                build_setup_fingerprint,
            )

            decision_quality = _live_service()._decision_quality_context(composite)
            context_state = dict(decision_quality.get("context_state") or {})
            top_contributors = list(decision_quality.get("top_contributors") or [])
            setup_fingerprint = build_setup_fingerprint(
                symbol="XAUUSD+",
                direction=int(composite.direction or 0),
                regime=str(decision_quality.get("regime_id") or ""),
                session=str(context_state.get("session_state") or ""),
                event_state=str(context_state.get("event_window_state") or ""),
                signal_score=float(composite.score or 0.0),
                alpha_family=[str(item.get("factor") or "") for item in top_contributors],
            )
            reservation = NurseryExplorationBudgetService().reserve(
                reasons=[str(item.get("reason") or "") for item in observations],
                setup_fingerprint=setup_fingerprint,
                per_reason_limit=int(getattr(cfg, "nursery_exploration_per_reason_daily_limit", 5) or 5),
                global_limit=int(getattr(cfg, "nursery_exploration_global_daily_limit", 15) or 15),
                setup_limit=int(getattr(cfg, "nursery_exploration_setup_daily_limit", 1) or 1),
                ttl_seconds=int(getattr(cfg, "nursery_exploration_reservation_ttl_seconds", 300) or 300),
            )
            audit_payload["nursery_exploration_budget"] = reservation
            risk_verdict.audit_payload = audit_payload
            if reservation.get("allowed"):
                nursery_reservation_id = str(reservation.get("reservation_id") or "")
            else:
                order_block = {
                    **order_block,
                    "order_blocked": True,
                    "block_reason": str(reservation.get("status") or "nursery_exploration_budget_exhausted"),
                    "skip_stage": "nursery_exploration_budget",
                }
        except Exception as exc:
            order_block = {
                **order_block,
                "order_blocked": True,
                "block_reason": "nursery_exploration_budget_unavailable",
                "skip_stage": "nursery_exploration_budget",
            }
            _live_service().logger.warning("[live] nursery exploration reservation failed closed: {}", exc)

    return _live_service()._OpenTradeCandidate(
        direction_name=direction_name,
        bridge_meta=bridge_meta,
        digits=digits,
        sl_dist=sl_dist,
        tp_dist=tp_dist,
        sl_price=sl_price,
        tp_price=tp_price,
        base_volume=base_volume,
        volume=volume,
        event_multiplier=event_multiplier,
        event_sizing_context=event_sizing_context,
        sizing_trace=sizing_trace,
        risk_verdict=risk_verdict,
        market_session=market_session,
        order_block=order_block,
        nursery_reservation_id=nursery_reservation_id,
        position_supervisor_binding=position_supervisor_binding,
    )


def _blocked_open_trade_gate_result(block_reason: str):
    return type("GateResult", (), {
        "passed": False,
        "reason": block_reason,
    })()


def _maybe_tighten_incident_for_live_autonomy_budget_breach(risk_verdict: Any, *, tick: int, log) -> dict[str, Any]:
    verdict = risk_verdict.to_dict() if hasattr(risk_verdict, "to_dict") else dict(risk_verdict or {})
    payload = dict(verdict.get("audit_payload") or {})
    reason = str(verdict.get("reason") or "")
    if reason != "live_autonomy_budget_breach" and payload.get("source") != "live_autonomy_budget":
        return {"ok": True, "status": "not_budget_breach"}

    target_mode = str(payload.get("recommended_incident_mode") or "no_new_risk").strip().lower()
    if target_mode != "no_new_risk":
        target_mode = "no_new_risk"

    try:
        service = RuntimeIncidentControlService()
        current_mode = str((service.status() or {}).get("mode") or "normal").strip().lower()
        if INCIDENT_MODE_RANK.get(current_mode, 0) >= INCIDENT_MODE_RANK[target_mode]:
            return {"ok": True, "status": "already_strict", "current_mode": current_mode, "target_mode": target_mode}
        result = service.set_mode(
            target_mode,
            reason="live_autonomy_budget_breach",
            actor="system:live_autonomy_budget",
            confirm_thaw=False,
        )
        log(f"tick {tick}: live autonomy budget breach incident tighten -> {target_mode} ({result.get('status')})")
        return dict(result or {})
    except Exception as exc:
        _live_service().logger.warning("[live] live autonomy budget incident tighten failed: {}", exc)
        return {"ok": False, "status": "incident_tighten_failed", "error": str(exc)[:300]}


def _record_open_trade_blocked_by_policy(
    *,
    bridge: Any,
    cfg: Any,
    bar: dict,
    account: dict,
    positions: list,
    composite: Any,
    candidate: _live_service()._OpenTradeCandidate,
    current_price: float,
    tick: int,
    log,
):
    block_reason = str(candidate.order_block["block_reason"])
    log(f"tick {tick}: v4 {candidate.direction_name} SKIP ({block_reason})")
    _maybe_tighten_incident_for_live_autonomy_budget_breach(candidate.risk_verdict, tick=tick, log=log)
    gate_result = _blocked_open_trade_gate_result(block_reason)
    if not _live_service()._LEDGER:
        return gate_result
    try:
        learning_context = _live_service()._open_learning_context_payload(
            bridge=bridge,
            bar=bar,
            positions_before=positions,
            composite=composite,
            symbol="XAUUSD+",
            pid=0,
            actual_api_volume=0.0,
            requested_volume=float(candidate.volume or 0.0),
            base_requested_volume=float(candidate.base_volume or 0.0),
            current_price=float(current_price or 0.0),
            fill_price=0.0,
            sl_price=float(candidate.sl_price or 0.0),
            tp_price=float(candidate.tp_price or 0.0),
            sl_dist=float(candidate.sl_dist or 0.0),
            tp_dist=float(candidate.tp_dist or 0.0),
            event_sizing_context=candidate.event_sizing_context,
            sizing_trace=candidate.sizing_trace,
            risk_verdict=candidate.risk_verdict,
            market_session=candidate.market_session,
        )
        _live_service()._LEDGER.log_composite_decision(
            **_tick_build_skip_ledger_payload(
                composite=composite,
                gate_result=gate_result,
                cfg=cfg,
                bar=bar,
                account=account,
                positions_before=positions,
                risk_state=_live_service()._risk_state_with_verdict(candidate.risk_verdict),
                risk_verdict=candidate.risk_verdict,
                block_reason=block_reason,
                skip_stage=str(candidate.order_block["skip_stage"]),
                tick=tick,
                sizing_trace=candidate.sizing_trace,
                market_session=candidate.market_session,
                event_sizing_context=candidate.event_sizing_context,
                learning_context=learning_context,
                decision_ts_fallback=time.time(),
            )
        )
    except Exception as _ledger_err:
        _live_service().logger.debug("[live] ledger risk policy skip failed: {}", _ledger_err)
    return gate_result


def _record_open_trade_admission_blocked(
    *,
    cfg: Any,
    bar: dict[str, Any],
    account: dict[str, Any],
    positions: list,
    composite: Any,
    gate_result: Any,
    blockers: tuple[str, ...],
    block_reason: str,
    skip_stage: str,
    tick: int,
) -> None:
    """Persist a signal-pass admission stop before RiskPolicy is reached.

    This is deliberately a ledger-only observation.  It does not invoke
    RiskPolicy, create an execution intent, or turn the admission blocker into
    a risk verdict.  The same decision ledger remains the audit authority used
    by later risk/order/position records.
    """
    if not _live_service()._LEDGER:
        return
    action_json = {
        "bar_ts": (bar or {}).get("time"),
        "gate_passed": bool(getattr(gate_result, "passed", False)),
        "admission_gate_passed": False,
        "blockers": list(blockers),
        "action_reason": str(block_reason or "open_admission_blocked"),
        "execution_intent_created": False,
        **_lifecycle_build_open_decision_replay_payload(
            cfg=cfg,
            composite=composite,
            include_risk=False,
        ),
    }
    try:
        payload = _tick_build_skip_ledger_payload(
            composite=composite,
            gate_result=gate_result,
            cfg=cfg,
            bar=bar,
            account=account,
            positions_before=positions,
            risk_state=_live_service().live_state_get("risk", {}, clone=True) or {},
            risk_verdict=None,
            block_reason=block_reason,
            skip_stage=skip_stage,
            tick=tick,
            sizing_trace={},
            market_session=_live_service().live_state_get("market_session", {}, clone=True) or {},
            event_sizing_context={},
            learning_context={},
            decision_ts_fallback=time.time(),
        )
        payload["action_json"].update(action_json)
        decision_id = _live_service()._LEDGER.log_composite_decision(**payload)
        _live_service().logger.debug(
            "[live] open admission blocker audited decision_id={} stage={} blockers={}",
            decision_id,
            skip_stage,
            list(blockers),
        )
    except Exception as ledger_error:
        # Admission remains fail-closed when the audit sink is unavailable;
        # the ledger is observability, never permission to submit an order.
        _live_service().logger.debug(
            "[live] open admission blocker ledger persist failed: {}",
            ledger_error,
        )


def _serialize_open_risk_verdict(verdict: Any) -> dict[str, Any]:
    if verdict is None:
        return {}
    if hasattr(verdict, "to_dict"):
        try:
            payload = verdict.to_dict()
            return dict(payload) if isinstance(payload, dict) else {}
        except Exception:
            return {}
    if isinstance(verdict, dict):
        return dict(verdict)
    payload = getattr(verdict, "__dict__", {})
    return dict(payload) if isinstance(payload, dict) else {}


def open_runtime_factor_lineage() -> dict[str, Any]:
    try:
        from backend.services.runtime_factor_selection_projection import (
            RuntimeFactorSelectionProjectionService,
        )

        projection = RuntimeFactorSelectionProjectionService().latest(
            max_age_seconds=900.0
        )
    except Exception as exc:
        return {
            "schema_version": "runtime_factor_selection.v1",
            "status": "unavailable",
            "error": f"{type(exc).__name__}:{exc}",
            "live_generation_id": _live_service()._current_generation_id(),
        }
    status = str(projection.get("status") or "unknown")
    return {
        "schema_version": "runtime_factor_selection.v1",
        "status": status,
        "source": str(projection.get("source") or ""),
        "selection_fingerprint": str(
            projection.get("selection_fingerprint") or ""
        ),
        "config_version": int(projection.get("config_version") or 0),
        "config_hash": str(projection.get("config_hash") or ""),
        "live_generation_id": str(
            projection.get("live_generation_id") or _live_service()._current_generation_id()
        ),
        "published_at": float(projection.get("published_at") or 0.0),
        "age_seconds": float(projection.get("age_seconds") or 0.0),
    }


def _open_causal_timing(*, decision_ts: float, intent_prepared_at: float) -> dict[str, Any]:
    """Describe the known hot-path timestamps without inferring later success."""
    def known(ts: float) -> dict[str, Any]:
        return {"ts": float(ts or 0.0) or None, "status": "known", "reason": ""}

    def unknown(reason: str) -> dict[str, Any]:
        return {"ts": None, "status": "unknown", "reason": str(reason or "unknown")}

    return {
        "schema_version": "causal_timing.v1",
        "stages": {
            "decision": known(decision_ts),
            "intent_prepared": known(intent_prepared_at),
            "intent_submitted": unknown("broker_submission_pending"),
            "broker_ack": unknown("broker_ack_pending"),
            "order_position": unknown("broker_identity_pending"),
            "supervisor": unknown("position_supervision_pending"),
            "review": unknown("trade_review_pending"),
            "learning": unknown("learning_application_pending"),
            "effect": unknown("learning_effect_not_observed"),
        },
        "path": {
            "hot": [
                "factor_selection",
                "open_intent",
                "risk_policy",
                "broker_execution_intent",
                "protection",
                "supervision",
            ],
            "warm": ["trade_review", "feature_provider", "posterior", "candidate_review"],
            "cold": ["v16_command", "governance_coordinator", "mutation_effect"],
        },
    }


def _prepare_open_trade_intent(
    *,
    bridge: Any,
    broker: str,
    cfg: Any,
    bar: dict[str, Any],
    tick: int,
    account: dict[str, Any],
    positions: list[Any],
    composite: Any,
    gate_result: Any,
    candidate: _live_service()._OpenTradeCandidate,
    current_price: float,
    signal_decision_id: str = "",
) -> str:
    """Persist the root decision immediately before broker submission."""
    if not _live_service()._LEDGER:
        raise RuntimeError("canonical_risk_decision_unavailable")
    decision_ts = float((bar or {}).get("time") or time.time())
    intent_prepared_at = time.time()
    risk_payload = _serialize_open_risk_verdict(candidate.risk_verdict)
    audit_payload = dict(risk_payload.get("audit_payload") or {})
    runtime_binding = open_runtime_factor_lineage()
    factor_set_version = str(
        runtime_binding.get("selection_fingerprint") or ""
    )
    config_version = int(runtime_binding.get("config_version") or 0)
    action_json = {
        "schema_version": "open_intent.v1",
        "tick": int(tick or 0),
        "decision_ts": decision_ts,
        "signal_decision_id": str(signal_decision_id or ""),
        "runtime_binding": runtime_binding,
        "factor_set_version": factor_set_version,
        "config_version": config_version,
        "config_hash": str(runtime_binding.get("config_hash") or ""),
        "policy_version": str(getattr(cfg, "policy_version", "") or ""),
        "evidence_refs": [
            ref
            for ref in (
                f"signal_decision:{signal_decision_id}" if signal_decision_id else "",
                f"factor_selection:{factor_set_version}" if factor_set_version else "",
            )
            if ref
        ],
        "causal_timing": _open_causal_timing(
            decision_ts=decision_ts,
            intent_prepared_at=intent_prepared_at,
        ),
        "requested_direction": int(getattr(composite, "direction", 0) or 0),
        "current_price": float(current_price or 0.0),
        "risk_verdict": risk_payload,
        "sizing": {
            "base_volume": float(candidate.base_volume or 0.0),
            "requested_volume": float(candidate.volume or 0.0),
            "event_multiplier": float(candidate.event_multiplier or 0.0),
            "sizing_trace": dict(candidate.sizing_trace or {}),
        },
        "learning_context_quality": dict(
            audit_payload.get("open_learning_context_quality") or {}
        ),
        "execution_intent_created": False,
        **_lifecycle_build_open_decision_replay_payload(
            cfg=cfg,
            composite=composite,
            include_risk=True,
        ),
    }
    if candidate.position_supervisor_binding:
        action_json["position_supervisor_binding"] = dict(
            candidate.position_supervisor_binding
        )
        binding_hash = str(
            candidate.position_supervisor_binding.get("template_hash") or ""
        )
        if binding_hash:
            action_json["evidence_refs"].append(
                f"position_supervisor_selection:{binding_hash}"
            )
    return _live_service()._LEDGER.log_composite_decision(
        event_type="open_intent",
        composite=composite,
        gate_result=gate_result,
        symbol="XAUUSD+",
        timeframe=str(getattr(cfg, "timeframe", "") or ""),
        decision_ts=decision_ts,
        portfolio_state={
            "balance": (account or {}).get("balance", 0),
            "equity": (account or {}).get("equity", 0),
            "n_positions": len(positions or []),
            "session_pnl": _live_service().live_state_get("session_pnl", 0),
        },
        risk_state=_live_service()._risk_state_with_verdict(candidate.risk_verdict),
        policy_version=str(getattr(cfg, "policy_version", "") or ""),
        factor_set_version=factor_set_version,
        action_reason="open_intent",
        action_json={
            **action_json,
            "config_version": config_version,
            "config_hash": str(runtime_binding.get("config_hash") or ""),
        },
    )


def _submit_open_trade_order(
    bridge: Any,
    composite: Any,
    volume: float,
    *,
    decision_id: str,
    trade_id: str,
    risk_verdict: Any,
):
    risk_payload = _serialize_open_risk_verdict(risk_verdict)
    if composite.direction == 1:
        return bridge.market_buy(
            volume=volume,
            sl=0.0,
            tp=0.0,
            comment="quant-v4",
            decision_id=str(decision_id or ""),
            trade_id=str(trade_id or ""),
            risk_verdict=risk_payload,
        )
    if composite.direction == -1:
        return bridge.market_sell(
            volume=volume,
            sl=0.0,
            tp=0.0,
            comment="quant-v4",
            decision_id=str(decision_id or ""),
            trade_id=str(trade_id or ""),
            risk_verdict=risk_payload,
        )
    return None


def _persist_pending_entry_protection_plan(
    *,
    broker: str,
    position_id: int,
    composite: Any,
    fill_price: float,
    current_price: float,
    actual_api_volume: float,
    sl_price: float,
    tp_price: float,
    entry_protection_plan: dict[str, Any],
    tick: int,
    entry_decision_id: str = "",
    execution_intent_id: str = "",
) -> None:
    try:
        # Stamp the decision-time regime so position_path metrics can later
        # detect regime_shift (entry != current).  Without this snapshot the
        # supervisor's regime-shift evidence family is structurally dead.
        entry_regime = _live_service()._current_regime_hint()
        live_close_settlement.upsert_recovery_position_state(
            {
                "position_id": position_id,
                "symbol": "XAUUSD+",
                "direction": composite.direction,
                "open_price": float(fill_price or current_price),
                "volume": float(actual_api_volume),
                "entry_decision_id": str(
                    entry_decision_id or live_close_settlement.lookup_entry_decision_id(int(position_id))
                ),
            },
            broker=broker,
            strategy_name=_live_service()._current_loop_strategy_name(),
            status="open",
            meta={
                "tick": tick,
                "sl": round(sl_price, 2),
                "tp": round(tp_price, 2),
                "entry_protection_plan": entry_protection_plan,
                "entry_decision_id": str(entry_decision_id or ""),
                "execution_intent_id": str(execution_intent_id or ""),
                "entry_regime": str(entry_regime or ""),
            },
        )
    except Exception as _protection_plan_err:
        # 计划持久化失败 = 该仓位失去自动修复能力, 必须显性报错而非 debug 吞掉。
        _live_service().logger.error(
            "[live] entry protection plan persist failed for pos {}: {}",
            position_id,
            _protection_plan_err,
        )


def _attach_open_trade_protection(
    *,
    bridge: Any,
    attr_engine: Any,
    broker: str,
    cfg: Any,
    bar: dict,
    tick: int,
    position_id: int,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    sl_dist: float,
    tp_dist: float,
    account: dict,
    positions: list,
    composite: Any,
    gate_result: Any,
    candidate: _live_service()._OpenTradeCandidate,
    entry_protection_plan: dict[str, Any],
    log,
    submit_started_at: float | None = None,
    fill_received_at: float | None = None,
) -> None:
    _runtime_attach_open_trade_protection(
        OpenProtectionRequest(
            bridge=bridge,
            attr_engine=attr_engine,
            broker=broker,
            cfg=cfg,
            bar=bar,
            tick=tick,
            position_id=position_id,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            base_requested_volume=base_requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            sl_dist=sl_dist,
            tp_dist=tp_dist,
            account=account,
            positions=positions,
            composite=composite,
            gate_result=gate_result,
            candidate=candidate,
            entry_protection_plan=entry_protection_plan,
            log=log,
            submit_started_at=submit_started_at,
            fill_received_at=fill_received_at,
        ),
        runtime=OpenProtectionRuntime(
            amend_position=lambda *, bridge, position_id, sl, tp: (
                bridge.amend_position_sltp(
                    position_id=position_id,
                    sl=sl,
                    tp=tp,
                )
            ),
            reconcile_positions=_live_service()._explicit_position_reconcile,
            verify_projection=_verify_position_protection_projection,
            publish_projection=_live_service()._publish_fresh_position_reconcile,
            release_pending_latch=_live_service()._release_entry_protection_pending_latch,
            record_success=live_open_processing.record_amended_open_success_context_from_live,
            record_failure=live_open_processing.record_amend_failure_after_fill_from_live,
            record_aux_failure=live_close_settlement.record_risk_reduction_aux_failure,
        ),
    )


def _record_open_trade_order_failure(
    *,
    result: Any,
    cfg: Any,
    bar: dict,
    account: dict,
    positions: list,
    composite: Any,
    gate_result: Any,
    candidate: _live_service()._OpenTradeCandidate,
    current_price: float,
    tick: int,
    log,
    decision_id: str = "",
) -> None:
    log(
        f"tick {tick}: v4 {candidate.direction_name} ORDER FAILED: "
        f"{getattr(result, 'error_code', '?')} {getattr(result, 'comment', '')}"
    )
    if not _live_service()._LEDGER:
        return
    try:
        order_failed_payloads = _tick_build_order_failed_ledger_payloads(
            composite=composite,
            gate_result=gate_result,
            cfg=cfg,
            bar=bar,
            account=account,
            positions_before=positions,
            risk_state=_live_service().live_state_get("risk", {}, clone=True) or {},
            requested_volume=float(candidate.volume),
            current_price=float(current_price),
            sl_price=float(candidate.sl_price),
            tp_price=float(candidate.tp_price),
            tick=tick,
            error_code=str(getattr(result, "error_code", "") or ""),
            comment=str(getattr(result, "comment", "") or ""),
            decision_ts_fallback=time.time(),
        )
        failed_decision_id = str(decision_id or "")
        if not failed_decision_id:
            failed_decision_id = _live_service()._LEDGER.log_composite_decision(
                **order_failed_payloads["decision"]
            )
        order_event = dict(order_failed_payloads["order_event"])
        order_event["details"] = {
            **dict(order_event.get("details") or {}),
            "decision_id": failed_decision_id,
            "execution_intent_id": str(
                getattr(result, "intent_id", "") or ""
            ),
            "parent_decision_id": str(decision_id or ""),
        }
        _live_service()._LEDGER.log_order_event(
            decision_id=failed_decision_id,
            **order_event,
        )
    except Exception as _ledger_err:
        _live_service().logger.debug("[live] ledger order failed event failed: {}", _ledger_err)


def _handle_open_trade_order_success(
    *,
    result: Any,
    bridge: Any,
    attr_engine: Any,
    broker: str,
    cfg: Any,
    bar: dict,
    tick: int,
    account: dict,
    positions: list,
    composite: Any,
    gate_result: Any,
    candidate: _live_service()._OpenTradeCandidate,
    current_price: float,
    log,
    submit_started_at: float | None = None,
    fill_received_at: float | None = None,
    decision_id: str = "",
) -> None:
    fill_price = _live_service()._tick_resolve_order_fill_price(result, current_price=current_price)
    position_id = _live_service()._tick_resolve_order_position_id(result, positions_before=positions)
    try:
        candidate.open_decision_id = str(
            decision_id or getattr(candidate, "open_decision_id", "") or ""
        )
        candidate.execution_intent_id = str(
            getattr(result, "intent_id", "")
            or getattr(candidate, "execution_intent_id", "")
            or ""
        )
    except Exception:
        pass
    if position_id <= 0:
        failure = live_safety_watchdog.persist_safety_fail_closed(
            blockers=("confirmed_open_position_identity_missing",),
            source="entry_protection_initialization",
            error=(
                "broker reported a successful market-open without a uniquely "
                "matched position_id"
            ),
        )
        reconcile = _live_service()._explicit_position_reconcile(bridge)
        if bool(reconcile.get("success")):
            _live_service()._publish_fresh_position_reconcile(reconcile, broker=broker)
        try:
            _live_service().append_safety_outbox(
                event_type="confirmed_open_position_identity_missing",
                payload={
                    "broker": str(broker or ""),
                    "tick": int(tick),
                    "outcome": str(getattr(result, "outcome", "") or ""),
                    "intent_id": str(getattr(result, "intent_id", "") or ""),
                    "reconcile_id": str(reconcile.get("reconcile_id") or ""),
                    "reconcile_success": bool(reconcile.get("success")),
                    "safety_failure": failure,
                },
                error="confirmed_open_position_identity_missing",
            )
        except Exception:
            pass
        log(
            f"tick {tick}: v4 {candidate.direction_name} ORDER OUTCOME UNKNOWN "
            f"(confirmed response without position_id) vol={candidate.volume}"
        )
        return

    # Establish both the in-memory attach marker and durable no-new-risk cause
    # before any fallible broker refresh, price calculation, PG write, or audit.
    _live_service()._remember_pending_open_attach(int(position_id))
    _live_service()._activate_entry_protection_pending_latch(
        int(position_id),
        broker=broker,
        tick=tick,
    )
    refreshed_reconcile = bridge.reconcile_positions(
        getattr(bridge, "symbol", "") or "",
        force=True,
        allow_cache_fallback=False,
    )
    refreshed_positions = list(
        refreshed_reconcile.positions
        if str(getattr(refreshed_reconcile, "status", "")) == "fresh"
        else []
    )
    actual_api_volume = _live_service()._resolve_position_api_volume(
        position_id,
        refreshed_positions,
        candidate.volume,
    )
    protection_prices = _live_service()._tick_resolve_open_protection_prices(
        direction=int(composite.direction or 0),
        fill_price=float(fill_price or 0.0),
        current_price=float(current_price or 0.0),
        sl_dist=float(candidate.sl_dist or 0.0),
        tp_dist=float(candidate.tp_dist or 0.0),
        digits=int(candidate.digits or 2),
        position_id=int(position_id),
        refreshed_positions=refreshed_positions,
        position_open_price=_live_service()._position_open_price,
        protection_prices=_live_service()._protection_prices_from_reference,
    )
    protection_reference_price = float(
        protection_prices.get("reference_price") or 0.0
    )
    if protection_reference_price <= 0.0:
        raise RuntimeError("broker_entry_price_unavailable_for_protection")
    if fill_price <= 0.0:
        # A confirmed open without a result price can still use the fresh
        # broker position entry; never fall back to the signal/current mark.
        fill_price = protection_reference_price
    sl_price = float(protection_prices["sl_price"])
    tp_price = float(protection_prices["tp_price"])
    entry_protection_plan = _live_service()._entry_protection_plan_payload(
        position_id=int(position_id),
        direction=composite.direction,
        entry_price=float(fill_price or current_price),
        target_stop_loss=sl_price,
        target_take_profit=tp_price,
        requested_volume=candidate.volume,
        actual_api_volume=actual_api_volume,
        tick=tick,
        status="pending",
        supervisor_binding=(
            dict(getattr(candidate, "position_supervisor_binding", {}) or {})
            or None
        ),
    )
    _persist_pending_entry_protection_plan(
        broker=broker,
        position_id=int(position_id),
        composite=composite,
        fill_price=fill_price,
        current_price=current_price,
        actual_api_volume=actual_api_volume,
        sl_price=sl_price,
        tp_price=tp_price,
        entry_protection_plan=entry_protection_plan,
        entry_decision_id=str(decision_id or ""),
        execution_intent_id=str(
            getattr(result, "intent_id", "") or ""
        ),
        tick=tick,
    )
    _attach_open_trade_protection(
        bridge=bridge,
        attr_engine=attr_engine,
        broker=broker,
        cfg=cfg,
        bar=bar,
        tick=tick,
        position_id=int(position_id),
        actual_api_volume=actual_api_volume,
        requested_volume=candidate.volume,
        base_requested_volume=candidate.base_volume,
        fill_price=fill_price,
        current_price=current_price,
        sl_price=sl_price,
        tp_price=tp_price,
        sl_dist=candidate.sl_dist,
        tp_dist=candidate.tp_dist,
        account=account,
        positions=positions,
        composite=composite,
        gate_result=gate_result,
        candidate=candidate,
        entry_protection_plan=entry_protection_plan,
        log=log,
        submit_started_at=submit_started_at,
        fill_received_at=fill_received_at,
    )


def _open_submission_runtime(bridge: Any) -> OpenSubmissionRuntime:
    """Assemble live callbacks for the canonical open-submission service."""
    return OpenSubmissionRuntime(
        probe_final_admission=_probe_final_open_admission,
        admission_lock=_live_service()._OPEN_TRADE_ADMISSION_LOCK,
        open_trade_draining=_open_trade_draining,
        persist_safety_fail_closed=live_safety_watchdog.persist_safety_fail_closed,
        submit_order=_submit_open_trade_order,
        prepare_open_intent=_prepare_open_trade_intent,
        handle_order_success=_handle_open_trade_order_success,
        record_order_failure=_record_open_trade_order_failure,
        reconcile_positions=_live_service()._explicit_position_reconcile,
        publish_positions=_live_service()._publish_fresh_position_reconcile,
        append_safety_outbox=_live_service().append_safety_outbox,
        finalize_nursery_reservation=lambda reservation_id, consumed: (
            _runtime_finalize_nursery_reservation(
                reservation_id,
                consumed,
                warning=_live_service().logger.warning,
            )
        ),
        now=time.time,
    )


def _submit_open_trade_candidate(
    *,
    bridge: Any,
    attr_engine: Any,
    broker: str,
    cfg: Any,
    bar: dict,
    tick: int,
    account: dict,
    positions: list,
    composite: Any,
    gate_result: Any,
    candidate: _live_service()._OpenTradeCandidate,
    current_price: float,
    log,
    signal_decision_id: str = "",
    stop_requested=None,
) -> bool:
    return _runtime_submit_open_trade_candidate(
        bridge=bridge,
        attr_engine=attr_engine,
        broker=broker,
        cfg=cfg,
        bar=bar,
        tick=tick,
        account=account,
        positions=positions,
        composite=composite,
        gate_result=gate_result,
        candidate=candidate,
        current_price=current_price,
        log=log,
        signal_decision_id=signal_decision_id,
        stop_requested=stop_requested,
        runtime=_open_submission_runtime(bridge),
    )


def _probe_final_open_admission(
    *,
    bridge: Any,
    candidate: _live_service()._OpenTradeCandidate,
) -> dict[str, Any]:
    """Collect fresh open-only facts before broker-mutation ownership.

    PostgreSQL probing intentionally happens outside ``_OPEN_TRADE_ADMISSION_LOCK``
    so a database outage cannot delay emergency close/reduce/tighten ownership.
    The lock rechecks draining and the durable latch before using this result.
    """

    postgres = _probe_postgres_authority(_live_service()._get_final_open_probe_conn)
    try:
        quote = bridge.get_spot_quote() if hasattr(bridge, "get_spot_quote") else {}
        quote = dict(quote or {})
    except Exception as exc:
        quote = {
            "error": f"{type(exc).__name__}:{exc}"[:500],
        }
    result = _evaluate_final_open_admission(
        postgres=postgres,
        market_session=getattr(candidate, "market_session", None),
        spot_quote=quote,
    ).to_dict()
    # Keep the exact pre-submit quote available to post-fill context capture;
    # a transient bridge read failure after the broker accepts the order must
    # not erase the execution inputs already observed at the open boundary.
    if quote:
        _live_service().live_state_update(spot_quote=quote)
    _live_service().live_state_update(final_open_admission=result)
    return result


def new_risk_reconciliation_blockers(*, now_ts: float | None = None) -> list[str]:
    """Validate broker facts at the final open-order admission boundary."""

    checked_at = float(time.time() if now_ts is None else now_ts)
    account = _live_service().live_state_get("account_reconciled", {}, clone=True) or {}
    positions = _live_service().live_state_get("positions_reconciled", None, clone=True)
    result = _evaluate_reconciliation_snapshot(
        account=account,
        account_updated_at=_live_service().live_state_get("account_updated_at", 0.0),
        account_reconcile_id=_live_service().live_state_get("account_reconcile_id", ""),
        account_reconcile_failed_at=_live_service().live_state_get(
            "account_reconcile_failed_at", 0.0
        ),
        positions=positions,
        positions_updated_at=_live_service().live_state_get("positions_updated_at", 0.0),
        positions_reconcile_id=_live_service().live_state_get("positions_reconcile_id", ""),
        positions_reconcile_failed_at=_live_service().live_state_get(
            "positions_reconcile_failed_at", 0.0
        ),
        checked_at=checked_at,
    )
    return list(result["blockers"])


def _bar_open_already_recorded(bar_ts: float) -> bool:
    """True if the durable ledger already has a successful open for this bar.

    Same-bar dedup guard: stale decision bar repair replays and pending open
    retry re-entry must not open a second position for a bar that already
    filled.  Fail-open by design (this is a duplicate guard, not a risk
    gate): an unreadable ledger logs and allows the open rather than
    silently blocking a legitimate signal.
    """

    if not bar_ts or bar_ts <= 0:
        return False
    conn = live_close_settlement.get_state_read_conn()
    try:
        # Bounded window around the bar time (reverse keyset); canonical
        # open decisions carry decision_ts == bar_ts for the same bar.
        window = 5.0
        for candidate in _live_service().iter_decision_rows(
            conn,
            min_observed_epoch=float(bar_ts) - window,
            max_observed_epoch=float(bar_ts) + window,
            reverse=True,
        ):
            if str(candidate.get("event_type") or "") == "open":
                return True
        return False
    except Exception as exc:
        _live_service().logger.warning("[live] bar open dedup check failed: {}", exc)
        return False
    finally:
        conn.close()


def _open_trade_admission_blockers(stop_requested=None) -> tuple[str, ...]:
    blockers: list[str] = []
    if _live_service()._process_shutdown_requested:
        blockers.append("process_shutdown_requested")
    # The durable latch is checked again while the admission lock is held by
    # _submit_open_trade_candidate.  This linearizes emergency activation with
    # the broker open RPC and fails closed if the latch ledger is unreadable.
    if _live_service().no_new_risk_latched(fail_closed=True):
        blockers.append("no_new_risk_latched")
    if not _live_service()._LIVE_LOOP_CONTROLLER.accepting_new_risk(_live_service()._current_generation_id()):
        blockers.append("generation_not_accepting_new_risk")
    if bool(_live_service().live_state_get("loop_running", False)):
        # The controller is the only lifecycle/admission authority.  The
        # shared value is a projection for APIs and WebSocket consumers, never
        # an independent gate that can diverge from the generation.
        if str(
            _live_service().live_state_get("session_state_status", "unknown") or "unknown"
        ) != "available":
            blockers.append("session_state_unavailable")
        if bool(_live_service().live_state_get("circuit_breaker", False)):
            blockers.append("session_circuit_breaker")
        reconcile_blockers = new_risk_reconciliation_blockers()
        _live_service().live_state_update(new_risk_reconcile_blockers=reconcile_blockers)
        blockers.extend(reconcile_blockers)
    if bool(stop_requested is not None and stop_requested()):
        blockers.append("loop_stop_requested")
    return tuple(dict.fromkeys(blockers))


def _open_trade_draining(stop_requested=None) -> bool:
    return bool(_open_trade_admission_blockers(stop_requested))


def _open_admission_gate_reason(blockers: tuple[str, ...]) -> str:
    draining = {
        "process_shutdown_requested",
        "generation_not_accepting_new_risk",
        "loop_stop_requested",
    }
    if any(blocker in draining for blocker in blockers):
        return "loop_draining"
    for blocker in blockers:
        if blocker != "accepting_new_risk_false":
            return blocker
    return blockers[0] if blockers else "open_admission_blocked"


def _watchdog_freshness_retry_eligible(
    blockers: tuple[str, ...],
    *,
    latch_status: dict[str, Any] | None = None,
) -> bool:
    """Allow one same-bar retry only for the watchdog's stale fact snapshots."""

    if set(blockers) - {"no_new_risk_latched", "accepting_new_risk_false"}:
        return False
    unknown_raw = live_safety_watchdog.live_safety_watchdog_probe().get("unknown_execution_count")
    try:
        if unknown_raw is None or int(unknown_raw) != 0:
            return False
    except (TypeError, ValueError):
        return False
    latch = (
        dict(latch_status)
        if latch_status is not None
        else no_new_risk_latch_status(fail_closed=True)
    )
    causes = list(latch.get("causes") or [])
    if not bool(latch.get("active")) or len(causes) != 1:
        return False
    cause = causes[0] if isinstance(causes[0], dict) else {}
    if (
        str(cause.get("cause") or "") != "safety_freshness"
        or str(cause.get("cause_id") or "") != "safety_watchdog"
    ):
        return False
    freshness_blockers = set(
        (cause.get("metadata") or {}).get("blockers") or []
    )
    allowed = {
        "safety_freshness_stale",
        "safety_freshness_unknown",
        "account_freshness_stale",
        "account_freshness_unknown",
        "positions_freshness_stale",
        "positions_freshness_unknown",
    }
    return bool(freshness_blockers) and freshness_blockers <= allowed


def _open_admission_gate_result(
    *,
    tick: int,
    stage: str,
    blockers: tuple[str, ...],
    log,
):
    reason = _open_admission_gate_reason(blockers)
    log(
        f"tick {tick}: v4 open SKIP "
        f"({reason} stage={stage} blockers={list(blockers)})"
    )
    result = _blocked_open_trade_gate_result(reason)
    result.retryable_watchdog_freshness = (
        reason == "no_new_risk_latched"
        and _watchdog_freshness_retry_eligible(blockers)
    )
    return result


def run_open_trade_pipeline(
    *,
    bridge: Any,
    pipeline: dict,
    broker: str,
    cfg: Any,
    bar: dict[str, Any],
    factor_values: dict[str, Any],
    composite: Any,
    gate_result: Any,
    account: dict,
    positions: list,
    attr_engine: Any,
    current_price: float,
    atr_price: float,
    pending_open_attach_ids: list[int],
    send: bool,
    tick: int,
    log,
    signal_decision_id: str = "",
    stop_requested=None,
):
    if not (composite.direction != 0 and gate_result.passed and send):
        return gate_result
    admission_blockers = _open_trade_admission_blockers(stop_requested)
    # ★ same-bar dedup: a bar that already filled must not open a second
    # position (stale decision bar repair replay / pending open retry re-entry).
    # Distinct bars may still open concurrently; this only blocks the same bar.
    if _bar_open_already_recorded(float(bar.get("time") or 0.0)):
        admission_blockers = list(admission_blockers) + ["bar_already_opened"]
    if admission_blockers:
        admission_result = _open_admission_gate_result(
            tick=tick,
            stage="before_candidate",
            blockers=admission_blockers,
            log=log,
        )
        _record_open_trade_admission_blocked(
            cfg=cfg,
            bar=bar,
            account=account,
            positions=positions,
            composite=composite,
            gate_result=gate_result,
            blockers=admission_blockers,
            block_reason=_open_admission_gate_reason(admission_blockers),
            skip_stage="before_candidate",
            tick=tick,
        )
        return admission_result
    if pending_open_attach_ids:
        log(
            f"tick {tick}: v4 open SKIP (pending_open_attach "
            f"positions={pending_open_attach_ids})"
        )
        return gate_result

    candidate = _prepare_open_trade_candidate(
        bridge=bridge,
        pipeline=pipeline,
        cfg=cfg,
        bar=bar,
        factor_values=factor_values,
        composite=composite,
        positions=positions,
        current_price=current_price,
        atr_price=atr_price,
        tick=tick,
        log=log,
    )
    if bool(candidate.order_block["order_blocked"]):
        return _record_open_trade_blocked_by_policy(
            bridge=bridge,
            cfg=cfg,
            bar=bar,
            account=account,
            positions=positions,
            composite=composite,
            candidate=candidate,
            current_price=current_price,
            tick=tick,
            log=log,
        )

    admitted = _submit_open_trade_candidate(
        bridge=bridge,
        attr_engine=attr_engine,
        broker=broker,
        cfg=cfg,
        bar=bar,
        tick=tick,
        account=account,
        positions=positions,
        composite=composite,
        gate_result=gate_result,
        candidate=candidate,
        current_price=current_price,
        log=log,
        signal_decision_id=signal_decision_id,
        stop_requested=stop_requested,
    )
    if not admitted:
        return _blocked_open_trade_gate_result("loop_draining")
    return gate_result


def remember_or_clear_pending_open_retry(
    *,
    pipeline: dict,
    bar: dict[str, Any],
    factor_values: dict[str, Any],
    composite: Any,
    signal_gate_result: Any,
    open_result: Any,
) -> None:
    if bool(getattr(open_result, "retryable_watchdog_freshness", False)):
        pipeline["pending_open_retry"] = {
            "bar": dict(bar),
            "factor_values": dict(factor_values),
            "composite": composite,
            "gate_result": signal_gate_result,
            "signal_decision_id": str(pipeline.get("last_signal_decision_id") or ""),
        }
        return
    pipeline.pop("pending_open_retry", None)


def retry_pending_open_trade(
    *,
    bridge: Any,
    frame: Any,
    last_bar: Any,
    broker: str,
    tick: int,
    log,
    stop_requested=None,
) -> None:
    """Retry a same-bar signal after the canonical watchdog latch recovers."""

    pipeline = _live_service()._factor_pipeline
    if pipeline is None:
        return
    pending = pipeline.get("pending_open_retry")
    if not isinstance(pending, dict):
        return

    bar = dict(pending.get("bar") or {})
    current_bar = _tick_build_factor_bar(
        last_bar,
        frame,
        str(bar.get("timeframe") or "M5"),
    )
    if float(current_bar.get("time") or 0.0) != float(bar.get("time") or 0.0):
        pipeline.pop("pending_open_retry", None)
        log(f"tick {tick}: discarded stale pending open retry")
        return

    acct = _live_service().live_state_get("account", {}, clone=True) or {}
    positions_payload = _live_service().live_state_get("positions", [], clone=True) or []
    positions_probe = (
        (positions_payload.get("positions", []) or [])
        if isinstance(positions_payload, dict)
        else positions_payload
    )
    if positions_probe and not isinstance(positions_probe[0], dict):
        from backend.ws.endpoints import position_to_dict
    else:
        position_to_dict = None
    positions = _live_service()._tick_normalize_live_positions_payload(
        positions_payload,
        position_to_dict=position_to_dict,
    )
    current_price = float(last_bar["close"])
    if bridge is not None and hasattr(bridge, "get_spot_quote"):
        price_guard = _tick_guard_current_price_with_spot_quote(
            current_price=current_price,
            get_spot_quote=bridge.get_spot_quote,
            quote_is_fresh=_live_service()._quote_is_fresh,
        )
        current_price = float(price_guard["current_price"])

    factor_values = dict(pending.get("factor_values") or {})
    atr_ratio = factor_values.get("atr_ratio", 0)
    atr_price = (
        float(atr_ratio) * current_price
        if atr_ratio and float(atr_ratio) > 0
        else 0.0
    )
    current_pids = _live_service()._tick_collect_position_ids(positions)
    try:
        from config.runtime_config import shared as _runtime_config

        cfg = _runtime_config()
    except Exception:
        cfg = None
    result = run_open_trade_pipeline(
        bridge=bridge,
        pipeline=pipeline,
        broker=broker,
        cfg=cfg,
        bar=bar,
        factor_values=factor_values,
        composite=pending["composite"],
        gate_result=pending["gate_result"],
        account=acct,
        positions=positions,
        attr_engine=pipeline.get("attribution"),
        current_price=current_price,
        atr_price=atr_price,
        pending_open_attach_ids=_live_service()._active_pending_open_attach_ids(current_pids),
        send=_live_service()._should_send_orders(broker),
        tick=tick,
        log=log,
        signal_decision_id=str(pending.get("signal_decision_id") or ""),
        stop_requested=stop_requested,
    )
    if bool(getattr(result, "retryable_watchdog_freshness", False)):
        return
    pipeline.pop("pending_open_retry", None)
    log(f"tick {tick}: completed pending open retry for bar={bar.get('time')}")
