import threading
import time
from types import SimpleNamespace

import pytest

from backend.services import live_service
from backend.services.live_loop_controller import LiveLoopController
from backend.services.live_safety_plane import LiveSafetyPlane
from backend.services.live_safety_planner import SafetyPlan, safety_candidate


class _SnapshotBridge:
    is_connected = True

    def __init__(self, *, positions=()):
        self.positions = tuple(positions)
        self.calls: list[str] = []

    def reconcile_positions(self, *, force=True, allow_cache_fallback=False):
        self.calls.append("positions")
        now = time.time()
        return SimpleNamespace(
            reconcile_id="positions-r1",
            status="fresh",
            positions=self.positions,
            observed_at=now,
            generated_at=now,
            success=True,
            fresh=True,
            authoritative=True,
            error_code="",
            error_message="",
        )

    def reconcile_account(self, *, force=True, allow_cache_fallback=False):
        self.calls.append("account")
        now = time.time()
        return SimpleNamespace(
            reconcile_id="account-r1",
            status="fresh",
            account={"balance": 1000.0, "equity": 1000.0},
            observed_at=now,
            generated_at=now,
        )

    def unresolved_execution_intent_count(self):
        return 0

    def recover_execution_intents(self):
        self.calls.append("execution_recovery")
        return {"ready": True, "unresolved_count": 0}

    def get_spot_quote(self):
        return {}


class _OwnedThread:
    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.ident = 9876
        self._alive = False

    def start(self):
        self._alive = True

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        # The fake deliberately remains alive, modelling a draining tick that
        # has not yet acknowledged exit.
        return None


def _enable_phase2(monkeypatch):
    live_service._live_safety_plane = None
    live_service._live_safety_plane_owner = ""
    today = live_service.datetime.now(live_service.timezone.utc).strftime("%Y-%m-%d")
    live_service._live_state_update(
        trade_date=today,
        session_state_status="available",
        circuit_breaker=False,
        circuit_reason="",
        positions=[],
        positions_updated_at=0.0,
        execution_recovery={
            "ready": True,
            "unresolved_count": 0,
            "recovered": [],
        },
    )


def test_phase2_runs_broker_snapshot_and_safety_before_missing_online_bars(monkeypatch):
    _enable_phase2(monkeypatch)
    bridge = _SnapshotBridge()
    order: list[str] = []
    original_reconcile = bridge.reconcile_positions
    original_account = bridge.reconcile_account

    def _account(**kwargs):
        order.append("account_snapshot")
        return original_account(**kwargs)

    def _reconcile(**kwargs):
        order.append("broker_snapshot")
        return original_reconcile(**kwargs)

    bridge.reconcile_positions = _reconcile
    bridge.reconcile_account = _account
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))
    monkeypatch.setattr(
        live_service,
        "_market_session_snapshot",
        lambda *_args, **_kwargs: order.append("session") or {"status": "open_confirmed"},
    )
    monkeypatch.setattr(
        live_service,
        "_warmup_from_local_db",
        lambda *_args, **_kwargs: order.append("bars") or None,
    )
    monkeypatch.setattr(
        live_service,
        "_bootstrap_position_recovery",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        live_service,
        "_restore_session_state_for_day",
        lambda *_args, **_kwargs: live_service._live_state_update(
            session_state_status="available"
        ) or True,
    )

    result = live_service._run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=1,
        recovery_bootstrapped=True,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert order[:2] == ["broker_snapshot", "account_snapshot"]
    assert "session" in order[2:]
    assert "bars" not in order
    assert result["safety"]["reconciliation_state"] == "fresh"
    assert result["safety"]["heartbeat_at"] > 0
    assert result["wait_seconds"] == 5.0


def test_phase2_circuit_blocks_alpha_only_after_safety(monkeypatch):
    _enable_phase2(monkeypatch)
    monkeypatch.setattr(live_service, "bounded_demo_mode_active", lambda: False)
    bridge = _SnapshotBridge()
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))
    monkeypatch.setattr(
        live_service,
        "_evaluate_daily_drawdown",
        lambda: {"tripped": True, "dd_pct": 5.0},
    )
    monkeypatch.setattr(
        live_service,
        "_warmup_from_local_db",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("alpha bars must not run")),
    )

    result = live_service._run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=2,
        recovery_bootstrapped=True,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert bridge.calls[:2] == ["positions", "account"]
    assert result["safety"]["heartbeat_at"] > 0
    assert result["break_loop"] is False


