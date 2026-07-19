from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from execution import ctrader_bridge as ctrader_module
from execution.base import AccountInfo, PositionInfo, PositionReconcileResult
from execution.ctrader_bridge import CTraderBridge, CTraderOrderResult
from backend.services.live_safety_state import (
    activate_no_new_risk_latch,
    no_new_risk_latch_status,
    reset_safety_state_for_tests,
    unresolved_broker_outcome_mutations,
)


pytestmark = pytest.mark.skipif(
    not ctrader_module.HAS_CTRADER,
    reason="ctrader-open-api not installed",
)


@pytest.fixture(autouse=True)
def _isolated_execution_safety_state(monkeypatch, tmp_path):
    reset_safety_state_for_tests()
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path / "safety"))
    yield
    reset_safety_state_for_tests()


class ProtoOAExecutionEvent:
    def __init__(self, *, position_id: int = 0, order_id: int = 0):
        self.errorCode = ""
        self.position = SimpleNamespace(positionId=position_id) if position_id else None
        self.order = SimpleNamespace(orderId=order_id, clientOrderId="")
        self.deal = SimpleNamespace(dealId=700, positionId=position_id, orderId=order_id)


class _IntentStore:
    def __init__(
        self,
        *,
        unresolved: int = 0,
        unresolved_items: list[object] | None = None,
        prepare_error: Exception | None = None,
    ):
        self.unresolved_n = unresolved
        self.unresolved_items = list(unresolved_items or [])
        self.prepare_error = prepare_error
        self.events: list[tuple[str, object]] = []
        self.completed: list[dict] = []

    def unresolved_count(self, **_kwargs):
        count = len(self.unresolved(**_kwargs)) if self.unresolved_items else self.unresolved_n
        self.events.append(("unresolved_count", count))
        return count

    def unresolved(self, **_kwargs):
        final_ids = {
            str(item["intent_id"])
            for item in self.completed
            if item.get("outcome") in {"confirmed", "rejected", "simulated"}
        }
        return [
            item for item in self.unresolved_items
            if str(getattr(item, "intent_id", "")) not in final_ids
        ]

    def prepare(self, **kwargs):
        self.events.append(("prepared", kwargs["intent_id"]))
        if self.prepare_error:
            raise self.prepare_error
        return SimpleNamespace(intent_id=kwargs["intent_id"], request=kwargs["request"])

    def mark_submitting(self, intent_id, **_kwargs):
        self.events.append(("submitting", intent_id))
        return SimpleNamespace(intent_id=intent_id)

    def complete(self, intent_id, **kwargs):
        self.events.append(("completed", kwargs["outcome"]))
        self.completed.append({"intent_id": intent_id, **kwargs})
        return SimpleNamespace(intent_id=intent_id, **kwargs)


def _bridge(*, store=None, enabled=True):
    bridge = CTraderBridge(
        send_orders=True,
        account_id=123,
        forced_symbol_id=41,
        execution_outcome_v2_enabled=enabled,
        execution_intent_store=store,
    )
    bridge._connected = True
    bridge._app_authed = True
    bridge._account_authed = True
    bridge._symbol_id = 41
    bridge._symbol_meta = {"api_min_volume": 100, "api_step_volume": 100}
    return bridge


def _reconcile(reconcile_id: str, positions=()):
    return PositionReconcileResult(
        reconcile_id=reconcile_id,
        status="fresh",
        positions=tuple(positions),
        observed_at=10.0,
        generated_at=10.0,
    )


def _recovery_intent(
    *,
    intent_id: str,
    action: str,
    side: str = "",
    status: str = "unknown",
    attempt_count: int = 1,
    requested_volume: float = 0.0,
    target_stop_loss: float = 0.0,
    target_take_profit: float = 0.0,
    request: dict | None = None,
):
    return SimpleNamespace(
        intent_id=intent_id,
        action=action,
        side=side,
        status=status,
        attempt_count=attempt_count,
        requested_volume=requested_volume,
        target_stop_loss=target_stop_loss,
        target_take_profit=target_take_profit,
        position_id="",
        request=dict(request or {}),
        broker_response={},
        prepared_at=9.0,
    )


def test_ctrader_order_result_is_frozen_and_enforces_outcome_success_invariant():
    result = CTraderOrderResult(success=False, outcome="unknown")
    assert result.outcome == "unknown"
    with pytest.raises(FrozenInstanceError):
        result.outcome = "confirmed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="success must be true only"):
        CTraderOrderResult(success=True, outcome="unknown")
    with pytest.raises(ValueError, match="invalid cTrader order outcome"):
        CTraderOrderResult(success=False, outcome="accepted")


