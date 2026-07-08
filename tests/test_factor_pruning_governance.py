import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.brain_governance_candidates import BrainGovernanceCandidateService
from backend.services.factor_pruning_governance import FactorPruningGovernanceService
from research.learning.governor import RuleEvolutionGovernor


class _Verdict:
    def __init__(self, allowed=True, reason="test_allowed"):
        self.allowed = allowed
        self.reason = reason

    def to_dict(self):
        return {"allowed": self.allowed, "reason": self.reason, "audit_payload": {"test": True}}


class _Risk:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    def evaluate(self, action, context):
        self.calls.append((action, context))
        return self.verdict


def _source_candidate(priority=0.95):
    return {
        "ok": True,
        "schema_version": "factor_pruning_candidates.v1",
        "status": "actionable",
        "generated_count": 1,
        "candidates": [
            {
                "candidate_id": "factor_prune:dsl_auto_test",
                "schema_version": "factor_pruning_candidate.v1",
                "factor": "dsl_auto_test",
                "family": "dsl_auto",
                "current_weight": 0.01,
                "recommended_action": "review_disable",
                "suggested_target_weight": 0.0,
                "priority_score": priority,
                "confidence": 0.9,
                "reasons": [
                    {"code": "recent_live_decision_participation", "decision_review_count": 3},
                    {"code": "low_weight_tail", "threshold": 0.02},
                    {"code": "large_noise_family", "family": "dsl_auto", "family_count": 99},
                    {"code": "weak_factor_health", "score": 25.0, "status": "watch", "n_obs": 2000, "rolling_ic": -0.02},
                ],
                "evidence": {
                    "family_count": 99,
                    "active_alpha_count": 140,
                    "recent_decision_evidence": {"decision_review_count": 3, "loss_review_count": 2},
                },
            }
        ],
    }


def _source_live_harm_candidate(priority=0.9):
    return {
        "ok": True,
        "schema_version": "factor_pruning_candidates.v1",
        "status": "actionable",
        "generated_count": 1,
        "candidates": [
            {
                "candidate_id": "factor_prune:dsl_auto_hot_bad",
                "schema_version": "factor_pruning_candidate.v1",
                "factor": "dsl_auto_hot_bad",
                "family": "dsl_auto",
                "current_weight": 0.3,
                "recommended_action": "review_downweight",
                "suggested_target_weight": 0.15,
                "priority_score": priority,
                "confidence": 0.86,
                "reasons": [
                    {"code": "recent_live_decision_participation", "decision_review_count": 4},
                    {"code": "recent_loss_contribution_pressure", "loss_review_count": 3, "loss_abs_contribution": 0.08},
                    {"code": "loss_win_contribution_sign_flip"},
                    {"code": "system_issue_caveat", "signal_execution_delay_count": 1, "priority_bonus": 0.0},
                ],
                "evidence": {
                    "family_count": 99,
                    "active_alpha_count": 140,
                    "recent_decision_evidence": {
                        "decision_review_count": 4,
                        "loss_review_count": 3,
                        "win_review_count": 1,
                    },
                },
            }
        ],
    }