def test_account_reconcile_failure_blocks_alpha_but_safety_still_runs_first(monkeypatch):
    _enable_phase2(monkeypatch)
    protected: list[dict] = []
    bridge = _SnapshotBridge(
        positions=(
            {
                "position_id": 903,
                "symbol": "XAUUSD+",
                "direction": 1,
                "volume": 100.0,
                "entry_price": 2400.0,
                "current_price": 2401.0,
            },
        )
    )

    def _account_failure(**_kwargs):
        bridge.calls.append("account")
        raise TimeoutError("account rpc timeout")

    bridge.reconcile_account = _account_failure
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))
    monkeypatch.setattr(
        live_service,
        "_run_position_protection_cycle",
        lambda _bridge, positions, **_kwargs: protected.extend(positions) or {"ok": True},
    )
    monkeypatch.setattr(
        live_service,
        "_market_session_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("session/alpha must remain blocked")
        ),
    )

    result = live_service._run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=3,
        recovery_bootstrapped=True,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert bridge.calls[:2] == ["positions", "account"]
    assert result["safety"]["reconciliation_state"] == "fresh"
    assert result["safety"]["heartbeat_at"] > 0
    assert live_service._live_state_get("accepting_new_risk") is False
    assert [item["position_id"] for item in protected] == [903]


def test_unknown_position_snapshot_retries_safety_in_five_seconds(monkeypatch):
    _enable_phase2(monkeypatch)
    bridge = _SnapshotBridge()
    failed_reconcile = {
        "status": "failed",
        "success": False,
        "positions": (),
        "reconcile_id": "positions-unknown",
        "observed_at": 0.0,
    }
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))
    monkeypatch.setattr(
        live_service,
        "_explicit_position_reconcile",
        lambda _bridge: failed_reconcile,
    )
    monkeypatch.setattr(
        live_service,
        "_market_session_snapshot",
        lambda *_args, **_kwargs: {"status": "closed_confirmed"},
    )

    result = live_service._run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=4,
        recovery_bootstrapped=True,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert result["safety"]["reconciliation_state"] == "failed"
    assert result["safety"]["accepting_new_risk"] is False
    assert result["wait_seconds"] == 5.0


def test_generation_startup_barrier_requires_all_authoritative_steps(monkeypatch):
    controller = LiveLoopController()
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")
    monkeypatch.setattr(live_service, "_LIVE_LOOP_CONTROLLER", controller)
    bridge = _SnapshotBridge()
    positions = bridge.reconcile_positions()
    account = bridge.reconcile_account()
    controller.heartbeat(generation.generation_id, "safety")
    monkeypatch.setattr(
        live_service,
        "_factor_pipeline",
        {"engine": SimpleNamespace(is_warm=True)},
    )
    monkeypatch.setattr(
        live_service,
        "_restore_session_state_for_day",
        lambda *_args, **_kwargs: live_service._live_state_update(
            session_state_status="available"
        ) or True,
    )
    monkeypatch.setattr(live_service, "_bootstrap_position_recovery", lambda *_args, **_kwargs: True)

    ready = live_service._attempt_generation_startup_barrier(
        generation_id=generation.generation_id,
        bridge=bridge,
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        account_reconcile=account,
        positions_reconcile=positions,
        safety_result={"reconciliation_state": "fresh", "blockers": []},
    )

    status = controller.status()
    assert ready is True
    assert status["ready"] is True
    assert status["accepting_new_risk"] is True
    assert all(status["startup_barrier"].values())
    recovery_index = bridge.calls.index("execution_recovery")
    assert bridge.calls[recovery_index + 1:]
    assert all(item == "positions" for item in bridge.calls[recovery_index + 1:])


def test_startup_barrier_fails_closed_when_fresh_account_is_unavailable(monkeypatch):
    controller = LiveLoopController()
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")
    monkeypatch.setattr(live_service, "_LIVE_LOOP_CONTROLLER", controller)
    bridge = _SnapshotBridge()
    positions = bridge.reconcile_positions()
    controller.heartbeat(generation.generation_id, "safety")
    monkeypatch.setattr(
        live_service,
        "_factor_pipeline",
        {"engine": SimpleNamespace(is_warm=True)},
    )

    ready = live_service._attempt_generation_startup_barrier(
        generation_id=generation.generation_id,
        bridge=bridge,
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        account_reconcile=None,
        positions_reconcile=positions,
        safety_result={"reconciliation_state": "fresh", "blockers": []},
    )

    status = controller.status()
    assert ready is False
    assert status["ready"] is False
    assert status["accepting_new_risk"] is False
    assert status["startup_barrier"]["broker_ready"] is True
    assert status["startup_barrier"]["fresh_account"] is False