def test_market_order_persists_prepared_and_submitting_before_rpc_and_confirms_unique_diff(monkeypatch):
    store = _IntentStore()
    bridge = _bridge(store=store)
    position = PositionInfo(
        position_id=9001,
        symbol_id=41,
        symbol="XAUUSD",
        direction=1,
        volume=100,
    )
    reconciles = iter([_reconcile("pre"), _reconcile("post", [position])])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])

    def send(req, timeout=None, *, client_msg_id=""):
        assert [event[0] for event in store.events][-2:] == ["prepared", "submitting"]
        assert req.clientOrderId
        assert client_msg_id
        assert "qid:" in req.comment
        store.events.append(("rpc", req.clientOrderId))
        return ProtoOAExecutionEvent(position_id=9001, order_id=8001)

    monkeypatch.setattr(bridge, "_send", send)

    result = bridge.market_buy("XAUUSD", 100, comment="live-open")

    assert result.success is True
    assert result.outcome == "confirmed"
    assert result.position_id == 9001
    assert result.order_id == 8001
    assert result.intent_id
    assert result.client_order_id
    assert result.client_msg_id
    assert [event[0] for event in store.events] == [
        "unresolved_count", "prepared", "submitting", "rpc", "completed",
    ]
    assert store.completed[0]["outcome"] == "confirmed"


def test_timeout_does_not_guess_existing_same_direction_position(monkeypatch):
    store = _IntentStore()
    bridge = _bridge(store=store)
    existing = PositionInfo(
        position_id=42,
        symbol_id=41,
        symbol="XAUUSD",
        direction=1,
        volume=100,
    )
    reconciles = iter([_reconcile("pre", [existing]), _reconcile("post", [existing])])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("late broker receipt")),
    )

    result = bridge.market_buy("XAUUSD", 100)

    assert result.success is False
    assert result.outcome == "unknown"
    assert result.position_id == 0
    assert store.completed[0]["outcome"] == "unknown"
    assert store.completed[0]["broker_response"]["resolution"]["reason"] == "no_unique_position_match"
    assert no_new_risk_latch_status()["active"] is True


def test_multiple_position_differential_is_unknown_and_never_selects_max_id(monkeypatch):
    store = _IntentStore()
    bridge = _bridge(store=store)
    post = [
        PositionInfo(position_id=100, symbol_id=41, direction=1, volume=100),
        PositionInfo(position_id=999, symbol_id=41, direction=1, volume=100),
    ]
    reconciles = iter([_reconcile("pre"), _reconcile("post", post)])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("unknown")),
    )

    result = bridge.market_buy("XAUUSD", 100)

    assert result.outcome == "unknown"
    assert result.position_id == 0
    resolution = store.completed[0]["broker_response"]["resolution"]
    assert resolution["candidate_position_ids"] == [100, 999]


def test_live_post_resolution_uses_client_order_identity_for_unique_match(monkeypatch):
    store = _IntentStore()
    bridge = _bridge(store=store)
    post = [
        PositionInfo(position_id=100, symbol_id=41, direction=1, volume=100),
        PositionInfo(position_id=999, symbol_id=41, direction=1, volume=100),
    ]
    reconciles = iter([_reconcile("pre"), _reconcile("post", post)])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("late receipt")),
    )

    def _orders(**_kwargs):
        prepared = next(
            event for event in store.events if event[0] == "prepared"
        )
        intent_id = str(prepared[1])
        return ({
            "8001": {
                "order_id": 8001,
                "position_id": 999,
                "symbol_id": 41,
                "trade_side": "buy",
                "client_order_id": "",
                "comment": f"live-open qid:{intent_id.replace('-', '')[:16]}",
            }
        }, True)

    monkeypatch.setattr(bridge, "_order_history_for_recovery", _orders)

    result = bridge.market_buy("XAUUSD", 100, comment="live-open")

    assert result.outcome == "confirmed"
    assert result.position_id == 999
    assert store.completed[0]["broker_order_id"] == 8001


def test_unique_new_deal_can_resolve_position_only_when_both_snapshots_are_available():
    bridge = _bridge(enabled=False)
    pre_deals = {"1": {"deal_id": 1, "position_id": 10, "symbol_id": 41, "trade_side": "buy"}}
    post_deals = {
        **pre_deals,
        "2": {
            "deal_id": 2,
            "order_id": 20,
            "position_id": 99,
            "symbol_id": 41,
            "trade_side": "buy",
            "close_detail": {},
        },
    }

    resolved = bridge._resolve_open_differential(
        side=1,
        symbol_id=41,
        pre_positions={},
        pre_deals=pre_deals,
        post_positions={},
        post_deals=post_deals,
        response={},
        deals_differential_available=True,
    )
    unavailable = bridge._resolve_open_differential(
        side=1,
        symbol_id=41,
        pre_positions={},
        pre_deals=pre_deals,
        post_positions={},
        post_deals=post_deals,
        response={},
        deals_differential_available=False,
    )

    assert resolved["outcome"] == "confirmed"
    assert resolved["position_id"] == 99
    assert unavailable["outcome"] == "unknown"


def test_unresolved_intent_blocks_new_rpc(monkeypatch):
    store = _IntentStore(unresolved=2)
    bridge = _bridge(store=store)
    rpc_calls = []
    monkeypatch.setattr(bridge, "_send", lambda *args, **kwargs: rpc_calls.append((args, kwargs)))

    result = bridge.market_sell("XAUUSD", 100)

    assert result.outcome == "rejected"
    assert result.error_code == "unresolved_execution_intent"
    assert rpc_calls == []
    assert no_new_risk_latch_status()["active"] is True


