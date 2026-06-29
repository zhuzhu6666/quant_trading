import threading
import sqlite3
import time
from types import SimpleNamespace

import pytest

from backend.ledger.service import DecisionLedger
from backend.services import live_service


class _IdleThread:
    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.ident = 12345
        self._alive = False

    def start(self):
        self._alive = True

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self._alive = False


@pytest.fixture(autouse=True)
def _reset_loop_state():
    live_service._loop_thread = None
    live_service._loop_stop_flag = None
    live_service._loop_broker = None
    live_service._loop_started_at = None
    live_service._loop_strategy_name = None
    live_service._last_loop_end = None
    live_service._pending_close_reasons.clear()
    live_service._pending_close_verdicts.clear()
    live_service._recovery_zero_confirmations.clear()
    live_service._live_state_update(
        broker=None,
        loop_running=False,
        loop_strategy=None,
        loop_started_at=None,
        account=None,
        account_updated_at=None,
    )
    live_service._reset_session_state_for_new_day()
    yield
    live_service._loop_thread = None
    live_service._loop_stop_flag = None
    live_service._loop_broker = None
    live_service._loop_started_at = None
    live_service._loop_strategy_name = None
    live_service._last_loop_end = None
    live_service._pending_close_reasons.clear()
    live_service._pending_close_verdicts.clear()
    live_service._recovery_zero_confirmations.clear()
    live_service._live_state_update(
        broker=None,
        loop_running=False,
        loop_strategy=None,
        loop_started_at=None,
        account=None,
        account_updated_at=None,
    )
    live_service._reset_session_state_for_new_day()


def test_prime_live_loop_state_sets_loop_and_session_fields():
    live_service._live_state_update(
        session_pnl=88.0,
        session_trades=3,
        session_winning=2,
        session_losing=1,
        session_consecutive_loss=1,
        session_max_drawdown_pct=4.1,
    )

    live_service._prime_live_loop_state(
        broker="ctrader",
        strategy_name="test_strategy",
        started_at=123.0,
        account={"ok": True, "broker": "ctrader", "balance": 1000.0, "equity": 1000.0},
    )

    assert live_service._live_state_get("broker") == "ctrader"
    assert live_service._live_state_get("loop_running") is True
    assert live_service._live_state_get("loop_strategy") == "test_strategy"
    assert live_service._live_state_get("loop_started_at") == 123.0
    assert live_service._live_state_get("account", clone=True)["balance"] == 1000.0
    assert live_service._live_state_get("session_pnl") == 0.0
    assert live_service._live_state_get("session_trades") == 0
    assert live_service._live_state_get("session_max_drawdown_pct") == 0.0


def test_start_loop_primes_shared_state_and_scheduler(monkeypatch):
    scheduler_calls = []

    monkeypatch.setattr(live_service, "_start_live_scheduler", lambda: scheduler_calls.append("started"))
    monkeypatch.setattr(live_service.threading, "Thread", _IdleThread)

    result = live_service.start_loop("ctrader", strategy_name="smoke")

    assert result["ok"] is True
    assert result["broker"] == "ctrader"
    assert result["strategy_name"] == "smoke"
    assert result["thread_id"] == 12345
    assert scheduler_calls == ["started"]
    assert isinstance(live_service._loop_stop_flag, threading.Event)
    assert live_service._live_state_get("loop_running") is True
    assert live_service._live_state_get("broker") == "ctrader"
    assert live_service._live_state_get("loop_strategy") == "smoke"

    acct = live_service._live_state_get("account", clone=True)
    assert acct["ok"] is True
    assert acct["broker"] == "ctrader"
    assert acct["balance"] == 0
    assert live_service._live_state_get("session_trades") == 0
    assert live_service._live_state_get("session_pnl") == 0.0


def test_mark_loop_stopped_for_display_preserves_cached_data():
    live_service._live_state_update(
        broker="ctrader",
        loop_running=True,
        loop_strategy="carry",
        account={"ok": True, "balance": 999.0},
    )

    live_service._mark_loop_stopped_for_display()

    assert live_service._live_state_get("loop_running") is False
    assert live_service._live_state_get("loop_strategy") is None
    assert live_service._live_state_get("broker") == "ctrader"
    assert live_service._live_state_get("account", clone=True)["balance"] == 999.0


