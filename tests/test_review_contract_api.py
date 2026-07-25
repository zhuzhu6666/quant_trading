import json

from backend.api import learning as learning_api
from backend.api import risk as risk_api
from backend.services.review_contract import build_system_issue_context


def test_unknown_broker_close_price_contaminates_learning_without_hiding_money_pnl():
    issue = build_system_issue_context(
        {
            "real_pnl": {
                "net": -2.5,
                "price_contract": "legacy_unknown",
                "price_quality": "unknown",
            }
        }
    )

    assert issue["contaminates_learning"] is True
    assert "broker_close_price_unknown" in issue["labels"]
    assert issue["evidence"]["broker_close_price"]["price_quality"] == "unknown"


def test_learning_parse_review_row_normalizes_phase_d_contract_fields():
    row = {
        "review_id": "rev_1",
        "trade_id": "t1",
        "position_id": "p1",
        "entry_quality": 0.61,
        "hold_quality": 0.52,
        "exit_quality": 0.47,
        "regime_fit_score": 0.43,
        "execution_quality": 0.58,
        "failure_tags_json": json.dumps(["bad_loss"]),
        "review_json": json.dumps(
            {
                "holding_efficiency": 0.31,
                "giveback_ratio": 0.66,
                "profit_capture_ratio": 0.22,
                "time_in_profit_seconds": 900.0,
                "thesis_status": "broken",
                "failure_taxonomy": {
                    "primary_responsibility": "exit",
                    "responsibility_labels": ["entry_good_exit_bad"],
                    "confidence": 0.71,
                },
                "primary_responsibility": "exit",
                "responsibility_labels": ["entry_good_exit_bad"],
            }
        ),
    }

    parsed = learning_api._parse_review_row(row)

    assert parsed["review"]["contract_version"] == "phase_d.v1"
    assert parsed["review"]["regime_fit"] == 0.43
    assert parsed["regime_fit"] == 0.43
    assert parsed["thesis_status_at_exit"] == "broken"
    assert parsed["holding_efficiency"] == 0.31
    assert parsed["time_in_profit"] == 900.0
    assert parsed["primary_responsibility"] == "exit"
    assert parsed["responsibility_labels"] == ["entry_good_exit_bad"]


def test_risk_parse_review_row_normalizes_phase_d_contract_fields():
    row = {
        "review_id": "rev_2",
        "trade_id": "t2",
        "position_id": "p2",
        "entry_quality": 0.74,
        "hold_quality": 0.69,
        "exit_quality": 0.41,
        "regime_fit_score": 0.28,
        "execution_quality": 0.63,
        "failure_tags_json": json.dumps(["good_loss"]),
        "review_json": json.dumps(
            {
                "holding_efficiency": 0.27,
                "giveback_ratio": 0.48,
                "profit_capture_ratio": 0.4,
                "time_in_profit": 1200.0,
                "thesis_status_at_exit": "weakening",
                "failure_taxonomy": {
                    "primary_responsibility": "holding",
                    "responsibility_labels": ["holding_inefficient"],
                    "confidence": 0.52,
                },
            }
        ),
    }

    parsed = risk_api._parse_review_row(row)

    assert parsed["review"]["contract_version"] == "phase_d.v1"
    assert parsed["review"]["regime_fit"] == 0.28
    assert parsed["regime_fit"] == 0.28
    assert parsed["thesis_status_at_exit"] == "weakening"
    assert parsed["profit_capture_ratio"] == 0.4
    assert parsed["time_in_profit"] == 1200.0
    assert parsed["primary_responsibility"] == "holding"
    assert parsed["responsibility_labels"] == ["holding_inefficient"]
