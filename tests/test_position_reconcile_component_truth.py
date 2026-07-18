from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.services.live_position_lifecycle import (
    apply_unrealized_pnl_fields,
    enrich_positions_with_lifecycle_metrics,
)
from backend.services.live_safety_planner import (
    SafetyPlan,
    SafetyPlannerRuntime,
    plan_live_safety_candidates,
)
from backend.services.live_loop_v2 import LiveSafetyCycleRuntime, run_live_safety_cycle
from backend.services.live_safety_plane import LiveSafetyPlane
from backend.services.position_supervisor import evaluate_position_supervisor
from execution import ctrader_bridge as ctrader_module
from execution.base import PositionInfo, PositionReconcileResult


def test_legacy_fresh_result_only_implies_identity_and_protection_authority():
    result = PositionReconcileResult(
        reconcile_id="r1",
        status="fresh",
        positions=(PositionInfo(position_id=7),),
        observed_at=100.0,
        generated_at=100.0,
    )

    assert result.authoritative is True
    assert result.components["identity"].state == "known"
    assert result.components["protection"].state == "known"
    assert result.components["price"].state == "unknown"
    assert result.components["pnl"].state == "unknown"
    with pytest.raises(TypeError):
        result.components["pnl"] = result.components["identity"]


def _connected_bridge():
    bridge = ctrader_module.CTraderBridge(
        send_orders=False,
        account_id=123,
        forced_symbol_id=41,
        execution_outcome_v2_enabled=False,
    )
    bridge._connected = True
    bridge._app_authed = True
    bridge._account_authed = True
    bridge._symbol_id = 41
    return bridge


def _proto_position(position_id: int = 17):
    return SimpleNamespace(
        positionId=position_id,
        tradeData=SimpleNamespace(
            symbolId=41,
            tradeSide=ctrader_module.TRADE_SIDE["BUY"],
            volume=100,
            openTimestamp=1_700_000_000_000,
        ),
        price=2400.0,
        stopLoss=2390.0,
        takeProfit=2420.0,
        commission=0.0,
        swap=0.0,
    )


@pytest.mark.skipif(
    not ctrader_module.HAS_CTRADER,
    reason="ctrader-open-api not installed",
)
def test_reconcile_keeps_identity_fresh_when_pnl_rpc_fails_and_uses_fresh_spot(
    monkeypatch,
):
    bridge = _connected_bridge()
    now = time.time()
    with bridge._spot_lock:
        bridge._spot_price = 2401.25
        bridge._spot_bid = 2401.2
        bridge._spot_ask = 2401.3
        bridge._spot_ts = now
    responses = iter(
        [
            SimpleNamespace(position=[_proto_position()]),
            TimeoutError("pnl timeout"),
        ]
    )

    def send(*_args, **_kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(bridge, "_send", send)
    monkeypatch.setattr(
        bridge,
        "_recompute_account_equity_from_cache",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unknown PnL must not recompute equity")
        ),
    )

    result = bridge.reconcile_positions(force=True, allow_cache_fallback=False)

    assert result.status == "fresh"
    assert result.authoritative is True
    assert result.components["identity"].state == "known"
    assert result.components["protection"].state == "known"
    assert result.components["price"].state == "known"
    assert result.components["pnl"].state == "error"
    assert result.components["pnl"].reason_code == "unrealized_pnl_rpc_failed"
    position = result.positions[0]
    assert position.entry_price == pytest.approx(2400.0)
    assert position.current_price == pytest.approx(2401.25)
    assert position.current_price_state == "known"
    assert position.current_price_source == "ctrader_spot"
    assert position.pnl == 0.0
    assert position.pnl_state == "error"
    assert position.pnl_reason_code == "unrealized_pnl_rpc_failed"


@pytest.mark.skipif(
    not ctrader_module.HAS_CTRADER,
    reason="ctrader-open-api not installed",
)
def test_zero_pnl_is_known_only_when_broker_response_contains_position(monkeypatch):
    bridge = _connected_bridge()
    responses = iter(
        [
            SimpleNamespace(position=[_proto_position()]),
            SimpleNamespace(
                moneyDigits=2,
                positionUnrealizedPnL=[
                    SimpleNamespace(positionId=17, netUnrealizedPnL=0)
                ],
            ),
        ]
    )
    monkeypatch.setattr(bridge, "_send", lambda *_args, **_kwargs: next(responses))

    result = bridge.reconcile_positions(force=True, allow_cache_fallback=False)

    assert result.status == "fresh"
    assert result.components["price"].state == "unknown"
    assert result.components["pnl"].state == "known"
    position = result.positions[0]
    assert position.current_price == 0.0
    assert position.current_price_state == "unknown"
    assert position.pnl == 0.0
    assert position.pnl_state == "known"


