from __future__ import annotations

import json
import sqlite3
import time

import pytest

from backend.services import autonomous_learning as learning
from research.learning.governor import RuleEvolutionGovernor


def _sample(index: int, *, integrity: str = "full", contaminated: bool = False) -> dict:
    decision_id = f"decision-{index}"
    return {
        "sample_id": f"sample-{index}",
        "sample_type": "shadow_open_decision",
        "source_table": "decision_ledger",
        "source_id": decision_id,
        "decision_id": decision_id,
        "position_id": f"position-{index}",
        "event_ts": time.time() + index,
        "label_status": "matured",
        "executable_governance_allowed": True,
        "integrity": integrity,
        "verified_recovered": integrity == "recovered",
        "train_weight": 1.0,
        "causal_level": "intervention_observed",
        "features": {
            "action": {"same_direction_open_count": 2},
            "entry_cluster": {
                "same_direction_open_count_before": 2,
                "pyramid_depth": 2,
            },
        },
        "verdict": {
            "system_contamination": {
                "contaminated": contaminated,
            }
        },
        "label": {
            "label": "open_outcome",
            "outcome_label": "bad_loss",
            "pnl": -10.0,
            "failure_tags": ["entry_cluster_risk"],
            "system_contaminated": contaminated,
        },
        "trace": {
            "decision_id": decision_id,
            "position_id": f"position-{index}",
            "verified_recovered": integrity == "recovered",
        },
    }


def _insert_sample(db_path, item: dict) -> None:
    learning.ensure_autonomous_learning_tables(db_path)
    conn = learning._connect(db_path)
    try:
        assert learning._upsert_sample(conn, item) is True
        conn.commit()
    finally:
        conn.close()


