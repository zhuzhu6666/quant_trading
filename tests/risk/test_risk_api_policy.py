import json
import sqlite3
from types import SimpleNamespace
import json

from backend.api import risk as risk_api


def test_recent_policy_verdicts_summarizes_decision_ledger(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE decision_ledger (
            decision_id TEXT PRIMARY KEY,
            position_id TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            symbol TEXT DEFAULT '',
            timeframe TEXT DEFAULT '',
            decision_ts REAL NOT NULL DEFAULT 0.0,
            action_reason TEXT DEFAULT '',
            action_json TEXT DEFAULT '{}',
            risk_state_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE position_supervisor_trace (
            trace_id TEXT PRIMARY KEY,
            decision_id TEXT DEFAULT '',
            stage TEXT DEFAULT '',
            outcome TEXT DEFAULT '',
            execution_status TEXT DEFAULT '',
            execution_reason TEXT DEFAULT '',
            event_ts REAL NOT NULL DEFAULT 0.0,
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE recovery_position_state (
            position_id TEXT PRIMARY KEY,
            direction INTEGER DEFAULT 0
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO decision_ledger
        (decision_id, position_id, event_type, symbol, timeframe, decision_ts,
         action_reason, action_json, risk_state_json, created_at)
        VALUES (?, ?, ?, 'XAUUSD+', 'M5', ?, ?, ?, ?, ?)
        """,
        [
            (
                "dec_allowed",
                "284214987",
                "open",
                200.0,
                "executed",
                json.dumps({"risk_verdict": {"allowed": True, "reason": "ok", "audit_payload": {"action": "open_trade"}}}),
                "{}",
                200.0,
            ),
            (
                "dec_skipped",
                "",
                "supervisor_reduce",
                150.0,
                "risk_reducing_action",
                json.dumps({"risk_verdict": {"allowed": True, "reason": "risk_reducing_action", "audit_payload": {"action": "reduce_position"}}}),
                "{}",
                150.0,
            ),
            (
                "dec_blocked",
                "",
                "skip",
                100.0,
                "仓位上限: 3/3",
                "{}",
                json.dumps({"policy_verdict": {"allowed": False, "reason": "仓位上限: 3/3", "audit_payload": {"action": "open_trade"}}}),
                100.0,
            ),
            (
                "dec_pre_policy",
                "",
                "skip",
                175.0,
                "no_new_risk_latched",
                json.dumps({
                    "tick": 7,
                    "direction": 1,
                    "gate_passed": True,
                    "gate_reason": "passed",
                    "skip_stage": "before_candidate",
                    "risk_stage": "not_reached",
                    "risk_policy_reached": False,
                    "admission_gate_passed": False,
                    "blockers": ["no_new_risk_latched", "accepting_new_risk_false"],
                    "execution_intent_created": False,
                    "action_reason": "no_new_risk_latched",
                }),
                "{}",
                175.0,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO recovery_position_state(position_id, direction) VALUES (?, ?)",
        ("284214987", 1),
    )
    conn.executemany(
        """
        INSERT INTO position_supervisor_trace
        (trace_id, decision_id, stage, outcome, execution_status,
         execution_reason, event_ts, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "trace-applied",
                "dec_allowed",
                "executed",
                "applied",
                "applied",
                "broker_confirmed",
                200.0,
                200.0,
            ),
            (
                "trace-skipped",
                "dec_skipped",
                "no_op_suppressed",
                "skipped",
                "no_op",
                "invalid_reduce_volume",
                150.0,
                150.0,
            ),
        ],
    )
    conn.commit()
    conn.close()

    recovery_params = []

    class _SpyConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "from recovery_position_state" in str(sql).lower():
                recovery_params.append(tuple(parameters))
            return super().execute(sql, parameters)

    def _conn():
        c = sqlite3.connect(str(db_path), factory=_SpyConnection)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)

    result = risk_api._recent_policy_verdicts(limit=10)

    assert result["total"] == 3
    assert result["counts"] == {"allowed": 2, "blocked": 1}
    assert result["execution_counts"] == {
        "applied": 1,
        "skipped": 1,
        "blocked": 0,
        "failed": 0,
        "unknown": 1,
    }
    assert result["by_action"] == {"open_trade": 2, "reduce_position": 1}
    assert result["by_reason"]["ok"] == 1
    assert len(result["pre_policy_skips"]) == 1
    assert result["pre_policy_skips"][0]["decision_id"] == "dec_pre_policy"
    assert result["pre_policy_skips"][0]["gate_passed"] is True
    assert result["pre_policy_skips"][0]["risk_policy_reached"] is False
    assert result["pre_policy_skips"][0]["admission_owner"] == "safety+live_loop"
    assert result["pre_policy_skips"][0]["blockers"] == [
        "no_new_risk_latched",
        "accepting_new_risk_false",
    ]
    assert result["items"][0]["decision_id"] == "dec_allowed"
    assert result["items"][0]["execution_applied"] is True
    assert result["items"][1]["decision_id"] == "dec_skipped"
    assert result["items"][1]["execution_category"] == "skipped"
    assert result["items"][1]["execution_reason"] == "invalid_reduce_volume"
    assert recovery_params == [("284214987",)]


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
                },
            )

    monkeypatch.setattr(risk_api, "_get_system_health_report", lambda: _SystemHealth().get_last_report())
    monkeypatch.setattr(
        risk_api,
        "_runtime_risk_policy",
        lambda: {"block_on_disk_critical": True},
    )
    result = risk_api._system_health_summary()

    assert result["overall"] == "critical"
    assert result["overall_score"] == 0.8
    assert result["critical_components"] == ["disk_space"]
    assert result["degraded_components"] == []
    assert result["blocking_components"] == ["disk_space"]
    assert result["advisory_critical_components"] == []
    assert result["trading_blocked"] is True
    assert result["impact_status"] == "blocked"
    assert result["components"]["disk_space"]["detail"] == "3.2 GB left"



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
            position_id TEXT PRIMARY KEY,
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
                150.0,
                "{}",
                json.dumps({"policy_verdict": {"allowed": True, "reason": "manual_close"}}),
                0.0,
                "manual_close",
                json.dumps({"close_reason": "broker_close"}),
                200.0,
            ),
            (
                "dec_supervisor_close",
                "9001",
                "9001",
                "supervisor_close",
                180.0,
                "{}",
                json.dumps({"policy_verdict": {"allowed": True, "reason": "risk_reducing_action"}}),
                0.91,
                "thesis_broken",
                json.dumps(
                    {
                        "supervisor_verdict": {
                            "action": "close",
                            "summary_reason": "thesis_broken",
                            "evidence": {"holding_efficiency": 0.08},
                            "recommended_controls": {"protection_mode": "full_exit"},
                        },
                        "risk_verdict": {"allowed": True, "reason": "risk_reducing_action"},
                    }
                ),
                180.0,
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
            ("macd_hist", 0.4, 0.2, 0.0, 0.6, 0.8, json.dumps({"source": "rule_review", "primary_responsibility": "exit", "responsibility_labels": ["entry_good_exit_bad"], "factor_role": "helpful"})),
            ("rsi_14", -0.1, 0.0, 0.0, -0.1, 0.5, json.dumps({"source": "rule_review", "primary_responsibility": "exit", "responsibility_labels": ["entry_good_exit_bad"], "factor_role": "harmful"})),
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

    recovery_params = []

    class _SpyConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "from recovery_position_state" in str(sql).lower():
                recovery_params.append(tuple(parameters))
            return super().execute(sql, parameters)

    def _conn():
        c = sqlite3.connect(str(db_path), factory=_SpyConnection)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)

    result = risk_api._trade_trace(position_id="9001")

    assert result["summary"]["position_id"] == "9001"
    assert result["summary"]["ledger_events"] == 3
    assert result["summary"]["position_events"] == 1
    assert result["summary"]["order_events"] == 1
    assert result["summary"]["has_review"] is True
    assert result["summary"]["latest_close_reason"] == "broker_close"
    assert result["summary"]["close_reason_source"] == "supervisor_inferred"
    assert result["summary"]["inferred_close_supervisor_action"] == "close"
    assert result["summary"]["inferred_close_supervisor_reason"] == "thesis_broken"
    assert result["position_supervisor"]["close_source"]["supervisor_decision_id"] == "dec_supervisor_close"
    assert result["position_supervisor"]["close_source"]["seconds_before_close"] == 20.0
    assert result["inferred_close_supervisor"]["decision_id"] == "dec_supervisor_close"
    assert result["decision_ledger"][0]["risk_state"]["policy_verdict"]["reason"] == "ok"
    assert result["order_lifecycle"][0]["details"]["sl"] == 4020.0
    assert result["review"]["failure_tags"] == ["manual"]
    assert result["factor_contributions"][0]["factor"] == "macd_hist"
    assert result["factor_contributions"][0]["primary_responsibility"] == "exit"
    assert result["factor_contributions"][0]["responsibility_labels"] == ["entry_good_exit_bad"]
    assert result["factor_contributions"][0]["factor_role"] == "helpful"
    assert result["recovery_state"]["recovery_meta"]["source"] == "replay"
    assert recovery_params == [("9001",)]
    assert result["parameter_governance"]["overview"]["show_stage_card"] is False
    assert result["parameter_governance"]["timeline_filter_context"]["focus_filters"]["all"]["summary_template"] == "当前证据链共 {count} 个事件。"