@pytest.mark.parametrize(
    ("account_patch", "expected_blocker"),
    [
        ({"status": "cache"}, "fresh_account_unavailable"),
        ({"observed_at": 0.0}, "fresh_account_timestamp_unknown"),
        ({"observed_at": time.time() - 60.0}, "fresh_account_stale"),
    ],
)
def test_startup_barrier_rejects_nonfresh_account_authority(
    monkeypatch,
    account_patch,
    expected_blocker,
):
    controller = LiveLoopController()
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")
    monkeypatch.setattr(live_service, "_LIVE_LOOP_CONTROLLER", controller)
    bridge = _SnapshotBridge()
    positions = bridge.reconcile_positions()
    account = bridge.reconcile_account()
    for field, value in account_patch.items():
        setattr(account, field, value)

    ready = live_service._attempt_generation_startup_barrier(
        generation_id=generation.generation_id,
        bridge=bridge,
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        account_reconcile=account,
        positions_reconcile=positions,
        safety_result={"reconciliation_state": "fresh", "blockers": []},
    )

    status = controller.status()
    assert ready is False
    assert status["startup_barrier"]["fresh_account"] is False
    assert expected_blocker in status["blockers"]


@pytest.mark.parametrize(
    ("positions_patch", "expected_blocker"),
    [
        ({"status": "cache"}, "fresh_positions_unavailable"),
        ({"observed_at": 0.0}, "fresh_positions_timestamp_unknown"),
        ({"observed_at": time.time() - 60.0}, "fresh_positions_stale"),
    ],
)
def test_startup_barrier_rejects_nonfresh_position_authority(
    monkeypatch,
    positions_patch,
    expected_blocker,
):
    controller = LiveLoopController()
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")
    monkeypatch.setattr(live_service, "_LIVE_LOOP_CONTROLLER", controller)
    bridge = _SnapshotBridge()
    positions = bridge.reconcile_positions()
    account = bridge.reconcile_account()
    for field, value in positions_patch.items():
        setattr(positions, field, value)

    ready = live_service._attempt_generation_startup_barrier(
        generation_id=generation.generation_id,
        bridge=bridge,
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        account_reconcile=account,
        positions_reconcile=positions,
        safety_result={"reconciliation_state": "fresh", "blockers": []},
    )

    status = controller.status()
    assert ready is False
    assert status["startup_barrier"]["fresh_account"] is True
    assert status["startup_barrier"]["fresh_positions"] is False
    assert expected_blocker in status["blockers"]


def test_startup_barrier_rechecks_position_freshness_after_intent_recovery(monkeypatch):
    controller = LiveLoopController()
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")
    monkeypatch.setattr(live_service, "_LIVE_LOOP_CONTROLLER", controller)
    bridge = _SnapshotBridge()
    initial_positions = bridge.reconcile_positions()
    account = bridge.reconcile_account()

    def _post_recovery_missing_timestamp(**_kwargs):
        bridge.calls.append("positions")
        return SimpleNamespace(
            reconcile_id="positions-post-recovery-missing-ts",
            status="fresh",
            positions=(),
            observed_at=0.0,
            generated_at=time.time(),
        )

    bridge.reconcile_positions = _post_recovery_missing_timestamp

    ready = live_service._attempt_generation_startup_barrier(
        generation_id=generation.generation_id,
        bridge=bridge,
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        account_reconcile=account,
        positions_reconcile=initial_positions,
        safety_result={"reconciliation_state": "fresh", "blockers": []},
    )

    status = controller.status()
    assert ready is False
    assert status["startup_barrier"]["unknown_execution_recovered"] is True
    assert status["startup_barrier"]["session_restored"] is False
    assert "post_recovery_positions_unavailable" in status["blockers"]