def test_sample_upsert_persists_full_recovered_and_contaminated_eligibility(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    _insert_sample(db_path, _sample(1))
    _insert_sample(db_path, _sample(2, integrity="recovered"))
    _insert_sample(db_path, _sample(3, contaminated=True))
    unverified = _sample(4, integrity="recovered")
    unverified["verified_recovered"] = False
    unverified["trace"].pop("verified_recovered")
    _insert_sample(db_path, unverified)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT sample_id, system_contaminated, governance_eligible,
                   governance_effective_weight, governance_eligibility_version,
                   governance_eligibility_fingerprint, governance_ineligible_reason
            FROM autonomous_learning_sample
            ORDER BY sample_id
            """
        ).fetchall()
    finally:
        conn.close()

    assert rows[0][1:4] == (0, 1, 1.0)
    assert rows[1][1:4] == (0, 1, 0.5)
    assert rows[2][1:4] == (1, 0, 0.0)
    assert "system_contaminated" in rows[2][6]
    assert rows[3][1:4] == (0, 0, 0.0)
    assert "integrity_recovered" in rows[3][6]
    assert all(row[4] == learning.GOVERNANCE_ELIGIBILITY_VERSION for row in rows)
    assert all(len(row[5]) == 64 for row in rows)
    health = learning.validate_evidence_contract_health(db_path=db_path)
    assert health["counts"]["contaminated_governance_eligible"] == 0
    assert health["counts"]["contaminated_allows_strong_governance"] == 0
    assert health["counts"]["contaminated_quality_model_ready"] == 0
    assert health["counts"]["contaminated_quality_executable_governance"] == 0


def test_weighted_materializer_uses_effective_sample_size(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    for index in (1, 2):
        _insert_sample(db_path, _sample(index))
    for index in (3, 4):
        _insert_sample(db_path, _sample(index, integrity="recovered"))

    result = learning.materialize_entry_cluster_governance_suggestions(
        db_path=db_path,
        min_samples=3,
        min_bad_rate=0.5,
    )

    assert result["suggestions"] == 1
    conn = sqlite3.connect(db_path)
    try:
        stats = conn.execute(
            """
            SELECT sample_count, effective_sample_count, weighted_bad_loss_count,
                   weighted_avg_reward, governance_eligibility_version,
                   governance_eligibility_fingerprint
            FROM experience_pattern_stats
            WHERE scope_type='entry_cluster' AND scope_key='same_direction_ge_2'
            """
        ).fetchone()
        suggestion = conn.execute(
            """
            SELECT governance_eligible, governance_eligibility_version,
                   governance_eligibility_fingerprint, evidence_json
            FROM policy_suggestion
            WHERE scope_type='entry_cluster'
            """
        ).fetchone()
    finally:
        conn.close()

    assert stats[:4] == pytest.approx((4, 3.0, 3.0, -0.2))
    assert stats[4] == learning.GOVERNANCE_ELIGIBILITY_VERSION
    assert len(stats[5]) == 64
    assert suggestion[:3] == (1, learning.GOVERNANCE_ELIGIBILITY_VERSION, stats[5])
    evidence = json.loads(suggestion[3])
    assert evidence["effective_sample_count"] == 3.0
    assert evidence["weighted_bad_count"] == 3.0


def test_governor_rejects_missing_contract_and_uses_weighted_metrics(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    learning.ensure_autonomous_learning_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        now = time.time()
        for scope_key, fingerprint in (
            ("missing-contract", ""),
            ("weighted-contract", "f" * 64),
        ):
            conn.execute(
                """
                INSERT INTO experience_pattern_stats
                (scope_type, scope_key, sample_count, win_count, bad_loss_count,
                 avg_reward, effective_sample_count, weighted_win_count,
                 weighted_bad_loss_count, weighted_avg_reward,
                 governance_eligibility_version, governance_eligibility_fingerprint,
                 recommended_action, updated_at)
                VALUES ('entry_cluster', ?, 20, 8, 12, -0.2, 10.0, 4.0,
                        6.0, -0.2, ?, ?, 'increase_same_direction_cooldown', ?)
                """,
                (
                    scope_key,
                    learning.GOVERNANCE_ELIGIBILITY_VERSION if fingerprint else "",
                    fingerprint,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO policy_suggestion
                (suggestion_id, scope_type, scope_key, action, confidence,
                 evidence_json, status, governance_eligible,
                 governance_eligibility_version, governance_eligibility_fingerprint,
                 created_at)
                VALUES (?, 'entry_cluster', ?, 'increase_same_direction_cooldown',
                        0.8, '{}', 'proposed', ?, ?, ?, ?)
                """,
                (
                    f"suggestion-{scope_key}",
                    scope_key,
                    1 if fingerprint else 0,
                    learning.GOVERNANCE_ELIGIBILITY_VERSION if fingerprint else "",
                    fingerprint,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = RuleEvolutionGovernor(str(db_path)).review_pending()

    assert result["approved"] == 1
    assert result["rejected"] == 1
    conn = sqlite3.connect(db_path)
    try:
        statuses = dict(
            conn.execute(
                "SELECT scope_key, status FROM policy_suggestion ORDER BY scope_key"
            ).fetchall()
        )
    finally:
        conn.close()
    assert statuses == {
        "missing-contract": "rejected",
        "weighted-contract": "approved",
    }


def test_repair_backfills_eligibility_and_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    learning.ensure_autonomous_learning_tables(db_path)
    item = _sample(10)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO autonomous_learning_sample
            (sample_id, sample_type, source_table, source_id, decision_id,
             position_id, event_ts, label_status, integrity, train_weight,
             features_json, verdict_json, label_json, trace_json,
             evidence_contract_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["sample_id"],
                item["sample_type"],
                item["source_table"],
                item["source_id"],
                item["decision_id"],
                item["position_id"],
                item["event_ts"],
                item["label_status"],
                item["integrity"],
                item["train_weight"],
                json.dumps(item["features"]),
                json.dumps(item["verdict"]),
                json.dumps(item["label"]),
                json.dumps(item["trace"]),
                json.dumps({"quality": {"executable_governance_allowed": True}}),
                time.time(),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    first = learning.repair_evidence_contracts(db_path=db_path)
    second = learning.repair_evidence_contracts(db_path=db_path)

    assert first["repaired"] == 1
    assert second["repaired"] == 0
    sample = learning.list_autonomous_learning_samples(db_path=db_path)["items"][0]
    assert sample["governance_eligible"] is True
    assert sample["governance_effective_weight"] == 1.0
    assert len(sample["governance_eligibility_fingerprint"]) == 64


def test_repair_does_not_infer_executable_governance_from_sample_type(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    learning.ensure_autonomous_learning_tables(db_path)
    item = _sample(11)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO autonomous_learning_sample
            (sample_id, sample_type, source_table, source_id, decision_id,
             position_id, event_ts, label_status, integrity, train_weight,
             features_json, verdict_json, label_json, trace_json,
             evidence_contract_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
            """,
            (
                item["sample_id"],
                item["sample_type"],
                item["source_table"],
                item["source_id"],
                item["decision_id"],
                item["position_id"],
                item["event_ts"],
                item["label_status"],
                item["integrity"],
                item["train_weight"],
                json.dumps(item["features"]),
                json.dumps(item["verdict"]),
                json.dumps(item["label"]),
                json.dumps(item["trace"]),
                time.time(),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    learning.repair_evidence_contracts(db_path=db_path)
    sample = learning.list_autonomous_learning_samples(db_path=db_path)["items"][0]

    assert sample["governance_eligible"] is False
    assert "executable_governance_not_allowed" in sample["governance_ineligible_reason"]
