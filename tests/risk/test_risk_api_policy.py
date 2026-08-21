import json
import sqlite3
from types import SimpleNamespace

from backend.api import risk as risk_api
from backend.core.db import STATE_DB_DDL
from backend.services.canonical_v2 import (
    record_decision_event,
    record_order_event,
    record_position_event,
    record_review,
    record_supervisor_trace_event,
)
from tests.canonical_fixture import make_canonical_sqlite


def _canonical_db(path):
    conn = make_canonical_sqlite(path)
    conn.executescript(STATE_DB_DDL)
    return conn


def _insert_factor_contribution(
    conn,
    *,
    review_id,
    trade_id,
    factor,
    entry_contribution=0.0,
    hold_contribution=0.0,
    exit_contribution=0.0,
    net_contribution=0.0,
    confidence=0.0,
    notes=None,
):
    conn.execute(
        """
        INSERT INTO factor_contribution_review
        (review_id, trade_id, factor, entry_contribution, hold_contribution,
         exit_contribution, net_contribution, confidence, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review_id,
            trade_id,
            factor,
            entry_contribution,
            hold_contribution,
            exit_contribution,
            net_contribution,
            confidence,
            json.dumps(notes or {}, ensure_ascii=False),
        ),
    )


def _insert_parameter_candidate(
    conn,
    *,
    candidate_id,
    factor_id,
    status="approved",
    validation_summary=None,
    created_at=0.0,
    updated_at=0.0,
):
    conn.execute(
        """
        INSERT INTO parameter_template_release_candidate
        (candidate_id, factor_id, template_id, regime_key, status,
         validation_summary_json, validation_report_path, created_at, updated_at)
        VALUES (?, ?, ?, '', ?, ?, '', ?, ?)
        """,
        (
            candidate_id,
            factor_id,
            f"{factor_id}:conservative.v1:default",
            status,
            json.dumps(validation_summary or {}, ensure_ascii=False),
            created_at,
            updated_at,
        ),
    )


def test_recent_policy_verdicts_summarizes_decision_ledger(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = _canonical_db(db_path)
    record_decision_event(
        conn,
        decision_id="dec_allowed",
        position_id="284214987",
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=200.0,
        action={
            "risk_verdict": {
                "allowed": True,
                "reason": "ok",
                "audit_payload": {"action": "open_trade"},
            }
        },
        risk_state={
            "policy_verdict": {
                "allowed": True,
                "reason": "ok",
                "audit_payload": {"action": "open_trade"},
            }
        },
    )
    record_decision_event(
        conn,
        decision_id="dec_skipped",
        event_type="supervisor_reduce",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=150.0,
        action={
            "risk_verdict": {
                "allowed": True,
                "reason": "risk_reducing_action",
                "audit_payload": {"action": "reduce_position"},
            }
        },
        risk_state={
            "policy_verdict": {
                "allowed": True,
                "reason": "risk_reducing_action",
                "audit_payload": {"action": "reduce_position"},
            }
        },
    )
    record_decision_event(
        conn,
        decision_id="dec_blocked",
        event_type="skip",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=100.0,
        action={
            "risk_verdict": {
                "allowed": False,
                "reason": "仓位上限: 3/3",
                "audit_payload": {"action": "open_trade"},
            }
        },
        risk_state={
            "policy_verdict": {
                "allowed": False,
                "reason": "仓位上限: 3/3",
                "audit_payload": {"action": "open_trade"},
            }
        },
    )
    record_decision_event(
        conn,
        decision_id="dec_pre_policy",
        event_type="skip",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=175.0,
        action={
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
        },
        risk_state={},
    )
    record_supervisor_trace_event(
        conn,
        trace_id="trace-applied",
        decision_id="dec_allowed",
        event_ts=200.0,
        payload={
            "trace_id": "trace-applied",
            "decision_id": "dec_allowed",
            "stage": "executed",
            "outcome": "applied",
            "execution_status": "applied",
            "execution_reason": "broker_confirmed",
        },
    )
    record_supervisor_trace_event(
        conn,
        trace_id="trace-skipped",
        decision_id="dec_skipped",
        event_ts=150.0,
        payload={
            "trace_id": "trace-skipped",
            "decision_id": "dec_skipped",
            "stage": "no_op_suppressed",
            "outcome": "skipped",
            "execution_status": "no_op",
            "execution_reason": "invalid_reduce_volume",
        },
    )
    conn.execute(
        "INSERT INTO recovery_position_state(position_id, direction) VALUES (?, ?)",
        (284214987, 1),
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

    monkeypatch.setattr(
        risk_api,
        "_get_system_health_report",
        lambda: _SystemHealth().get_last_report(),
    )
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
    conn = _canonical_db(db_path)
    record_decision_event(
        conn,
        decision_id="dec_open",
        trade_id="9001",
        position_id="9001",
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=100.0,
        portfolio_state={"equity": 10000},
        risk_state={"policy_verdict": {"allowed": True, "reason": "ok"}},
        action={"side": "sell"},
        action_score=0.7,
        action_reason="opened",
        created_at=100.0,
    )
    record_decision_event(
        conn,
        decision_id="dec_close",
        trade_id="9001",
        position_id="9001",
        event_type="close",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=150.0,
        risk_state={"policy_verdict": {"allowed": True, "reason": "manual_close"}},
        action={"close_reason": "broker_close"},
        action_reason="manual_close",
        created_at=200.0,
    )
    record_decision_event(
        conn,
        decision_id="dec_supervisor_close",
        trade_id="9001",
        position_id="9001",
        event_type="supervisor_close",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=180.0,
        action_score=0.91,
        action_reason="thesis_broken",
        action={
            "supervisor_verdict": {
                "action": "close",
                "summary_reason": "thesis_broken",
                "evidence": {"holding_efficiency": 0.08},
                "recommended_controls": {"protection_mode": "full_exit"},
            },
            "risk_verdict": {"allowed": True, "reason": "risk_reducing_action"},
        },
        risk_state={"policy_verdict": {"allowed": True, "reason": "risk_reducing_action"}},
        created_at=180.0,
    )
    record_position_event(
        conn,
        event_id="pos_evt_close",
        position_id="9001",
        trade_id="9001",
        symbol="XAUUSD+",
        event_type="closed",
        event_ts=200.0,
        net_volume=0,
        avg_price=3988.2,
        realized_pnl=12.5,
        details={"reason": "broker_close"},
    )
    record_order_event(
        conn,
        event_id="ord_evt_fill",
        decision_id="dec_open",
        trade_id="9001",
        order_id="ord-1",
        broker_order_id="brk-1",
        event_type="filled",
        event_ts=101.0,
        price=3992.0,
        volume=100,
        status="filled",
        details={"sl": 4020.0, "tp": 3960.0},
    )
    record_review(
        conn,
        review_id="rev_1",
        trade_id="9001",
        position_id="9001",
        entry_decision_id="dec_open",
        exit_decision_id="dec_close",
        pnl=12.5,
        outcome_label="win",
        failure_tags=["manual"],
        summary_text="手动平仓已记录",
        review={"close_reason": "broker_close", "real_pnl": {"net": 12.5}},
        created_at=220.0,
    )
    _insert_factor_contribution(
        conn,
        review_id="rev_1",
        trade_id="9001",
        factor="macd_hist",
        entry_contribution=0.4,
        hold_contribution=0.2,
        net_contribution=0.6,
        confidence=0.8,
        notes={
            "source": "rule_review",
            "primary_responsibility": "exit",
            "responsibility_labels": ["entry_good_exit_bad"],
            "factor_role": "helpful",
        },
    )
    _insert_factor_contribution(
        conn,
        review_id="rev_1",
        trade_id="9001",
        factor="rsi_14",
        entry_contribution=-0.1,
        net_contribution=-0.1,
        confidence=0.5,
        notes={
            "source": "rule_review",
            "primary_responsibility": "exit",
            "responsibility_labels": ["entry_good_exit_bad"],
            "factor_role": "harmful",
        },
    )
    conn.execute(
        """
        INSERT INTO recovery_position_state
        (position_id, broker, symbol, direction, open_price, volume, status,
         entry_decision_id, context_integrity, recovery_meta_json, close_reason, close_pnl)
        VALUES (?, 'ctrader', 'XAUUSD+', -1, 3992.0, 100, 'closed', ?, 'full', ?, 'broker_close', 12.5)
        """,
        (9001, "dec_open", json.dumps({"source": "replay"})),
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
    conn = _canonical_db(db_path)
    for decision_id, position_id, decision_ts in (
        ("entry_old", "8001", 100.0),
        ("entry_new", "8002", 200.0),
    ):
        record_decision_event(
            conn,
            decision_id=decision_id,
            trade_id=position_id,
            position_id=position_id,
            event_type="open",
            symbol="XAUUSD+",
            timeframe="M5",
            decision_ts=decision_ts,
            created_at=decision_ts,
            action={},
            risk_state={},
        )
    record_review(
        conn,
        review_id="rev_old",
        trade_id="8001",
        position_id="8001",
        entry_decision_id="entry_old",
        exit_decision_id="dec_old",
        pnl=4.2,
        outcome_label="win",
        failure_tags=["manual"],
        summary_text="旧样本",
        review={"close_reason": "broker_close", "real_pnl": {"net": 4.2}},
        created_at=150.0,
    )
    record_review(
        conn,
        review_id="rev_new",
        trade_id="8002",
        position_id="8002",
        entry_decision_id="entry_new",
        exit_decision_id="dec_new",
        pnl=-5.0,
        outcome_label="bad_loss",
        failure_tags=["param_suspect"],
        summary_text="新样本",
        review={
            "close_reason": "thesis_broken",
            "real_pnl": {"net": -5.0},
            "primary_responsibility": "parameter",
            "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
            "failure_taxonomy": {
                "primary_responsibility": "parameter",
                "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
            },
        },
        created_at=250.0,
    )
    _insert_factor_contribution(
        conn,
        review_id="rev_new",
        trade_id="8002",
        factor="rsi_14",
        net_contribution=-0.7,
        confidence=0.9,
        notes={
            "primary_responsibility": "parameter",
            "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
        },
    )
    _insert_parameter_candidate(
        conn,
        candidate_id="ptrc_recent",
        factor_id="rsi_14",
        status="approved",
        created_at=300.0,
        updated_at=310.0,
    )
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)
    monkeypatch.setattr(risk_api, "_db_path_from_conn", lambda _conn: str(db_path))

    result = risk_api._recent_trade_trace_index(limit=5)

    assert result["count"] == 2
    assert result["items"][0]["review_id"] == "rev_new"
    assert result["items"][0]["position_id"] == "8002"
    assert result["items"][0]["primary_responsibility"] == "parameter"
    assert result["items"][0]["parameter_governance_factor"] == "rsi_14"
    assert result["items"][0]["parameter_candidate_status"] == "approved"
    assert result["items"][0]["parameter_candidate_id"] == "ptrc_recent"
    assert (
        result["items"][0]["parameter_recommendation_id"]
        == "ptr_rsi_14_rsi_14_conservative.v1_default"
    )
    assert result["items"][0]["parameter_governance_stage"] == "等待发布"
    assert "切到运行态" in result["items"][0]["parameter_governance_next_step"]
    assert result["items"][0]["parameter_governance_entry_type"] == "candidate"
    assert result["items"][0]["parameter_governance_target_type"] == "模板候选"
    assert result["items"][0]["parameter_governance_entry_hint_text"] == "建议先看模板候选"
    assert result["items"][0]["symbol"] == "XAUUSD+"


def test_trade_trace_includes_parameter_governance_context(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = _canonical_db(db_path)
    record_decision_event(
        conn,
        decision_id="dec_entry",
        trade_id="9101",
        position_id="9101",
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=100.0,
        action_score=0.6,
        action_reason="opened",
        created_at=100.0,
        action={},
        risk_state={},
    )
    record_review(
        conn,
        review_id="rev_param",
        trade_id="9101",
        position_id="9101",
        entry_decision_id="dec_entry",
        exit_decision_id="dec_exit",
        pnl=-8.5,
        outcome_label="bad_loss",
        failure_tags=["param_suspect"],
        summary_text="参数疑似失配",
        review={
            "close_reason": "thesis_broken",
            "real_pnl": {"net": -8.5},
            "primary_responsibility": "parameter",
            "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
            "failure_taxonomy": {
                "primary_responsibility": "parameter",
                "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
            },
        },
        created_at=220.0,
    )
    _insert_factor_contribution(
        conn,
        review_id="rev_param",
        trade_id="9101",
        factor="rsi_14",
        entry_contribution=0.2,
        hold_contribution=-0.4,
        exit_contribution=-0.3,
        net_contribution=-0.5,
        confidence=0.88,
        notes={
            "source": "rule_review",
            "primary_responsibility": "parameter",
            "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
            "factor_role": "harmful",
        },
    )
    _insert_parameter_candidate(
        conn,
        candidate_id="ptrc_param_9101",
        factor_id="rsi_14",
        status="approved",
        validation_summary={
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
        },
        created_at=230.0,
        updated_at=240.0,
    )
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)
    monkeypatch.setattr(risk_api, "_db_path_from_conn", lambda _conn: str(db_path))

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
    assert any(
        item["type"] == "template_recommendation"
        and item["recommendation_id"] == "ptr_rsi_9101"
        for item in governance_actions
    )


def test_trade_trace_resolves_from_decision_id(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = _canonical_db(db_path)
    record_decision_event(
        conn,
        decision_id="dec_only",
        trade_id="3003",
        position_id="3003",
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=10.0,
        action_reason="opened",
        created_at=10.0,
        action={},
        risk_state={},
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
    conn = _canonical_db(db_path)
    record_review(
        conn,
        review_id="review_recent",
        trade_id="trade_recent",
        position_id="268728362",
        entry_decision_id="dec_entry_missing",
        exit_decision_id="dec_exit_missing",
        outcome_label="acceptable_loss",
        summary_text="复盘记录已生成",
        review={"symbol": "XAUUSD+", "close_reason": "thesis_invalid"},
        created_at=20.0,
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
