import json
import sqlite3
from types import SimpleNamespace

from backend.api import risk as risk_api


def test_recent_policy_verdicts_summarizes_decision_ledger(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE decision_ledger (
            decision_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            symbol TEXT DEFAULT '',
            timeframe TEXT DEFAULT '',
            decision_ts REAL NOT NULL DEFAULT 0.0,
            action_reason TEXT DEFAULT '',
            action_json TEXT DEFAULT '{}',
            risk_state_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO decision_ledger
        (decision_id, event_type, symbol, timeframe, decision_ts, action_reason, action_json, risk_state_json, created_at)
        VALUES (?, ?, 'XAUUSD+', 'M5', ?, ?, ?, ?, ?)
        """,
        [
            (
                "dec_allowed",
                "open",
                200.0,
                "executed",
                json.dumps({"risk_verdict": {"allowed": True, "reason": "ok", "audit_payload": {"action": "open_trade"}}}),
                "{}",
                200.0,
            ),
            (
                "dec_blocked",
                "skip",
                100.0,
                "仓位上限: 3/3",
                "{}",
                json.dumps({"policy_verdict": {"allowed": False, "reason": "仓位上限: 3/3", "audit_payload": {"action": "open_trade"}}}),
                100.0,
            ),
        ],
    )
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)

    result = risk_api._recent_policy_verdicts(limit=10)

    assert result["total"] == 2
    assert result["counts"] == {"allowed": 1, "blocked": 1}
    assert result["by_action"] == {"open_trade": 2}
    assert result["by_reason"]["ok"] == 1
    assert result["items"][0]["decision_id"] == "dec_allowed"


def test_system_health_summary_serializes_latest_report(monkeypatch):
    class _Component:
        def __init__(self, status, detail, score):
            self.status = status
            self.detail = detail
            self.score = score

    class _SystemHealth:
        def get_last_report(self):
            return SimpleNamespace(
                overall="critical",
                overall_score=0.8,
                ts=123.0,
                errors=["disk low"],
                components={
                    "disk_space": _Component("critical", "3.2 GB left", 0.0),
                    "l2_depth": _Component("degraded", "5 min stale", 0.5),
                },
            )

    monkeypatch.setattr(risk_api, "_get_system_health_report", lambda: _SystemHealth().get_last_report())
    monkeypatch.setattr(
        risk_api,
        "_runtime_risk_policy",
        lambda: {"require_l2_depth": False, "block_on_disk_critical": True},
    )
    result = risk_api._system_health_summary()

    assert result["overall"] == "critical"
    assert result["overall_score"] == 0.8
    assert result["critical_components"] == ["disk_space"]
    assert result["degraded_components"] == ["l2_depth"]
    assert result["blocking_components"] == ["disk_space"]
    assert result["advisory_critical_components"] == []
    assert result["trading_blocked"] is True
    assert result["impact_status"] == "blocked"
    assert result["components"]["disk_space"]["detail"] == "3.2 GB left"


def test_system_health_summary_marks_optional_l2_as_observe_only(monkeypatch):
    class _Component:
        def __init__(self, status, detail, score):
            self.status = status
            self.detail = detail
            self.score = score

    monkeypatch.setattr(
        risk_api,
        "_get_system_health_report",
        lambda: SimpleNamespace(
            overall="critical",
            overall_score=0.75,
            ts=456.0,
            errors=[],
            components={
                "l2_depth": _Component("critical", "15 min stale", 0.0),
                "disk_space": _Component("degraded", "18 GB left", 0.6),
            },
        ),
    )
    monkeypatch.setattr(
        risk_api,
        "_runtime_risk_policy",
        lambda: {"require_l2_depth": False, "block_on_disk_critical": True},
    )

    result = risk_api._system_health_summary()

    assert result["critical_components"] == ["l2_depth"]
    assert result["blocking_components"] == []
    assert result["advisory_critical_components"] == ["l2_depth"]
    assert result["trading_blocked"] is False
    assert result["impact_status"] == "observe"
    assert "不会直接阻断交易" in result["impact_summary"]


def test_trade_trace_collects_ledger_review_and_lifecycle(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE decision_ledger (
            decision_id TEXT PRIMARY KEY,
            trade_id TEXT DEFAULT '',
            position_id TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            symbol TEXT DEFAULT '',
            timeframe TEXT DEFAULT '',
            decision_ts REAL NOT NULL DEFAULT 0.0,
            regime_id TEXT DEFAULT '',
            regime_confidence REAL DEFAULT 0.0,
            portfolio_state_json TEXT DEFAULT '{}',
            risk_state_json TEXT DEFAULT '{}',
            policy_version TEXT DEFAULT '',
            factor_set_version TEXT DEFAULT '',
            action_score REAL DEFAULT 0.0,
            action_reason TEXT DEFAULT '',
            action_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE position_lifecycle_event (
            event_id TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            trade_id TEXT DEFAULT '',
            symbol TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            event_ts REAL NOT NULL DEFAULT 0.0,
            net_volume REAL DEFAULT 0.0,
            avg_price REAL DEFAULT 0.0,
            unrealized_pnl REAL DEFAULT 0.0,
            realized_pnl REAL DEFAULT 0.0,
            details_json TEXT DEFAULT '{}'
        );
        CREATE TABLE order_lifecycle_event (
            event_id TEXT PRIMARY KEY,
            decision_id TEXT DEFAULT '',
            trade_id TEXT DEFAULT '',
            order_id TEXT DEFAULT '',
            broker_order_id TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            event_ts REAL NOT NULL DEFAULT 0.0,
            price REAL DEFAULT 0.0,
            volume REAL DEFAULT 0.0,
            status TEXT DEFAULT '',
            details_json TEXT DEFAULT '{}'
        );
        CREATE TABLE trade_outcome_review (
            review_id TEXT PRIMARY KEY,
            trade_id TEXT DEFAULT '',
            position_id TEXT DEFAULT '',
            entry_decision_id TEXT DEFAULT '',
            exit_decision_id TEXT DEFAULT '',
            entry_quality REAL DEFAULT 0.0,
            hold_quality REAL DEFAULT 0.0,
            exit_quality REAL DEFAULT 0.0,
            regime_fit_score REAL DEFAULT 0.0,
            execution_quality REAL DEFAULT 0.0,
            pnl REAL DEFAULT 0.0,
            mae REAL DEFAULT 0.0,
            mfe REAL DEFAULT 0.0,
            outcome_label TEXT DEFAULT '',
            failure_tags_json TEXT DEFAULT '[]',
            summary_text TEXT DEFAULT '',
            review_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE factor_contribution_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id TEXT NOT NULL,
            trade_id TEXT DEFAULT '',
            factor TEXT NOT NULL,
            entry_contribution REAL DEFAULT 0.0,
            hold_contribution REAL DEFAULT 0.0,
            exit_contribution REAL DEFAULT 0.0,
            net_contribution REAL DEFAULT 0.0,
            confidence REAL DEFAULT 0.0,
            notes TEXT DEFAULT ''
        );
        CREATE TABLE recovery_position_state (
            position_id INTEGER PRIMARY KEY,
            broker TEXT DEFAULT 'ctrader',
            symbol TEXT DEFAULT '',
            direction INTEGER DEFAULT 0,
            open_price REAL DEFAULT 0.0,
            volume REAL DEFAULT 0.0,
            first_seen_at REAL DEFAULT 0.0,
            last_seen_at REAL DEFAULT 0.0,
            status TEXT DEFAULT 'open',
            strategy_name TEXT DEFAULT '',
            entry_decision_id TEXT DEFAULT '',
            context_integrity TEXT DEFAULT 'full',
            recovery_meta_json TEXT DEFAULT '{}',
            closed_at REAL DEFAULT 0.0,
            close_reason TEXT DEFAULT '',
            close_pnl REAL DEFAULT 0.0
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO decision_ledger
        (decision_id, trade_id, position_id, event_type, symbol, timeframe, decision_ts,
         portfolio_state_json, risk_state_json, action_score, action_reason, action_json, created_at)
        VALUES (?, ?, ?, ?, 'XAUUSD+', 'M5', ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "dec_open",
                "9001",
                "9001",
                "open",
                100.0,
                json.dumps({"equity": 10000}),
                json.dumps({"policy_verdict": {"allowed": True, "reason": "ok"}}),
                0.7,
                "opened",
                json.dumps({"side": "sell"}),
                100.0,
            ),
            (
                "dec_close",
                "9001",
                "9001",
                "close",
                200.0,
                "{}",
                json.dumps({"policy_verdict": {"allowed": True, "reason": "manual_close"}}),
                0.0,
                "manual_close",
                json.dumps({"close_reason": "broker_close"}),
                200.0,
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO position_lifecycle_event
        (event_id, position_id, trade_id, symbol, event_type, event_ts, net_volume, avg_price, realized_pnl, details_json)
        VALUES ('pos_evt_close', '9001', '9001', 'XAUUSD+', 'closed', 200.0, 0, 3988.2, 12.5, ?)
        """,
        (json.dumps({"reason": "broker_close"}),),
    )
    conn.execute(
        """
        INSERT INTO order_lifecycle_event
        (event_id, decision_id, trade_id, order_id, broker_order_id, event_type, event_ts, price, volume, status, details_json)
        VALUES ('ord_evt_fill', 'dec_open', '9001', 'ord-1', 'brk-1', 'filled', 101.0, 3992.0, 100, 'filled', ?)
        """,
        (json.dumps({"sl": 4020.0, "tp": 3960.0}),),
    )
    conn.execute(
        """
        INSERT INTO trade_outcome_review
        (review_id, trade_id, position_id, entry_decision_id, exit_decision_id, pnl, outcome_label,
         failure_tags_json, summary_text, review_json, created_at)
        VALUES ('rev_1', '9001', '9001', 'dec_open', 'dec_close', 12.5, 'win', ?, '手动平仓已记录', ?, 220.0)
        """,
        (
            json.dumps(["manual"]),
            json.dumps({"close_reason": "broker_close", "real_pnl": {"net": 12.5}}),
        ),
    )
    conn.executemany(
        """
        INSERT INTO factor_contribution_review
        (review_id, trade_id, factor, entry_contribution, hold_contribution, exit_contribution, net_contribution, confidence, notes)
        VALUES ('rev_1', '9001', ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("macd_hist", 0.4, 0.2, 0.0, 0.6, 0.8, "helpful"),
            ("rsi_14", -0.1, 0.0, 0.0, -0.1, 0.5, "late"),
        ],
    )
    conn.execute(
        """
        INSERT INTO recovery_position_state
        (position_id, broker, symbol, direction, open_price, volume, status, entry_decision_id, context_integrity, recovery_meta_json, close_reason, close_pnl)
        VALUES (9001, 'ctrader', 'XAUUSD+', -1, 3992.0, 100, 'closed', 'dec_open', 'full', ?, 'broker_close', 12.5)
        """,
        (json.dumps({"source": "replay"}),),
    )
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)

    result = risk_api._trade_trace(position_id="9001")

    assert result["summary"]["position_id"] == "9001"
    assert result["summary"]["ledger_events"] == 2
    assert result["summary"]["position_events"] == 1
    assert result["summary"]["order_events"] == 1
    assert result["summary"]["has_review"] is True
    assert result["summary"]["latest_close_reason"] == "broker_close"
    assert result["decision_ledger"][0]["risk_state"]["policy_verdict"]["reason"] == "ok"
    assert result["order_lifecycle"][0]["details"]["sl"] == 4020.0
    assert result["review"]["failure_tags"] == ["manual"]
    assert result["factor_contributions"][0]["factor"] == "macd_hist"
    assert result["recovery_state"]["recovery_meta"]["source"] == "replay"


def test_trade_trace_resolves_from_decision_id(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE decision_ledger (
            decision_id TEXT PRIMARY KEY,
            trade_id TEXT DEFAULT '',
            position_id TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            symbol TEXT DEFAULT '',
            timeframe TEXT DEFAULT '',
            decision_ts REAL NOT NULL DEFAULT 0.0,
            regime_id TEXT DEFAULT '',
            regime_confidence REAL DEFAULT 0.0,
            portfolio_state_json TEXT DEFAULT '{}',
            risk_state_json TEXT DEFAULT '{}',
            policy_version TEXT DEFAULT '',
            factor_set_version TEXT DEFAULT '',
            action_score REAL DEFAULT 0.0,
            action_reason TEXT DEFAULT '',
            action_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE position_lifecycle_event (
            event_id TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            trade_id TEXT DEFAULT '',
            symbol TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            event_ts REAL NOT NULL DEFAULT 0.0,
            net_volume REAL DEFAULT 0.0,
            avg_price REAL DEFAULT 0.0,
            unrealized_pnl REAL DEFAULT 0.0,
            realized_pnl REAL DEFAULT 0.0,
            details_json TEXT DEFAULT '{}'
        );
        CREATE TABLE order_lifecycle_event (
            event_id TEXT PRIMARY KEY,
            decision_id TEXT DEFAULT '',
            trade_id TEXT DEFAULT '',
            order_id TEXT DEFAULT '',
            broker_order_id TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            event_ts REAL NOT NULL DEFAULT 0.0,
            price REAL DEFAULT 0.0,
            volume REAL DEFAULT 0.0,
            status TEXT DEFAULT '',
            details_json TEXT DEFAULT '{}'
        );
        CREATE TABLE trade_outcome_review (
            review_id TEXT PRIMARY KEY,
            trade_id TEXT DEFAULT '',
            position_id TEXT DEFAULT '',
            entry_decision_id TEXT DEFAULT '',
            exit_decision_id TEXT DEFAULT '',
            entry_quality REAL DEFAULT 0.0,
            hold_quality REAL DEFAULT 0.0,
            exit_quality REAL DEFAULT 0.0,
            regime_fit_score REAL DEFAULT 0.0,
            execution_quality REAL DEFAULT 0.0,
            pnl REAL DEFAULT 0.0,
            mae REAL DEFAULT 0.0,
            mfe REAL DEFAULT 0.0,
            outcome_label TEXT DEFAULT '',
            failure_tags_json TEXT DEFAULT '[]',
            summary_text TEXT DEFAULT '',
            review_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE factor_contribution_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id TEXT NOT NULL,
            trade_id TEXT DEFAULT '',
            factor TEXT NOT NULL,
            entry_contribution REAL DEFAULT 0.0,
            hold_contribution REAL DEFAULT 0.0,
            exit_contribution REAL DEFAULT 0.0,
            net_contribution REAL DEFAULT 0.0,
            confidence REAL DEFAULT 0.0,
            notes TEXT DEFAULT ''
        );
        CREATE TABLE recovery_position_state (
            position_id INTEGER PRIMARY KEY,
            broker TEXT DEFAULT 'ctrader',
            symbol TEXT DEFAULT '',
            direction INTEGER DEFAULT 0,
            open_price REAL DEFAULT 0.0,
            volume REAL DEFAULT 0.0,
            first_seen_at REAL DEFAULT 0.0,
            last_seen_at REAL DEFAULT 0.0,
            status TEXT DEFAULT 'open',
            strategy_name TEXT DEFAULT '',
            entry_decision_id TEXT DEFAULT '',
            context_integrity TEXT DEFAULT 'full',
            recovery_meta_json TEXT DEFAULT '{}',
            closed_at REAL DEFAULT 0.0,
            close_reason TEXT DEFAULT '',
            close_pnl REAL DEFAULT 0.0
        );
        INSERT INTO decision_ledger
        (decision_id, trade_id, position_id, event_type, symbol, timeframe, decision_ts, action_reason, action_json, created_at)
        VALUES ('dec_only', '3003', '3003', 'open', 'XAUUSD+', 'M5', 10.0, 'opened', '{}', 10.0);
        """
    )
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)

    result = risk_api._trade_trace(decision_id="dec_only")

    assert result["summary"]["decision_id"] == "dec_only"
    assert result["summary"]["position_id"] == "3003"
    assert result["decision_ledger"][0]["decision_id"] == "dec_only"