def test_draining_generation_keeps_thread_ownership_and_rejects_replacement(monkeypatch):
    controller = LiveLoopController()
    monkeypatch.setattr(live_service, "_LIVE_LOOP_CONTROLLER", controller)
    monkeypatch.setattr(live_service, "_start_live_scheduler", lambda: None)
    monkeypatch.setattr(live_service, "_start_live_safety_watchdog", lambda: False)
    monkeypatch.setattr(live_service.threading, "Thread", _OwnedThread)
    monkeypatch.setattr(live_service, "_process_shutdown_requested", False)
    monkeypatch.setattr(live_service, "_runtime_kv_set", lambda *_args, **_kwargs: None)

    first = live_service.start_loop("ctrader", persist_desired=False)
    draining = live_service.stop_loop(persist_desired=False)
    replacement = live_service.start_loop("ctrader", persist_desired=False)

    assert first["ok"] is True, first
    assert draining["phase"] == "draining"
    assert draining["thread_alive"] is True
    assert replacement["ok"] is False
    assert "live_loop_generation_busy:draining" in replacement["error"]
    owned = controller.ownership_snapshot()
    assert owned.thread is not None
    assert owned.thread.is_alive() is True


def test_stop_waits_for_admitted_open_rpc_then_keeps_generation_draining(monkeypatch):
    controller = LiveLoopController()
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")
    for step in controller.status()["startup_barrier"]:
        controller.complete_barrier_step(generation.generation_id, step)
    controller.heartbeat(generation.generation_id, "safety")

    class _LoopThread:
        ident = 4321

        def is_alive(self):
            return True

        def join(self, timeout=None):
            return None

    loop_thread = _LoopThread()
    controller.bind_thread(generation.generation_id, loop_thread)
    monkeypatch.setattr(live_service, "_LIVE_LOOP_CONTROLLER", controller)
    monkeypatch.setattr(live_service, "_process_shutdown_requested", False)
    monkeypatch.setattr(
        live_service,
        "_probe_final_open_admission",
        lambda **_kwargs: {"ok": True, "blockers": ()},
    )
    monkeypatch.setattr(live_service, "no_new_risk_latched", lambda **_kwargs: False)
    monkeypatch.setattr(live_service, "_runtime_kv_set", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live_service, "_stop_live_scheduler", lambda: None)
    # Earlier safety-cycle tests intentionally leave their fail-closed
    # projection in process state.  This case exercises controller/RPC
    # draining in isolation and does not model an already-running live loop.
    live_service._live_state_update(
        loop_running=False,
        accepting_new_risk=True,
        session_state_status="available",
        circuit_breaker=False,
    )

    rpc_entered = threading.Event()
    release_rpc = threading.Event()
    rpc_calls: list[float] = []

    def _rpc(*_args, **_kwargs):
        rpc_calls.append(time.time())
        rpc_entered.set()
        assert release_rpc.wait(timeout=2.0)
        return SimpleNamespace(success=False, outcome="rejected", comment="test rejection")

    monkeypatch.setattr(live_service, "_submit_open_trade_order", _rpc)
    monkeypatch.setattr(
        live_service,
        "_prepare_open_trade_intent",
        lambda **_kwargs: "decision-draining-open",
    )
    monkeypatch.setattr(live_service, "_record_open_trade_order_failure", lambda **_kwargs: None)
    candidate = live_service._OpenTradeCandidate(
        direction_name="LONG",
        bridge_meta={},
        digits=2,
        sl_dist=1.0,
        tp_dist=2.0,
        sl_price=3999.0,
        tp_price=4002.0,
        base_volume=100.0,
        volume=100.0,
        event_multiplier=1.0,
        event_sizing_context={},
        sizing_trace={},
        risk_verdict=SimpleNamespace(),
        market_session={},
        order_block={"order_blocked": False},
    )

    submit_result: dict[str, bool] = {}
    submit_thread = threading.Thread(
        target=lambda: submit_result.setdefault(
            "admitted",
            live_service._submit_open_trade_candidate(
                bridge=SimpleNamespace(),
                attr_engine=None,
                broker="ctrader",
                cfg=SimpleNamespace(),
                bar={},
                tick=1,
                account={},
                positions=[],
                composite=SimpleNamespace(direction=1),
                gate_result=SimpleNamespace(),
                candidate=candidate,
                current_price=4000.0,
                log=lambda _message: None,
            ),
        )
    )
    submit_thread.start()
    assert rpc_entered.wait(timeout=1.0)

    stop_result: dict[str, dict] = {}
    stop_thread = threading.Thread(
        target=lambda: stop_result.setdefault(
            "value", live_service.stop_loop(persist_desired=False)
        )
    )
    stop_thread.start()
    deadline = time.time() + 1.0
    while controller.status()["phase"] != "draining" and time.time() < deadline:
        time.sleep(0.01)

    assert controller.status()["phase"] == "draining"
    assert stop_thread.is_alive() is True
    assert len(rpc_calls) == 1

    release_rpc.set()
    submit_thread.join(timeout=1.0)
    stop_thread.join(timeout=1.0)

    assert submit_result["admitted"] is True
    assert stop_result["value"]["phase"] == "draining"
    replacement = live_service.start_loop("ctrader", persist_desired=False)
    assert replacement["ok"] is False
    assert "live_loop_generation_busy:draining" in replacement["error"]
    assert len(rpc_calls) == 1


