from types import SimpleNamespace

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.factor_pruning_candidates import FactorPruningCandidateService


def _cfg(signal_cfg, weights):
    return SimpleNamespace(
        factor_signal_config=signal_cfg,
        factor_portfolio_weights=weights,
        factor_redundancy_max_group_weight=0.35,
        awe_max_type_weight_pct=0.40,
    )


def test_factor_pruning_candidates_are_candidate_only_and_prioritized(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO factor_health (factor, score, status, n_obs, rolling_ic)
            VALUES ('dsl_auto_000', 18.0, 'decaying', 220, -0.03)
            """
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, entry_decision_id, pnl, outcome_label, failure_tags_json, review_json, created_at)
            VALUES ('review_recent_1', 'trade_recent_1', 'decision_recent_1', -1.0, 'bad_loss', '[]', '{}', 1000.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    signal_cfg = {
        "rsi_14": {"role": "alpha", "tags": ["technical"]},
        "bb_width": {"role": "context", "tags": ["volatility"]},
    }
    weights = {"rsi_14": 1.0, "bb_width": 2.0}
    for idx in range(45):
        name = f"dsl_auto_{idx:03d}"
        signal_cfg[name] = {"role": "alpha", "tags": ["discovered"], "source": "gp"}
        weights[name] = 0.01
    for idx in range(41):
        name = f"pca_{idx}"
        signal_cfg[name] = {"role": "alpha", "tags": ["pca"], "source": "pca"}
        weights[name] = 0.015

    conn = connect_sqlite(db_path)
    try:
        for name in sorted(set(signal_cfg) | set(weights)):
            conn.execute(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, source, raw_value, normalized_value, direction,
                 base_weight, policy_weight, shadow_score, health_score, gated, gated_reason, contribution_score)
                VALUES ('decision_recent_1', ?, 'test', 1.0, 1.0, 1.0, ?, ?, 0.0, 20.0, 0, '', 0.01)
                """,
                (name, weights[name], weights[name]),
            )
        conn.commit()
    finally:
        conn.close()

    original_weights = dict(weights)
    result = FactorPruningCandidateService(db_path).build(_cfg(signal_cfg, weights), limit=10)

    assert result["schema_version"] == "factor_pruning_candidates.v1"
    assert result["status"] == "actionable"
    assert result["generated_count"] >= 80
    assert result["candidate_count"] == 10
    assert result["skipped_cold_factor_count"] == 0
    assert result["boundary"]["candidate_only"] is True
    assert result["boundary"]["writes_policy_suggestion"] is False
    assert result["boundary"]["writes_brain_governance_candidate"] is False
    assert weights == original_weights

    top = result["candidates"][0]
    assert top["factor"] == "dsl_auto_000"
    reason_codes = {item["code"] for item in top["reasons"]}
    assert {"recent_live_decision_participation", "low_weight_tail", "large_noise_family", "weak_factor_health"} <= reason_codes
    assert top["recommended_action"] == "review_disable"
    assert top["suggested_target_weight"] == 0.0
    assert top["evidence"]["recent_decision_evidence"]["decision_review_count"] == 1
    assert "direct_runtime_write" in top["blocked_uses"]


def test_factor_pruning_candidates_keep_context_and_disabled_out(tmp_path):
    signal_cfg = {
        "rsi_14": {"role": "alpha", "tags": ["technical"]},
        "bb_width": {"role": "context", "tags": ["volatility"]},
        "dsl_auto_disabled": {"role": "alpha", "enabled": False, "tags": ["discovered"]},
    }
    weights = {"rsi_14": 1.0, "bb_width": 0.001, "dsl_auto_disabled": 0.001}

    result = FactorPruningCandidateService(tmp_path / "state.db").build(_cfg(signal_cfg, weights))

    assert result["status"] == "ok"
    assert result["generated_count"] == 0
    assert result["candidate_count"] == 0


