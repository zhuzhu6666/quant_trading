from __future__ import annotations

import sqlite3

from alpha.reflection.reviewer import TradeReviewer
from backend.ledger.service import DecisionLedger
from research.learning.experience_builder import ExperienceBuilder
from research.learning.policy_suggester import PolicySuggester


def _rows(db_path: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def test_rule_learning_pipeline_persists_full_chain(tmp_path):
    db_path = str(tmp_path / "state.db")
    ledger = DecisionLedger(db_path)
    reviewer = TradeReviewer(db_path)
    builder = ExperienceBuilder(db_path)
    suggester = PolicySuggester(db_path)

    entry_decision_id = ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="101",
        position_id="101",
        action_score=0.82,
        action_reason="executed",
        action_json={"price": 3300.0},
        factor_snapshots=[
            {
                "factor": "trend_alpha",
                "raw_value": 1.2,
                "normalized_value": 0.8,
                "direction": 1.0,
                "base_weight": 0.2,
                "policy_weight": 0.2,
                "contribution_score": 0.16,
            },
            {
                "factor": "noise_factor",
                "raw_value": -0.6,
                "normalized_value": -0.7,
                "direction": -1.0,
                "base_weight": 0.35,
                "policy_weight": 0.35,
                "contribution_score": -0.245,
            },
        ],
    )
    assert entry_decision_id

    review = reviewer.review_closed_trade(
        position_id="101",
        pnl=-120.0,
        close_price=3280.0,
        close_ts=1_000_000.0,
        contributions={"trend_alpha": 10.0, "noise_factor": -90.0},
        exit_decision_id="dec_close_1",
        real_pnl={"net": -120.0, "commission": 4.0},
    )
    experience = builder.build_from_review(review)
    suggestion = suggester.suggest_from_experience(experience)

    assert review["outcome_label"] == "bad_loss"
    assert "overweight_noise_factor" in review["failure_tags"]
    assert experience["recommended_action"] == "downweight"
    assert suggestion is None

    assert len(_rows(db_path, "SELECT * FROM decision_ledger")) == 1
    assert len(_rows(db_path, "SELECT * FROM decision_factor_snapshot")) == 2
    assert len(_rows(db_path, "SELECT * FROM trade_outcome_review")) == 1
    assert len(_rows(db_path, "SELECT * FROM factor_contribution_review")) == 2
    assert len(_rows(db_path, "SELECT * FROM experience_memory")) == 1
    assert len(_rows(db_path, "SELECT * FROM policy_suggestion")) == 0


def test_policy_suggester_downweights_after_repeated_bad_losses(tmp_path):
    db_path = str(tmp_path / "state.db")
    suggester = PolicySuggester(db_path)

    actions = []
    for idx in range(3):
        suggestion = suggester.suggest_from_experience(
            {
                "experience_id": f"exp_{idx}",
                "primary_factor": "fragile_factor",
                "outcome_label": "bad_loss",
                "reward_score": -0.8,
                "failure_tags": ["bad_loss", "regime_mismatch"],
            }
        )
        actions.append(suggestion["action"] if suggestion else None)

    assert actions[-1] == "downweight"
    stats = _rows(
        db_path,
        "SELECT * FROM experience_pattern_stats WHERE scope_type='factor' AND scope_key='fragile_factor'",
    )[0]
    assert int(stats["sample_count"]) == 3
    assert int(stats["bad_loss_count"]) == 3
    assert float(stats["avg_reward"]) < 0


def test_policy_suggester_skips_watch_and_promotes_fast_positive_factor(tmp_path):
    db_path = str(tmp_path / "state.db")
    suggester = PolicySuggester(db_path)

    weak = suggester.suggest_from_experience(
        {
            "experience_id": "exp_watch",
            "primary_factor": "slow_factor",
            "outcome_label": "lucky_win",
            "reward_score": 0.12,
            "failure_tags": [],
        }
    )
    assert weak is None
    assert len(_rows(db_path, "SELECT * FROM policy_suggestion")) == 0

    result = None
    for idx, reward in enumerate((0.45, 0.55, 0.32, 0.40), start=1):
        result = suggester.suggest_from_experience(
            {
                "experience_id": f"exp_fast_{idx}",
                "primary_factor": "fast_factor",
                "outcome_label": "good_win",
                "reward_score": reward,
                "failure_tags": [],
            }
        )

    assert result is not None
    assert result["action"] == "boost_small"
    rows = _rows(
        db_path,
        "SELECT * FROM policy_suggestion WHERE scope_key='fast_factor'",
    )
    assert len(rows) == 1


def test_rule_learning_pipeline_deweights_recovery_replay_samples(tmp_path):
    db_path = str(tmp_path / "state.db")
    ledger = DecisionLedger(db_path)
    reviewer = TradeReviewer(db_path)
    builder = ExperienceBuilder(db_path)

    entry_decision_id = ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="202",
        position_id="202",
        action_score=0.68,
        action_reason="executed",
        action_json={"price": 3310.0},
        factor_snapshots=[
            {
                "factor": "dsl_auto_factor",
                "raw_value": 1.0,
                "normalized_value": 0.6,
                "direction": 1.0,
                "base_weight": 0.25,
                "policy_weight": 0.25,
                "contribution_score": 0.15,
            }
        ],
    )
    assert entry_decision_id

    review = reviewer.review_closed_trade(
        position_id="202",
        pnl=42.0,
        close_price=3322.0,
        close_ts=1_100_000.0,
        contributions={"dsl_auto_factor": 42.0},
        exit_decision_id="dec_close_202",
        real_pnl={"net": 42.0},
        close_reason="restart_replay",
        context_integrity="partial",
    )
    experience = builder.build_from_review(review)

    assert "partial_context" in experience["failure_tags"]
    assert "restart_replay" in experience["failure_tags"]
    assert experience["recommended_action"] == "watch"
    assert float(experience["reward_score"]) < 0.3
    assert float(experience["evidence_strength"]) < 0.2
