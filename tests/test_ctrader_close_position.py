from types import SimpleNamespace
import sqlite3

import pytest

from backend.core.db import STATE_DB_DDL
from backend.services.live_position_lifecycle import build_replayed_close_payloads
from execution.base import PositionInfo, PositionReconcileResult
from execution import ctrader_bridge as ctrader_module
from execution.ctrader_bridge import CTraderBridge
from execution.deal_sync import sync_close_deals_batch

pytestmark = pytest.mark.skipif(not ctrader_module.HAS_CTRADER, reason="ctrader-open-api not installed")


class ProtoOAOrderErrorEvent:
    errorCode = "TRADING_BAD_VOLUME"
    description = "Bad volume"


class ProtoOAExecutionEvent:
    errorCode = ""


def _bridge(monkeypatch):
    bridge = CTraderBridge(send_orders=True, account_id=123, forced_symbol_id=41)
    bridge._connected = True
    bridge._app_authed = True
    bridge._account_authed = True
    bridge._symbol_id = 41
    return bridge


def _fresh(reconcile_id, positions):
    return PositionReconcileResult(
        reconcile_id=reconcile_id,
        status="fresh",
        positions=tuple(positions),
        observed_at=1.0,
        generated_at=1.0,
    )


def _isolate_close_intent(monkeypatch, bridge):
    """Keep close-path assertions independent of durable intent history."""
    monkeypatch.setattr(
        bridge,
        "_prepare_risk_reduction_intent",
        lambda **_kwargs: (object(), "intent-test", "client-test", None),
    )
    monkeypatch.setattr(
        bridge,
        "_finalize_risk_reduction_intent",
        lambda *_args, **_kwargs: True,
    )


def test_spot_event_still_updates_realtime_quote_after_depth_removal(monkeypatch):
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASpotEvent

    bridge = _bridge(monkeypatch)
    bridge._symbol_meta = {"digits": 2, "pip_position": 2}
    event = ProtoOASpotEvent()
    event.bid = 412_050_000
    event.ask = 412_070_000

    bridge._handle_spot_event(event)

    quote = bridge.get_spot_quote()
    assert quote["bid"] == pytest.approx(4120.5)
    assert quote["ask"] == pytest.approx(4120.7)
    assert quote["mid"] == pytest.approx(4120.6)
    assert quote["ts"] > 0
    assert quote["source"] == "ctrader_spot"


def test_live_trendbar_event_is_decoded_into_online_feed(monkeypatch):
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASpotEvent

    bridge = _bridge(monkeypatch)
    bridge._symbol_meta = {"digits": 2, "pip_position": 2}
    event = ProtoOASpotEvent()
    event.symbolId = 41
    trendbar = event.trendbar.add()
    trendbar.period = 5
    trendbar.utcTimestampInMinutes = 1_783_395_600 // 60
    trendbar.low = 412_000_000
    trendbar.deltaOpen = 20_000
    trendbar.deltaClose = 30_000
    trendbar.deltaHigh = 50_000
    trendbar.volume = 17

    bridge._handle_spot_event(event)

    frame = bridge.get_live_bars("M5", 1)
    assert frame is not None
    assert list(frame.index.astype(str)) == ["2026-07-07 03:40:00+00:00"]
    assert frame.iloc[-1]["open"] == pytest.approx(4_120.2)
    assert frame.iloc[-1]["close"] == pytest.approx(4_120.3)
    assert frame.iloc[-1]["volume"] == 17


def test_live_trendbar_subscription_is_idempotent(monkeypatch):
    bridge = _bridge(monkeypatch)
    sent = []
    monkeypatch.setattr(bridge, "_send", lambda req, timeout=None: sent.append(req))

    assert bridge.live_trendbars_need_subscription(("M5",)) is True
    assert bridge.subscribe_live_trendbars(("M5",)) is True
    assert bridge.subscribe_live_trendbars(("M5",)) is True
    assert len(sent) == 1
    assert sent[0].symbolId == 41
    assert sent[0].period == 5
    assert bridge.live_trendbars_need_subscription(("M5",)) is False


def test_subscribe_spots_is_idempotent_per_connection(monkeypatch):
    bridge = _bridge(monkeypatch)
    sent = []
    monkeypatch.setattr(bridge, "_send", lambda req, timeout=None: sent.append(req))

    assert bridge.subscribe_spots() is True
    assert bridge.subscribe_spots() is True
    assert len(sent) == 1