def test_intent_prepare_failure_blocks_new_rpc(monkeypatch):
    store = _IntentStore(prepare_error=RuntimeError("postgres unavailable"))
    bridge = _bridge(store=store)
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: _reconcile("pre"))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])
    rpc_calls = []
    monkeypatch.setattr(bridge, "_send", lambda *args, **kwargs: rpc_calls.append((args, kwargs)))

    result = bridge.market_buy("XAUUSD", 100)

    assert result.success is False
    assert result.outcome == "rejected"
    assert result.error_code == "execution_intent_persist_failed"
    assert rpc_calls == []


def test_compat_mode_unknown_protobuf_is_not_reported_success(monkeypatch):
    bridge = _bridge(enabled=False)
    monkeypatch.setattr(bridge, "_send", lambda *_args, **_kwargs: SimpleNamespace(orderId=12))

    result = bridge.market_buy("XAUUSD", 100)

    assert result.success is False
    assert result.outcome == "unknown"
    assert result.position_id == 0
    assert no_new_risk_latch_status()["active"] is True

    rpc_calls = []
    monkeypatch.setattr(bridge, "_send", lambda *args, **kwargs: rpc_calls.append((args, kwargs)))
    retry = bridge.market_buy("XAUUSD", 100)

    assert retry.outcome == "rejected"
    assert retry.error_code == "no_new_risk_latched"
    assert rpc_calls == []


def test_v2_unknown_protobuf_with_position_shaped_fields_is_not_a_broker_receipt(monkeypatch):
    store = _IntentStore()
    bridge = _bridge(store=store)
    reconciles = iter([_reconcile("pre"), _reconcile("post")])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])

    class ProtoOAUnknownEvent:
        position = SimpleNamespace(positionId=9901)
        order = SimpleNamespace(orderId=8801, clientOrderId="")
        deal = None
        # Field-name similarity alone is not a documented broker rejection.
        errorCode = "UNKNOWN_MESSAGE_ERROR"

    monkeypatch.setattr(bridge, "_send", lambda *_args, **_kwargs: ProtoOAUnknownEvent())

    result = bridge.market_buy("XAUUSD", 100)

    assert result.success is False
    assert result.outcome == "unknown"
    assert result.position_id == 0
    resolution = store.completed[0]["broker_response"]["resolution"]
    assert resolution["candidate_position_ids"] == []
    assert resolution["candidate_order_ids"] == []
    assert resolution["reason"] == "no_unique_position_match"
    assert no_new_risk_latch_status()["active"] is True


def test_v2_documented_order_error_event_remains_an_explicit_rejection(monkeypatch):
    store = _IntentStore()
    bridge = _bridge(store=store)
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: _reconcile("pre"))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])

    class ProtoOAOrderErrorEvent:
        position = None
        order = None
        deal = None
        errorCode = "TRADING_BAD_VOLUME"
        description = "bad volume"

    monkeypatch.setattr(bridge, "_send", lambda *_args, **_kwargs: ProtoOAOrderErrorEvent())

    result = bridge.market_buy("XAUUSD", 100)

    assert result.success is False
    assert result.outcome == "rejected"
    assert result.error_code == "TRADING_BAD_VOLUME"
    assert store.completed[0]["outcome"] == "rejected"
    assert no_new_risk_latch_status()["active"] is False


def test_compat_malformed_unknown_protobuf_is_total_and_latches_unknown(monkeypatch):
    bridge = _bridge(enabled=False)

    class ProtoOAMalformedEvent:
        position = SimpleNamespace(positionId="not-an-integer")
        order = SimpleNamespace(orderId=object(), clientOrderId="")
        deal = None
        errorCode = ""

    monkeypatch.setattr(bridge, "_send", lambda *_args, **_kwargs: ProtoOAMalformedEvent())

    result = bridge.market_buy("XAUUSD", 100)

    assert result.success is False
    assert result.outcome == "unknown"
    assert result.position_id == 0
    assert no_new_risk_latch_status()["active"] is True


def test_position_reconcile_distinguishes_fresh_empty_from_failure(monkeypatch):
    bridge = _bridge(enabled=False)
    monkeypatch.setattr(bridge, "_send", lambda *_args, **_kwargs: SimpleNamespace(position=[]))
    monkeypatch.setattr(bridge, "get_unrealized_pnl", lambda: {})

    fresh = bridge.reconcile_positions(force=True, allow_cache_fallback=False)

    assert fresh.status == "fresh"
    assert fresh.success is True
    assert fresh.authoritative is True
    assert fresh.positions == ()

    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("reconcile timeout")),
    )
    failed = bridge.reconcile_positions(force=True, allow_cache_fallback=False)

    assert failed.status == "failed"
    assert failed.success is False
    assert failed.authoritative is False
    assert failed.positions == ()
    assert failed.error_code == "position_reconcile_failed"


def test_position_reconcile_labels_event_snapshot_instead_of_fresh(monkeypatch):
    bridge = _bridge(enabled=False)
    bridge._merge_position_cache(
        PositionInfo(position_id=88, symbol_id=41, direction=-1, volume=100),
        emit=False,
        reason="execution_event",
    )
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("reconcile timeout")),
    )

    result = bridge.reconcile_positions(force=True, allow_cache_fallback=True)

    assert result.status == "event"
    assert result.fresh is False
    assert result.positions[0].position_id == 88
    assert result.error_code == "position_reconcile_failed"