def test_factor_pruning_governance_materializes_candidate_lane_only(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()
    risk = _Risk(_Verdict(True))
    monkeypatch.setattr("backend.services.factor_pruning_governance.RiskPolicyService.shared", lambda: risk)
    monkeypatch.setattr(
        "backend.services.factor_pruning_governance.FactorPruningCandidateService.build",
        lambda self, limit=50: _source_candidate(),
    )

    result = FactorPruningGovernanceService(db_path).materialize_latest(limit=10, persist=True)

    assert result["schema_version"] == "factor_pruning_governance_run.v1"
    assert result["materialized_count"] == 1
    assert result["boundary"]["does_not_write_policy_suggestion_directly"] is True
    assert risk.calls[0][0] == "update_weight"

    conn = connect_sqlite(db_path)
    try:
        candidate = conn.execute(
            """
            SELECT candidate_id, source_agent, proposal_stage, scope_type, scope_key,
                   action, risk_verdict_json, decision_policy_json, expected_effect_json,
                   lineage_json
            FROM brain_governance_candidate
            WHERE candidate_id='factor_pruning:dsl_auto_test'
            """
        ).fetchone()
        suggestion_count = conn.execute("SELECT COUNT(*) FROM policy_suggestion").fetchone()[0]
    finally:
        conn.close()

    assert candidate is not None
    assert candidate[1] == "factor_pruning_governance"
    assert candidate[2] == "brain_candidate"
    assert candidate[3] == "factor"
    assert candidate[4] == "dsl_auto_test"
    assert candidate[5] == "downweight"
    assert json.loads(candidate[6])["allowed"] is True
    assert json.loads(candidate[7])["applied"] is False
    assert json.loads(candidate[8])["suggested_target_weight"] == 0.0
    lineage = json.loads(candidate[9])
    assert lineage["agent_context"]["schema_version"] == "agent_generation_context.v1"
    assert lineage["agent_context"]["source_agent"] == "factor_pruning_governance"
    assert lineage["agent_context"]["authority_verdict"]["allowed"] is True
    assert suggestion_count == 0

    second = FactorPruningGovernanceService(db_path).materialize_latest(limit=10, persist=True)
    conn = connect_sqlite(db_path)
    try:
        candidate_count = conn.execute(
            "SELECT COUNT(*) FROM brain_governance_candidate WHERE candidate_id='factor_pruning:dsl_auto_test'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert second["updated_count"] == 1
    assert candidate_count == 1


def test_factor_pruning_governance_blocks_when_risk_blocks(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("backend.services.factor_pruning_governance.RiskPolicyService.shared", lambda: _Risk(_Verdict(False, "blocked")))
    monkeypatch.setattr(
        "backend.services.factor_pruning_governance.FactorPruningCandidateService.build",
        lambda self, limit=50: _source_candidate(),
    )

    result = FactorPruningGovernanceService(db_path).materialize_latest(limit=10, persist=True)

    assert result["blocked_count"] == 1
    conn = connect_sqlite(db_path)
    try:
        candidate_count = conn.execute("SELECT COUNT(*) FROM brain_governance_candidate").fetchone()[0]
        suggestion_count = conn.execute("SELECT COUNT(*) FROM policy_suggestion").fetchone()[0]
    finally:
        conn.close()
    assert candidate_count == 0
    assert suggestion_count == 0


def test_factor_pruning_governance_promotes_ready_without_submitting_policy_suggestion(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("backend.services.factor_pruning_governance.RiskPolicyService.shared", lambda: _Risk(_Verdict(True)))
    monkeypatch.setattr(
        "backend.services.factor_pruning_governance.FactorPruningCandidateService.build",
        lambda self, limit=50: _source_candidate(),
    )
    service = FactorPruningGovernanceService(db_path)
    service.materialize_latest(limit=10, persist=True)

    promoted = service.promote_ready(limit=10, min_evidence_score=0.9, require_weak_health=True)

    assert promoted["promoted_count"] == 1
    conn = connect_sqlite(db_path)
    try:
        row = conn.execute(
            "SELECT proposal_stage, status, submitted_suggestion_id FROM brain_governance_candidate WHERE candidate_id='factor_pruning:dsl_auto_test'"
        ).fetchone()
        suggestion_count = conn.execute("SELECT COUNT(*) FROM policy_suggestion").fetchone()[0]
    finally:
        conn.close()
    assert row[0] == "governance_ready"
    assert row[1] == "active"
    assert row[2] == ""
    assert suggestion_count == 0

    preview = BrainGovernanceCandidateService(db_path).preview_policy_suggestion_bridge("factor_pruning:dsl_auto_test")

    assert preview["bridge_ready"] is True
    assert preview["policy_suggestion"]["scope_type"] == "factor"
    assert preview["policy_suggestion"]["action"] == "downweight"


def test_factor_pruning_governance_counter_evidence_blocks_promotion(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO shadow_factor_perf
            (factor, oos_bars, cumulative_pnl, hit_rate, updated_at)
            VALUES ('dsl_auto_test', 240, 150.0, 0.6, ?)
            """,
            (now,),
        )
        for idx in range(3):
            review_id = f"review_keep_{idx}"
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, pnl, outcome_label, review_json, created_at)
                VALUES (?, ?, 25.0, 'good_win', ?, ?)
                """,
                (review_id, f"trade_keep_{idx}", json.dumps({"regime": "trend"}), now + idx),
            )
            conn.execute(
                """
                INSERT INTO factor_contribution_review
                (review_id, trade_id, factor, net_contribution, confidence)
                VALUES (?, ?, 'dsl_auto_test', 0.2, 0.8)
                """,
                (review_id, f"trade_keep_{idx}"),
            )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("backend.services.factor_pruning_governance.RiskPolicyService.shared", lambda: _Risk(_Verdict(True)))
    monkeypatch.setattr(
        "backend.services.factor_pruning_governance.FactorPruningCandidateService.build",
        lambda self, limit=50: _source_candidate(),
    )
    service = FactorPruningGovernanceService(db_path)
    service.materialize_latest(limit=10, persist=True)

    promoted = service.promote_ready(limit=10, min_evidence_score=0.9, require_weak_health=True)

    assert promoted["promoted_count"] == 0
    assert promoted["blocked_count"] == 1
    assert "counter_evidence_keep_signal" in promoted["items"][0]["blockers"]
    conn = connect_sqlite(db_path)
    try:
        row = conn.execute(
            """
            SELECT proposal_stage, counter_evidence_refs_json
            FROM brain_governance_candidate
            WHERE candidate_id='factor_pruning:dsl_auto_test'
            """
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "brain_candidate"
    counter_refs = json.loads(row[1])
    assert counter_refs["factor_counter_evidence"]["recommended_stage"] == "block_pruning"


def test_factor_pruning_governance_promotes_live_loss_pressure_without_weak_health(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("backend.services.factor_pruning_governance.RiskPolicyService.shared", lambda: _Risk(_Verdict(True)))
    monkeypatch.setattr(
        "backend.services.factor_pruning_governance.FactorPruningCandidateService.build",
        lambda self, limit=50: _source_live_harm_candidate(),
    )
    service = FactorPruningGovernanceService(db_path)
    service.materialize_latest(limit=10, persist=True)

    promoted = service.promote_ready(limit=10, min_evidence_score=0.8, require_weak_health=True)

    assert promoted["promoted_count"] == 1
    assert promoted["items"][0]["factor"] == "dsl_auto_hot_bad"
    assert "recent_loss_contribution_pressure" in promoted["items"][0]["reason_codes"]


def test_factor_pruning_governance_bridges_and_governor_approves_in_demo_nursery(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("backend.services.factor_pruning_governance.RiskPolicyService.shared", lambda: _Risk(_Verdict(True)))
    monkeypatch.setattr(
        "backend.services.factor_pruning_governance.FactorPruningCandidateService.build",
        lambda self, limit=50: _source_candidate(),
    )
    monkeypatch.setattr(
        "config.runtime_config.shared",
        lambda: type("Cfg", (), {"autonomy_mode": "demo_nursery"})(),
    )
    service = FactorPruningGovernanceService(db_path)
    service.materialize_latest(limit=10, persist=True)
    service.promote_ready(limit=10)

    bridge = service.bridge_ready_candidates(limit=5, require_demo_nursery=True)

    assert bridge["submitted_count"] == 1
    assert bridge["items"][0]["suggestion_id"]
    conn = connect_sqlite(db_path)
    try:
        proposed = conn.execute(
            "SELECT suggestion_id, scope_type, scope_key, action, status, evidence_json FROM policy_suggestion"
        ).fetchone()
        candidate = conn.execute(
            "SELECT proposal_stage, status, submitted_suggestion_id FROM brain_governance_candidate WHERE candidate_id='factor_pruning:dsl_auto_test'"
        ).fetchone()
    finally:
        conn.close()
    assert proposed[1] == "factor"
    assert proposed[2] == "dsl_auto_test"
    assert proposed[3] == "downweight"
    assert proposed[4] == "proposed"
    assert "factor_pruning_governance" in proposed[5]
    assert candidate[0] == "submitted_to_policy_suggestion"
    assert candidate[1] == "submitted"
    assert candidate[2] == proposed[0]

    reviewed = RuleEvolutionGovernor(str(db_path)).review_pending()

    assert reviewed["approved"] == 1
    conn = connect_sqlite(db_path)
    try:
        approved_status = conn.execute("SELECT status, review_note FROM policy_suggestion WHERE suggestion_id=?", (proposed[0],)).fetchone()
    finally:
        conn.close()
    assert approved_status[0] == "approved"
    assert "factor pruning governance evidence" in approved_status[1]


def test_factor_pruning_governance_auto_bridge_requires_candidate_review(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action,
             suggestion_ids_json, status, details_json, created_at)
            VALUES ('app_bad_pruning_agent', ?, 'factor', 'dsl_auto_test', 'downweight',
                    '[]', 'applied', '{"source_agent":"factor_pruning_governance"}', ?)
            """,
            (now - 10, now - 10),
        )
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status,
             delta_avg_reward, updated_at, created_at)
            VALUES ('app_bad_pruning_agent', 'factor', 'dsl_auto_test', 'downweight',
                    'ineffective', -0.2, ?, ?)
            """,
            (now - 5, now - 5),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("backend.services.factor_pruning_governance.RiskPolicyService.shared", lambda: _Risk(_Verdict(True)))
    monkeypatch.setattr(
        "backend.services.factor_pruning_governance.FactorPruningCandidateService.build",
        lambda self, limit=50: _source_candidate(),
    )
    monkeypatch.setattr(
        "config.runtime_config.shared",
        lambda: type("Cfg", (), {"autonomy_mode": "demo_nursery"})(),
    )
    service = FactorPruningGovernanceService(db_path)
    service.materialize_latest(limit=10, persist=True)
    service.promote_ready(limit=10)

    bridge = service.bridge_ready_candidates(limit=5, require_demo_nursery=True)

    assert bridge["submitted_count"] == 0
    assert bridge["blocked_count"] == 1
    assert bridge["items"][0]["status"] == "blocked_candidate_review"
    assert "agent_negative_effect_history_requires_counter_evidence" in bridge["items"][0]["evidence_gaps"]
    assert bridge["boundary"]["candidate_review_required_before_bridge"] is True
    conn = connect_sqlite(db_path, read_only=True)
    try:
        proposed = conn.execute("SELECT COUNT(*) FROM policy_suggestion").fetchone()[0]
        review = conn.execute(
            """
            SELECT review_status, bridge_ready, evidence_gaps_json
            FROM brain_governance_candidate_review
            WHERE candidate_id='factor_pruning:dsl_auto_test'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    assert proposed == 0
    assert review is not None
    assert review[0] == "needs_evidence"
    assert review[1] == 0
    assert "agent_negative_effect_history_requires_counter_evidence" in review[2]


def test_factor_pruning_governance_bridges_live_harm_candidate_and_governor_approves(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("backend.services.factor_pruning_governance.RiskPolicyService.shared", lambda: _Risk(_Verdict(True)))
    monkeypatch.setattr(
        "backend.services.factor_pruning_governance.FactorPruningCandidateService.build",
        lambda self, limit=50: _source_live_harm_candidate(),
    )
    monkeypatch.setattr(
        "config.runtime_config.shared",
        lambda: type("Cfg", (), {"autonomy_mode": "demo_nursery"})(),
    )
    service = FactorPruningGovernanceService(db_path)
    service.materialize_latest(limit=10, persist=True)
    service.promote_ready(limit=10, min_evidence_score=0.8)
    bridge = service.bridge_ready_candidates(limit=5, require_demo_nursery=True)

    assert bridge["submitted_count"] == 1
    reviewed = RuleEvolutionGovernor(str(db_path)).review_pending()

    assert reviewed["approved"] == 1
    conn = connect_sqlite(db_path)
    try:
        approved_status = conn.execute("SELECT status, scope_key, review_note, evidence_json FROM policy_suggestion").fetchone()
    finally:
        conn.close()
    assert approved_status[0] == "approved"
    assert approved_status[1] == "dsl_auto_hot_bad"
    assert "factor pruning governance evidence" in approved_status[2]
    evidence = json.loads(approved_status[3])
    assert evidence["bridge"]["candidate_review_required_before_submit"] is True
    assert evidence["bridge"]["candidate_review"]["bridge_ready"] is True