def test_loop_status_exposes_generation_phase_heartbeats_and_blockers(monkeypatch):
    controller = LiveLoopController(clock=lambda: 100.0)
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")
    thread = _OwnedThread()
    thread.start()
    controller.bind_thread(generation.generation_id, thread)
    controller.heartbeat(generation.generation_id, "safety")
    monkeypatch.setattr(live_service, "_LIVE_LOOP_CONTROLLER", controller)
    monkeypatch.setattr(
        live_service,
        "safety_shadow_gate_status",
        lambda: {"status": "observing", "ok": False},
    )

    status = live_service.loop_status()

    assert status["phase"] == "starting"
    assert status["generation"] == generation.generation_id
    assert status["thread_alive"] is True
    assert status["ready"] is False
    assert status["accepting_new_risk"] is False
    assert status["safety_heartbeat_at"] == 100.0
    assert "fresh_account" in status["blockers"]
    assert "safety" in status
    assert status["safety_shadow_gate"] == {"status": "observing", "ok": False}


@pytest.mark.parametrize(
    ("state_patch", "latched", "expected_blocker"),
    [
        ({"session_state_status": "available", "circuit_breaker": False, "market_session": {"can_open_positions": True}}, True, "no_new_risk_latched"),
        ({"session_state_status": "unavailable", "circuit_breaker": False, "market_session": {"can_open_positions": True}}, False, "session_state_unavailable"),
        ({"session_state_status": "available", "circuit_breaker": True, "market_session": {"can_open_positions": True}}, False, "session_circuit_breaker"),
        ({"session_state_status": "available", "circuit_breaker": False, "market_session": {"can_open_positions": False}}, False, "market_session_blocks_open"),
    ],
)
def test_loop_status_merges_local_fail_closed_open_blockers(
    monkeypatch,
    state_patch,
    latched,
    expected_blocker,
):
    controller = LiveLoopController(clock=lambda: 100.0)
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")
    thread = _OwnedThread()
    thread.start()
    controller.bind_thread(generation.generation_id, thread)
    controller.heartbeat(generation.generation_id, "safety")
    for step in controller.status()["startup_barrier"]:
        controller.complete_barrier_step(generation.generation_id, step)
    monkeypatch.setattr(live_service, "_LIVE_LOOP_CONTROLLER", controller)
    monkeypatch.setattr(live_service, "no_new_risk_latched", lambda **_kwargs: latched)
    live_service._live_state_update(**state_patch)

    status = live_service.loop_status()

    assert status["phase"] == "degraded"
    assert status["accepting_new_risk"] is False
    assert expected_blocker in status["blockers"]


def test_missing_reconcile_timestamp_is_not_fresh(monkeypatch):
    bridge = _SnapshotBridge()
    bridge.reconcile_positions = lambda **_kwargs: SimpleNamespace(
        reconcile_id="missing-ts",
        status="fresh",
        positions=(),
        observed_at=0.0,
        generated_at=time.time(),
    )

    result = live_service._explicit_position_reconcile(bridge)

    assert result["status"] == "failed"
    assert result["error_code"] == "position_reconcile_timestamp_unknown"


