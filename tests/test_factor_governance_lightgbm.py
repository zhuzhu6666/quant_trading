import json
import sqlite3

from research.factor_governance_lightgbm import (
    MODEL_TYPE,
    FactorGovernanceLightGBMService,
)


def _create_factor_reviews(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
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
        CREATE TABLE policy_suggestion (
            suggestion_id TEXT PRIMARY KEY,
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            action TEXT NOT NULL,
            confidence REAL DEFAULT 0.0,
            reason TEXT DEFAULT '',
            evidence_json TEXT DEFAULT '{}',
            status TEXT DEFAULT 'proposed',
            reviewed_at REAL DEFAULT 0.0,
            review_note TEXT DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    for i in range(10):
        positive = i % 2 == 0
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, entry_quality, hold_quality,
             exit_quality, regime_fit_score, execution_quality, pnl, mae, mfe,
             outcome_label, review_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"rev_{i}",
                f"trade_{i}",
                f"pos_{i}",
                0.8 if positive else 0.2,
                0.7 if positive else 0.2,
                0.7 if positive else 0.2,
                0.8 if positive else 0.3,
                0.8 if positive else 0.4,
                2.0 if positive else -2.0,
                0.4 if positive else 2.4,
                2.8 if positive else 0.3,
                "small_win" if positive else "bad_loss",
                json.dumps({"case": i}),
                1000.0 + i,
            ),
        )
        conn.execute(
            """
            INSERT INTO factor_contribution_review
            (review_id, trade_id, factor, entry_contribution, hold_contribution,
             exit_contribution, net_contribution, confidence, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"rev_{i}",
                f"trade_{i}",
                "momentum_factor" if positive else "weak_factor",
                0.4 if positive else -0.4,
                0.2 if positive else -0.3,
                0.2 if positive else -0.2,
                0.8 if positive else -0.9,
                0.8,
                "",
            ),
        )
    conn.commit()
    conn.close()


def test_factor_governance_lightgbm_trains_or_reports_missing_dependency(tmp_path):
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_factor_reviews(db_path)

    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    samples = service.load_samples(limit=20)
    assert len(samples) == 8
    assert {sample["label_source"] for sample in samples} == {"next_same_factor_outcome"}

    result = service.train(limit=20, min_samples=6, register=False)

    if not result["ok"]:
        assert result["error"] == "dependency_missing"
        return

    assert result["model_type"] == MODEL_TYPE
    assert result["metrics"]["holdout"]["majority_baseline_accuracy"] is not None
    assert result["metrics"]["holdout"]["balanced_accuracy"] is not None
    assert result["capabilities"]["live_trading"] is False
    assert result["capabilities"]["can_change_factor_weights"] is False
    shadow = service.score_samples(
        artifact_path=result["artifact_path"],
        limit=10,
        materialize=True,
        min_weakness_score=0.1,
    )
    assert shadow["ok"] is True
    assert shadow["count"] == len(samples)
    audits = service.list_audits(limit=20)
    assert audits["count"] == len(samples)
    assert audits["items"][0]["result"]["capabilities"]["shadow_only"] is True
    rows = sqlite3.connect(str(db_path)).execute(
        "SELECT scope_type, action, status, evidence_json FROM policy_suggestion"
    ).fetchall()
    assert rows
    assert rows[0][0] == "factor"
    assert rows[0][1] == "review_factor_weight_or_template"
    assert rows[0][2] == "proposed"
    evidence = json.loads(rows[0][3])
    assert evidence["source_agent"] == "lightgbm_shadow_models"
    assert evidence["agent_context"]["schema_version"] == "agent_generation_context.v1"
    assert evidence["agent_context"]["authority_verdict"]["advisory_only"] is True


def test_factor_governance_lightgbm_skips_system_contaminated_reviews(tmp_path):
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_factor_reviews(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        system_issue = {
            "schema_version": "trade_review_system_issue.v1",
            "system_contaminated": True,
            "contaminates_learning": True,
            "labels": ["market_data_stale", "signal_execution_delay"],
        }
        conn.execute(
            """
            UPDATE trade_outcome_review
            SET review_json=?
            WHERE review_id='rev_1'
            """,
            (json.dumps({"case": 1, "system_issue_context": system_issue}),),
        )
        conn.execute(
            """
            UPDATE factor_contribution_review
            SET notes=?
            WHERE review_id='rev_1'
            """,
            (json.dumps({"system_contaminated": True}),),
        )
        conn.commit()
    finally:
        conn.close()

    service = FactorGovernanceLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    samples = service.load_samples(limit=20)

    assert len(samples) == 7
    assert all(sample["review_id"] != "rev_1" for sample in samples)
