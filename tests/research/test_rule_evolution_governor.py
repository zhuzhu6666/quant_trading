from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from backend.services.canonical_v2 import (
    ensure_sqlite_schema,
    record_decision_event,
    record_review,
)
from backend.services.parameter_templates import ParameterTemplateService
from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
from backend.services.learning_application_store import LearningApplicationStore
from research.learning.governor import RuleEvolutionGovernor


@pytest.fixture(autouse=True)
def _parameter_template_coordinator_mode(monkeypatch):
    """Exercise the post-f2eb9c9 governed activation path in these tests."""
    import backend.core.static_feature_flags as static_feature_flags

    monkeypatch.setattr(
        static_feature_flags,
        "shared_static_feature_flags",
        lambda: SimpleNamespace(
            governance_mutation_coordinator_v2_mode="dual_record",
        ),
    )


def _approve_parameter_template_suggestion(db_path: str, suggestion_id: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE policy_suggestion
            SET status='approved', governance_eligible=1,
                governance_eligibility_version=?,
                governance_eligibility_fingerprint=?
            WHERE suggestion_id=?
            """,
            (GOVERNANCE_ELIGIBILITY_VERSION, "pytest-eligibility", suggestion_id),
        )
        conn.commit()
    finally:
        conn.close()


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_sqlite_schema(conn)
    return conn


def _record_review(
    conn: sqlite3.Connection,
    *,
    review_id: str,
    trade_id: str,
    position_id: str = "",
    entry_decision_id: str = "",
    pnl: float = 0.0,
    outcome_label: str = "",
    created_at: float,
    review_payload: dict | None = None,
) -> None:
    record_review(
        conn,
        review_id=review_id,
        trade_id=trade_id,
        position_id=position_id,
        entry_decision_id=entry_decision_id,
        pnl=pnl,
        outcome_label=outcome_label,
        failure_tags=[],
        review=review_payload or {"context_integrity": "full"},
        created_at=created_at,
    )


def _record_factor_review(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    factor: str,
    review_id: str,
    trade_id: str,
    position_id: str,
    ts: float,
    pnl: float,
    outcome_label: str,
    review_payload: dict | None = None,
) -> None:
    record_decision_event(
        conn,
        decision_id=decision_id,
        event_type="entry_signal",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=ts,
        factor_snapshots=[
            {
                "decision_id": decision_id,
                "factor": factor,
                "contribution_score": pnl / 100.0,
            }
        ],
    )
    _record_review(
        conn,
        review_id=review_id,
        trade_id=trade_id,
        position_id=position_id,
        entry_decision_id=decision_id,
        pnl=pnl,
        outcome_label=outcome_label,
        created_at=ts,
        review_payload=review_payload,
    )


def _store(path: str) -> LearningApplicationStore:
    return LearningApplicationStore(str(path))


def _load_json(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value) if isinstance(value, str) else value
    except Exception:
        return default


def _app(path: str, application_id: str) -> dict:
    app = _store(path).get_application(str(application_id))
    return app if app is not None else {}


def _apps(path: str, **kwargs) -> list[dict]:
    return list(_store(path).iter_applications(**kwargs))


def _effect(path: str, application_id: str) -> dict:
    aid = str(application_id)
    for eff in _store(path).iter_effects():
        if str(eff.get("application_id")) == aid:
            return eff
    return {}


def _effects(path: str, **kwargs) -> list[dict]:
    return list(_store(path).iter_effects(**kwargs))


def _set_app_field(path: str, application_id: str, key: str, value):
    """Update a single caller field inside details_json (lean column only)."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT details_json FROM learning_application_log WHERE application_id=?",
            (str(application_id),),
        ).fetchone()
        data = _load_json(row["details_json"], {})
        data[key] = value
        conn.execute(
            "UPDATE learning_application_log SET details_json=? WHERE application_id=?",
            (json.dumps(data, ensure_ascii=False), str(application_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _set_effect_field(path: str, application_id: str, key: str, value):
    """Update a single field inside effect_json (lean column only)."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT effect_json FROM learning_application_effect WHERE application_id=?",
            (str(application_id),),
        ).fetchone()
        data = _load_json(row["effect_json"], {})
        data[key] = value
        conn.execute(
            "UPDATE learning_application_effect SET effect_json=? WHERE application_id=?",
            (json.dumps(data, ensure_ascii=False), str(application_id)),
        )
        conn.commit()
    finally:
        conn.close()


def test_governor_reviews_pending_and_rolls_back(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO experience_pattern_stats
            (scope_type, scope_key, sample_count, win_count, bad_loss_count,
             avg_reward, effective_sample_count, weighted_win_count,
             weighted_bad_loss_count, weighted_avg_reward,
             governance_eligibility_version, governance_eligibility_fingerprint,
             last_outcome_label, recommended_action, updated_at)
            VALUES
            ('factor', 'fragile_factor', 4, 1, 3, -0.45, 4.0, 1.0, 3.0, -0.45,
             ?, 'fragile-fingerprint', 'bad_loss', 'downweight', 1.0),
            ('factor', 'strong_factor', 5, 4, 0, 0.32, 5.0, 4.0, 0.0, 0.32,
             ?, 'strong-fingerprint', 'good_win', 'boost_small', 1.0)
            """,
            (GOVERNANCE_ELIGIBILITY_VERSION, GOVERNANCE_ELIGIBILITY_VERSION),
        )
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             status, governance_eligible, governance_eligibility_version,
             governance_eligibility_fingerprint, created_at)
            VALUES
            ('p1', 'factor', 'fragile_factor', 'downweight', 0.8, 'test',
             'proposed', 1, ?, 'fragile-fingerprint', 1.0),
            ('p2', 'factor', 'strong_factor', 'boost_small', 0.7, 'test',
             'proposed', 1, ?, 'strong-fingerprint', 1.0)
            """,
            (GOVERNANCE_ELIGIBILITY_VERSION, GOVERNANCE_ELIGIBILITY_VERSION),
        )
        conn.commit()
    finally:
        conn.close()

    reviewed = gov.review_pending()
    assert reviewed["approved"] == 2

    items = gov.list_suggestions(status="approved")
    assert {i["scope_key"] for i in items} == {"fragile_factor", "strong_factor"}

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE experience_pattern_stats
            SET sample_count=6, avg_reward=0.20,
                effective_sample_count=6.0, weighted_avg_reward=0.20
            WHERE scope_key='fragile_factor'
            """
        )
        conn.commit()
    finally:
        conn.close()

    reconciled = gov.reconcile_active()
    assert reconciled["rolled_back"] == 1

    rolled = gov.list_suggestions(status="rolled_back")
    assert rolled[0]["scope_key"] == "fragile_factor"


