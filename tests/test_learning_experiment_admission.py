import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.experience_prior import ExperiencePriorService
from backend.services.learning_application_store import LearningApplicationStore
from backend.services.learning_experiment_admission import LearningExperimentAdmissionService


def _db(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    conn.executescript(STATE_DB_DDL)
    conn.commit()
    conn.close()
    return db_path


def test_admission_blocks_active_scope_and_immaterial_weight_delta(tmp_path):
    db_path = _db(tmp_path)
    now = time.time()
    store = LearningApplicationStore(db_path)
    app_id = store.prepare_application(
        scope_type="factor",
        scope_key="rsi_14",
        action="update_weight",
        status="applied",
        cycle_ts=now,
        details={"created_at": now},
    )
    store.write_effect(
        application_id=app_id,
        scope_key="rsi_14",
        scope_type="factor",
        action="update_weight",
        status="observing",
        updated_at=now,
    )

    service = LearningExperimentAdmissionService(db_path)
    blocked = service.evaluate(
        scope_type="factor", scope_key="rsi_14", action="update_weight", old_weight=0.10, new_weight=0.08
    )
    assert blocked["allowed"] is False
    assert blocked["status"] == "blocked_active_experiment"

    store.update_effect(app_id, patch={"status": "reinforced"})
    conn = connect_sqlite(db_path)
    try:
        conn.execute("UPDATE learning_application_log SET status='reinforced' WHERE application_id=?", (app_id,))
        conn.commit()
    finally:
        conn.close()

    immaterial = service.evaluate(
        scope_type="factor", scope_key="rsi_14", action="update_weight", old_weight=0.10, new_weight=0.099
    )
    admitted = service.evaluate(
        scope_type="factor", scope_key="rsi_14", action="update_weight", old_weight=0.10, new_weight=0.09
    )
    structural = service.evaluate(scope_type="factor", scope_key="redundancy", action="update_redundancy_groups")
    assert immaterial["status"] == "blocked_immaterial_delta"
    assert admitted["allowed"] is True
    assert structural["effect_tracking"] == "not_trade_attributed"


def test_experience_prior_uses_only_terminal_bounded_effects(tmp_path):
    db_path = _db(tmp_path)
    now = time.time()
    bounded = {
        "evidence_quality": {
            "bounded_attribution_allowed": True,
            "target_regime": "trend",
        }
    }
    unbounded = {"evidence_quality": {"bounded_attribution_allowed": False}}
    store = LearningApplicationStore(db_path)
    store.write_effect(
        application_id="bounded", scope_key="rsi_14", scope_type="factor",
        action="update_weight", status="reinforced", observed_trade_count=5,
        delta_avg_reward=0.2, decision=bounded, updated_at=now,
    )
    store.write_effect(
        application_id="unbounded", scope_key="macd_hist", scope_type="factor",
        action="update_weight", status="ineffective", observed_trade_count=10,
        delta_avg_reward=-0.2, decision=unbounded, updated_at=now,
    )

    result = ExperiencePriorService(db_path).build(cache_seconds=0.0)
    assert result["eligible_count"] == 1
    assert result["rejected_unbounded_count"] == 1
    assert result["priors"]["rsi_14"]["multiplier"] == 1.1
    assert result["priors"]["rsi_14"]["confidence"] >= 0.6
    assert result["boundary"]["decision_policy_remains_authority"] is True


def test_admission_enforces_global_active_experiment_budget(tmp_path):
    db_path = _db(tmp_path)
    now = time.time()
    store = LearningApplicationStore(db_path)
    for name in ("active_1", "active_2"):
        app_id = store.prepare_application(
            scope_type="factor",
            scope_key=("factor_a" if name == "active_1" else "factor_b"),
            action="update_weight",
            status="applied",
            cycle_ts=now,
            details={"created_at": now},
        )
        store.write_effect(
            application_id=app_id,
            scope_key=("factor_a" if name == "active_1" else "factor_b"),
            scope_type="factor",
            action="update_weight",
            status="observing",
            updated_at=now,
        )

    service = LearningExperimentAdmissionService(db_path)
    result = service.evaluate(
        scope_type="factor",
        scope_key="factor_c",
        action="update_weight",
        old_weight=0.1,
        new_weight=0.08,
        max_global_active_experiments=2,
    )

    assert result["allowed"] is False
    assert result["status"] == "blocked_global_experiment_budget"
    assert result["global_active_count"] == 2