def test_account_reconcile_distinguishes_fresh_and_event_fallback(monkeypatch):
    bridge = _bridge(enabled=False)
    trader = SimpleNamespace(
        balance=123450,
        traderLogin=7001,
        depositAssetId=1,
        leverageInCents=10000,
        maxLeverage=100,
    )
    monkeypatch.setattr(bridge, "_send", lambda *_args, **_kwargs: SimpleNamespace(trader=trader))
    monkeypatch.setattr(bridge, "_unrealized_pnl", lambda: 5.0)

    fresh = bridge.reconcile_account(force=True, allow_cache_fallback=False)

    assert fresh.status == "fresh"
    assert fresh.account is not None
    assert fresh.account.account_id == 7001
    assert fresh.account.balance == pytest.approx(1234.5)
    assert fresh.account.equity == pytest.approx(1239.5)

    bridge._set_account_cache(
        AccountInfo(account_id=7001, balance=1234.5, equity=1240.0),
        emit=False,
        reason="trader_updated",
    )
    bridge._connected = True
    bridge._account_authed = True
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("account timeout")),
    )

    fallback = bridge.reconcile_account(force=True, allow_cache_fallback=True)

    assert fallback.status == "event"
    assert fallback.fresh is False
    assert fallback.account is not None
    assert fallback.account.equity == pytest.approx(1240.0)


def test_account_reconcile_never_marks_failed_unrealized_pnl_as_fresh(monkeypatch):
    bridge = _bridge(enabled=False)
    trader = SimpleNamespace(
        balance=123450,
        traderLogin=7001,
        depositAssetId=1,
        leverageInCents=10000,
        maxLeverage=100,
    )
    bridge._set_account_cache(
        AccountInfo(account_id=7001, balance=1234.5, equity=1240.0),
        emit=False,
        reason="trader_updated",
    )
    monkeypatch.setattr(bridge, "_send", lambda *_args, **_kwargs: SimpleNamespace(trader=trader))
    monkeypatch.setattr(
        bridge,
        "_unrealized_pnl",
        lambda: (_ for _ in ()).throw(TimeoutError("unrealized PnL unavailable")),
    )

    failed = bridge.reconcile_account(force=True, allow_cache_fallback=False)

    assert failed.status == "failed"
    assert failed.fresh is False
    assert failed.account is None
    assert failed.error_code == "account_reconcile_failed"

    bridge._connected = True
    bridge._account_authed = True
    cached = bridge.reconcile_account(force=True, allow_cache_fallback=True)
    assert cached.status == "event"
    assert cached.fresh is False
    assert cached.account is not None
    assert cached.account.equity == pytest.approx(1240.0)


def test_account_reconcile_reuses_fresh_confirmed_empty_position_evidence(monkeypatch):
    bridge = _bridge(enabled=False)
    trader = SimpleNamespace(
        balance=123450,
        traderLogin=7001,
        depositAssetId=1,
        leverageInCents=10000,
        maxLeverage=100,
    )
    monkeypatch.setattr(bridge, "_send", lambda *_args, **_kwargs: SimpleNamespace(trader=trader))
    monkeypatch.setattr(
        bridge,
        "_unrealized_pnl",
        lambda: (_ for _ in ()).throw(AssertionError("redundant PnL RPC")),
    )
    observed_at = ctrader_module.time.time()
    empty = PositionReconcileResult(
        reconcile_id="positions-empty-1",
        status="fresh",
        positions=(),
        observed_at=observed_at,
        generated_at=observed_at,
    )

    result = bridge.reconcile_account(
        force=True,
        allow_cache_fallback=False,
        confirmed_empty_positions=empty,
    )

    assert result.status == "fresh"
    assert result.account is not None
    assert result.account.balance == pytest.approx(1234.5)
    assert result.account.equity == pytest.approx(1234.5)
    assert result.observed_at == pytest.approx(observed_at)


def test_account_reconcile_does_not_trust_stale_or_nonempty_position_evidence(monkeypatch):
    bridge = _bridge(enabled=False)
    trader = SimpleNamespace(
        balance=123450,
        traderLogin=7001,
        depositAssetId=1,
        leverageInCents=10000,
        maxLeverage=100,
    )
    monkeypatch.setattr(bridge, "_send", lambda *_args, **_kwargs: SimpleNamespace(trader=trader))
    pnl_calls: list[bool] = []
    monkeypatch.setattr(bridge, "_unrealized_pnl", lambda: pnl_calls.append(True) or 7.0)
    now = ctrader_module.time.time()
    stale_empty = PositionReconcileResult(
        reconcile_id="positions-empty-stale",
        status="fresh",
        positions=(),
        observed_at=now - 16.0,
        generated_at=now - 16.0,
    )
    nonempty = PositionReconcileResult(
        reconcile_id="positions-open-1",
        status="fresh",
        positions=(PositionInfo(position_id=1, symbol_id=41, direction=1, volume=100),),
        observed_at=now,
        generated_at=now,
    )

    stale_result = bridge.reconcile_account(confirmed_empty_positions=stale_empty)
    nonempty_result = bridge.reconcile_account(confirmed_empty_positions=nonempty)

    assert stale_result.account is not None
    assert stale_result.account.equity == pytest.approx(1241.5)
    assert nonempty_result.account is not None
    assert nonempty_result.account.equity == pytest.approx(1241.5)
    assert len(pnl_calls) == 2