def test_subscribe_spots_treats_already_subscribed_as_success(monkeypatch):
    bridge = _bridge(monkeypatch)

    def _already_subscribed(_req, timeout=None):
        raise RuntimeError("errorCode=ALREADY_SUBSCRIBED")

    monkeypatch.setattr(bridge, "_send", _already_subscribed)

    assert bridge.subscribe_spots() is True
    assert bridge._symbol_id in bridge._spot_subscribed_symbol_ids


def test_get_deals_keeps_price_raw_and_scales_only_money(monkeypatch):
    from ctrader_open_api.messages import OpenApiMessages_pb2 as TradeMsg

    bridge = _bridge(monkeypatch)
    response = TradeMsg.ProtoOADealListRes()
    fixtures = (
        (7001, 2, -125, "BUY", 100, 90, 1_724_000_000_000),
        (7002, 4, -125, "SELL", 200, 150, 1_724_000_001_000),
    )
    for deal_id, money_digits, commission, side, volume, filled, timestamp in fixtures:
        deal = response.deal.add()
        deal.dealId = deal_id
        deal.orderId = 8000 + deal_id
        deal.positionId = 269
        deal.symbolId = 41
        deal.volume = volume
        deal.filledVolume = filled
        deal.executionPrice = 4048.25
        deal.tradeSide = ctrader_module.TRADE_SIDE[side]
        deal.dealStatus = 2
        deal.executionTimestamp = timestamp
        deal.commission = commission
        deal.moneyDigits = money_digits
    for index, multiplier in ((0, 100), (1, 10_000)):
        detail = response.deal[index].closePositionDetail
        detail.entryPrice = 4050.5
        detail.grossProfit = int(-2.5 * multiplier)
        detail.swap = int(-0.25 * multiplier)
        detail.commission = int(-0.5 * multiplier)
        detail.balance = int(1_000 * multiplier)
        detail.closedVolume = response.deal[index].filledVolume
        detail.moneyDigits = 2 if index == 0 else 4
    sent = []

    def _send(req, timeout=None):
        sent.append(req)
        return response

    monkeypatch.setattr(bridge, "_send", _send)

    deals = bridge.get_deals()

    assert sent[0].toTimestamp > 0
    assert [deal["execution_price"] for deal in deals] == [4048.25, 4048.25]
    assert [deal["commission"] for deal in deals] == [-1.25, -0.0125]
    assert [deal["close_detail"]["entry_price"] for deal in deals] == [4050.5, 4050.5]
    assert [deal["close_detail"]["gross_profit"] for deal in deals] == [-2.5, -2.5]
    assert [deal["close_detail"]["swap"] for deal in deals] == [-0.25, -0.25]
    assert [deal["close_detail"]["commission"] for deal in deals] == [-0.5, -0.5]
    assert [deal["close_detail"]["balance"] for deal in deals] == [1000.0, 1000.0]
    assert [deal["close_detail"]["closed_volume"] for deal in deals] == [90, 150]
    assert [deal["trade_side"] for deal in deals] == ["buy", "sell"]
    assert [deal["volume"] for deal in deals] == [100, 200]
    assert [deal["filled_volume"] for deal in deals] == [90, 150]
    assert [deal["execution_timestamp"] for deal in deals] == [
        1_724_000_000,
        1_724_000_001,
    ]


def test_get_deals_keeps_zero_money_digits_and_marks_invalid_price_unknown(monkeypatch):
    from ctrader_open_api.messages import OpenApiMessages_pb2 as TradeMsg

    bridge = _bridge(monkeypatch)
    response = TradeMsg.ProtoOADealListRes()
    deal = response.deal.add()
    deal.dealId = 7101
    deal.positionId = 270
    deal.symbolId = 41
    deal.executionPrice = 0.0
    deal.commission = -3
    deal.moneyDigits = 0
    monkeypatch.setattr(bridge, "_send", lambda req, timeout=None: response)

    result = bridge.get_deals()[0]

    assert result["execution_price"] == 0.0
    assert result["price_contract"] == "unknown"
    assert result["price_quality"] == "unknown"
    assert result["commission"] == -3.0


def test_get_deals_quarantines_same_position_price_scale_mismatch(monkeypatch):
    from ctrader_open_api.messages import OpenApiMessages_pb2 as TradeMsg

    bridge = _bridge(monkeypatch)
    response = TradeMsg.ProtoOADealListRes()
    deal = response.deal.add()
    deal.dealId = 7102
    deal.positionId = 271
    deal.symbolId = 41
    deal.executionPrice = 40.4825
    deal.executionTimestamp = 1_724_000_001_000
    detail = deal.closePositionDetail
    detail.entryPrice = 4050.5
    detail.grossProfit = -250
    detail.balance = 100_000
    detail.moneyDigits = 2
    monkeypatch.setattr(bridge, "_send", lambda req, timeout=None: response)

    result = bridge.get_deals()[0]

    assert result["execution_price"] == 0.0
    assert result["raw_execution_price"] == pytest.approx(40.4825)
    assert result["price_quality"] == "unknown"