def test_protection_prices_from_reference_use_direction_and_digits():
    assert live_service._protection_prices_from_reference(1, 4000.123, 10.0, 15.0, 2) == (3990.12, 4015.12)
    assert live_service._protection_prices_from_reference(-1, 4000.123, 10.0, 15.0, 2) == (4010.12, 3985.12)


def test_position_open_price_accepts_dict_and_object_payloads():
    assert live_service._position_open_price({"entry_price": 4008.5}) == 4008.5
    assert live_service._position_open_price(SimpleNamespace(open_price=4010.25)) == 4010.25
    assert live_service._position_open_price({"entry_price": None, "price": 3999.0}) == 3999.0


def test_record_filled_open_context_persists_even_before_amend_success(monkeypatch):
    calls = {"orders": [], "positions": [], "upserts": []}

    class _Ledger:
        def log_composite_decision(self, **kwargs):
            calls["decision"] = kwargs
            return "dec_open"

        def log_order_event(self, **kwargs):
            calls["orders"].append(kwargs)

        def log_position_event(self, **kwargs):
            calls["positions"].append(kwargs)

    class _Attr:
        def record_open(self, pid, trade_attr):
            calls["attr"] = (pid, trade_attr)

    composite = SimpleNamespace(
        direction=-1,
        score=-0.7,
        tactical_score=-0.8,
        macro_score=0.0,
        factor_signals={"rsi": -0.5},
        factor_values={"rsi": 70.0},
        active_weights={"rsi": 0.5},
        tags_breakdown={},
        n_active_factors=1,
        n_abstain_factors=0,
    )
    gate = SimpleNamespace(passed=True, reason="passed")
    cfg = SimpleNamespace(timeframe="M5")
    risk_verdict = SimpleNamespace(
        to_dict=lambda: {
            "allowed": True,
            "reason": "ok",
            "audit_payload": {"action": "open_trade"},
        }
    )

    monkeypatch.setattr(live_service, "_LEDGER", _Ledger())
    monkeypatch.setattr(
        live_service,
        "_upsert_recovery_position_state",
        lambda raw, **kwargs: calls["upserts"].append((raw, kwargs)),
    )

    decision_id = live_service._record_filled_position_open_context(
        attr_engine=_Attr(),
        broker="ctrader",
        cfg=cfg,
        bar={"time": 123.0},
        tick=7,
        pid=268,
        actual_api_volume=100.0,
        requested_volume=100.0,
        fill_price=4008.5,
        current_price=4008.4,
        sl_price=4012.5,
        tp_price=3994.5,
        acct={"balance": 10000, "equity": 10001},
        pos=[],
        composite=composite,
        gate_result=gate,
        risk_verdict=risk_verdict,
    )

    assert decision_id == "dec_open"
    assert calls["decision"]["event_type"] == "open"
    assert calls["decision"]["risk_state"]["policy_verdict"]["allowed"] is True
    assert calls["decision"]["action_json"]["risk_verdict"]["audit_payload"]["action"] == "open_trade"
    assert [item["event_type"] for item in calls["orders"]] == ["submitted", "filled"]
    assert calls["positions"][0]["event_type"] == "opened"
    assert calls["upserts"][0][0]["entry_decision_id"] == "dec_open"
    assert calls["attr"][0] == 268