def test_governor_accepts_eligible_demo_model_bridge_without_experience_stats(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    evidence = {
        "schema_version": "factor_governance_advisory.v1",
        "source_agent": "lightgbm_shadow_models",
        "model_type": "factor_governance_lightgbm",
        "advisory_only": True,
        "sample_count": 20,
        "weak_sample_count": 2,
        "min_weakness_score": 0.85,
        "avg_weakness_score": 0.92,
        "governed_action": "downweight",
        "promotion_gate": {"passed": True, "reason": "promotion_gate_passed"},
        "mutation_eligible": True,
        "artifact_sha256": "test-artifact",
        "factor_generation": "runtime_bounded_v1",
        "lineage_hash": "test-lineage",
        "label_contract_hash": "test-label-contract",
        "candidate_id": "factor_model:model_bridge_1",
        "counter_evidence_refs": {
            "factor_counter_evidence": {
                "status": "observed",
                "recommended_stage": "governance_ready",
            }
        },
        "active_factor_context": {"used_in_score": True, "role": "alpha"},
        "bridge": {
            "automatic_demo": True,
            "demo_nursery": True,
            "actor": "system:autonomous_learning.demo_nursery_model_governance",
            "candidate_review_required_before_submit": True,
            "candidate_review": {
                "review_id": "brain_candidate_review_model_bridge_1",
                "bridge_ready": True,
            },
        },
    }
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, governance_eligible,
             governance_eligibility_version,
             governance_eligibility_fingerprint, created_at)
            VALUES ('model_bridge_1', 'factor', 'weak_factor', 'downweight',
                    0.55, 'model evidence', ?, 'proposed', 1, ?, ?, 1.0)
            """,
            (
                json.dumps(evidence),
                GOVERNANCE_ELIGIBILITY_VERSION,
                "eligible-model-bridge-fingerprint",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    reviewed = gov.review_pending()

    assert reviewed["approved"] == 1
    row = _connect(db_path).execute(
        "SELECT status, review_note FROM policy_suggestion WHERE suggestion_id='model_bridge_1'"
    ).fetchone()
    assert row["status"] == "approved"
    assert "factor model evidence bridged" in row["review_note"]


def test_governor_rejects_model_bridge_without_eligibility_contract(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    evidence = {
        "schema_version": "factor_governance_advisory.v1",
        "source_agent": "lightgbm_shadow_models",
        "model_type": "factor_governance_lightgbm",
        "advisory_only": True,
        "sample_count": 2,
        "weak_sample_count": 2,
        "min_weakness_score": 0.85,
        "avg_weakness_score": 0.92,
        "governed_action": "downweight",
        "active_factor_context": {"used_in_score": True, "role": "alpha"},
        "bridge": {
            "automatic_demo": True,
            "demo_nursery": True,
            "actor": "system:autonomous_learning.demo_nursery_model_governance",
        },
    }
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, created_at)
            VALUES ('model_bridge_unverified', 'factor', 'weak_factor',
                    'downweight', 0.9, 'model evidence', ?, 'proposed', 1.0)
            """,
            (json.dumps(evidence),),
        )
        conn.commit()
    finally:
        conn.close()

    reviewed = gov.review_pending()

    assert reviewed["approved"] == 0
    assert reviewed["rejected"] == 1
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """SELECT status, governance_eligible,
                      governance_ineligible_reason
               FROM policy_suggestion
               WHERE suggestion_id='model_bridge_unverified'"""
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "rejected"
    assert row["governance_eligible"] == 0
    assert row["governance_ineligible_reason"] == "eligibility_contract_invalid"


