from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

from backend.services.autonomous_learning import ensure_autonomous_learning_tables
from backend.services.entry_cluster_governance import EntryClusterGovernanceService
from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
from backend.services.live_committed_policy import load_live_policy_controls
from backend.services.live_learning_policy import (
    LiveLearningPolicyRuntime,
    entry_cluster_threshold,
    load_active_learning_policy,
)


class _AllowedVerdict:
    def to_dict(self):
        return {"allowed": True, "reason": "bounded_demo_risk_tightening"}


class _Risk:
    def evaluate(self, _action, _context):
        return _AllowedVerdict()


def test_entry_cluster_threshold_decodes_scope_bucket():
    assert entry_cluster_threshold("same_direction_ge_1") == 1
    assert entry_cluster_threshold("same_direction_ge_3") == 3
    assert entry_cluster_threshold("unparsable_bucket") == 0
    assert entry_cluster_threshold("") == 0


def test_demo_applies_entry_cluster_cooldown_as_committed_mutation(tmp_path, monkeypatch):
    """The surface had a live consumer but no writer: an approved cooldown
    suggestion stayed approved forever, so live never saw the control."""
    db_path = tmp_path / "state.db"
    ensure_autonomous_learning_tables(db_path)
    monkeypatch.setattr(
        "backend.services.entry_cluster_governance.runtime_config",
        lambda: SimpleNamespace(autonomy_mode="demo_autonomous"),
    )
    monkeypatch.setattr(
        "backend.services.entry_cluster_governance.RiskPolicyService.shared",
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
            VALUES ('ec-current', 'entry_cluster', 'same_direction_ge_1',
                    'increase_same_direction_cooldown', 0.8, 'clustered bad opens',
                    ?, 'approved', 1, ?, 'fingerprint-current', '', ?)
            """,
            (
                json.dumps(
                    {
                        "bucket": "same_direction_ge_1",
                        "sample_count": 12,
                        "bad_rate": 0.667,
                        "recommended_controls": {
                            "increase_same_direction_cooldown": True,
                            "raise_pyramid_entry_threshold": False,
                            "advisory_only": True,
                        },
                    }
                ),
                GOVERNANCE_ELIGIBILITY_VERSION,
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    service = EntryClusterGovernanceService(db_path)
    result = service.apply_next_cooldown(run_id="pytest-entry-cluster")

    assert result["ok"] is True
    assert result["status"] == "committed"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        suggestion = conn.execute(
            "SELECT status, applied_mutation_id FROM policy_suggestion "
            "WHERE suggestion_id='ec-current'"
        ).fetchone()
        intent = conn.execute(
            "SELECT status, risk_class FROM governance_mutation_intent WHERE mutation_id=?",
            (suggestion["applied_mutation_id"],),
        ).fetchone()
        controls = load_live_policy_controls(
            conn,
            scope_type="entry_cluster",
            allowed_actions={"increase_same_direction_cooldown"},
            limit=20,
            coordinator_mode="enforce",
        )
    finally:
        conn.close()

    assert suggestion["status"] == "applied"
    assert suggestion["applied_mutation_id"]
    assert intent["status"] == "committed"
    # Activating the cooldown only removes entries, so the classifier must see
    # a tightening mutation (which is what exempts it from a V16 claim).
    assert intent["risk_class"] == "risk_tightening"
    assert controls[0]["suggestion_id"] == "ec-current"

    def _factory(*, read_only: bool = False):
        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
        return connection

    policy = load_active_learning_policy(
        "entry_cluster",
        runtime=LiveLearningPolicyRuntime(
            connection_factory=_factory,
            load_controls=load_live_policy_controls,
            cache={},
            cache_lock=__import__("threading").Lock(),
            warning=lambda *args, **kwargs: None,
            now=time.time,
        ),
    )
    assert policy["active"] is True
    assert policy["min_same_direction_open_count"] == 1

    application = __import__(
        "backend.services.learning_application_store", fromlist=["LearningApplicationStore"]
    ).LearningApplicationStore(str(db_path)).latest_application(
        scope_type="entry_cluster", scope_key="same_direction_ge_1"
    )
    assert (application or {}).get("status") == "observing"

    # Idempotent: the applied row is no longer eligible.
    again = service.apply_next_cooldown(run_id="pytest-entry-cluster-2")
    assert again["status"] == "skipped_no_eligible_entry_cluster_suggestion"