def test_emergency_close_evaluates_and_remembers_close_verdict(monkeypatch):
    calls = []
    close_calls = []

    class _Policy:
        def evaluate(self, action, context):
            calls.append((action, context))
            return SimpleNamespace(
                allowed=True,
                reason="risk_reducing_action",
                to_dict=lambda: {
                    "allowed": True,
                    "reason": "risk_reducing_action",
                    "audit_payload": {
                        "action": action,
                        "position_id": context["position_id"],
                        "close_reason": context["close_reason"],
                    },
                },
            )

    class _Bridge:
        is_connected = True

        def refresh_positions(self, *, force=False, allow_cache_fallback=True):
            self.refresh_args = {
                "force": force,
                "allow_cache_fallback": allow_cache_fallback,
            }
            return [{"position_id": 268, "symbol": "XAUUSD+", "volume": 100.0}]

        def close_position(self, pid, volume=0.0):
            close_calls.append((pid, volume))
            return SimpleNamespace(success=True, position_id=pid)

    bridge = _Bridge()
    monkeypatch.setattr(live_service, "_RISK_POLICY", _Policy())
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))

    result = live_service.emergency_close("ctrader", "XAUUSD+")

    assert result["ok"] is True
    assert result["attempted"] == 1
    assert result["closed"] == 1
    assert result["failed"] == 0
    assert bridge.refresh_args == {"force": True, "allow_cache_fallback": False}
    assert close_calls == [(268, 100.0)]
    assert calls[0][0] == "close_position"
    assert calls[0][1]["close_reason"] == "emergency_close"
    assert live_service._consume_close_reason(268) == "emergency_close"
    verdict = live_service._consume_close_verdict(268, "emergency_close")
    assert verdict["allowed"] is True
    assert verdict["audit_payload"]["action"] == "close_position"


def test_emergency_close_reports_close_failures(monkeypatch):
    class _Policy:
        def evaluate(self, action, context):
            return SimpleNamespace(
                allowed=True,
                reason="risk_reducing_action",
                to_dict=lambda: {"allowed": True, "reason": "risk_reducing_action"},
            )

    class _Bridge:
        is_connected = True

        def refresh_positions(self, *, force=False, allow_cache_fallback=True):
            return [{"position_id": 269, "symbol": "XAUUSD+", "volume": 100.0}]

        def close_position(self, pid, volume=0.0):
            return SimpleNamespace(
                success=False,
                position_id=pid,
                error_code="TRADING_BAD_VOLUME",
                comment="close rejected",
            )

    monkeypatch.setattr(live_service, "_RISK_POLICY", _Policy())
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (_Bridge(), None, False))

    result = live_service.emergency_close("ctrader", "XAUUSD+")

    assert result["ok"] is False
    assert result["attempted"] == 1
    assert result["closed"] == 0
    assert result["failed"] == 1
    assert result["failures"][0]["position_id"] == 269
    assert result["failures"][0]["error_code"] == "TRADING_BAD_VOLUME"