def test_unresolved_close_intent_blocks_duplicate_broker_rpc(monkeypatch):
    position = PositionInfo(position_id=777, symbol_id=41, direction=1, volume=100)
    unresolved = _recovery_intent(
        intent_id="close-unknown",
        action="close_position",
        request={"position_id": 777, "position_volume_before": 100.0},
    )
    store = _IntentStore(unresolved_items=[unresolved])
    bridge = _bridge(store=store)
    monkeypatch.setattr(
        bridge,
        "reconcile_positions",
        lambda **_kwargs: _reconcile("pre-duplicate", [position]),
    )
    rpc_calls = []
    monkeypatch.setattr(bridge, "_send", lambda *args, **kwargs: rpc_calls.append((args, kwargs)))

    result = bridge.close_position(777, volume=100)

    assert result.success is False
    assert result.outcome == "unknown"
    assert result.intent_id == "close-unknown"
    assert result.error_code == "DUPLICATE_MUTATION_BLOCKED"
    assert rpc_calls == []


def test_compat_unknown_close_blocks_same_mutation_without_postgres(monkeypatch):
    bridge = _bridge(enabled=False)
    position = PositionInfo(position_id=778, symbol_id=41, direction=1, volume=100)
    monkeypatch.setattr(
        bridge,
        "reconcile_positions",
        lambda **_kwargs: _reconcile("fresh", [position]),
    )
    rpc_calls = []
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **_kwargs: rpc_calls.append(True) or SimpleNamespace(orderId=12),
    )

    first = bridge.close_position(778, volume=100)
    second = bridge.close_position(778, volume=100)

    assert first.outcome == "unknown"
    assert second.outcome == "unknown"
    assert second.error_code == "DUPLICATE_MUTATION_BLOCKED"
    assert len(rpc_calls) == 1
    recovery = bridge.execution_intent_recovery_status()
    assert recovery["ready"] is False
    assert recovery["unresolved_count"] == 1
    assert recovery["unresolved"][0]["source"] == "local_safety_latch"


@pytest.mark.parametrize("enabled", [False, True])
def test_unreadable_local_unknown_ledger_is_fail_closed_in_all_modes(monkeypatch, enabled):
    bridge = _bridge(store=_IntentStore(), enabled=enabled)

    def unavailable_ledger():
        raise OSError("safety ledger unreadable")

    monkeypatch.setattr(
        "backend.services.live_safety_state.unresolved_broker_outcome_mutations",
        unavailable_ledger,
    )

    recovery = bridge.execution_intent_recovery_status()

    assert recovery["ready"] is False
    assert recovery["enabled"] is enabled
    assert recovery["unresolved_count"] is None
    assert recovery["local_safety_latch_status"] == "unavailable"
    assert recovery["error"].startswith("local_unknown_ledger_unavailable:OSError:")
    with pytest.raises(RuntimeError, match="local_unknown_ledger_unavailable"):
        bridge.unresolved_execution_intent_count()


def test_durable_unknown_close_blocks_resend_after_bridge_restart(monkeypatch):
    position = PositionInfo(position_id=779, symbol_id=41, direction=1, volume=100)
    first_bridge = _bridge(enabled=False)
    monkeypatch.setattr(
        first_bridge,
        "reconcile_positions",
        lambda **_kwargs: _reconcile("fresh", [position]),
    )
    monkeypatch.setattr(
        first_bridge,
        "_send",
        lambda *_args, **_kwargs: SimpleNamespace(orderId=13),
    )
    assert first_bridge.close_position(779, volume=100).outcome == "unknown"

    restarted_bridge = _bridge(enabled=False)
    monkeypatch.setattr(
        restarted_bridge,
        "reconcile_positions",
        lambda **_kwargs: _reconcile("fresh", [position]),
    )
    rpc_calls = []
    monkeypatch.setattr(
        restarted_bridge,
        "_send",
        lambda *_args, **_kwargs: rpc_calls.append(True),
    )

    result = restarted_bridge.close_position(779, volume=100)

    assert result.outcome == "unknown"
    assert result.error_code == "DUPLICATE_MUTATION_BLOCKED"
    assert rpc_calls == []


def test_close_v2_confirms_only_after_fresh_position_disappears(monkeypatch):
    store = _IntentStore()
    bridge = _bridge(store=store)
    position = PositionInfo(position_id=501, symbol_id=41, direction=1, volume=100)
    reconciles = iter([_reconcile("pre-close", [position]), _reconcile("post-close")])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))

    def send(_req, timeout=None, *, client_msg_id=""):
        assert [event[0] for event in store.events][-2:] == ["prepared", "submitting"]
        assert client_msg_id
        store.events.append(("rpc", client_msg_id))
        return ProtoOAExecutionEvent(position_id=501, order_id=601)

    monkeypatch.setattr(bridge, "_send", send)

    result = bridge.close_position(501, volume=100)

    assert result.success is True
    assert result.outcome == "confirmed"
    assert result.intent_id
    assert result.client_msg_id
    assert store.completed[0]["outcome"] == "confirmed"
    assert store.completed[0]["broker_response"]["resolution"]["position_present"] is False