def test_session_restore_queries_deals_even_when_runtime_cache_is_missing(monkeypatch):
    monkeypatch.setattr(live_service, "_runtime_kv_get", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        live_service,
        "_load_authoritative_session_deal_facts",
        lambda *_args, **_kwargs: {
            "completed_position_trades": [
                {"position_id": 10, "net": -2.5, "exec_timestamp": 100.0}
            ],
            "realized_close_legs": [
                {"deal_id": 10, "position_id": 10, "net": -2.5, "exec_timestamp": 100.0}
            ],
        },
    )
    monkeypatch.setattr(live_service, "_persist_session_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        live_service,
        "_evaluate_daily_drawdown",
        lambda *_args, **_kwargs: {"tripped": False},
    )
    live_service._live_state_update(account={"balance": 997.5})

    restored = live_service._restore_session_state_for_day("2026-07-18")

    assert restored is True
    assert live_service._live_state_get("session_state_status") == "available"
    assert live_service._live_state_get("session_pnl") == -2.5
    assert live_service._live_state_get("session_trades") == 1


def test_authoritative_deals_exclude_positions_still_open_at_broker(monkeypatch):
    rows = [
        {
            "position_id": 11,
            "gross_profit": 1.0,
            "swap": 0.0,
            "close_commission": 0.0,
            "net": 1.0,
            "exec_timestamp": 100.0,
            "close_deals_count": 1,
        },
        {
            "position_id": 12,
            "gross_profit": -3.0,
            "swap": 0.0,
            "close_commission": 0.0,
            "net": -3.0,
            "exec_timestamp": 200.0,
            "close_deals_count": 1,
        },
    ]

    class _Result:
        def __init__(self, payload):
            self.payload = payload

        def fetchall(self):
            return self.payload

    class _Connection:
        def close(self):
            return None

    monkeypatch.setattr(live_service, "_get_state_read_conn", lambda: _Connection())
    def _execute(_conn, sql, *_args, **_kwargs):
        if "WITH final_close" in sql:
            return _Result(rows)
        if "SELECT deal_id, position_id" in sql:
            return _Result(
                [
                    {
                        "deal_id": 111,
                        "position_id": 11,
                        "gross_profit": 1.0,
                        "swap": 0.0,
                        "close_commission": 0.0,
                        "net": 1.0,
                        "exec_timestamp": 100.0,
                        "closed_volume": 100.0,
                    },
                    {
                        "deal_id": 112,
                        "position_id": 12,
                        "gross_profit": -3.0,
                        "swap": 0.0,
                        "close_commission": 0.0,
                        "net": -3.0,
                        "exec_timestamp": 200.0,
                        "closed_volume": 50.0,
                    },
                ]
            )
        return _Result([])

    monkeypatch.setattr(live_service, "_state_execute", _execute)

    trades = live_service._load_authoritative_session_trades(
        "2026-07-18",
        broker_open_position_ids={12},
    )

    assert [item["position_id"] for item in trades] == [11]


def test_session_unavailable_never_zeros_last_known_risk(monkeypatch):
    monkeypatch.setattr(live_service, "_runtime_kv_get", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        live_service,
        "_load_authoritative_session_deal_facts",
        lambda *_args, **_kwargs: None,
    )
    live_service._live_state_update(session_pnl=-8.0, session_trades=2)

    restored = live_service._restore_session_state_for_day("2026-07-18")

    assert restored is False
    assert live_service._live_state_get("session_state_status") == "unavailable"
    assert live_service._live_state_get("session_pnl") == -8.0
    assert live_service._live_state_get("session_trades") == 2
    assert live_service._live_state_get("accepting_new_risk") is False