def test_unrealized_enrichment_preserves_explicit_unknown_and_known_zero():
    enriched = apply_unrealized_pnl_fields(
        [
            {
                "position_id": 1,
                "entry_price": 100.0,
                "current_price": 110.0,
                "direction": 1,
                "volume": 100.0,
                "pnl": 0.0,
                "pnl_state": "error",
                "pnl_reason_code": "rpc_failed",
            },
            {
                "position_id": 2,
                "entry_price": 100.0,
                "current_price": 110.0,
                "direction": 1,
                "volume": 100.0,
                "pnl": 0.0,
                "pnl_state": "known",
            },
        ],
        account={"balance": 1000.0, "equity": 1100.0},
    )

    assert enriched[0]["pnl"] is None
    assert enriched[0]["unrealized_pnl"] is None
    assert enriched[0]["pnl_state"] == "error"
    assert enriched[1]["pnl"] == 0.0
    assert enriched[1]["unrealized_pnl"] == 0.0
    assert enriched[1]["pnl_source"] == "broker"


def test_lifecycle_skips_path_metrics_when_pnl_component_is_unknown():
    path_calls: list[int] = []
    result = enrich_positions_with_lifecycle_metrics(
        [{"position_id": 1}],
        account={"balance": 1000.0, "equity": 1000.0},
        cfg=SimpleNamespace(),
        now_ts=100.0,
        persist=True,
        broker="ctrader",
        strategy_name="factor_v4",
        coerce_positions=lambda _rows: [
            {
                "position_id": 1,
                "pnl": 0.0,
                "pnl_state": "error",
                "pnl_reason_code": "rpc_failed",
            }
        ],
        apply_unrealized_pnl_fields_fn=apply_unrealized_pnl_fields,
        holding_summary_for_position=lambda *_args, **_kwargs: {
            "holding_seconds": 10.0
        },
        position_path_metrics_for_position=lambda position, **_kwargs: (
            path_calls.append(position["position_id"]) or {"mfe": 1.0}
        ),
        evaluate_position_supervisor_for_position=lambda *_args, **_kwargs: {
            "action": "hold"
        },
    )

    assert path_calls == []
    assert result[0]["pnl"] is None
    assert result[0]["position_path_metrics_state"] == "unknown"


def _component_planner_runtime():
    def timeout(position, *_args):
        return {
            "holding_seconds": 100.0 if position["position_id"] == 1 else 1.0,
            "max_holding_seconds": 50.0,
        }

    def supervisor(position, *_args):
        action = {
            3: "close",
            4: "reduce",
            5: "tighten",
        }.get(position["position_id"], "hold")
        return {
            "action": action,
            "recommended_controls": {
                "reduce_fraction": 0.5 if action == "reduce" else 0.0,
                "target_stop_loss": 99.0 if action == "tighten" else 0.0,
            },
        }

    return SafetyPlannerRuntime(
        build_timeout_context=timeout,
        load_entry_protection_plan=lambda pid: (
            {
                "schema_version": "entry_protection_plan.v1",
                "direction": 1,
                "target_stop_loss": 90.0,
                "target_take_profit": 120.0,
            }
            if pid == 2
            else {}
        ),
        evaluate_supervisor=supervisor,
        build_trailing_update=lambda position, *_args: {
            "candidate": {"controls": {"target_stop_loss": 101.0}}
        },
        trailing_state=lambda _pid: {},
        composite_conviction=lambda: 0.5,
    )


def test_safety_planner_blocks_only_metric_dependent_candidates():
    unknown = {"current_price_state": "unknown", "pnl_state": "error"}
    positions = [
        {"position_id": 1, **unknown},
        {"position_id": 2, "direction": 1, "sl": 0.0, "tp": 0.0, **unknown},
        {"position_id": 3, **unknown},
        {"position_id": 4, **unknown},
        {"position_id": 5, **unknown},
        {"position_id": 6, **unknown},
        {"position_id": 7, "current_price_state": "known", "pnl_state": "known"},
    ]

    plan = plan_live_safety_candidates(
        positions=positions,
        cfg=SimpleNamespace(),
        account={},
        current_price=110.0,
        atr_price=2.0,
        runtime=_component_planner_runtime(),
        planned_at=100.0,
    )

    assert [(item.action, item.position_id) for item in plan.candidates] == [
        ("timeout", 1),
        ("repair_entry_protection", 2),
        ("close", 3),
        ("reduce", 4),
        ("trailing", 7),
    ]
    blocked = [
        item
        for item in plan.arbitration
        if item.get("decision") == "blocked_component_unknown"
    ]
    assert any(item.get("position_id") == 5 and item.get("action") == "tighten" for item in blocked)
    assert any(item.get("position_id") == 6 and item.get("action") == "trailing" for item in blocked)