def test_close_v2_execution_event_without_broker_change_is_unknown_and_latched(monkeypatch):
    store = _IntentStore()
    bridge = _bridge(store=store)
    position = PositionInfo(position_id=502, symbol_id=41, direction=1, volume=100)
    reconciles = iter([
        _reconcile("pre-close", [position]),
        _reconcile("post-close", [position]),
    ])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **_kwargs: ProtoOAExecutionEvent(position_id=502, order_id=602),
    )
    latched = []
    monkeypatch.setattr(
        bridge,
        "_latch_unknown_broker_outcome",
        lambda **kwargs: latched.append(kwargs),
    )

    result = bridge.close_position(502, volume=100)

    assert result.success is False
    assert result.outcome == "unknown"
    assert result.error_code == "CLOSE_OUTCOME_UNKNOWN"
    assert store.completed[0]["outcome"] == "unknown"
    assert len(latched) == 1
    assert latched[0]["position_id"] == 502


def test_close_v2_pg_intent_failure_does_not_block_risk_reduction(monkeypatch):
    store = _IntentStore(prepare_error=RuntimeError("postgres unavailable"))
    bridge = _bridge(store=store)
    position = PositionInfo(position_id=503, symbol_id=41, direction=-1, volume=100)
    reconciles = iter([_reconcile("pre-close", [position]), _reconcile("post-close")])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))
    rpc_calls = []

    def send(*_args, **kwargs):
        rpc_calls.append(kwargs.get("client_msg_id"))
        return ProtoOAExecutionEvent(position_id=503, order_id=603)

    monkeypatch.setattr(bridge, "_send", send)

    result = bridge.close_position(503, volume=100)

    assert result.success is True
    assert result.outcome == "confirmed"
    assert rpc_calls and rpc_calls[0]


def test_partial_close_v2_pg_intent_failure_still_reduces_existing_position(monkeypatch):
    store = _IntentStore(prepare_error=RuntimeError("postgres unavailable"))
    bridge = _bridge(store=store)
    before = PositionInfo(position_id=504, symbol_id=41, direction=1, volume=200)
    after = PositionInfo(position_id=504, symbol_id=41, direction=1, volume=100)
    reconciles = iter([
        _reconcile("pre-reduce", [before]),
        _reconcile("post-reduce", [after]),
    ])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))
    rpc_calls = []
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **kwargs: rpc_calls.append(kwargs.get("client_msg_id"))
        or ProtoOAExecutionEvent(position_id=504, order_id=604),
    )
    outbox = []
    monkeypatch.setattr(
        "backend.services.live_safety_state.append_safety_outbox",
        lambda **kwargs: outbox.append(kwargs) or {},
    )

    result = bridge.close_position(504, volume=100)

    assert result.success is True
    assert result.outcome == "confirmed"
    assert result.volume == 100
    assert rpc_calls and rpc_calls[0]
    assert outbox[0]["event_type"] == "broker_risk_reduction_intent_persist_failed"


def test_amend_v2_pg_intent_failure_still_confirms_fresh_broker_projection(monkeypatch):
    store = _IntentStore(prepare_error=RuntimeError("postgres unavailable"))
    bridge = _bridge(store=store)
    before = PositionInfo(
        position_id=703,
        symbol_id=41,
        direction=1,
        volume=100,
        sl=2300.0,
        tp=2400.0,
    )
    updated = PositionInfo(
        position_id=703,
        symbol_id=41,
        direction=1,
        volume=100,
        sl=2310.0,
        tp=2410.0,
    )
    reconciles = iter([
        _reconcile("pre-amend", [before]),
        _reconcile("post-amend", [updated]),
    ])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **_kwargs: ProtoOAExecutionEvent(position_id=703, order_id=803),
    )
    outbox = []
    monkeypatch.setattr(
        "backend.services.live_safety_state.append_safety_outbox",
        lambda **kwargs: outbox.append(kwargs) or {},
    )

    result = bridge.amend_position_sltp(703, sl=2310.0, tp=2410.0)

    assert result.success is True
    assert result.outcome == "confirmed"
    assert outbox[0]["event_type"] == "broker_risk_reduction_intent_persist_failed"


def test_amend_v2_requires_fresh_sltp_projection_ack(monkeypatch):
    store = _IntentStore()
    bridge = _bridge(store=store)
    before = PositionInfo(position_id=701, symbol_id=41, direction=1, volume=100, sl=2300.0, tp=2400.0)
    unchanged = PositionInfo(position_id=701, symbol_id=41, direction=1, volume=100, sl=2300.0, tp=2400.0)
    reconciles = iter([_reconcile("pre-amend", [before]), _reconcile("post-amend", [unchanged])])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **_kwargs: ProtoOAExecutionEvent(position_id=701, order_id=801),
    )
    latched = []
    monkeypatch.setattr(
        bridge,
        "_latch_unknown_broker_outcome",
        lambda **kwargs: latched.append(kwargs),
    )

    result = bridge.amend_position_sltp(701, sl=2310.0, tp=2400.0)

    assert result.success is False
    assert result.outcome == "unknown"
    assert store.completed[0]["outcome"] == "unknown"
    assert latched[0]["action"] == "amend_position_sltp"