def test_failed_position_reconcile_blocks_open_but_cached_position_protection_continues(monkeypatch):
    bridge = _SnapshotBridge()
    protected = []
    live_service._live_state_update(
        positions=[{
            "position_id": 901,
            "symbol": "XAUUSD+",
            "direction": 1,
            "volume": 100.0,
            "entry_price": 2400.0,
            "current_price": 2401.0,
        }],
        account={"balance": 1000.0, "equity": 1000.0},
    )
    monkeypatch.setattr(
        live_service,
        "_get_live_safety_plane",
        lambda _generation_id="": LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
    )
    monkeypatch.setattr(
        live_service,
        "_plan_live_safety_candidates",
        lambda **_kwargs: SafetyPlan(
            candidates=(
                safety_candidate(
                    action="tighten",
                    position_id=901,
                    source="supervisor_tighten",
                    controls={"target_stop_loss": 2400.5},
                ),
            ),
            arbitration=(),
            planned_at=100.0,
        ),
    )
    monkeypatch.setattr(
        live_service,
        "_execute_live_safety_candidate",
        lambda _candidate, *, positions, **_kwargs: (
            protected.extend(positions) or {"ok": True, "status": "dispatched"}
        ),
    )
    failed = {
        "status": "failed",
        "success": False,
        "positions": (),
        "reconcile_id": "failed-r1",
        "observed_at": 0.0,
    }

    result = live_service._run_live_safety_cycle(
        bridge=bridge,
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        reconcile_result=failed,
    )

    assert result["accepting_new_risk"] is False
    assert "positions_reconciliation_failed" in result["blockers"]
    assert result["position_ids"] == [901]
    assert result["next_full_cycle_in_sec"] == 5.0
    assert [item["position_id"] for item in protected] == [901]


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_authoritative_protection_exception_blocks_new_risk_during_migration(monkeypatch, mode):
    bridge = _SnapshotBridge(
        positions=(
            {
                "position_id": 902,
                "symbol_id": 41,
                "symbol": "XAUUSD+",
                "direction": 1,
                "volume": 100.0,
                "entry_price": 2400.0,
                "current_price": 2401.0,
                "sl": 2390.0,
                "tp": 2420.0,
                "open_timestamp": time.time() - 60.0,
            },
        )
    )
    monkeypatch.setattr(
        live_service,
        "_get_live_safety_plane",
        lambda _generation_id="": LiveSafetyPlane(mode=mode, clock=lambda: 100.0),
    )
    monkeypatch.setattr(
        live_service,
        "_run_position_protection_cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("protection failed")),
    )

    result = live_service._run_live_safety_cycle(
        bridge=bridge,
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        reconcile_result=bridge.reconcile_positions(),
    )

    assert result["protection"]["ok"] is False
    assert result["accepting_new_risk"] is False
    assert "safety_protection_cycle_failed" in result["blockers"]


def test_position_without_stable_broker_identity_blocks_new_risk_without_index_error(monkeypatch):
    bridge = _SnapshotBridge()
    monkeypatch.setattr(
        live_service,
        "_get_live_safety_plane",
        lambda _generation_id="": LiveSafetyPlane(mode="off", clock=lambda: 100.0),
    )
    monkeypatch.setattr(
        live_service,
        "_publish_fresh_position_reconcile",
        lambda *_args, **_kwargs: [{"position_id": 0, "symbol": "XAUUSD+"}],
    )
    monkeypatch.setattr(
        live_service,
        "_run_position_protection_cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("position without ID must not be mutated")
        ),
    )

    result = live_service._run_live_safety_cycle(
        bridge=bridge,
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        reconcile_result=bridge.reconcile_positions(),
    )

    assert result["accepting_new_risk"] is False
    assert "broker_position_identity_missing" in result["blockers"]


def test_phase2_session_restore_failure_is_not_reset_to_zero(monkeypatch):
    reset_calls = []
    monkeypatch.setattr(
        live_service,
        "_restore_session_state_for_day",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        live_service,
        "_reset_session_state_for_new_day",
        lambda: reset_calls.append(True),
    )
    live_service._live_state_update(session_pnl=-9.0, session_trades=3)

    live_service._prime_live_loop_state(
        broker="ctrader",
        strategy_name="factor_v4",
        started_at=100.0,
        account={},
        accepting_new_risk=False,
        restore_session=True,
        account_observed=False,
    )

    assert reset_calls == []
    assert live_service._live_state_get("session_pnl") == -9.0
    assert live_service._live_state_get("session_trades") == 3


def test_session_drawdown_is_peak_to_trough_not_only_loss_from_start(monkeypatch):
    monkeypatch.setattr(
        live_service.RiskLimitSnapshot,
        "from_runtime_config",
        lambda: SimpleNamespace(max_consecutive_losses=99, max_daily_loss_pct=99.0),
    )
    live_service._live_state_update(account={"balance": 1020.0})

    restored = live_service._build_session_state_from_authoritative_trades(
        trade_date="2026-07-18",
        trades=[
            {"net": 100.0, "exec_timestamp": 1.0},
            {"net": -80.0, "exec_timestamp": 2.0},
        ],
    )

    # start=1000, peak=1100, trough=1020 => 80/1100.
    assert restored["session_max_drawdown_pct"] == pytest.approx(80.0 / 1100.0 * 100.0)
