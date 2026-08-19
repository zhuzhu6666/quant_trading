import json

from backend.api import learning as learning_api
from backend.api import risk as risk_api
from backend.services.review_contract import (
    build_execution_quality_evidence,
    build_system_issue_context,
    classify_4label_outcome,
)




def test_non_factor_responsibilities_unify_factor_penalty_exclusions():
    """B1/B2: execution_timing / operator_intervention are excluded from factor
    penalty, and the exclusion vocabulary is the single shared authority."""
    from backend.services.failure_taxonomy import FACTOR_PENALTY_BLOCKED_RESPONSIBILITIES
    from backend.services.review_contract import NON_FACTOR_RESPONSIBILITIES

    assert "execution_timing" in NON_FACTOR_RESPONSIBILITIES
    assert "operator_intervention" in NON_FACTOR_RESPONSIBILITIES
    assert "execution" in NON_FACTOR_RESPONSIBILITIES
    assert "exit" in NON_FACTOR_RESPONSIBILITIES
    assert "holding" in NON_FACTOR_RESPONSIBILITIES
    assert FACTOR_PENALTY_BLOCKED_RESPONSIBILITIES == NON_FACTOR_RESPONSIBILITIES



def test_classify_4label_outcome_is_single_authority():
    """A2: the 4-label rule is the single producer for canonical review labels."""
    # Profit requires attribution proof: positive_share >= 0.55 -> good_win.
    assert classify_4label_outcome(pnl=5.0, positive_share=0.7)[0] == "good_win"
    assert classify_4label_outcome(pnl=5.0, positive_share=0.3)[0] == "lucky_win"
    # No attribution evidence -> conservative lucky_win (never invents good_win).
    assert classify_4label_outcome(pnl=5.0, positive_share=None)[0] == "lucky_win"
    # High-conviction loss -> bad_loss; low-conviction, not avoidable -> good_loss.
    assert classify_4label_outcome(pnl=-2.0, entry_score=0.8)[0] == "bad_loss"
    assert classify_4label_outcome(pnl=-2.0, entry_score=0.3)[0] == "good_loss"
    # Avoidable low-conviction loss with conflict -> bad_loss.
    label, conflict, weak_entry, avoidable = classify_4label_outcome(
        pnl=-2.0,
        entry_score=0.3,
        positive_share=0.3,
        has_entry_context=True,
        has_attribution=True,
        pos_mc=1.0,
        neg_mc=-0.5,
        factor_conflict_ratio=0.45,
        effective_alpha_factor_count=3,
    )
    assert label == "bad_loss"
    assert conflict is True
    assert weak_entry is True
    assert avoidable is True

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


def test_execution_quality_requires_broker_chain_and_uses_observed_cost():
    evidence = build_execution_quality_evidence(
        order_events=[
            {"event_type": "submitted", "price": 100.0},
            {"event_type": "filled", "price": 101.0},
        ],
        entry_action={
            "direction": 1,
            "market_micro_context": {"spread": 2.0},
        },
        broker_deal={
            "deal_id": 7,
            "exec_price": 101.0,
            "price_quality": "broker_reported",
        },
        direction=1,
    )

    assert evidence["evidence_state"] == "full"
    assert evidence["broker_deal_fill_match"] is True
    assert evidence["score"] == 0.5
    assert evidence["score_formula"].startswith("clamp(1-")

    incomplete = build_execution_quality_evidence(
        entry_action={"direction": 1},
        direction=1,
    )
    assert incomplete["evidence_state"] == "unknown"
    assert incomplete["score"] == 0.0


def test_execution_quality_treats_broker_slippage_as_observation_not_missing_evidence():
    evidence = build_execution_quality_evidence(
        order_events=[
            {
                "event_type": "submitted",
                "price": 100.0,
                "details": {
                    "direction": 1,
                    "market_micro_context": {"bid": 99.0, "ask": 101.0},
                },
            },
            {"event_type": "filled", "price": 100.0, "details": {"direction": 1}},
        ],
        broker_deal={
            "deal_id": 8,
            "exec_price": 100.5,
            "price_quality": "broker_reported",
        },
        direction=1,
    )

    assert evidence["evidence_state"] == "full"
    assert evidence["broker_deal_fill_match"] is False
    assert evidence["fill_price_source"] == "broker_deal"
    assert evidence["lifecycle_broker_fill_delta_points"] == 0.5
    assert "broker_deal_fill_mismatch" not in evidence["issues"]
    assert "lifecycle_fill_differs_from_broker_fill" in evidence["observations"]


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


def test_restart_replay_close_contaminates_learning():
    issue = build_system_issue_context(
        {
            "close_reason": "restart_replay",
            "close_reason_source": "restart_replay",
            "real_pnl": {
                "net": -1.29,
                "price_quality": "broker_reconciled",
            },
        }
    )

    assert issue["contaminates_learning"] is True
    assert "restart_replay" in issue["labels"]
    assert issue["primary_responsibility"] == "operator_intervention"
    assert issue["system_contaminated"] is True


def test_manual_close_contaminates_learning():
    issue = build_system_issue_context(
        {
            "close_reason": "manual_close",
            "real_pnl": {"net": 0.5, "price_quality": "broker_reconciled"},
        }
    )

    assert issue["contaminates_learning"] is True
    assert "manual_close" in issue["labels"]
    assert issue["primary_responsibility"] == "operator_intervention"


def test_normal_close_does_not_contaminate():
    issue = build_system_issue_context(
        {
            "close_reason": "broker_close",
            "real_pnl": {"net": 3.0, "price_quality": "broker_reconciled"},
        }
    )

    assert issue["contaminates_learning"] is False
    assert issue["labels"] == []
