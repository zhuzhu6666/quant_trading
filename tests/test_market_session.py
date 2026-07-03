from datetime import datetime, timezone

from backend.services.market_session import evaluate_market_session


def _ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def test_xauusd_session_uses_utc_schedule_not_local_time():
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=_ts(2026, 6, 26, 16, 40),
        latest_quote_ts=_ts(2026, 6, 26, 16, 39),
        latest_quote_change_ts=_ts(2026, 6, 26, 16, 39),
    )

    assert state.status == "open_confirmed"
    assert state.can_open_positions is True
    assert state.high_load_allowed is False
    assert state.confirmation_source == "fresh_quote"


def test_xauusd_scheduled_closed_waits_for_confirmation_when_flat():
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=_ts(2026, 6, 27, 2, 0),
        has_open_positions=False,
    )

    assert state.status == "closed_pending_confirmation"
    assert state.can_open_positions is False
    assert state.can_keep_market_connection is True
    assert state.high_load_allowed is False


def test_xauusd_closed_confirmed_allows_high_load_when_quote_stale_and_flat():
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=_ts(2026, 6, 27, 2, 0),
        latest_quote_ts=_ts(2026, 6, 26, 21, 59),
        has_open_positions=False,
    )

    assert state.status == "closed_confirmed"
    assert state.can_open_positions is False
    assert state.can_keep_market_connection is False
    assert state.high_load_allowed is True
    assert state.high_load_profile == "full"
    assert state.confirmation_source == "quote_stale_after_schedule_close"


def test_xauusd_closed_keeps_connection_when_positions_exist():
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=_ts(2026, 6, 27, 2, 0),
        latest_quote_ts=_ts(2026, 6, 26, 21, 59),
        has_open_positions=True,
    )

    assert state.status == "closed_pending_positions"
    assert state.can_open_positions is False
    assert state.can_keep_market_connection is True
    assert state.high_load_allowed is True
    assert state.high_load_profile == "limited_with_positions"


def test_quote_stale_blocks_opening_without_releasing_connection():
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=_ts(2026, 6, 26, 16, 40),
        latest_quote_ts=_ts(2026, 6, 26, 16, 20),
    )

    assert state.status == "quote_stale"
    assert state.can_open_positions is False
    assert state.can_keep_market_connection is True


def test_api_alive_quote_stale_is_classified_separately_from_network_down():
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=_ts(2026, 6, 26, 16, 40),
        latest_quote_ts=_ts(2026, 6, 26, 16, 20),
        api_available=True,
        broker_connected=True,
        account_api_ok=True,
        positions_api_ok=True,
    )

    assert state.status == "broker_connected_quote_stale"
    assert state.can_open_positions is False
    assert state.can_keep_market_connection is True
    assert state.market_closed_confidence == "medium"
    assert "api_alive_while_quote_stale" in state.evidence


def test_api_alive_repeated_quote_timestamp_waits_for_real_price_motion():
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=_ts(2026, 6, 26, 16, 40),
        latest_quote_ts=_ts(2026, 6, 26, 16, 39),
        api_available=True,
        broker_connected=True,
        account_api_ok=True,
        positions_api_ok=True,
        has_open_positions=False,
    )

    assert state.status == "open_pending_quote_movement"
    assert state.can_open_positions is False
    assert state.can_keep_market_connection is True
    assert state.market_closed_confidence == "low"
    assert "api_alive_without_quote_motion" in state.evidence


def test_api_alive_quote_motion_confirms_open_session():
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=_ts(2026, 6, 26, 16, 40),
        latest_quote_ts=_ts(2026, 6, 26, 16, 39),
        latest_quote_change_ts=_ts(2026, 6, 26, 16, 39),
        api_available=True,
        broker_connected=True,
        account_api_ok=True,
        positions_api_ok=True,
        has_open_positions=False,
    )

    assert state.status == "open_confirmed"
    assert state.can_open_positions is True
    assert state.market_closed_confidence == "none"


def test_api_alive_fresh_quote_but_stale_market_data_blocks_opening():
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=_ts(2026, 6, 26, 16, 40),
        latest_quote_ts=_ts(2026, 6, 26, 16, 39),
        latest_quote_change_ts=_ts(2026, 6, 26, 16, 39),
        latest_market_data_ts=_ts(2026, 6, 26, 16, 20),
        api_available=True,
        broker_connected=True,
        account_api_ok=True,
        positions_api_ok=True,
        has_open_positions=False,
    )

    assert state.status == "broker_connected_market_data_stale"
    assert state.can_open_positions is False
    assert state.can_keep_market_connection is True
    assert state.market_closed_confidence == "medium"
    assert "api_alive_while_market_data_stale" in state.evidence


def test_scheduled_open_requires_fresh_quote_before_opening():
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=_ts(2026, 6, 26, 16, 40),
    )

    assert state.status == "open_pending_quote"
    assert state.schedule_open is True
    assert state.can_open_positions is False


def test_pre_close_window_blocks_new_opening_even_with_fresh_quote():
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=_ts(2026, 6, 26, 21, 40),
        latest_quote_ts=_ts(2026, 6, 26, 21, 39),
        latest_quote_change_ts=_ts(2026, 6, 26, 21, 39),
    )

    assert state.status == "pre_close_risk"
    assert state.near_close is True
    assert state.can_open_positions is False
    assert state.can_keep_market_connection is True