def test_amend_v2_confirms_fresh_matching_sltp(monkeypatch):
    store = _IntentStore()
    bridge = _bridge(store=store)
    before = PositionInfo(position_id=702, symbol_id=41, direction=1, volume=100, sl=2300.0, tp=2400.0)
    updated = PositionInfo(position_id=702, symbol_id=41, direction=1, volume=100, sl=2310.0, tp=2410.0)
    reconciles = iter([_reconcile("pre-amend", [before]), _reconcile("post-amend", [updated])])
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: next(reconciles))
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda *_args, **_kwargs: ProtoOAExecutionEvent(position_id=702, order_id=802),
    )

    result = bridge.amend_position_sltp(702, sl=2310.0, tp=2410.0)

    assert result.success is True
    assert result.outcome == "confirmed"
    assert store.completed[0]["outcome"] == "confirmed"


def test_recovery_uses_client_order_identity_to_resolve_one_of_multiple_positions(monkeypatch):
    intent = _recovery_intent(
        intent_id="open-1",
        action="market_open",
        side="buy",
        requested_volume=100,
        request={
            "symbol_id": 41,
            "positions_before": {},
            "deals_before": {},
            "deals_before_available": True,
            "client_order_id": "client-open-1",
            "comment_token": "qid:open-1",
        },
    )
    store = _IntentStore(unresolved_items=[intent])
    bridge = _bridge(store=store)
    positions = [
        PositionInfo(position_id=100, symbol_id=41, direction=1, volume=100),
        PositionInfo(position_id=999, symbol_id=41, direction=1, volume=100),
    ]
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: _reconcile("fresh", positions))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])
    bridge._last_deals_fetch_ok = True
    monkeypatch.setattr(
        bridge,
        "_order_history_for_recovery",
        lambda **_kwargs: ({
            "8001": {
                "order_id": 8001,
                "position_id": 999,
                "symbol_id": 41,
                "trade_side": "buy",
                "client_order_id": "client-open-1",
                "comment": "live-open qid:open-1",
            }
        }, True),
    )

    result = bridge.recover_execution_intents()

    assert result["recovered"][0]["outcome"] == "confirmed"
    assert result["recovered"][0]["position_id"] == 999
    assert result["recovered"][0]["reason"] == "unique_correlated_broker_match"
    assert store.completed[0]["broker_order_id"] == 8001


def test_recovery_dispatches_close_by_action_and_confirms_absent_target(monkeypatch):
    intent = _recovery_intent(
        intent_id="close-1",
        action="close_position",
        requested_volume=100,
        request={"position_id": 501, "position_volume_before": 100},
    )
    store = _IntentStore(unresolved_items=[intent])
    bridge = _bridge(store=store)
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: _reconcile("fresh"))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])
    monkeypatch.setattr(bridge, "_order_history_for_recovery", lambda **_kwargs: ({}, False))
    monkeypatch.setattr(
        bridge,
        "_resolve_open_differential",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("close must not use open recovery")),
    )

    result = bridge.recover_execution_intents()

    assert result["recovered"][0]["outcome"] == "confirmed"
    assert result["recovered"][0]["position_id"] == 501
    assert result["recovered"][0]["reason"] == "close_target_absent_in_fresh_reconcile"


@pytest.mark.parametrize(
    ("remaining_volume", "expected_outcome"),
    [(40.0, "confirmed"), (100.0, "unknown")],
)
def test_recovery_requires_fresh_expected_partial_close_volume(
    monkeypatch,
    remaining_volume,
    expected_outcome,
):
    intent = _recovery_intent(
        intent_id=f"reduce-{remaining_volume}",
        action="reduce_position",
        requested_volume=60,
        request={"position_id": 502, "position_volume_before": 100},
    )
    store = _IntentStore(unresolved_items=[intent])
    bridge = _bridge(store=store)
    position = PositionInfo(
        position_id=502,
        symbol_id=41,
        direction=1,
        volume=remaining_volume,
    )
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: _reconcile("fresh", [position]))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])
    monkeypatch.setattr(bridge, "_order_history_for_recovery", lambda **_kwargs: ({}, False))

    result = bridge.recover_execution_intents()

    assert result["recovered"][0]["outcome"] == expected_outcome
    assert store.completed[0]["outcome"] == expected_outcome


@pytest.mark.parametrize(
    ("actual_sl", "expected_outcome"),
    [(2310.0, "confirmed"), (2300.0, "unknown")],
)
def test_recovery_requires_fresh_amend_projection_ack(monkeypatch, actual_sl, expected_outcome):
    intent = _recovery_intent(
        intent_id=f"amend-{actual_sl}",
        action="amend_position_sltp",
        target_stop_loss=2310.0,
        request={"position_id": 701, "target_stop_loss": 2310.0},
    )
    store = _IntentStore(unresolved_items=[intent])
    bridge = _bridge(store=store)
    bridge._symbol_meta["digits"] = 2
    position = PositionInfo(
        position_id=701,
        symbol_id=41,
        direction=1,
        volume=100,
        sl=actual_sl,
    )
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: _reconcile("fresh", [position]))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])
    monkeypatch.setattr(bridge, "_order_history_for_recovery", lambda **_kwargs: ({}, False))

    result = bridge.recover_execution_intents()

    assert result["recovered"][0]["outcome"] == expected_outcome
    assert store.completed[0]["outcome"] == expected_outcome