def test_governor_logs_learning_application(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="factor",
        scope_key="foo",
        action="downweight",
        bias_multiplier=0.84,
        old_weight=0.5,
        new_weight=0.42,
        suggestion_ids=["s1", "s2"],
        cycle_ts=1234.0,
        details={"note": "demo"},
    )
    assert app_id

    app = _app(db_path, app_id)
    assert app.get("application_id") == app_id
    assert app.get("scope_key") == "foo"
    assert float(app.get("new_weight")) == 0.42


def test_governor_reuses_active_application_for_same_suggestion(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    first_id = gov.log_application(
        scope_type="factor",
        scope_key="foo",
        action="boost_small",
        bias_multiplier=1.04,
        old_weight=0.2,
        new_weight=0.208,
        suggestion_ids=["s1"],
        cycle_ts=100.0,
        details={"note": "first"},
    )
    second_id = gov.log_application(
        scope_type="factor",
        scope_key="foo",
        action="boost_small",
        bias_multiplier=1.05,
        old_weight=0.21,
        new_weight=0.2205,
        suggestion_ids=["s1"],
        cycle_ts=200.0,
        details={"note": "refresh"},
    )

    assert second_id == first_id

    apps = _apps(db_path)
    assert len(apps) == 1
    app = apps[0]
    assert float(app.get("cycle_ts")) == 200.0
    assert float(app.get("new_weight")) == 0.2205
    assert app.get("note") == "refresh"
    eff = _effect(db_path, first_id)
    assert eff.get("decision", {}).get("new_weight") == 0.2205


def test_governor_supersedes_older_duplicate_active_applications(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    first_id = gov.log_application(
        scope_type="factor",
        scope_key="foo",
        action="boost_small",
        bias_multiplier=1.04,
        old_weight=0.2,
        new_weight=0.208,
        suggestion_ids=["s1"],
        cycle_ts=100.0,
        details={"note": "first"},
    )

    # Seed an older duplicate active application for the same suggestion batch,
    # written through the store (newest duplicate gets reused, this one superseded).
    store = _store(db_path)
    dup_old = store.prepare_application(
        scope_type="factor",
        scope_key="foo",
        action="boost_small",
        status="observing",
        bias_multiplier=1.03,
        old_weight=0.19,
        new_weight=0.1957,
        suggestion_ids=["s1"],
        cycle_ts=90.0,
        details={},
    )
    store.write_effect(
        application_id=dup_old,
        scope_type="factor",
        scope_key="foo",
        action="boost_small",
        status="observing",
        decision={},
        last_review_at=0.0,
        updated_at=90.0,
    )

    second_id = gov.log_application(
        scope_type="factor",
        scope_key="foo",
        action="boost_small",
        bias_multiplier=1.05,
        old_weight=0.21,
        new_weight=0.2205,
        suggestion_ids=["s1"],
        cycle_ts=200.0,
        details={"note": "refresh"},
    )

    assert second_id == first_id

    apps = _apps(db_path)
    status_by_id = {str(a["application_id"]): str(a.get("status") or "") for a in apps}
    eff_status_by_id = {
        str(e["application_id"]): str(e.get("status") or "") for e in _effects(db_path)
    }
    assert status_by_id == {
        str(dup_old): "superseded",
        first_id: "applied",
    }
    assert eff_status_by_id == {
        str(dup_old): "superseded",
        first_id: "observing",
    }


def test_reconcile_application_effects_marks_ineffective_and_rolls_back(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="factor",
        scope_key="fragile_factor",
        action="boost_small",
        bias_multiplier=1.05,
        old_weight=0.5,
        new_weight=0.525,
        suggestion_ids=["s1"],
        cycle_ts=200.0,
        details={"note": "demo"},
    )

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, status, created_at)
            VALUES ('s1', 'factor', 'fragile_factor', 'boost_small', 0.8, 'test', 'approved', 100.0)
            """
        )
        for idx, ts in enumerate((100.0, 120.0, 140.0, 220.0, 240.0, 260.0), start=1):
            decision_id = f"dec_{idx}"
            pnl = 60.0 if ts < 200.0 else -90.0
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            _record_factor_review(
                conn,
                decision_id=decision_id,
                factor="fragile_factor",
                review_id=f"r_{idx}",
                trade_id=f"t_{idx}",
                position_id=f"p_{idx}",
                ts=ts,
                pnl=pnl,
                outcome_label="bad_loss" if pnl < 0 else "lucky_win",
                review_payload=review_payload,
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)
    assert result["observed"] == 1
    assert result["rolled_back"] == 1

    conn = _connect(db_path)
    try:
        app_row = conn.execute(
            "SELECT status, details_json FROM learning_application_log WHERE application_id=?",
            (app_id,),
        ).fetchone()
        suggestion_row = conn.execute(
            "SELECT status, review_note FROM policy_suggestion WHERE suggestion_id='s1'"
        ).fetchone()
    finally:
        conn.close()
    eff = _effect(db_path, app_id)

    assert app_row["status"] == "ineffective"
    assert suggestion_row["status"] == "rolled_back"
    assert eff.get("status") == "ineffective"
    assert int(eff.get("observed_trade_count")) == 3
    assert int(eff.get("baseline_trade_count")) == 3
    assert float(eff.get("delta_avg_reward") or 0.0) < 0


def test_reconcile_application_effects_reinforces_positive_application(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="factor",
        scope_key="strong_factor",
        action="boost_small",
        bias_multiplier=1.05,
        old_weight=0.3,
        new_weight=0.315,
        suggestion_ids=["s2"],
        cycle_ts=200.0,
        details={"note": "demo"},
    )

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, status, created_at)
            VALUES ('s2', 'factor', 'strong_factor', 'boost_small', 0.7, 'test', 'approved', 100.0)
            """
        )
        samples = (
            (100.0, 20.0),
            (120.0, 25.0),
            (140.0, 18.0),
            (220.0, 95.0),
            (240.0, 85.0),
            (260.0, 90.0),
        )
        for idx, (ts, pnl) in enumerate(samples, start=1):
            decision_id = f"strong_dec_{idx}"
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            _record_factor_review(
                conn,
                decision_id=decision_id,
                factor="strong_factor",
                review_id=f"sr_{idx}",
                trade_id=f"st_{idx}",
                position_id=f"sp_{idx}",
                ts=ts,
                pnl=pnl,
                outcome_label="lucky_win",
                review_payload=review_payload,
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)
    assert result["observed"] == 1
    assert result["reinforced"] == 1

    conn = _connect(db_path)
    try:
        app_row = conn.execute(
            "SELECT status FROM learning_application_log WHERE application_id=?",
            (app_id,),
        ).fetchone()
        reinforced_rows = conn.execute(
            """
            SELECT suggestion_id, status, action, review_note
            FROM policy_suggestion
            WHERE scope_key='strong_factor'
            ORDER BY created_at DESC
            """
        ).fetchall()
    finally:
        conn.close()
    eff = _effect(db_path, app_id)

    assert app_row["status"] == "reinforced"
    assert eff.get("status") == "reinforced"
    assert int(eff.get("observed_trade_count")) == 3
    assert int(eff.get("baseline_trade_count")) == 3
    assert float(eff.get("delta_avg_reward") or 0.0) > 0
    assert len(reinforced_rows) == 2
    assert reinforced_rows[0]["status"] == "proposed"
    assert reinforced_rows[0]["action"] == "boost_small"


