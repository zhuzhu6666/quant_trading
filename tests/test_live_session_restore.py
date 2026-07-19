from __future__ import annotations

import copy
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.core import db as db_module
from backend.services import live_service
from backend.services.session_restore import (
    authoritative_close_pnl,
    rebuild_session_risk_projection,
    resolve_session_restore,
)


@pytest.fixture(autouse=True)
def _preserve_process_live_state():
    with live_service._LIVE_STATE_LOCK:
        snapshot = copy.deepcopy(live_service._live_state)
    yield
    with live_service._LIVE_STATE_LOCK:
        live_service._live_state.clear()
        live_service._live_state.update(snapshot)


def _disable_restore_side_effects(monkeypatch) -> None:
    monkeypatch.setattr(
        live_service,
        "_persist_session_state",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        live_service,
        "_evaluate_daily_drawdown",
        lambda *_args, **_kwargs: {"tripped": False},
    )


def _publish_fresh_admission_reconciles() -> None:
    observed_at = live_service.time.time()
    live_service._live_state_update(
        account_reconciled={"ok": True, "balance": 1000.0},
        account_updated_at=observed_at,
        account_reconcile_id="account-admission-r1",
        account_reconcile_failed_at=None,
        positions_reconciled=[],
        positions_updated_at=observed_at,
        positions_reconcile_id="positions-admission-r1",
        positions_reconcile_failed_at=None,
    )


def test_rebuild_orders_completed_positions_before_building_equity_path():
    projection = rebuild_session_risk_projection(
        trade_date="2026-07-19",
        completed_position_trades=[
            {"position_id": 3, "net": -30.0, "exec_timestamp": 300.0},
            {"position_id": 1, "net": 100.0, "exec_timestamp": 100.0},
            {"position_id": 2, "net": -20.0, "exec_timestamp": 200.0},
        ],
        session_start_balance=1000.0,
        max_consecutive_losses=8,
        max_daily_loss_pct=99.0,
    )

    assert projection["session_trade_pnls"] == [100.0, -20.0, -30.0]
    assert projection["trade_equity_history"] == [1000.0, 1100.0, 1080.0, 1050.0]
    assert projection["session_peak_equity"] == 1100.0
    assert projection["session_max_drawdown_pct"] == pytest.approx(50.0 / 1100.0 * 100.0)
    assert projection["session_consecutive_loss"] == 2
    assert projection["session_last_trade_ts"] == 300.0


def test_pure_restore_prefers_authoritative_deals_over_poisoned_cache():
    result = resolve_session_restore(
        trade_date="2026-07-19",
        raw_cache={
            "trade_date": "2026-07-19",
            "session_pnl": -999.0,
            "session_trades": 99,
            "session_start_balance": 1.0,
        },
        authoritative_facts={
            "completed_position_trades": [
                {"position_id": 7, "net": 20.0, "exec_timestamp": 10.0}
            ],
            "realized_close_legs": [
                {
                    "deal_id": 11,
                    "position_id": 7,
                    "net": 20.0,
                    "exec_timestamp": 10.0,
                }
            ],
        },
        current_balance=1020.0,
        max_consecutive_losses=3,
        max_daily_loss_pct=5.0,
        observed_at=100.0,
    )

    assert result["restored"] is True
    assert result["authoritative"] is True
    assert result["state"]["session_state_status"] == "available"
    assert result["state"]["session_pnl"] == 20.0
    assert result["state"]["session_start_balance"] == 1000.0
    assert result["state"]["session_recorded_position_ids"] == [7]


def test_pure_restore_cache_is_protection_only():
    result = resolve_session_restore(
        trade_date="2026-07-19",
        raw_cache={
            "trade_date": "2026-07-19",
            "session_pnl": -10.0,
            "session_trades": 1,
            "session_start_balance": 1000.0,
            "session_observed_at": 50.0,
        },
        authoritative_facts=None,
        current_balance=1000.0,
        max_consecutive_losses=3,
        max_daily_loss_pct=5.0,
        observed_at=100.0,
    )

    assert result["restored"] is True
    assert result["authoritative"] is False
    assert result["state"]["session_state_status"] == "degraded_cache"
    assert result["state"]["accepting_new_risk"] is False
    assert result["state"]["session_observed_at"] == 50.0