def test_recovery_rejects_prepared_intent_that_never_reached_submitting(monkeypatch):
    intent = _recovery_intent(
        intent_id="never-submitted",
        action="market_open",
        side="sell",
        status="prepared",
        attempt_count=0,
    )
    store = _IntentStore(unresolved_items=[intent])
    bridge = _bridge(store=store)
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: _reconcile("fresh"))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])
    monkeypatch.setattr(bridge, "_order_history_for_recovery", lambda **_kwargs: ({}, False))

    result = bridge.recover_execution_intents()

    assert result["recovered"][0]["outcome"] == "rejected"
    assert result["recovered"][0]["reason"] == "intent_never_reached_submitting"


def test_recovery_resolves_prepared_risk_reduction_from_broker_facts(monkeypatch):
    intent = _recovery_intent(
        intent_id="close-submitting-ledger-failed",
        action="close_position",
        status="prepared",
        attempt_count=0,
        requested_volume=100,
        request={"position_id": 501, "position_volume_before": 100},
    )
    store = _IntentStore(unresolved_items=[intent])
    bridge = _bridge(store=store)
    monkeypatch.setattr(
        bridge,
        "reconcile_positions",
        lambda **_kwargs: _reconcile("fresh"),
    )
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])
    monkeypatch.setattr(
        bridge,
        "_order_history_for_recovery",
        lambda **_kwargs: ({}, False),
    )

    result = bridge.recover_execution_intents()

    assert result["recovered"][0]["outcome"] == "confirmed"
    assert result["recovered"][0]["reason"] == "close_target_absent_in_fresh_reconcile"


def test_pg_recovery_appends_explicit_local_unknown_resolution(monkeypatch):
    intent = _recovery_intent(
        intent_id="close-local-and-pg",
        action="close_position",
        requested_volume=100,
        request={"position_id": 801, "position_volume_before": 100},
    )
    activate_no_new_risk_latch(
        reason="broker_execution_outcome_unknown",
        actor="execution:ctrader_bridge",
        correlation_id=intent.intent_id,
        metadata={
            "action": "close_position",
            "position_id": 801,
            "evidence": {"requested_volume": 100, "position_volume_before": 100},
        },
    )
    store = _IntentStore(unresolved_items=[intent])
    bridge = _bridge(store=store)
    monkeypatch.setattr(bridge, "reconcile_positions", lambda **_kwargs: _reconcile("fresh-pg"))
    monkeypatch.setattr(bridge, "get_deals", lambda **_kwargs: [])
    monkeypatch.setattr(bridge, "_order_history_for_recovery", lambda **_kwargs: ({}, False))

    result = bridge.recover_execution_intents()

    assert result["recovered"][0]["outcome"] == "confirmed"
    assert result["recovered"][0]["local_resolution"]["released"] == 1
    assert unresolved_broker_outcome_mutations() == []
    assert no_new_risk_latch_status()["active"] is False


def test_compat_local_recovery_uses_fresh_absence_and_preserves_incident_cause(
    monkeypatch,
):
    activate_no_new_risk_latch(
        reason="incident active",
        actor="operator:test",
        cause="incident_control",
        cause_id="runtime_incident_mode",
    )
    activate_no_new_risk_latch(
        reason="broker_execution_outcome_unknown",
        actor="execution:ctrader_bridge",
        correlation_id="compat-close-unknown",
        metadata={
            "action": "close_position",
            "position_id": 802,
            "evidence": {"requested_volume": 100, "position_volume_before": 100},
        },
    )
    bridge = _bridge(enabled=False)
    monkeypatch.setattr(
        bridge,
        "reconcile_positions",
        lambda **_kwargs: _reconcile("fresh-local"),
    )

    result = bridge.recover_execution_intents()

    assert result["recovered"][0]["outcome"] == "confirmed"
    assert result["unresolved_count"] == 0
    assert result["ready"] is True
    assert unresolved_broker_outcome_mutations() == []
    status = no_new_risk_latch_status()
    assert status["active"] is True
    assert [item["cause"] for item in status["causes"]] == ["incident_control"]


def test_fresh_absence_remains_confirmed_when_local_resolution_append_fails(
    monkeypatch,
):
    from backend.services import live_safety_state as safety_state

    activate_no_new_risk_latch(
        reason="broker_execution_outcome_unknown",
        actor="execution:ctrader_bridge",
        correlation_id="close-resolution-append-fails",
        metadata={"action": "close_position", "position_id": 803},
    )
    bridge = _bridge(enabled=False)
    monkeypatch.setattr(
        bridge,
        "reconcile_positions",
        lambda **_kwargs: _reconcile("fresh-absent"),
    )
    monkeypatch.setattr(
        safety_state,
        "resolve_broker_outcome_mutation",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("safety disk unavailable")),
    )

    result = bridge.close_position(803, volume=100)

    assert result.outcome == "confirmed"
    assert result.success is True
    assert unresolved_broker_outcome_mutations()[0]["intent_id"] == (
        "close-resolution-append-fails"
    )
    assert no_new_risk_latch_status()["active"] is True