def test_factor_pruning_candidates_prioritize_harmful_live_participants_over_cold_tail(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        for idx, pnl in enumerate([-1.2, -0.8, -0.6, 0.4]):
            decision_id = f"decision_live_{idx}"
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, entry_decision_id, pnl, outcome_label, failure_tags_json, review_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    f"review_live_{idx}",
                    f"trade_live_{idx}",
                    decision_id,
                    pnl,
                    "bad_loss" if pnl <= 0 else "good_win",
                    '["signal_execution_delay"]' if idx == 0 else "[]",
                    1000.0 + idx,
                ),
            )
            conn.execute(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, source, raw_value, normalized_value, direction,
                 base_weight, policy_weight, shadow_score, health_score, gated, gated_reason, contribution_score)
                VALUES (?, 'dsl_auto_hot_bad', 'test', 1.0, 1.0, 1.0, 0.3, 0.3, 0.0, 30.0, 0, '', ?)
                """,
                (decision_id, 0.08 if pnl <= 0 else -0.04),
            )
        conn.commit()
    finally:
        conn.close()

    signal_cfg = {
        "dsl_auto_hot_bad": {"role": "alpha", "tags": ["discovered"], "source": "gp"},
        "dsl_auto_cold_tail": {"role": "alpha", "tags": ["discovered"], "source": "gp"},
    }
    weights = {"dsl_auto_hot_bad": 0.3, "dsl_auto_cold_tail": 0.005}

    result = FactorPruningCandidateService(db_path).build(_cfg(signal_cfg, weights), limit=10)

    assert result["status"] == "actionable"
    assert result["generated_count"] == 1
    assert result["skipped_cold_factor_count"] == 1
    top = result["candidates"][0]
    assert top["factor"] == "dsl_auto_hot_bad"
    reason_codes = {item["code"] for item in top["reasons"]}
    assert "recent_loss_contribution_pressure" in reason_codes
    assert "loss_win_contribution_sign_flip" in reason_codes
    assert "system_issue_caveat" in reason_codes
    assert top["evidence"]["recent_decision_evidence"]["loss_review_count"] == 3


def test_factor_pruning_candidates_include_snapshot_only_discovered_live_factors(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        for idx, pnl in enumerate([-1.0, -0.7, -0.5, 0.3]):
            decision_id = f"decision_snapshot_{idx}"
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, entry_decision_id, pnl, outcome_label, failure_tags_json, review_json, created_at)
                VALUES (?, ?, ?, ?, ?, '[]', '{}', ?)
                """,
                (
                    f"review_snapshot_{idx}",
                    f"trade_snapshot_{idx}",
                    decision_id,
                    pnl,
                    "bad_loss" if pnl <= 0 else "good_win",
                    2000.0 + idx,
                ),
            )
            conn.execute(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, source, raw_value, normalized_value, direction,
                 base_weight, policy_weight, shadow_score, health_score, gated, gated_reason, contribution_score)
                VALUES (?, 'dsl_auto_snapshot_hot', 'snapshot', 1.0, 1.0, 1.0, 0.3, 0.3, 0.0, 20.0, 0, '', ?)
                """,
                (decision_id, 0.12 if pnl <= 0 else -0.05),
            )
        conn.commit()
    finally:
        conn.close()

    cfg = _cfg({"rsi_14": {"role": "alpha"}}, {"rsi_14": 1.0})

    result = FactorPruningCandidateService(db_path).build(cfg, limit=10)

    factors = [item["factor"] for item in result["candidates"]]
    assert "dsl_auto_snapshot_hot" in factors
    candidate = next(item for item in result["candidates"] if item["factor"] == "dsl_auto_snapshot_hot")
    assert candidate["evidence"]["snapshot_only"] is True
    assert candidate["evidence"]["recent_decision_evidence"]["decision_review_count"] == 4
    assert candidate["priority_score"] >= 0.75