def test_spot_relative_price_uses_symbol_metadata_without_value_threshold(monkeypatch):
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASpotEvent

    bridge = _bridge(monkeypatch)
    bridge._symbol_meta = {"digits": 2, "pip_position": 2}
    event = ProtoOASpotEvent()
    event.bid = 1_250_000
    event.ask = 1_251_000

    bridge._handle_spot_event(event)

    quote = bridge.get_spot_quote()
    assert quote["bid"] == pytest.approx(12.5)
    assert quote["ask"] == pytest.approx(12.51)


def test_broker_deal_price_reaches_restart_payload_without_money_scaling(
    monkeypatch,
    tmp_path,
):
    from ctrader_open_api.messages import OpenApiMessages_pb2 as TradeMsg

    bridge = _bridge(monkeypatch)
    response = TradeMsg.ProtoOADealListRes()
    deal = response.deal.add()
    deal.dealId = 7201
    deal.positionId = 269
    deal.symbolId = 41
    deal.volume = 100
    deal.filledVolume = 100
    deal.executionPrice = 4048.25
    deal.executionTimestamp = 1_724_000_001_000
    deal.tradeSide = ctrader_module.TRADE_SIDE["SELL"]
    deal.moneyDigits = 2
    detail = deal.closePositionDetail
    detail.entryPrice = 4050.5
    detail.grossProfit = -250
    detail.swap = -25
    detail.commission = -50
    detail.balance = 100_000
    detail.closedVolume = 100
    detail.moneyDigits = 2
    monkeypatch.setattr(bridge, "_send", lambda req, timeout=None: response)

    conn = sqlite3.connect(str(tmp_path / "state.db"))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(STATE_DB_DDL)
        realized = sync_close_deals_batch(
            bridge,
            conn,
            {269},
            from_ts=1_724_000_000,
            to_ts=1_724_000_002,
        )[269]
    finally:
        conn.close()

    payload = build_replayed_close_payloads(
        position_id=269,
        position_state={"symbol": "XAUUSD+"},
        real_pnl=realized,
        strategy_name="factor_pipeline_v4",
        now_ts=1_724_000_010.0,
        context_integrity_default="partial",
    )

    assert realized["exec_price"] == 4048.25
    assert realized["entry_price"] == 4050.5
    assert realized["price_quality"] == "broker_reported"
    assert realized["net"] == pytest.approx(-3.25)
    assert payload["close_price"] == 4048.25
    assert payload["total_pnl"] == pytest.approx(-3.25)


def test_close_position_refreshes_for_full_close_and_rejects_order_error(monkeypatch):
    bridge = _bridge(monkeypatch)
    _isolate_close_intent(monkeypatch, bridge)
    refresh_calls = []

    def _reconcile_positions(*, force=False, allow_cache_fallback=True):
        refresh_calls.append((force, allow_cache_fallback))
        return _fresh(
            "full-close-pre",
            [PositionInfo(position_id=269, symbol_id=41, symbol="XAUUSD", direction=1, volume=100.0)],
        )

    monkeypatch.setattr(bridge, "reconcile_positions", _reconcile_positions)
    monkeypatch.setattr(
        bridge,
        "_send",
        lambda req, timeout=None, *, client_msg_id="": ProtoOAOrderErrorEvent(),
    )

    result = bridge.close_position(269)

    assert refresh_calls == [(True, False)]
    assert result.success is False
    assert result.outcome == "rejected"
    assert result.position_id == 269
    assert result.volume == pytest.approx(100.0)
    assert result.error_code == "TRADING_BAD_VOLUME"
    assert result.execution_intent_status == "persisted"


def test_close_position_rejects_zero_volume_before_send(monkeypatch):
    bridge = _bridge(monkeypatch)
    send_calls = []
    monkeypatch.setattr(
        bridge,
        "reconcile_positions",
        lambda *, force=False, allow_cache_fallback=True: _fresh(
            "zero-volume-pre",
            [PositionInfo(position_id=269, symbol_id=41, symbol="XAUUSD", direction=1, volume=0.0)],
        ),
    )
    monkeypatch.setattr(bridge, "_send", lambda req, timeout=None: send_calls.append(req))

    result = bridge.close_position(269)

    assert result.success is False
    assert result.error_code == "invalid_close_volume"
    assert send_calls == []


