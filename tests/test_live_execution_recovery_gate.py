from types import SimpleNamespace
import time

from backend.services import live_service


def _flags(*, execution=True, generation=False, safety="off"):
    return SimpleNamespace(
        ctrader_execution_outcome_v2_enabled=execution,
        live_generation_controller_v2_enabled=generation,
        live_safety_plane_v2_mode=safety,
    )


class _RecoveryBridge:
    is_connected = True

    def __init__(self, *, recovery_status):
        self.recovery_status = dict(recovery_status)
        self.recovered = False
        self.calls: list[str] = []

    def reconcile_positions(self, *, force=True, allow_cache_fallback=False):
        self.calls.append("positions")
        now = time.time()
        positions = (
            (
                {
                    "position_id": 901,
                    "symbol": "XAUUSD+",
                    "direction": 1,
                    "volume": 100.0,
                    "entry_price": 2400.0,
                    "current_price": 2401.0,
                },
            )
            if self.recovered
            and any(
                str(item.get("outcome") or "") == "confirmed"
                for item in self.recovery_status.get("recovered", ())
            )
            else ()
        )
        return SimpleNamespace(
            status="fresh",
            success=True,
            fresh=True,
            authoritative=True,
            reconcile_id=f"positions-{len(self.calls)}",
            positions=positions,
            observed_at=now,
            generated_at=now,
        )

    def reconcile_account(self, *, force=True, allow_cache_fallback=False):
        self.calls.append("account")
        now = time.time()
        return SimpleNamespace(
            status="fresh",
            reconcile_id="account-1",
            account={"balance": 1000.0, "equity": 1000.0},
            observed_at=now,
            generated_at=now,
        )

    def unresolved_execution_intent_count(self):
        return 0 if self.recovered else 1

    def recover_execution_intents(self):
        self.calls.append("execution_recovery")
        self.recovered = True
        return dict(self.recovery_status)


def _install_tick_boundary(monkeypatch, bridge, order):
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))

    def safety_cycle(*, reconcile_result, **_kwargs):
        positions = list(getattr(reconcile_result, "positions", ()) or ())
        ids = [int(item["position_id"]) for item in positions]
        unknown = bridge.unresolved_execution_intent_count()
        order.append(("safety", ids, unknown))
        return {
            "status": "completed",
            "accepting_new_risk": unknown == 0,
            "reconciliation_state": "fresh",
            "reconcile_id": str(getattr(reconcile_result, "reconcile_id", "")),
            "position_ids": ids,
            "unknown_execution_count": unknown,
            "heartbeat_at": time.time(),
            "blockers": ["unknown_execution"] if unknown else [],
        }

    monkeypatch.setattr(live_service, "_run_live_safety_cycle", safety_cycle)
    monkeypatch.setattr(
        live_service,
        "_market_session_snapshot",
        lambda *_args, **_kwargs: order.append(("session",))
        or {"status": "open_confirmed"},
    )
    monkeypatch.setattr(
        live_service,
        "_warmup_from_local_db",
        lambda *_args, **_kwargs: order.append(("bars",)) or None,
    )
    today = live_service.datetime.now(live_service.timezone.utc).strftime("%Y-%m-%d")
    live_service._live_state_update(
        trade_date=today,
        session_state_status="available",
        circuit_breaker=False,
        execution_recovery={"enabled": True, "ready": False, "unresolved_count": None},
    )


def test_execution_outcome_flag_alone_selects_safety_first_loop(monkeypatch):
    monkeypatch.setattr(live_service, "_phase2_feature_flags", lambda: _flags())

    assert live_service._generation_controller_enabled() is False
    assert live_service._phase2_v2_active() is True


def test_loop_recovers_delayed_fill_and_runs_safety_before_alpha(monkeypatch):
    monkeypatch.setattr(live_service, "_phase2_feature_flags", lambda: _flags())
    bridge = _RecoveryBridge(
        recovery_status={
            "ready": True,
            "unresolved_count": 0,
            "recovered": [{"intent_id": "open-1", "outcome": "confirmed"}],
        }
    )
    order: list[tuple] = []
    _install_tick_boundary(monkeypatch, bridge, order)

    result = live_service._run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=1,
        recovery_bootstrapped=False,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert bridge.calls == ["positions", "account", "execution_recovery", "positions"]
    assert order[:2] == [("safety", [], 1), ("safety", [901], 0)]
    assert order[2:] == [("session",), ("bars",)]
    assert result["safety"]["position_ids"] == [901]
    assert result["safety"]["unknown_execution_count"] == 0
    assert result["wait_seconds"] == 5.0


def test_unresolved_recovery_blocks_session_and_alpha(monkeypatch):
    monkeypatch.setattr(live_service, "_phase2_feature_flags", lambda: _flags())
    bridge = _RecoveryBridge(
        recovery_status={
            "ready": False,
            "unresolved_count": 1,
            "recovered": [{"intent_id": "open-1", "outcome": "unknown"}],
        }
    )
    order: list[tuple] = []
    _install_tick_boundary(monkeypatch, bridge, order)

    result = live_service._run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=2,
        recovery_bootstrapped=False,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert "execution_recovery" in bridge.calls
    assert ("session",) not in order
    assert ("bars",) not in order
    assert result["wait_seconds"] == 5.0
    assert result["safety"]["accepting_new_risk"] is False
    assert "unknown_execution_unresolved" in result["safety"]["blockers"]


def test_missing_recovery_contract_fails_closed_before_session(monkeypatch):
    monkeypatch.setattr(live_service, "_phase2_feature_flags", lambda: _flags())
    now = time.time()
    bridge = SimpleNamespace(is_connected=True)
    reconcile = SimpleNamespace(
        status="fresh",
        success=True,
        fresh=True,
        authoritative=True,
        reconcile_id="positions-1",
        positions=(),
        observed_at=now,
        generated_at=now,
    )
    account = SimpleNamespace(
        status="fresh",
        reconcile_id="account-1",
        account={"balance": 1000.0, "equity": 1000.0},
        observed_at=now,
        generated_at=now,
    )
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))
    monkeypatch.setattr(live_service, "_explicit_position_reconcile", lambda _bridge: reconcile)
    monkeypatch.setattr(live_service, "_explicit_account_reconcile", lambda _bridge: account)
    monkeypatch.setattr(
        live_service,
        "_run_live_safety_cycle",
        lambda **_kwargs: {
            "accepting_new_risk": False,
            "reconciliation_state": "fresh",
            "position_ids": [],
            "unknown_execution_count": 1,
            "blockers": ["unknown_execution"],
        },
    )
    monkeypatch.setattr(
        live_service,
        "_market_session_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("session must remain blocked")
        ),
    )
    live_service._live_state_update(
        execution_recovery={"enabled": True, "ready": False},
    )

    result = live_service._run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=3,
        recovery_bootstrapped=False,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert result["wait_seconds"] == 5.0
    assert result["safety"]["accepting_new_risk"] is False
    assert "execution_recovery_contract_missing" in result["safety"]["blockers"]


def test_prime_resets_execution_recovery_to_pending(monkeypatch):
    monkeypatch.setattr(live_service, "_phase2_feature_flags", lambda: _flags())

    live_service._prime_live_loop_state(
        broker="ctrader",
        strategy_name="factor_v4",
        started_at=time.time(),
        account={},
        accepting_new_risk=False,
        restore_session=False,
        account_observed=False,
    )

    recovery = live_service._live_state_get("execution_recovery", {}, clone=True)
    assert recovery["enabled"] is True
    assert recovery["ready"] is False
    assert recovery["unresolved_count"] is None
    assert recovery["status"] == "pending"