def test_recent_trade_trace_index_surfaces_recent_samples(monkeypatch, tmp_path):
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
        CREATE TABLE position_lifecycle_event (
            event_id TEXT PRIMARY KEY,
            position_id TEXT DEFAULT '',
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
        CREATE TABLE parameter_template_release_candidate (
            candidate_id TEXT PRIMARY KEY,
            factor_id TEXT NOT NULL,
            template_id TEXT NOT NULL,
            regime_key TEXT DEFAULT '',
            status TEXT DEFAULT 'pending_review',
            validation_summary_json TEXT DEFAULT '{}',
            validation_report_path TEXT DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0.0,
            updated_at REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO decision_ledger
        (decision_id, trade_id, position_id, event_type, symbol, timeframe, decision_ts, created_at)
        VALUES (?, ?, ?, 'close', 'XAUUSD+', 'M5', ?, ?)
        """,
        [
            ("dec_old", "8001", "8001", 100.0, 100.0),
            ("dec_new", "8002", "8002", 200.0, 200.0),
        ],
    )
    conn.executemany(
        """
        INSERT INTO position_lifecycle_event
        (event_id, position_id, trade_id, symbol, event_type, event_ts, details_json)
        VALUES (?, ?, ?, 'XAUUSD+', 'opened', ?, '{}')
        """,
        [
            ("posevt_old", "8001", "8001", 50.0),
            ("posevt_new", "8002", "8002", 150.0),
        ],
    )
    conn.executemany(
        """
        INSERT INTO trade_outcome_review
        (review_id, trade_id, position_id, entry_decision_id, exit_decision_id, pnl, outcome_label,
         failure_tags_json, summary_text, review_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "rev_old", "8001", "8001", "entry_old", "dec_old", 4.2, "win",
                json.dumps(["manual"]), "旧样本", json.dumps({"close_reason": "broker_close", "real_pnl": {"net": 4.2}}), 150.0,
            ),
            (
                "rev_new", "8002", "8002", "entry_new", "dec_new", -5.0, "bad_loss",
                json.dumps(["param_suspect"]), "新样本", json.dumps({
                    "close_reason": "thesis_broken",
                    "real_pnl": {"net": -5.0},
                    "primary_responsibility": "parameter",
                    "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
                    "failure_taxonomy": {
                        "primary_responsibility": "parameter",
                        "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
                    },
                }), 250.0,
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO factor_contribution_review
        (review_id, trade_id, factor, net_contribution, confidence, notes)
        VALUES ('rev_new', '8002', 'rsi_14', -0.7, 0.9, ?)
        """,
        (
            json.dumps(
                {
                    "primary_responsibility": "parameter",
                    "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
                }
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO parameter_template_release_candidate
        (candidate_id, factor_id, template_id, status, validation_summary_json, created_at, updated_at)
        VALUES ('ptrc_recent', 'rsi_14', 'rsi_14:conservative.v1:default', 'approved', '{}', 300.0, 310.0)
        """
    )
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)

    result = risk_api._recent_trade_trace_index(limit=5)

    assert result["count"] == 2
    assert result["items"][0]["review_id"] == "rev_new"
    assert result["items"][0]["position_id"] == "8002"
    assert result["items"][0]["primary_responsibility"] == "parameter"
    assert result["items"][0]["parameter_governance_factor"] == "rsi_14"
    assert result["items"][0]["parameter_candidate_status"] == "approved"
    assert result["items"][0]["parameter_candidate_id"] == "ptrc_recent"
    assert result["items"][0]["parameter_recommendation_id"] == ""
    assert result["items"][0]["parameter_governance_stage"] == "等待发布"
    assert "切到运行态" in result["items"][0]["parameter_governance_next_step"]
    assert result["items"][0]["parameter_governance_entry_type"] == "candidate"
    assert result["items"][0]["parameter_governance_target_type"] == "模板候选"
    assert result["items"][0]["parameter_governance_entry_hint_text"] == "建议先看模板候选"
    assert result["items"][0]["symbol"] == "XAUUSD+"


def test_trade_trace_includes_parameter_governance_context(monkeypatch, tmp_path):
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
        CREATE TABLE parameter_template_release_candidate (
            candidate_id TEXT PRIMARY KEY,
            factor_id TEXT NOT NULL,
            template_id TEXT NOT NULL,
            regime_key TEXT DEFAULT '',
            status TEXT DEFAULT 'pending_review',
            validation_summary_json TEXT DEFAULT '{}',
            validation_report_path TEXT DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0.0,
            updated_at REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    conn.execute(
        """
        INSERT INTO decision_ledger
        (decision_id, trade_id, position_id, event_type, symbol, timeframe, decision_ts,
         portfolio_state_json, risk_state_json, action_score, action_reason, action_json, created_at)
        VALUES ('dec_entry', '9101', '9101', 'open', 'XAUUSD+', 'M5', 100.0, '{}', '{}', 0.6, 'opened', '{}', 100.0)
        """
    )
    conn.execute(
        """
        INSERT INTO trade_outcome_review
        (review_id, trade_id, position_id, entry_decision_id, exit_decision_id, pnl, outcome_label,
         failure_tags_json, summary_text, review_json, created_at)
        VALUES ('rev_param', '9101', '9101', 'dec_entry', 'dec_exit', -8.5, 'bad_loss', ?, '参数疑似失配', ?, 220.0)
        """,
        (
            json.dumps(["param_suspect"]),
            json.dumps(
                {
                    "close_reason": "thesis_broken",
                    "real_pnl": {"net": -8.5},
                    "primary_responsibility": "parameter",
                    "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
                    "failure_taxonomy": {
                        "primary_responsibility": "parameter",
                        "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
                    },
                }
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO factor_contribution_review
        (review_id, trade_id, factor, entry_contribution, hold_contribution, exit_contribution, net_contribution, confidence, notes)
        VALUES ('rev_param', '9101', 'rsi_14', 0.2, -0.4, -0.3, -0.5, 0.88, ?)
        """,
        (
            json.dumps(
                {
                    "source": "rule_review",
                    "primary_responsibility": "parameter",
                    "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
                    "factor_role": "harmful",
                }
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO parameter_template_release_candidate
        (candidate_id, factor_id, template_id, regime_key, status, validation_summary_json, validation_report_path, created_at, updated_at)
        VALUES ('ptrc_param_9101', 'rsi_14', 'rsi_14:conservative.v1:default', '', 'approved', ?, '/tmp/report.json', 230.0, 240.0)
        """,
        (
            json.dumps(
                {
                    "recommendation_source": {
                        "source": "parameter_template_recommendation",
                        "recommendation_id": "ptr_rsi_9101",
                        "reason": "parameter drift observed",
                        "responsibility": {
                            "primary_responsibility": "parameter",
                            "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
                        },
                        "approval_path": "offline_validation_then_gray_release",
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)

    result = risk_api._trade_trace(position_id="9101")

    assert result["summary"]["parameter_governance_factor"] == "rsi_14"
    assert result["parameter_governance"]["factor_id"] == "rsi_14"
    assert result["parameter_governance"]["latest_candidate"]["candidate_id"] == "ptrc_param_9101"
    assert result["parameter_governance"]["latest_candidate"]["trace"]["recommendation_id"] == "ptr_rsi_9101"
    assert "参数治理对象是 rsi_14" in result["parameter_governance"]["ops_summary"]
    assert "先离线验证再灰度发布" in result["parameter_governance"]["ops_summary"]
    assert result["parameter_governance"]["overview"]["entry_hint_text"] == "建议入口：模板候选"
    assert result["parameter_governance"]["overview"]["latest_candidate_summary_text"] == "最新模板候选 ptrc_param_9101 · 已批准"
    assert result["parameter_governance"]["overview"]["show_stage_card"] is True
    assert result["parameter_governance"]["overview"]["priority_label"] == "优先发布"
    assert result["parameter_governance"]["timeline_filter_context"]["focus_filters"]["governance"]["label"] == "治理相关"
    governance_actions = result["parameter_governance"]["timeline_context"]["governance_actions"]
    assert governance_actions[0]["type"] == "offline_candidate"
    assert governance_actions[0]["candidate_id"] == "ptrc_param_9101"
    assert governance_actions[0]["factor_id"] == "rsi_14"
    assert governance_actions[0]["source"] == "trade_trace_timeline"
    assert any(item["type"] == "template_recommendation" and item["recommendation_id"] == "ptr_rsi_9101" for item in governance_actions)


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


def test_trade_trace_uses_position_when_decision_ledger_is_missing(monkeypatch, tmp_path):
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
        INSERT INTO trade_outcome_review
        (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
         outcome_label, summary_text, review_json, created_at)
        VALUES
        ('review_recent', 'trade_recent', '268728362', 'dec_entry_missing', 'dec_exit_missing',
         'acceptable_loss', '复盘记录已生成', '{"symbol":"XAUUSD+","close_reason":"thesis_invalid"}', 20.0);
        """
    )
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)

    result = risk_api._trade_trace(position_id="268728362", decision_id="dec_exit_missing")

    assert result["summary"]["position_id"] == "268728362"
    assert result["summary"]["decision_id"] == "dec_exit_missing"
    assert result["summary"]["has_review"] is True
    assert result["summary"]["latest_outcome"] == "acceptable_loss"
    assert result["review"]["review_id"] == "review_recent"