def test_close_position_rejects_volume_below_symbol_step_before_send(monkeypatch):
    bridge = _bridge(monkeypatch)
    bridge._symbol_meta = {"api_min_volume": 100, "api_step_volume": 100}
    send_calls = []
    monkeypatch.setattr(bridge, "_send", lambda req, timeout=None: send_calls.append(req))

    result = bridge.close_position(269, volume=50.0)

    assert result.success is False
    assert result.error_code == "invalid_close_volume_step"
    assert "minVolume=100" in result.comment
    assert send_calls == []


def test_close_position_accepts_execution_event(monkeypatch):
    bridge = _bridge(monkeypatch)
    _isolate_close_intent(monkeypatch, bridge)
    sent = []
    refresh_calls = []

    def _reconcile_positions(*, force=False, allow_cache_fallback=True):
        refresh_calls.append((force, allow_cache_fallback))
        if len(refresh_calls) == 1:
            return _fresh(
                "execution-pre",
                [
                    PositionInfo(
                        position_id=269,
                        symbol_id=41,
                        symbol="XAUUSD",
                        direction=1,
                        volume=100.0,
                    )
                ],
            )
        return _fresh("execution-post", [])

    monkeypatch.setattr(bridge, "reconcile_positions", _reconcile_positions)

    def _send(req, timeout=None, *, client_msg_id=""):
        assert client_msg_id
        sent.append(SimpleNamespace(positionId=req.positionId, volume=req.volume))
        return ProtoOAExecutionEvent()

    monkeypatch.setattr(bridge, "_send", _send)

    result = bridge.close_position(269, volume=100.0)

    assert result.success is True
    assert result.outcome == "confirmed"
    assert result.position_id == 269
    assert result.volume == pytest.approx(100.0)
    assert refresh_calls == [(True, False), (True, False)]
    assert sent[0].positionId == 269
    assert sent[0].volume == 100


def test_execution_event_removes_cached_position_from_close_deal():
    from ctrader_open_api.messages import OpenApiMessages_pb2 as TradeMsg

    bridge = _bridge(None)
    bridge._merge_position_cache(
        PositionInfo(position_id=269, symbol_id=41, symbol="XAUUSD", direction=1, volume=100.0),
        emit=False,
    )
    events = []
    bridge.add_event_listener(lambda event_type, payload: events.append((event_type, payload)))

    event = TradeMsg.ProtoOAExecutionEvent()
    event.ctidTraderAccountId = 123
    event.executionType = 3
    event.isServerEvent = True
    event.deal.dealId = 7001
    event.deal.positionId = 269
    event.deal.volume = 100
    event.deal.filledVolume = 100
    event.deal.symbolId = 41
    event.deal.tradeSide = 1
    event.deal.dealStatus = 2
    event.deal.closePositionDetail.closedVolume = 100

    bridge._handle_execution_event(event)

    assert bridge._positions_snapshot() == []
    assert any(event_type == "positions" and payload["positions"] == [] for event_type, payload in events)
    execution_payload = next(payload for event_type, payload in events if event_type == "execution")
    assert execution_payload["position_id"] == 269
    assert execution_payload["closed_volume"] == pytest.approx(100.0)


def test_execution_event_keeps_partial_close_remaining_position():
    from ctrader_open_api.messages import OpenApiMessages_pb2 as TradeMsg

    bridge = _bridge(None)
    bridge._merge_position_cache(
        PositionInfo(position_id=270, symbol_id=41, symbol="XAUUSD", direction=1, volume=100.0),
        emit=False,
    )

    event = TradeMsg.ProtoOAExecutionEvent()
    event.ctidTraderAccountId = 123
    event.executionType = 3
    event.position.positionId = 270
    event.position.positionStatus = 1
    event.position.tradeData.symbolId = 41
    event.position.tradeData.volume = 50
    event.position.tradeData.tradeSide = 1
    event.position.price = 4000.0
    event.deal.dealId = 7002
    event.deal.positionId = 270
    event.deal.volume = 50
    event.deal.filledVolume = 50
    event.deal.symbolId = 41
    event.deal.tradeSide = 2
    event.deal.dealStatus = 2
    event.deal.closePositionDetail.closedVolume = 50

    bridge._handle_execution_event(event)

    positions = bridge._positions_snapshot()
    assert len(positions) == 1
    assert positions[0].position_id == 270
    assert positions[0].volume == pytest.approx(50.0)
