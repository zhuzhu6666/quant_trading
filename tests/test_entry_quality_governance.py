from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

from backend.services.autonomous_learning import ensure_autonomous_learning_tables
from backend.services.entry_quality_governance import EntryQualityGovernanceService
from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
from backend.services.live_committed_policy import load_live_policy_controls
from research.learning.governor import RuleEvolutionGovernor


class _AllowedVerdict:
    def to_dict(self):
        return {"allowed": True, "reason": "bounded_demo_risk_tightening"}


class _Risk:
    def evaluate(self, _action, _context):
        return _AllowedVerdict()


def test_demo_applies_one_entry_quality_control_as_committed_mutation(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_autonomous_learning_tables(db_path)
    monkeypatch.setattr(
        "backend.services.entry_quality_governance.runtime_config",
        lambda: SimpleNamespace(autonomy_mode="demo_autonomous"),
    )
    monkeypatch.setattr(
        "backend.services.entry_quality_governance.RiskPolicyService.shared",
        lambda: _Risk(),
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, governance_eligible,
             governance_eligibility_version, governance_eligibility_fingerprint,
             governance_ineligible_reason, created_at)
            VALUES ('eq-current', 'entry_quality', 'weak_signal',
                    'raise_weak_signal_threshold', 0.8, 'qualified weak signals',
                    ?, 'approved', 1, ?, 'fingerprint-current', '', ?)
            """,
            (
                json.dumps(
                    {
                        "recommended_controls": {
                            "min_abs_signal_score": 0.5,
                            "strong_signal_override": 0.75,
                        }
                    }
                ),
                GOVERNANCE_ELIGIBILITY_VERSION,
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = EntryQualityGovernanceService(db_path).apply_next_weak_signal(
        run_id="pytest-entry-quality"
    )

    assert result["ok"] is True
    assert result["status"] == "committed"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        suggestion = conn.execute(
            "SELECT status, applied_mutation_id FROM policy_suggestion WHERE suggestion_id='eq-current'"
        ).fetchone()
        intent = conn.execute(
            "SELECT status FROM governance_mutation_intent WHERE mutation_id=?",
            (suggestion["applied_mutation_id"],),
        ).fetchone()
        controls = load_live_policy_controls(
            conn,
            scope_type="entry_quality",
            allowed_actions={"raise_weak_signal_threshold"},
            limit=20,
            coordinator_mode="enforce",
        )
    finally:
        conn.close()
    assert suggestion["status"] == "applied"
    assert suggestion["applied_mutation_id"]
    assert intent["status"] == "committed"
    assert controls[0]["suggestion_id"] == "eq-current"
    evidence = json.loads(controls[0]["evidence_json"])
    assert evidence["recommended_controls"]["min_abs_signal_score"] == 0.5

    conn = sqlite3.connect(db_path)
    try:
        cycle_ts = conn.execute(
            "SELECT cycle_ts FROM learning_application_log"
        ).fetchone()[0]
        for idx in range(5):
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, entry_quality, pnl, mfe,
                 outcome_label, failure_tags_json, review_json, created_at)
                VALUES (?, ?, ?, 0.3, -10.0, 0.0, 'bad_loss', ?, ?, ?)
                """,
                (
                    f"pre-{idx}",
                    f"pre-{idx}",
                    f"pre-pos-{idx}",
                    json.dumps(["weak_signal_overtraded"]),
                    json.dumps({"signal_score": 0.3, "entry_quality": 0.3}),
                    cycle_ts - 10 + idx,
                ),
            )
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, entry_quality, pnl, mfe,
                 outcome_label, failure_tags_json, review_json, created_at)
                VALUES (?, ?, ?, 0.7, 10.0, 5.0, 'good_win', '[]', ?, ?)
                """,
                (
                    f"post-{idx}",
                    f"post-{idx}",
                    f"post-pos-{idx}",
                    json.dumps({"signal_score": 0.8, "entry_quality": 0.7}),
                    cycle_ts + 10 + idx,
                ),
            )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, entry_quality, pnl, mfe,
             outcome_label, failure_tags_json, review_json, created_at)
            VALUES ('post-duplicate', 'post-duplicate', 'post-pos-0',
                    0.7, 10.0, 5.0, 'good_win', '[]', ?, ?)
            """,
            (
                json.dumps({"signal_score": 0.8, "entry_quality": 0.7}),
                cycle_ts + 20,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    reconciled = RuleEvolutionGovernor(str(db_path)).reconcile_application_effects(
        min_trades=5,
        observe_trades=5,
        baseline_min_trades=5,
    )
    assert reconciled["observed"] == 1
    conn = sqlite3.connect(db_path)
    try:
        effect = conn.execute(
            "SELECT observed_trade_count, decision_json FROM learning_application_effect"
        ).fetchone()
    finally:
        conn.close()
    decision = json.loads(effect[1])
    assert effect[0] == 5
    assert decision["evidence_quality"]["raw_post_count"] == 6
    assert decision["entry_quality_effect"]["post"]["distinct_positions"] == 5
    assert (
        decision["entry_quality_effect"]["post"]["below_applied_threshold_open_count"]
        == 0
    )

    invalidated = EntryQualityGovernanceService(
        db_path
    ).invalidate_legacy_applied_control(run_id="pytest-v1-invalidation")
    assert invalidated["ok"] is True
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        suggestion = conn.execute(
            "SELECT status, review_note FROM policy_suggestion WHERE suggestion_id='eq-current'"
        ).fetchone()
        controls = load_live_policy_controls(
            conn,
            scope_type="entry_quality",
            allowed_actions={"raise_weak_signal_threshold"},
            limit=20,
            coordinator_mode="enforce",
        )
    finally:
        conn.close()
    assert suggestion["status"] == "invalidated_evidence"
    assert suggestion["review_note"] == "entry_quality_v1_population_bias"
    assert controls == []


def test_manual_mode_never_auto_applies_entry_quality_control(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(
        "backend.services.entry_quality_governance.runtime_config",
        lambda: SimpleNamespace(autonomy_mode="manual"),
    )
    result = EntryQualityGovernanceService(db_path).apply_next_weak_signal(run_id="manual")
    assert result["status"] == "skipped_non_demo_mode"


def test_v2_control_atomically_replaces_applied_v1_control(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ensure_autonomous_learning_tables(db_path)
    monkeypatch.setattr(
        "backend.services.entry_quality_governance.runtime_config",
        lambda: SimpleNamespace(
            autonomy_mode="demo_autonomous",
            factor_signal_threshold=0.30,
        ),
    )
    monkeypatch.setattr(
        "backend.services.entry_quality_governance.RiskPolicyService.shared",
        lambda: _Risk(),
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, governance_eligible,
             governance_eligibility_version, governance_eligibility_fingerprint,
             governance_ineligible_reason, created_at)
            VALUES (?, 'entry_quality', 'weak_signal',
                    'raise_weak_signal_threshold', 0.9, 'test', ?, ?, 1, ?, ?, '', ?)
            """,
            [
                (
                    "legacy-v1",
                    json.dumps(
                        {
                            "schema_version": "entry_quality_governance_evidence.v1",
                            "recommended_controls": {
                                "min_abs_signal_score": 0.6389,
                                "strong_signal_override": 0.75,
                            },
                        }
                    ),
                    "applied",
                    GOVERNANCE_ELIGIBILITY_VERSION,
                    "legacy-fingerprint",
                    time.time() - 10,
                ),
                (
                    "current-v2",
                    json.dumps(
                        {
                            "schema_version": "entry_quality_governance_evidence.v2",
                            "recommended_controls": {
                                "min_abs_signal_score": 0.40,
                                "strong_signal_override": 0.70,
                            },
                        }
                    ),
                    "approved",
                    GOVERNANCE_ELIGIBILITY_VERSION,
                    "current-fingerprint",
                    time.time(),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    result = EntryQualityGovernanceService(db_path).apply_next_weak_signal(
        run_id="pytest-atomic-v2-replacement"
    )

    assert result["ok"] is True
    assert result["status"] == "committed"
    conn = sqlite3.connect(db_path)
    try:
        statuses = dict(
            conn.execute(
                "SELECT suggestion_id, status FROM policy_suggestion"
            ).fetchall()
        )
        intent = conn.execute(
            """
            SELECT before_json, target_json
            FROM governance_mutation_intent
            WHERE mutation_id=?
            """,
            (result["mutation"]["mutation_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert statuses == {
        "legacy-v1": "invalidated_evidence",
        "current-v2": "applied",
    }
    assert json.loads(intent[0])["min_abs_signal_score"] == 0.6389
    assert json.loads(intent[1])["min_abs_signal_score"] == 0.40