def test_reconcile_application_effects_observes_position_supervisor_template(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="position_supervisor_template",
        scope_key="position_supervisor:profit_protection.v1",
        action="switch_position_supervisor_template",
        bias_multiplier=1.0,
        old_weight=0.0,
        new_weight=0.0,
        suggestion_ids=["psv1"],
        cycle_ts=200.0,
        details={
            "previous_template_id": "position_supervisor:default.v1",
            "target_template_id": "position_supervisor:profit_protection.v1",
        },
    )

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, status, created_at)
            VALUES ('psv1', 'position_supervisor_template', 'position_supervisor:profit_protection.v1',
                    'switch_position_supervisor_template', 0.7, 'test', 'approved', 100.0)
            """
        )
        samples = (
            (100.0, "pre_1", 4.0, 4.2, 0.10, 0.70),
            (120.0, "pre_2", 3.0, 3.5, 0.12, 0.65),
            (140.0, "pre_3", 2.5, 3.0, 0.15, 0.60),
            (220.0, "post_1", -2.0, 3.0, 0.96, 0.02),
            (240.0, "post_2", -2.5, 2.8, 0.94, 0.03),
            (260.0, "post_3", -1.8, 2.6, 0.92, 0.04),
        )
        for ts, review_id, pnl, mfe, giveback, capture in samples:
            review_payload = {
                "real_pnl": {"net": pnl},
                "close_reason": "broker_close",
                "close_reason_source": "supervisor_tighten_stopout",
                "context_integrity": "full",
                "mfe": mfe,
                "giveback_ratio": giveback,
                "profit_capture_ratio": capture,
                "inferred_close_supervisor": {
                    "event_type": "supervisor_tighten",
                    "action": "tighten",
                    "action_reason": "profit_giveback_after_mfe",
                },
            }
            _record_review(
                conn,
                review_id=review_id,
                trade_id=f"trade_{review_id}",
                position_id=f"pos_{review_id}",
                entry_decision_id=f"dec_{review_id}",
                pnl=pnl,
                outcome_label="bad_loss" if pnl < 0 else "good_win",
                created_at=ts,
                review_payload=review_payload,
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)
    assert result["observed"] == 1
    assert result["rolled_back"] == 1

    conn = _connect(db_path)
    try:
        suggestion_row = conn.execute(
            "SELECT status FROM policy_suggestion WHERE suggestion_id='psv1'"
        ).fetchone()
    finally:
        conn.close()
    eff = _effect(db_path, app_id)

    assert eff.get("status") == "ineffective"
    assert int(eff.get("observed_trade_count")) == 3
    assert int(eff.get("baseline_trade_count")) == 3
    assert float(eff.get("delta_avg_reward") or 0.0) < 0
    assert suggestion_row["status"] == "rolled_back"


def test_reconcile_application_effects_waits_when_baseline_too_thin(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="factor",
        scope_key="new_factor",
        action="boost_small",
        bias_multiplier=1.04,
        old_weight=0.2,
        new_weight=0.208,
        suggestion_ids=[],
        cycle_ts=200.0,
        details={},
    )

    conn = _connect(db_path)
    try:
        samples = (
            (120.0, 15.0),
            (220.0, 60.0),
            (240.0, 70.0),
            (260.0, 55.0),
        )
        for idx, (ts, pnl) in enumerate(samples, start=1):
            decision_id = f"new_dec_{idx}"
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            _record_factor_review(
                conn,
                decision_id=decision_id,
                factor="new_factor",
                review_id=f"nr_{idx}",
                trade_id=f"nt_{idx}",
                position_id=f"np_{idx}",
                ts=ts,
                pnl=pnl,
                outcome_label="lucky_win",
                review_payload=review_payload,
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)
    assert result["observed"] == 1
    assert result["waiting"] == 1
    assert result["reinforced"] == 0
    assert result["rolled_back"] == 0

    conn = _connect(db_path)
    try:
        app_row = conn.execute(
            "SELECT status, details_json FROM learning_application_log WHERE application_id=?",
            (app_id,),
        ).fetchone()
    finally:
        conn.close()
    eff = _effect(db_path, app_id)

    assert app_row["status"] == "observing"
    assert eff.get("status") == "observing"
    assert int(eff.get("observed_trade_count")) == 3
    assert int(eff.get("baseline_trade_count")) == 1


def test_demo_reconcile_terminalizes_comparatively_mixed_effects(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    app_id = gov.log_application(
        scope_type="factor",
        scope_key="mixed_factor",
        action="downweight",
        bias_multiplier=0.88,
        old_weight=0.4,
        new_weight=0.35,
        suggestion_ids=[],
        cycle_ts=1_700_000_000.0,
        details={},
    )
    conn = _connect(db_path)
    try:
        for idx in range(3):
            decision_id = f"mixed_post_{idx}"
            _record_factor_review(
                conn,
                decision_id=decision_id,
                factor="mixed_factor",
                review_id=f"mixed_post_review_{idx}",
                trade_id=f"mixed_trade_{idx}",
                position_id=f"mixed_post_position_{idx}",
                ts=1_700_000_100.0 + idx,
                pnl=1.0,
                outcome_label="mixed",
                review_payload={},
            )
        for idx in range(3):
            decision_id = f"mixed_pre_{idx}"
            _record_factor_review(
                conn,
                decision_id=decision_id,
                factor="mixed_factor",
                review_id=f"mixed_pre_review_{idx}",
                trade_id=f"mixed_pre_trade_{idx}",
                position_id=f"mixed_pre_position_{idx}",
                ts=1_699_999_900.0 + idx,
                pnl=1.0,
                outcome_label="mixed",
                review_payload={},
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(
        min_trades=3,
        observe_trades=3,
        baseline_min_trades=2,
        terminalize_mixed_after_recheck=True,
    )

    assert result["inconclusive"] == 1
    assert _effect(db_path, app_id).get("status") == "inconclusive"


def test_reconcile_application_effects_waits_when_no_post_reviews(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)

    app_id = gov.log_application(
        scope_type="factor",
        scope_key="sleeping_factor",
        action="downweight",
        bias_multiplier=0.88,
        old_weight=0.4,
        new_weight=0.352,
        suggestion_ids=[],
        cycle_ts=200.0,
        details={},
    )

    conn = _connect(db_path)
    try:
        for idx, ts in enumerate((100.0, 120.0, 140.0), start=1):
            decision_id = f"sleep_dec_{idx}"
            pnl = -30.0
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            _record_factor_review(
                conn,
                decision_id=decision_id,
                factor="sleeping_factor",
                review_id=f"sleep_r_{idx}",
                trade_id=f"sleep_t_{idx}",
                position_id=f"sleep_p_{idx}",
                ts=ts,
                pnl=pnl,
                outcome_label="good_loss",
                review_payload=review_payload,
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)
    assert result["observed"] == 1
    assert result["waiting"] == 1

    conn = _connect(db_path)
    try:
        app_row = conn.execute(
            "SELECT status FROM learning_application_log WHERE application_id=?",
            (app_id,),
        ).fetchone()
    finally:
        conn.close()
    eff = _effect(db_path, app_id)

    assert app_row["status"] == "observing"
    assert eff.get("status") == "observing"
    assert int(eff.get("observed_trade_count")) == 0
    assert int(eff.get("baseline_trade_count")) == 3
    assert float(eff.get("last_review_at") or 0.0) == 0.0


def test_reconcile_parameter_template_effects_rolls_back_active_template(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    svc = ParameterTemplateService(db_path)

    old_template = svc.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "range",
            "factor_family": "momentum_oscillator",
            "template_version": "range_safe.v1",
            "template_role": "default",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 18, "upper_band": 72, "lower_band": 28},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 10},
            "evidence": {"note": "old"},
        },
        source="manual",
    )
    new_template = svc.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "range",
            "factor_family": "momentum_oscillator",
            "template_version": "range_fast.v1",
            "template_role": "aggressive",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 14, "upper_band": 70, "lower_band": 30},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["low_vol"],
            "holding_profile_hint": {"style": "fast_reversion", "min_bars": 1, "max_bars": 6},
            "evidence": {"note": "new"},
        },
        source="manual",
    )
    svc.activate_template(
        factor_id="rsi_14",
        regime_key="range",
        template_id=old_template["template_id"],
        note="baseline active",
    )
    switch = svc.create_switch_suggestion(
        factor_id="rsi_14",
        regime_key="range",
        template_id=new_template["template_id"],
        note="proposed switch",
    )
    gov.set_status(switch["suggestion_id"], "approved", "approved for test")
    _approve_parameter_template_suggestion(db_path, switch["suggestion_id"])
    applied = svc.activate_template(
        factor_id="rsi_14",
        regime_key="range",
        template_id=new_template["template_id"],
        suggestion_id=switch["suggestion_id"],
        note="apply new template",
    )
    assert applied["ok"] is True

    apps = [a["application_id"] for a in _apps(db_path, scope_type="parameter_template")]
    assert len(apps) >= 2
    _set_app_field(db_path, apps[-1], "cycle_ts", 80.0)
    _set_app_field(db_path, apps[0], "cycle_ts", 200.0)
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE learning_application_log SET status='superseded' WHERE application_id=?",
            (str(apps[-1]),),
        )
        conn.commit()
    finally:
        conn.close()

    conn = _connect(db_path)
    try:
        baseline_samples = (
            (120.0, 55.0),
            (140.0, 45.0),
        )
        for idx, (ts, pnl) in enumerate(baseline_samples, start=1):
            decision_id = f"base_dec_{idx}"
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            _record_factor_review(
                conn,
                decision_id=decision_id,
                factor="rsi_14",
                review_id=f"base_r_{idx}",
                trade_id=f"base_t_{idx}",
                position_id=f"base_p_{idx}",
                ts=ts,
                pnl=pnl,
                outcome_label="good_win",
                review_payload=review_payload,
            )

        post_samples = (
            (300.0, -60.0),
            (320.0, -45.0),
            (340.0, -50.0),
        )
        for idx, (ts, pnl) in enumerate(post_samples, start=1):
            decision_id = f"post_dec_{idx}"
            review_payload = {"real_pnl": {"net": pnl}, "close_reason": "broker_close", "context_integrity": "full"}
            _record_factor_review(
                conn,
                decision_id=decision_id,
                factor="rsi_14",
                review_id=f"post_r_{idx}",
                trade_id=f"post_t_{idx}",
                position_id=f"post_p_{idx}",
                ts=ts,
                pnl=pnl,
                outcome_label="bad_loss",
                review_payload=review_payload,
            )
        conn.commit()
    finally:
        conn.close()

    result = gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)

    assert result["rolled_back"] == 1

    active = svc.get_active_template(factor_id="rsi_14", regime_key="range")
    logs = svc.list_switch_logs(factor_id="rsi_14", limit=10)

    assert active["template_id"] == old_template["template_id"]
    assert logs[0]["status"] == "rolled_back"
    assert logs[0]["new_template_id"] == old_template["template_id"]


def test_conflict_resolver_supersedes_factor_boost_when_entry_quality_suppresses_same_factor(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, evidence_json, status, created_at)
            VALUES
            ('boost1', 'factor', 'ema_slope', 'boost_small', 0.9, 'test', '{}', 'approved', 100.0),
            ('suppress1', 'entry_quality', 'ema_slope', 'suppress_recent_worst_factor', 0.7, 'test',
             '{"suppressed_factor":"ema_slope"}', 'approved', 90.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = gov.resolve_conflicts()

    assert result["superseded"] == 1
    conn = _connect(db_path)
    try:
        rows = {
            row["suggestion_id"]: row["status"]
            for row in conn.execute("SELECT suggestion_id, status FROM policy_suggestion").fetchall()
        }
    finally:
        conn.close()
    assert rows["boost1"] == "superseded"
    assert rows["suppress1"] == "approved"


def test_conflict_resolver_never_supersedes_applied_control(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, created_at)
            VALUES
            ('applied_old', 'entry_quality', 'weak_signal',
             'raise_weak_signal_threshold', 0.9, 'test', '{}', 'applied', 90.0),
            ('approved_new', 'entry_quality', 'weak_signal',
             'raise_weak_signal_threshold', 0.95, 'test', '{}', 'approved', 100.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    gov.resolve_conflicts()

    conn = _connect(db_path)
    try:
        rows = {
            row["suggestion_id"]: row["status"]
            for row in conn.execute(
                "SELECT suggestion_id, status FROM policy_suggestion"
            ).fetchall()
        }
    finally:
        conn.close()
    assert rows["applied_old"] == "applied"
    assert rows["approved_new"] == "approved"


def test_conflict_resolver_template_switch_blocks_same_factor_weight_change(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, evidence_json, status, created_at)
            VALUES
            ('weight1', 'factor', 'rsi_14', 'downweight', 0.9, 'test', '{}', 'approved', 100.0),
            ('tpl1', 'parameter_template', 'rsi_14:range', 'switch_parameter_template', 0.6, 'test',
             '{"factor_id":"rsi_14","regime_key":"range","target_template_id":"tpl_new"}', 'approved', 80.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    gov.resolve_conflicts()

    conn = _connect(db_path)
    try:
        rows = {
            row["suggestion_id"]: row["status"]
            for row in conn.execute("SELECT suggestion_id, status FROM policy_suggestion").fetchall()
        }
    finally:
        conn.close()
    assert rows["weight1"] == "superseded"
    assert rows["tpl1"] == "approved"


def test_governor_approves_online_light_parameter_template_switch(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, governance_eligible,
             governance_eligibility_version,
             governance_eligibility_fingerprint, created_at)
            VALUES
            ('tpl_switch', 'parameter_template', 'rsi_14:range', 'switch_parameter_template', 0.7, 'test',
             '{"factor_id":"rsi_14","regime_key":"range","target_template_id":"tpl_new","boundary":{"recommended_scope":"online_light"}}',
             'proposed', 1, ?, 'eligible-template-switch', 100.0)
            """
            ,
            (GOVERNANCE_ELIGIBILITY_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()

    result = gov.review_pending()

    assert result["approved"] == 1
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, review_note FROM policy_suggestion WHERE suggestion_id='tpl_switch'"
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "approved"
    assert "online_light" in row["review_note"]


def test_conflict_resolver_prefers_current_v16_lineage_over_legacy_supervisor_priority(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, governance_eligible,
             governance_eligibility_version, governance_eligibility_fingerprint,
             created_at)
            VALUES
            ('legacy_auto_tpsl', 'position_supervisor_template',
             'position_supervisor:auto_tpsl.v2', 'switch_position_supervisor_template',
             0.95, 'legacy',
             '{"target_template_id":"position_supervisor:auto_tpsl.v2"}',
             'approved', 1, ?, 'legacy-fingerprint', 200.0),
            ('v16_current_bridge', 'position_supervisor_template',
             'position_supervisor:conservative.v1', 'switch_position_supervisor_template',
             0.55, 'v16', ?, 'approved', 1, ?, 'v16-fingerprint', 100.0)
            """,
            (
                GOVERNANCE_ELIGIBILITY_VERSION,
                json.dumps(
                    {
                        "candidate_id": "brain_candidate_current",
                        "source_agent": "v16_brain",
                        "target_template_id": "position_supervisor:conservative.v1",
                        "bridge": {
                            "command_owner": "v16_brain",
                            "candidate_review": {"bridge_ready": True},
                        },
                        "lineage": {"parent_policy_decision_id": "parent-current"},
                    }
                ),
                GOVERNANCE_ELIGIBILITY_VERSION,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = gov.resolve_conflicts()

    assert result["superseded"] == 1
    conn = _connect(db_path)
    try:
        rows = {
            row["suggestion_id"]: row["status"]
            for row in conn.execute(
                "SELECT suggestion_id, status FROM policy_suggestion"
            ).fetchall()
        }
    finally:
        conn.close()
    assert rows["v16_current_bridge"] == "approved"
    assert rows["legacy_auto_tpsl"] == "superseded"


def test_conflict_resolver_keeps_auto_tpsl_over_stale_supervisor_template(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason, evidence_json, status, created_at)
            VALUES
            ('old_profit', 'position_supervisor_template', 'position_supervisor:profit_protection.v1',
             'switch_position_supervisor_template', 0.95, 'test', '{}', 'approved', 200.0),
            ('auto_tpsl', 'position_supervisor_template', 'position_supervisor:auto_tpsl.v2',
             'switch_position_supervisor_template', 0.7, 'test',
             '{"target_template_id":"position_supervisor:auto_tpsl.v2"}', 'approved', 100.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    gov.resolve_conflicts()

    conn = _connect(db_path)
    try:
        rows = {
            row["suggestion_id"]: row["status"]
            for row in conn.execute("SELECT suggestion_id, status FROM policy_suggestion").fetchall()
        }
    finally:
        conn.close()
    assert rows["old_profit"] == "superseded"
    assert rows["auto_tpsl"] == "approved"


def test_effect_reconciliation_filters_contaminated_and_wrong_regime_reviews():
    reviews = [
        {
            "review": {"regime_id": "range", "context_integrity": "full"},
            "failure_tags": [],
        },
        {
            "review": {"regime_id": "trend", "context_integrity": "full"},
            "failure_tags": [],
        },
        {
            "review": {"regime_id": "range", "context_integrity": "partial"},
            "failure_tags": ["partial_context"],
        },
        {
            "review": {
                "regime_id": "range",
                "context_integrity": "full",
                "system_issue_context": {"contaminates_learning": True},
            },
            "failure_tags": [],
        },
    ]

    comparable, contaminated, mismatched = RuleEvolutionGovernor._comparable_reviews(
        reviews,
        regime="range",
    )

    assert len(comparable) == 1
    assert contaminated == 2
    assert mismatched == 1


def test_effect_reconciliation_uses_stronger_unstratified_fallback_when_exact_baseline_is_thin():
    post = [
        {"review_id": f"post_{idx}", "review": {"regime_id": "trend" if idx < 3 else "range", "context_integrity": "full"}}
        for idx in range(5)
    ]
    baseline = [
        {"review_id": f"pre_{idx}", "review": {"regime_id": "range", "context_integrity": "full"}}
        for idx in range(5)
    ]

    selected = RuleEvolutionGovernor._select_effect_comparison(
        post,
        baseline,
        target_regime="trend",
        min_trades=3,
        baseline_min_trades=2,
        observe_trades=5,
    )

    selected_post, selected_baseline, _, _, post_mismatch, baseline_mismatch, basis = selected
    assert len(selected_post) == 5
    assert len(selected_baseline) == 5
    assert post_mismatch == 2
    assert baseline_mismatch == 5
    assert basis == "unstratified_bounded"


def test_effect_reconciliation_closes_window_before_concurrent_same_scope_change(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    first_id = gov.log_application(
        scope_type="factor",
        scope_key="rsi_14",
        action="downweight",
        bias_multiplier=0.9,
        old_weight=0.3,
        new_weight=0.27,
        suggestion_ids=[],
        cycle_ts=200.0,
    )
    gov.log_application(
        scope_type="factor",
        scope_key="rsi_14",
        action="boost_small",
        bias_multiplier=1.05,
        old_weight=0.27,
        new_weight=0.2835,
        suggestion_ids=[],
        cycle_ts=230.0,
    )
    conn = _connect(db_path)
    try:
        for idx, (ts, pnl) in enumerate(((100.0, -20.0), (120.0, -10.0), (240.0, 80.0), (260.0, 90.0), (280.0, 85.0))):
            decision_id = f"confound_dec_{idx}"
            _record_factor_review(
                conn,
                decision_id=decision_id,
                factor="rsi_14",
                review_id=f"confound_review_{idx}",
                trade_id=f"confound_trade_{idx}",
                position_id=f"confound_position_{idx}",
                ts=ts,
                pnl=pnl,
                outcome_label="good_win" if pnl > 0 else "bad_loss",
                review_payload={"context_integrity": "full", "close_reason": "broker_close"},
            )
        conn.commit()
    finally:
        conn.close()

    gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)

    eff = _effect(db_path, first_id)
    decision = eff.get("decision") or {}
    assert eff.get("status") == "inconclusive"
    assert decision["evidence_quality"]["causal_status"] == "bounded_window_insufficient_samples"
    assert decision["evidence_quality"]["observation_window"]["end_ts"] == 230.0
    assert decision["evidence_quality"]["bounded_attribution_allowed"] is False


def test_effect_reconciliation_uses_only_evidence_before_next_same_scope_change(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    first_id = gov.log_application(
        scope_type="factor",
        scope_key="bounded_factor",
        action="downweight",
        bias_multiplier=0.9,
        old_weight=0.3,
        new_weight=0.27,
        suggestion_ids=[],
        cycle_ts=200.0,
    )
    gov.log_application(
        scope_type="factor",
        scope_key="bounded_factor",
        action="boost_small",
        bias_multiplier=1.05,
        old_weight=0.27,
        new_weight=0.2835,
        suggestion_ids=[],
        cycle_ts=230.0,
    )
    conn = _connect(db_path)
    try:
        samples = ((100.0, -20.0), (120.0, -10.0), (205.0, 80.0), (210.0, 90.0), (220.0, 85.0), (240.0, -100.0))
        for idx, (ts, pnl) in enumerate(samples):
            decision_id = f"bounded_dec_{idx}"
            _record_factor_review(
                conn,
                decision_id=decision_id,
                factor="bounded_factor",
                review_id=f"bounded_review_{idx}",
                trade_id=f"bounded_trade_{idx}",
                position_id=f"bounded_position_{idx}",
                ts=ts,
                pnl=pnl,
                outcome_label="good_win" if pnl > 0 else "bad_loss",
                review_payload={"context_integrity": "full", "close_reason": "broker_close"},
            )
        conn.commit()
    finally:
        conn.close()

    gov.reconcile_application_effects(min_trades=3, observe_trades=3, baseline_min_trades=2)

    eff = _effect(db_path, first_id)
    decision = eff.get("decision") or {}
    assert eff.get("status") == "reinforced"
    assert eff.get("observed_trade_count") == 3
    assert decision["evidence_quality"]["causal_status"] == "bounded_comparative_effective"
    assert "bounded_review_5" not in decision["post_review_ids"]


def test_mixed_effect_is_rechecked_after_cooldown(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    app_id = gov.log_application(
        scope_type="factor",
        scope_key="mixed_factor",
        action="downweight",
        bias_multiplier=0.9,
        old_weight=0.3,
        new_weight=0.27,
        suggestion_ids=[],
        cycle_ts=200.0,
    )
    gov.reconcile_application_effects()
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE learning_application_log SET status='mixed' WHERE application_id=?", (app_id,))
        conn.commit()
    finally:
        conn.close()
    _set_effect_field(db_path, app_id, "status", "mixed")
    _set_effect_field(db_path, app_id, "updated_at", 0)

    result = gov.reconcile_application_effects(mixed_recheck_after_seconds=300.0)

    assert result["rechecked_mixed"] == 1
    assert result["observed"] == 1
    assert result["waiting"] == 1


def test_observation_window_expires_as_inconclusive(tmp_path):
    db_path = str(tmp_path / "state.db")
    gov = RuleEvolutionGovernor(db_path)
    app_id = gov.log_application(
        scope_type="factor",
        scope_key="expired_factor",
        action="downweight",
        bias_multiplier=0.9,
        old_weight=0.3,
        new_weight=0.27,
        suggestion_ids=[],
        cycle_ts=1_700_000_000.0,
    )

    result = gov.reconcile_application_effects(max_observation_age_seconds=86400.0)

    assert result["inconclusive"] == 1
    eff = _effect(db_path, app_id)
    assert eff.get("status") == "inconclusive"
    assert (eff.get("decision") or {}).get("evidence_quality", {}).get(
        "retry_via_new_application"
    ) is True