def test_pure_restore_invalid_authoritative_baseline_is_unavailable():
    result = resolve_session_restore(
        trade_date="2026-07-19",
        raw_cache=None,
        authoritative_facts={
            "completed_position_trades": [
                {"position_id": 7, "net": 20.0, "exec_timestamp": 10.0}
            ],
            "realized_close_legs": [],
        },
        current_balance=0.0,
        max_consecutive_losses=3,
        max_daily_loss_pct=5.0,
        observed_at=100.0,
    )

    assert result["restored"] is False
    assert result["authoritative"] is False
    assert result["authoritative_error"].startswith("ValueError:")
    assert result["state"] == {
        "session_state_status": "unavailable",
        "session_state_source": "unavailable",
        "accepting_new_risk": False,
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, False),
        ({"net": 0.0}, False),
        ({"net": -1.0, "exec_timestamp": 10.0}, False),
        (
            {
                "net": 0.0,
                "exec_timestamp": 10.0,
                "deal_id": 7,
                "source": "ctrader_deals",
            },
            True,
        ),
    ],
)
def test_authoritative_close_pnl_requires_concrete_deal_evidence(payload, expected):
    assert authoritative_close_pnl(payload) is expected


def test_authoritative_restore_rebuilds_peak_and_history_instead_of_using_cache(monkeypatch):
    _disable_restore_side_effects(monkeypatch)
    monkeypatch.setattr(
        live_service,
        "_runtime_kv_get",
        lambda *_args, **_kwargs: {
            "trade_date": "2026-07-19",
            "session_pnl": -999.0,
            "session_trades": 99,
            "session_start_balance": 5000.0,
            "session_peak_equity": 9000.0,
            "trade_equity_history": [9000.0, 1.0],
        },
    )
    monkeypatch.setattr(
        live_service,
        "_load_authoritative_session_deal_facts",
        lambda *_args, **_kwargs: {
            "completed_position_trades": [
                {"position_id": 1, "net": 100.0, "exec_timestamp": 100.0},
                {"position_id": 2, "net": -80.0, "exec_timestamp": 200.0},
            ],
            "realized_close_legs": [
                {"deal_id": 1, "position_id": 1, "net": 100.0, "exec_timestamp": 100.0},
                {"deal_id": 2, "position_id": 2, "net": -80.0, "exec_timestamp": 200.0},
            ],
        },
    )
    monkeypatch.setattr(
        live_service.RiskLimitSnapshot,
        "from_runtime_config",
        lambda: SimpleNamespace(max_consecutive_losses=8, max_daily_loss_pct=99.0),
    )
    live_service._live_state_update(account={"balance": 1020.0})

    assert live_service._restore_session_state_for_day("2026-07-19") is True

    assert live_service._live_state_get("session_state_status") == "available"
    assert live_service._live_state_get("session_start_balance") == 1000.0
    assert live_service._live_state_get("session_peak_equity") == 1100.0
    assert live_service._live_state_get("trade_equity_history", clone=True) == [
        1000.0,
        1100.0,
        1020.0,
    ]


def test_partial_close_legs_aggregate_by_position_and_open_position_is_excluded(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "state.db"

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    conn = _conn()
    try:
        conn.executescript(db_module.STATE_DB_DDL)
        day_start = datetime(2026, 7, 19, tzinfo=timezone.utc).timestamp()
        conn.executemany(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, exec_timestamp, gross_profit, swap,
             close_commission, closed_volume, is_close)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, 101, day_start + 100.0, -10.0, 0.0, 0.0, 50, 1),
                (2, 102, day_start + 200.0, -5.0, 0.0, 0.0, 50, 1),
                (3, 103, day_start + 250.0, 7.0, 0.0, 0.0, 100, 1),
                (4, 101, day_start + 300.0, 30.0, 0.0, 0.0, 50, 1),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(live_service, "_get_state_read_conn", _conn)

    trades = live_service._load_authoritative_session_trades(
        "2026-07-19",
        broker_open_position_ids={102},
    )

    assert [trade["position_id"] for trade in trades] == [103, 101]
    assert trades[1]["net"] == pytest.approx(20.0)
    assert trades[1]["close_deals_count"] == 2

    facts = live_service._load_authoritative_session_deal_facts(
        "2026-07-19",
        broker_open_position_ids={102},
    )
    assert facts is not None
    assert [leg["deal_id"] for leg in facts["realized_close_legs"]] == [1, 2, 3, 4]
    assert sum(leg["net"] for leg in facts["realized_close_legs"]) == pytest.approx(22.0)

    _disable_restore_side_effects(monkeypatch)
    live_service._live_state_update(account={"balance": 1022.0})
    assert live_service._restore_session_state_for_day(
        "2026-07-19",
        broker_open_position_ids={102},
    ) is True
    # The open position's -5 partial leg affects realized PnL/drawdown, but it
    # is not a completed trade for win/loss or consecutive-loss accounting.
    assert live_service._live_state_get("session_pnl") == pytest.approx(22.0)
    assert live_service._live_state_get("session_trades") == 2
    assert live_service._live_state_get("session_trade_pnls", clone=True) == [
        7.0,
        20.0,
    ]
    assert live_service._live_state_get("session_realized_pnl_legs", clone=True) == [
        -10.0,
        -5.0,
        7.0,
        30.0,
    ]