def test_safety_cycle_blocks_new_risk_but_still_runs_existing_protection():
    calls: list[str] = []

    class Bridge:
        def unresolved_execution_intent_count(self):
            return 0

    runtime = LiveSafetyCycleRuntime(
        get_safety_plane=lambda _owner: LiveSafetyPlane(
            mode="off", clock=time.time
        ),
        explicit_position_reconcile=lambda _bridge: {},
        publish_fresh_positions=lambda result, **_kwargs: list(result["positions"]),
        get_live_state=lambda key, default=None, **_kwargs: (
            {"balance": 1000.0} if key == "account" else default
        ),
        update_live_state=lambda **_payload: None,
        runtime_config=lambda: SimpleNamespace(),
        safety_reference_price=lambda _bridge, _positions: 100.0,
        factor_pipeline={"last_factor_values": {"atr_ratio": 0.01}},
        plan_safety_candidates=lambda **_kwargs: SafetyPlan(
            candidates=(), arbitration=(), planned_at=time.time()
        ),
        plan_legacy_candidates=lambda **_kwargs: SafetyPlan(
            candidates=(), arbitration=(), planned_at=time.time()
        ),
        execute_safety_candidate=lambda *_args, **_kwargs: {},
        run_position_protection_cycle=lambda *_args, **_kwargs: (
            calls.append("protection")
            or {"safety_candidates": [], "safety_arbitration": []}
        ),
        persist_safety_fail_closed=lambda **_kwargs: {},
        controller=SimpleNamespace(),
    )
    observed_at = time.time()
    payload = run_live_safety_cycle(
        bridge=Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=runtime,
        reconcile_result={
            "status": "fresh",
            "success": True,
            "reconcile_id": "r-component-error",
            "observed_at": observed_at,
            "positions": [
                {
                    "position_id": 7,
                    "current_price_state": "known",
                    "pnl_state": "error",
                }
            ],
            "price_component": {"state": "known", "source": "ctrader_spot"},
            "pnl_component": {
                "state": "error",
                "source": "ctrader_unrealized_pnl",
                "reason_code": "unrealized_pnl_rpc_failed",
            },
        },
    )

    assert calls == ["protection"]
    assert payload["accepting_new_risk"] is False
    assert "broker_position_pnl_unknown" in payload["blockers"]
    assert payload["position_components"]["pnl"]["state"] == "error"


def test_supervisor_does_not_interpret_unknown_pnl_as_non_positive():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": 7,
                "direction": 1,
                "entry_price": 100.0,
                "current_price": 95.0,
                "sl": 90.0,
                "tp": 120.0,
                "pnl": 0.0,
                "current_price_state": "known",
                "pnl_state": "error",
                "position_path_metrics_state": "unknown",
            },
            "risk": {
                "regime_shift": "confirmed",
                "thesis_status": "intact",
                "holding_efficiency": 1.0,
                "time_decay_score": 1.0,
                "giveback_ratio": 0.0,
            },
            "temporal_context": {"holding_seconds": 10.0},
        }
    )

    assert verdict["action"] == "hold"
    assert verdict["evidence"]["pnl_component_state"] == "error"
    assert verdict["evidence"]["position_path_metrics_state"] == "unknown"


def test_live_supervisor_does_not_score_model_with_unknown_components(monkeypatch):
    from backend.services import live_service
    from backend.services import model_influence

    position = {
        "position_id": 9,
        "current_price_state": "known",
        "pnl_state": "error",
        "position_path_metrics_state": "unknown",
    }
    monkeypatch.setattr(
        live_service,
        "_build_position_supervisor_context",
        lambda *_args, **_kwargs: {"position": dict(position), "risk": {}},
    )
    monkeypatch.setattr(
        live_service,
        "evaluate_position_supervisor",
        lambda _context: {
            "action": "hold",
            "confidence": 0.5,
            "evidence": {},
            "recommended_controls": {},
        },
    )

    class Advisor:
        def score_position_context(self, *_args, **_kwargs):
            raise AssertionError("model must not consume unknown components")

    class Influence:
        def fuse_position(self, *, verdict, advisory, **_kwargs):
            assert advisory["ok"] is False
            assert advisory["error"] == "position_component_unknown"
            return {
                **verdict,
                "model_influence": {
                    "applied": False,
                    "reason": "position_component_unknown",
                },
            }

    monkeypatch.setattr(live_service, "_POSITION_QUALITY_ADVISOR", Advisor())
    monkeypatch.setattr(
        model_influence,
        "shared_model_influence_service",
        lambda: Influence(),
    )

    verdict = live_service._evaluate_position_supervisor_for_position(
        position,
        cfg=SimpleNamespace(),
        positions=[position],
        persist=False,
    )

    advisory = verdict["evidence"]["position_quality_advisory"]
    assert advisory["error"] == "position_component_unknown"
    assert advisory["unavailable_components"] == ["path_metrics", "pnl"]
    assert verdict["model_influence"]["applied"] is False