def test_upsert_recovery_position_state_preserves_valid_volume_on_zero_snapshot(monkeypatch, tmp_path):
    from backend.core import db as db_module

    db_path = tmp_path / "state.db"

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    conn = _conn()
    try:
        conn.executescript(db_module.STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(live_service, "_get_state_conn", _conn)
    monkeypatch.setattr(live_service, "_lookup_entry_decision_id", lambda position_id: "dec_open")

    live_service._upsert_recovery_position_state(
        {"position_id": 270, "symbol": "XAUUSD+", "direction": 1, "open_price": 4050.0, "volume": 100.0},
        broker="ctrader",
        strategy_name="factor_v4",
        status="open",
    )
    live_service._upsert_recovery_position_state(
        {"position_id": 270, "symbol": "XAUUSD+", "direction": 1, "open_price": 4051.0, "volume": 0.0},
        broker="ctrader",
        strategy_name="factor_v4",
        status="open",
    )

    conn = _conn()
    try:
        row = conn.execute("SELECT volume, open_price FROM recovery_position_state WHERE position_id=270").fetchone()
    finally:
        conn.close()

    assert row["volume"] == pytest.approx(100.0)
    assert row["open_price"] == pytest.approx(4051.0)


def test_recovery_bootstrap_reconciles_persisted_positions_after_confirmed_broker_zero(monkeypatch, tmp_path):
    from backend.core import db as db_module
    import execution.deal_sync as deal_sync_module

    db_path = tmp_path / "state.db"

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

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
                301,
                "ctrader",
                "XAUUSD+",
                1,
                4060.0,
                100.0,
                1000.0,
                2000.0,
                "open",
                "factor_v4",
                "dec_open",
                "full",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    class _Bridge:
        is_connected = True

        def __init__(self):
            self._last_reconcile_at = 0.0
            self.calls = []

        def refresh_positions(self, *, force=False, allow_cache_fallback=True):
            self.calls.append((force, allow_cache_fallback))
            self._last_reconcile_at += 1.0
            return []

    bridge = _Bridge()
    logs = []
    live_service._live_state_update(positions=[{"position_id": 301, "volume": 0.0}], positions_updated_at=time.time())
    monkeypatch.setattr(live_service, "_get_state_conn", _conn)
    monkeypatch.setattr(live_service, "_LEDGER", None)
    monkeypatch.setattr(deal_sync_module, "sync_close_deals_batch", lambda *args, **kwargs: {})

    first = live_service._bootstrap_position_recovery(
        bridge,
        broker="ctrader",
        strategy_name="factor_v4",
        log=logs.append,
    )
    second = live_service._bootstrap_position_recovery(
        bridge,
        broker="ctrader",
        strategy_name="factor_v4",
        log=logs.append,
    )

    conn = _conn()
    try:
        row = conn.execute("SELECT status, close_reason FROM recovery_position_state WHERE position_id=301").fetchone()
    finally:
        conn.close()

    assert first is False
    assert second is True
    assert bridge.calls == [(True, False), (True, False)]
    assert live_service._live_state_get("positions", clone=True) == []
    assert row["status"] == "closed_replayed"
    assert row["close_reason"] == "restart_replay"
    assert any("confirmation 1/2" in item for item in logs)
    assert any("reconciled 1 persisted positions as closed" in item for item in logs)


def test_build_open_trade_risk_context_includes_runtime_health(monkeypatch):
    class _SyncHealth:
        def snapshot(self):
            return {"fresh": False, "stale": True, "degraded": True}

        def last_bar_age_seconds(self, timeframe):
            assert timeframe == "M5"
            return 321.0

    class _Bridge:
        is_connected = False

    class _Component:
        def __init__(self, status):
            self.status = status

    class _SystemHealth:
        def get_last_report(self):
            return SimpleNamespace(
                overall="critical",
                overall_score=0.8,
                components={
                    "l2_depth": _Component("critical"),
                    "disk_space": _Component("degraded"),
                },
            )

    now = time.time()
    live_service._live_state_update(
        loop_running=True,
        account_updated_at=now - 12,
        positions_updated_at=now - 34,
    )

    import data.live_sync.health as sync_health_module

    monkeypatch.setattr(sync_health_module.SyncHealth, "shared", staticmethod(lambda: _SyncHealth()))
    import monitor.system_health as system_health_module

    monkeypatch.setattr(system_health_module, "shared", staticmethod(lambda: _SystemHealth()))

    ctx = live_service._build_open_trade_risk_context(
        cfg=SimpleNamespace(
            timeframe="M5",
            var_enabled=True,
            var_cvar_threshold=0.02,
            risk_loss_cooldown_after_losses=2,
            risk_loss_cooldown_bars=3,
            risk_block_on_disk_critical=True,
            risk_require_l2_depth=False,
            max_position_count=3,
            max_position_api_volume=1000.0,
            pyramid_enabled=True,
        ),
        bridge=_Bridge(),
        acct={"balance": 10000, "equity": 10000},
        positions=[],
        requested_api_volume=100.0,
        signal_score=0.6,
    )

    assert ctx["bridge_connected"] is False
    assert ctx["loop_running"] is True
    assert ctx["data_lag_seconds"] == 321.0
    assert ctx["loss_cooldown_after_losses"] == 2
    assert ctx["loss_cooldown_bars"] == 3
    assert ctx["temporal_context"]["timeframe"] == "M5"
    assert "session_label" in ctx["temporal_context"]
    assert ctx["runtime_health"]["sync_health"]["degraded"] is True
    assert ctx["runtime_health"]["system_health"]["overall"] == "critical"
    assert "l2_depth" in ctx["runtime_health"]["system_health"]["critical_components"]
    assert ctx["runtime_health"]["account_cache_age_seconds"] >= 10.0
    assert ctx["runtime_health"]["positions_cache_age_seconds"] >= 30.0


def test_recovered_close_repairs_missing_open_ledger(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(live_service, "_get_state_conn", _conn)
    monkeypatch.setattr(live_service, "_LEDGER", ledger)

    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO recovery_position_state
            (position_id, broker, symbol, direction, open_price, volume,
             first_seen_at, last_seen_at, status, strategy_name,
             entry_decision_id, context_integrity, recovery_meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                268046003,
                "ctrader",
                "XAUUSD+",
                1,
                4015.92,
                100.0,
                1_782_373_400.0,
                1_782_373_500.0,
                "open",
                "smoke",
                "",
                "partial",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    decision_id = live_service._ensure_open_ledger_for_recovered_close(
        268046003,
        broker="ctrader",
        close_ts=1_782_373_646.154,
        close_price=3980.89,
        real_pnl={"net": 36.52, "entry_price": 4015.92},
        close_reason="broker_close",
    )

    rows = []
    conn = _conn()
    try:
        rows = list(
            conn.execute(
                "SELECT * FROM decision_ledger WHERE position_id='268046003' AND event_type='open'"
            )
        )
        recovery = conn.execute(
            "SELECT entry_decision_id, context_integrity FROM recovery_position_state WHERE position_id=268046003"
        ).fetchone()
    finally:
        conn.close()

    assert decision_id
    assert len(rows) == 1
    assert rows[0]["action_reason"] == "live_close_open_repair"
    assert recovery["entry_decision_id"] == decision_id
    assert recovery["context_integrity"] == "partial"


def test_build_close_position_risk_context_marks_timeout(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(live_service, "_get_state_conn", _conn)

    open_ts = time.time() - 3900.0
    ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="268",
        position_id="268",
        decision_ts=open_ts,
        portfolio_state={},
        risk_state={},
        action_score=0.0,
        action_reason="test_open",
        action_json={},
    )

    ctx = live_service._build_close_position_risk_context(
        position_id=268,
        close_reason="holding_timeout",
        cfg=SimpleNamespace(timeframe="M5", risk_max_holding_bars=12),
        decision_ts=open_ts + 3900.0,
    )

    assert ctx["entry_ts_source"] == "decision_ledger"
    assert ctx["holding_seconds"] == pytest.approx(3900.0)
    assert ctx["max_holding_seconds"] == pytest.approx(3600.0)


def test_holding_summary_for_position_reports_watch_status(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(live_service, "_get_state_conn", _conn)

    open_ts = time.time() - 3000.0
    ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="9001",
        position_id="9001",
        decision_ts=open_ts,
        portfolio_state={},
        risk_state={},
        action_score=0.0,
        action_reason="test_open",
        action_json={},
    )

    summary = live_service._holding_summary_for_position(
        {"position_id": 9001, "symbol": "XAUUSD+", "open_time": open_ts},
        cfg=SimpleNamespace(timeframe="M5", risk_max_holding_bars=12),
        now_ts=open_ts + 3000.0,
    )

    assert summary["timeout_enabled"] is True
    assert summary["holding_timeout_status"] == "watch"
    assert summary["holding_timeout_exceeded"] is False
    assert summary["holding_timeout_remaining_seconds"] == pytest.approx(600.0)


def test_position_path_metrics_tracks_mfe_giveback_and_time_in_profit(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(live_service, "_get_state_conn", _conn)

    open_ts = time.time() - 1200.0
    ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="9002",
        position_id="9002",
        decision_ts=open_ts,
        portfolio_state={},
        risk_state={},
        action_score=0.0,
        action_reason="test_open",
        action_json={},
    )

    first = live_service._position_path_metrics_for_position(
        {"position_id": 9002, "symbol": "XAUUSD+", "open_time": open_ts, "profit": 80.0},
        cfg=SimpleNamespace(timeframe="M5", risk_max_holding_bars=12),
        now_ts=open_ts + 600.0,
        persist=True,
        broker="ctrader",
        strategy_name="factor_v4",
    )
    second = live_service._position_path_metrics_for_position(
        {"position_id": 9002, "symbol": "XAUUSD+", "open_time": open_ts, "profit": 20.0},
        cfg=SimpleNamespace(timeframe="M5", risk_max_holding_bars=12),
        now_ts=open_ts + 1200.0,
        persist=True,
        broker="ctrader",
        strategy_name="factor_v4",
    )

    assert first["mfe"] == pytest.approx(80.0)
    assert second["mfe"] == pytest.approx(80.0)
    assert second["giveback_ratio"] == pytest.approx(0.75)
    assert second["profit_capture_ratio"] == pytest.approx(0.25)
    assert second["time_in_profit"] == pytest.approx(600.0)
    assert second["thesis_status"] == "weakening"