def test_cross_day_partial_leg_affects_only_its_realization_day(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    today = datetime(2026, 7, 19, tzinfo=timezone.utc).timestamp()
    conn = _conn()
    try:
        conn.executescript(db_module.STATE_DB_DDL)
        conn.executemany(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, exec_timestamp, gross_profit, swap,
             close_commission, closed_volume, is_close)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (701, 1701, today - 60.0, -10.0, 0.0, 0.0, 50, 1),
                (702, 1701, today + 60.0, 30.0, 0.0, 0.0, 50, 1),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(live_service, "_get_state_read_conn", _conn)
    facts = live_service._load_authoritative_session_deal_facts(
        "2026-07-19",
        broker_open_position_ids=set(),
    )

    assert facts is not None
    assert [item["net"] for item in facts["completed_position_trades"]] == [20.0]
    assert [item["net"] for item in facts["realized_close_legs"]] == [30.0]

    live_service._live_state_update(account={"balance": 1030.0})
    projection = live_service._build_session_state_from_authoritative_trades(
        trade_date="2026-07-19",
        trades=facts["completed_position_trades"],
        realized_close_legs=facts["realized_close_legs"],
    )
    assert projection["session_start_balance"] == pytest.approx(1000.0)
    assert projection["session_pnl"] == pytest.approx(30.0)
    assert projection["session_trade_pnls"] == [20.0]


def test_authoritative_session_requires_close_deal_for_broker_missing_recovery_row(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "state.db"

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    day_start = datetime(2026, 7, 19, tzinfo=timezone.utc).timestamp()
    conn = _conn()
    try:
        conn.executescript(db_module.STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO recovery_position_state
            (position_id, broker, symbol, direction, open_price, volume,
             first_seen_at, last_seen_at, status, strategy_name,
             entry_decision_id, context_integrity, recovery_meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                999,
                "ctrader",
                "XAUUSD+",
                1,
                4000.0,
                100.0,
                day_start,
                day_start + 22.0,
                "open",
                "factor_v4",
                "decision-999",
                "full",
                "{}",
            ),
        )
        # This is an older partial-close leg.  Once the broker position later
        # disappears it cannot stand in for the still-delayed final close deal.
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, exec_timestamp, gross_profit, swap,
             close_commission, closed_volume, is_close)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9990, 999, day_start + 20.0, -2.0, 0.0, 0.0, 50, 1),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(live_service, "_get_state_read_conn", _conn)

    assert live_service._load_authoritative_session_trades(
        "2026-07-19",
        broker_open_position_ids=set(),
    ) is None

    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO ctrader_deals
            (deal_id, position_id, exec_timestamp, gross_profit, swap,
             close_commission, closed_volume, is_close)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9991, 999, day_start + 120.0, -4.0, 0.0, -1.0, 50, 1),
        )
        conn.commit()
    finally:
        conn.close()

    # Deal presence alone cannot resolve an active recovery row: the current
    # close detector must first prove a new deal/volume delta for this broker
    # disappearance (or startup recovery must mark the row closed).
    assert live_service._load_authoritative_session_trades(
        "2026-07-19",
        broker_open_position_ids=set(),
    ) is None

    trades = live_service._load_authoritative_session_trades(
        "2026-07-19",
        broker_open_position_ids=set(),
        confirmed_closed_position_ids={999},
    )
    assert trades is not None
    assert [(item["position_id"], item["net"]) for item in trades] == [(999, -7.0)]


