import json
import sqlite3
import time

from backend.ledger.service import DecisionLedger
from research.meta_model_sidecar import MetaModelSidecar


def test_meta_model_context_and_advisory_are_shadow_only(tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))
    now = time.time()
    ledger.log_decision(
        event_type="risk_policy_check",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=now,
        risk_state={"policy_verdict": {"allowed": False, "reason": "late_session"}},
        action_score=0.0,
        action_reason="blocked",
        action_json={"blocked": True},
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO factor_health
            (factor, score, status, section, updated_at)
            VALUES ('momentum_breakout', 30.0, 'weak', 'trend', ?)
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()

    service = MetaModelSidecar(db_path)
    result = service.run(
        context={
            "market": {"session_state": "closing_soon", "minutes_to_close": 12},
            "system": {"health": "degraded"},
        },
        materialize=True,
    )

    assert result["ok"] is True
    assert result["context"]["schema_version"] == "meta_context.v1"
    assert result["decision"]["schema_version"] == "meta_decision.v1"
    assert result["decision"]["advisory_only"] is True
    assert result["decision"]["capabilities"]["live_trading"] is False
    assert result["decision"]["capabilities"]["can_place_orders"] is False
    assert result["ledger_decision_id"]
    assert result["decision"]["posture"] in {"observe", "contract"}

    advisories = service.list_advisories(limit=5)
    assert advisories["count"] == 1
    assert advisories["items"][0]["decision"]["advisory_only"] is True
    assert advisories["items"][0]["decision_id"] == result["ledger_decision_id"]


def test_meta_model_context_can_run_without_materializing(tmp_path):
    db_path = tmp_path / "state.db"
    service = MetaModelSidecar(db_path)

    result = service.run(
        context={
            "symbol": "XAUUSD+",
            "timeframe": "M5",
            "learning": {
                "position_quality_shadow": {
                    "count": 10,
                    "weak_count": 7,
                    "weak_rate": 0.7,
                },
            },
        },
        materialize=False,
    )

    assert result["ok"] is True
    assert result["materialized"] is False
    assert result["ledger_decision_id"] == ""
    assert result["permission"]["ok"] is True
    assert service.list_advisories()["items"] == []


def test_meta_model_ledger_payload_keeps_context_and_decision(tmp_path):
    db_path = tmp_path / "state.db"
    service = MetaModelSidecar(db_path)
    result = service.run(context={"risk": {"blocked_verdict_count_24h": 2}}, materialize=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM decision_ledger WHERE decision_id=?",
            (result["ledger_decision_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["event_type"] == "meta_model_advisory"
    payload = json.loads(row["action_json"])
    assert payload["schema_version"] == "meta_model_advisory_ledger.v1"
    assert payload["context"]["schema_version"] == "meta_context.v1"
    assert payload["decision"]["schema_version"] == "meta_decision.v1"
    assert payload["decision"]["advisory_only"] is True


def test_meta_model_sidecar_contracts_on_bad_rolling_learning():
    decision = MetaModelSidecar().decide(
        {
            "risk": {"blocked_verdict_count_24h": 2},
            "learning": {
                "rolling": {
                    "trade_count": 8,
                    "pnl_sum": -6.2,
                    "pnl_avg": -0.775,
                    "loss_rate": 0.75,
                    "bad_loss_rate": 0.5,
                    "giveback_avg": 0.7,
                },
                "counterfactual": {"correct_stop_rate": 0.5},
            },
            "factor": {"health": {"weak_count": 1}},
            "market": {},
            "system": {"health": "normal"},
        }
    )

    assert decision["posture"] == "contract"
    assert decision["rule_version"] == "meta_sidecar_rules.v2"
    assert decision["contract_score"] > decision["recover_score"]
    assert decision["risk_budget_advice"]["direction"] == "reduce"


def test_meta_model_sidecar_recovers_on_strong_learning_and_tight_counterfactuals():
    decision = MetaModelSidecar().decide(
        {
            "risk": {"blocked_verdict_count_24h": 0},
            "learning": {
                "rolling": {
                    "trade_count": 8,
                    "pnl_sum": 6.0,
                    "pnl_avg": 0.75,
                    "loss_rate": 0.25,
                    "win_rate": 0.75,
                    "profit_capture_avg": 0.7,
                    "mfe_mae_ratio": 1.8,
                    "broker_close_rate": 0.25,
                },
                "position_quality_shadow": {"weak_rate": 0.1},
                "factor_governance_shadow": {"weak_rate": 0.1},
                "counterfactual": {
                    "premature_rate": 0.5,
                    "protection_tight_rate": 0.5,
                    "correct_stop_rate": 0.0,
                },
            },
            "factor": {"health": {"weak_count": 0}},
            "market": {},
            "system": {"health": "normal"},
        }
    )

    assert decision["posture"] == "recover"
    assert decision["recover_score"] > decision["contract_score"]
    assert decision["trade_frequency_advice"]["direction"] == "normal"


def test_meta_model_sidecar_observes_when_contract_and_recover_are_mixed():
    decision = MetaModelSidecar().decide(
        {
            "risk": {"blocked_verdict_count_24h": 2},
            "learning": {
                "rolling": {
                    "trade_count": 8,
                    "pnl_sum": 3.0,
                    "loss_rate": 0.25,
                    "win_rate": 0.62,
                    "profit_capture_avg": 0.6,
                },
                "counterfactual": {"correct_stop_rate": 0.5},
            },
            "factor": {"health": {"weak_count": 3}},
            "market": {},
            "system": {"health": "degraded"},
        }
    )

    assert decision["posture"] == "observe"
    assert decision["contract_score"] > 0.0
    assert decision["recover_score"] > 0.0
