from types import SimpleNamespace

import pytest

from execution.base import PositionInfo
from execution import ctrader_bridge as ctrader_module
from execution.ctrader_bridge import CTraderBridge

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


def test_spot_event_still_updates_realtime_quote_after_depth_removal(monkeypatch):
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASpotEvent

    bridge = _bridge(monkeypatch)
    bridge._symbol_meta = {"digits": 2, "pip_position": 2}
    event = ProtoOASpotEvent()
    event.bid = 412050
    event.ask = 412070

    bridge._handle_spot_event(event)

    quote = bridge.get_spot_quote()
    assert quote["bid"] == pytest.approx(4120.5)
    assert quote["ask"] == pytest.approx(4120.7)
    assert quote["mid"] == pytest.approx(4120.6)
    assert quote["ts"] > 0


def test_close_position_refreshes_for_full_close_and_rejects_order_error(monkeypatch):
    bridge = _bridge(monkeypatch)
    refresh_calls = []

    def _refresh_positions(*, force=False, allow_cache_fallback=True):
        refresh_calls.append((force, allow_cache_fallback))
        return [PositionInfo(position_id=269, symbol_id=41, symbol="XAUUSD", direction=1, volume=100.0)]

    monkeypatch.setattr(bridge, "refresh_positions", _refresh_positions)
    monkeypatch.setattr(bridge, "_send", lambda req, timeout=None: ProtoOAOrderErrorEvent())

    result = bridge.close_position(269)

    assert refresh_calls == [(True, False)]
    assert result.success is False
    assert result.position_id == 269
    assert result.volume == pytest.approx(100.0)
    assert result.error_code == "TRADING_BAD_VOLUME"


def test_close_position_rejects_zero_volume_before_send(monkeypatch):
    bridge = _bridge(monkeypatch)
    send_calls = []
    monkeypatch.setattr(
        bridge,
        "refresh_positions",
        lambda *, force=False, allow_cache_fallback=True: [
            PositionInfo(position_id=269, symbol_id=41, symbol="XAUUSD", direction=1, volume=0.0)
        ],
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
    sent = []

    def _send(req, timeout=None):
        sent.append(SimpleNamespace(positionId=req.positionId, volume=req.volume))
        return ProtoOAExecutionEvent()

    monkeypatch.setattr(bridge, "_send", _send)

    result = bridge.close_position(269, volume=100.0)

    assert result.success is True
    assert result.position_id == 269
    assert result.volume == pytest.approx(100.0)
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