@pytest.mark.parametrize(
    "cached_state",
    [
        {},
        {
            "trade_date": "2026-07-19",
            "session_pnl": "NaN",
            "session_trades": 2,
            "session_start_balance": 1000.0,
        },
        {
            "trade_date": "2026-07-18",
            "session_pnl": -20.0,
            "session_trades": 2,
            "session_start_balance": 1000.0,
        },
    ],
    ids=["missing", "corrupt", "cross_day"],
)
def test_invalid_cache_never_zeros_last_known_risk_or_opens_new_risk(
    monkeypatch,
    cached_state,
):
    monkeypatch.setattr(
        live_service,
        "_runtime_kv_get",
        lambda *_args, **_kwargs: cached_state,
    )
    monkeypatch.setattr(
        live_service,
        "_load_authoritative_session_deal_facts",
        lambda *_args, **_kwargs: None,
    )
    live_service._live_state_update(
        session_pnl=-8.0,
        session_trades=2,
        session_peak_equity=1000.0,
        trade_equity_history=[1000.0, 992.0],
        accepting_new_risk=True,
    )

    assert live_service._restore_session_state_for_day("2026-07-19") is False

    assert live_service._live_state_get("session_state_status") == "unavailable"
    assert live_service._live_state_get("session_pnl") == -8.0
    assert live_service._live_state_get("session_trades") == 2
    assert live_service._live_state_get("session_peak_equity") == 1000.0
    assert live_service._live_state_get("trade_equity_history", clone=True) == [
        1000.0,
        992.0,
    ]
    assert live_service._live_state_get("accepting_new_risk") is False


def test_same_day_cache_is_degraded_and_cannot_authorize_new_risk(monkeypatch):
    monkeypatch.setattr(
        live_service,
        "_runtime_kv_get",
        lambda *_args, **_kwargs: {
            "trade_date": "2026-07-19",
            "session_pnl": -12.0,
            "session_trades": 1,
            "session_winning": 0,
            "session_losing": 1,
            "session_trade_pnls": [-12.0],
            "session_consecutive_loss": 1,
            "session_max_drawdown_pct": 1.2,
            "session_peak_equity": 1000.0,
            "session_start_balance": 1000.0,
            "session_last_trade_ts": 123.0,
            "circuit_breaker": False,
            "circuit_reason": "",
            "trade_equity_history": [1000.0, 988.0],
        },
    )
    monkeypatch.setattr(
        live_service,
        "_load_authoritative_session_deal_facts",
        lambda *_args, **_kwargs: None,
    )
    live_service._live_state_update(accepting_new_risk=True)

    assert live_service._restore_session_state_for_day("2026-07-19") is True

    assert live_service._live_state_get("session_state_status") == "degraded_cache"
    assert live_service._live_state_get("session_pnl") == -12.0
    assert live_service._live_state_get("accepting_new_risk") is False


def test_deals_without_fresh_account_balance_cannot_borrow_cache_baseline(monkeypatch):
    monkeypatch.setattr(
        live_service,
        "_runtime_kv_get",
        lambda *_args, **_kwargs: {
            "trade_date": "2026-07-19",
            "session_pnl": -5.0,
            "session_trades": 1,
            "session_start_balance": 1000.0,
        },
    )
    monkeypatch.setattr(
        live_service,
        "_load_authoritative_session_deal_facts",
        lambda *_args, **_kwargs: {
            "completed_position_trades": [
                {"position_id": 1, "net": -5.0, "exec_timestamp": 100.0}
            ],
            "realized_close_legs": [
                {"deal_id": 1, "position_id": 1, "net": -5.0, "exec_timestamp": 100.0}
            ],
        },
    )
    live_service._live_state_update(account={}, accepting_new_risk=True)

    assert live_service._restore_session_state_for_day("2026-07-19") is True

    assert live_service._live_state_get("session_state_status") == "degraded_cache"
    assert live_service._live_state_get("session_start_balance") == 1000.0
    assert live_service._live_state_get("accepting_new_risk") is False


def test_session_fact_observation_never_borrows_positions_timestamp():
    from backend.api import live as live_api

    live_service._live_state_update(
        positions_updated_at=9999.0,
        session_last_trade_ts=8888.0,
        session_observed_at=1234.0,
        session_state_source="runtime_legacy_snapshot",
    )

    observation = live_api._fact_runtime_observation()

    assert observation["session_observed_at"] == 1234.0
    assert observation["session_source"] == "runtime_legacy_snapshot"


@pytest.mark.parametrize("status", ["unknown", "unavailable", "degraded_cache"])
def test_legacy_final_open_boundary_blocks_non_authoritative_session(
    monkeypatch,
    status,
):
    monkeypatch.setattr(live_service, "_generation_controller_enabled", lambda: False)
    monkeypatch.setattr(live_service, "no_new_risk_latched", lambda **_kwargs: False)
    live_service._process_shutdown_requested = False
    live_service._live_state_update(
        loop_running=True,
        accepting_new_risk=True,
        session_state_status=status,
    )

    assert live_service._open_trade_draining(lambda: False) is True


def test_legacy_session_retry_runs_recovery_and_fresh_account_before_restore(
    monkeypatch,
):
    order: list[str] = []
    bridge = SimpleNamespace(is_connected=True)
    monkeypatch.setattr(
        live_service,
        "_get_ctrader",
        lambda: (bridge, None, False),
    )
    monkeypatch.setattr(
        live_service,
        "_bootstrap_position_recovery",
        lambda *_args, **_kwargs: order.append("deal_recovery") or True,
    )
    monkeypatch.setattr(
        live_service,
        "_explicit_account_reconcile",
        lambda _bridge: SimpleNamespace(
            status="fresh",
            reconcile_id="legacy-account-r1",
            observed_at=123.0,
            account={"balance": 1012.0, "equity": 1012.0},
        ),
    )
    monkeypatch.setattr(
        live_service,
        "_fresh_cached_broker_open_position_ids",
        lambda: {7},
    )

    def _restore(trade_date, **kwargs):
        order.append("session_restore")
        assert trade_date == "2026-07-19"
        assert kwargs["broker_open_position_ids"] == {7}
        assert live_service._live_state_get("account", clone=True)["balance"] == 1012.0
        live_service._live_state_update(session_state_status="available")
        return True

    monkeypatch.setattr(live_service, "_restore_session_state_for_day", _restore)

    assert live_service._retry_legacy_session_restore(
        broker="ctrader",
        strategy_name="factor_v4",
        trade_date="2026-07-19",
        log=lambda _message: None,
    ) is True
    assert order == ["deal_recovery", "session_restore"]
    assert live_service._live_state_get("account_reconcile_id") == (
        "legacy-account-r1"
    )


def test_legacy_final_open_boundary_allows_available_session(monkeypatch):
    monkeypatch.setattr(live_service, "_generation_controller_enabled", lambda: False)
    monkeypatch.setattr(live_service, "no_new_risk_latched", lambda **_kwargs: False)
    live_service._process_shutdown_requested = False
    live_service._live_state_update(
        loop_running=True,
        accepting_new_risk=True,
        session_state_status="available",
    )
    _publish_fresh_admission_reconciles()

    assert live_service._open_trade_draining(lambda: False) is False


def test_generation_open_boundary_also_requires_live_session_projection(monkeypatch):
    monkeypatch.setattr(live_service, "_generation_controller_enabled", lambda: True)
    monkeypatch.setattr(live_service, "_current_generation_id", lambda: "gen-ready")
    monkeypatch.setattr(
        live_service._LIVE_LOOP_CONTROLLER,
        "accepting_new_risk",
        lambda generation_id: generation_id == "gen-ready",
    )
    monkeypatch.setattr(live_service, "no_new_risk_latched", lambda **_kwargs: False)
    live_service._process_shutdown_requested = False
    live_service._live_state_update(
        loop_running=True,
        accepting_new_risk=False,
        session_state_status="unavailable",
        circuit_breaker=False,
    )

    assert live_service._open_trade_draining(lambda: False) is True

    live_service._live_state_update(
        accepting_new_risk=True,
        session_state_status="available",
    )
    _publish_fresh_admission_reconciles()
    assert live_service._open_trade_draining(lambda: False) is False


def test_fresh_open_position_ids_require_independent_position_fact():
    live_service._live_state_update(
        positions=[{"position_id": 101}, {"ticket": 102}],
        positions_reconciled=[{"position_id": 101}, {"ticket": 102}],
        positions_updated_at=100.0,
        positions_reconcile_id="positions-r1",
    )

    assert live_service._fresh_cached_broker_open_position_ids(now_ts=114.0) == {
        101,
        102,
    }
    assert live_service._fresh_cached_broker_open_position_ids(now_ts=116.0) is None


def test_degraded_cache_retains_original_session_observation(monkeypatch):
    _disable_restore_side_effects(monkeypatch)
    monkeypatch.setattr(
        live_service,
        "_runtime_kv_get",
        lambda *_args, **_kwargs: {
            "trade_date": "2026-07-19",
            "session_pnl": -12.0,
            "session_trades": 1,
            "session_start_balance": 1000.0,
            "updated_at": 4321.0,
        },
    )
    monkeypatch.setattr(
        live_service,
        "_load_authoritative_session_deal_facts",
        lambda *_args, **_kwargs: None,
    )

    assert live_service._restore_session_state_for_day("2026-07-19") is True

    assert live_service._live_state_get("session_state_status") == "degraded_cache"
    assert live_service._live_state_get("session_observed_at") == 4321.0
