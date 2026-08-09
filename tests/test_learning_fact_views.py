from __future__ import annotations

import sqlite3

import backend.api.learning as learning_api
from backend.services.learning_fact_views import (
    DurableSourceObservation,
    active_parameter_templates_fact_payload,
    autonomous_samples_fact_payload,
    dataset_readiness_fact_payload,
    factor_governance_advisories_fact_payload,
    learning_reviews_fact_payload,
    learning_summary_fact_payload,
    lifecycle_fact_payload,
    model_inference_audits_fact_payload,
    model_offmarket_high_load_audits_fact_payload,
    model_open_quality_audits_fact_payload,
    model_position_quality_audits_fact_payload,
    model_shadow_queue_fact_payload,
    observe_learning_dataset_source,
    suggestions_fact_payload,
)


def test_durable_list_uses_endpoint_persisted_timestamp_and_preserves_payload() -> None:
    payload = {
        "items": [
            {"suggestion_id": "s1", "created_at": 900.0, "reviewed_at": 950.0},
            {"suggestion_id": "s2", "created_at": 980.0, "reviewed_at": 0.0},
        ],
        "legacy": "kept",
    }

    result = suggestions_fact_payload(payload, now=1000.0)

    assert result["legacy"] == "kept"
    assert result["items"] == payload["items"]
    assert result["_fact"]["contract"] == "learning.suggestions.v2"
    assert result["_fact"]["state"] == "known"
    assert result["_fact"]["observed_at"] == 980.0
    assert result["_fact"]["components"]["timestamp_fields"] == [
        "reviewed_at",
        "created_at",
    ]


def test_old_persisted_record_is_stale_and_missing_timestamp_is_unknown() -> None:
    stale = lifecycle_fact_payload(
        {"items": [{"id": 1, "ts": 700.0}]},
        now=1000.0,
    )
    unknown = model_shadow_queue_fact_payload(
        {"items": [{"candidate_id": "candidate-without-time"}], "count": 1},
        now=1000.0,
    )

    assert stale["_fact"]["state"] == "stale"
    assert stale["_fact"]["reason_code"] == "freshness_expired"
    assert unknown["_fact"]["state"] == "unknown"
    assert unknown["_fact"]["reason_code"] == "persisted_timestamp_missing"


def test_authoritative_empty_query_is_known_and_cache_keeps_first_observation() -> None:
    initial = active_parameter_templates_fact_payload({"items": []}, now=1000.0)
    cached_render = active_parameter_templates_fact_payload(initial, now=1100.0)

    assert initial["_fact"]["state"] == "known"
    assert initial["_fact"]["observed_at"] == 1000.0
    assert initial["_fact"]["components"]["authoritative_empty"] is True
    assert cached_render["_fact"]["state"] == "known"
    assert cached_render["_fact"]["observed_at"] == 1000.0
    assert cached_render["_fact"]["generated_at"] == 1100.0


def test_last_good_fallback_is_reported_as_error_without_dropping_old_fields() -> None:
    result = suggestions_fact_payload(
        {
            "items": [{"suggestion_id": "s1", "created_at": 990.0}],
            "stale": True,
            "stale_reason": "compute_error",
        },
        now=1000.0,
    )

    assert result["items"][0]["suggestion_id"] == "s1"
    assert result["stale"] is True
    assert result["_fact"]["state"] == "error"
    assert result["_fact"]["reason_code"] == "compute_error"


def test_advisory_fact_uses_source_audit_time_not_advisory_render_time() -> None:
    result = factor_governance_advisories_fact_payload(
        {"items": [{"suggestion_id": "fgm_1"}], "count": 1},
        audit_observed_at=700.0,
        audit_count=3,
        now=1000.0,
    )

    assert result["_fact"]["state"] == "stale"
    assert result["_fact"]["observed_at"] == 700.0
    assert result["_fact"]["components"]["source_audit_count"] == 3


def test_learning_and_model_console_contracts_use_explicit_endpoint_timestamps() -> None:
    summary = learning_summary_fact_payload(
        {
            "suggestions": {"proposed": 1},
            "latest_review": {"review_id": "r1", "created_at": 990.0},
            "decoy": {"created_at": 999.0},
        },
        now=1000.0,
    )
    assert summary["_fact"]["contract"] == "learning.summary.v2"
    assert summary["_fact"]["observed_at"] == 990.0

    summary_without_time = learning_summary_fact_payload(
        {"suggestions": {"proposed": 1}},
        now=1000.0,
    )
    assert summary_without_time["_fact"]["state"] == "unknown"
    assert summary_without_time["_fact"]["reason_code"] == "persisted_timestamp_missing"

    cases = [
        (
            learning_reviews_fact_payload,
            {"items": [{"created_at": 991.0}]},
            "learning.reviews.v2",
            991.0,
        ),
        (
            autonomous_samples_fact_payload,
            {"items": [{"event_ts": 980.0, "updated_at": 992.0}]},
            "learning.autonomous-samples.v2",
            992.0,
        ),
        (
            model_position_quality_audits_fact_payload,
            {"items": [{"created_at": 993.0}]},
            "learning.model-position-quality-audits.v2",
            993.0,
        ),
        (
            model_open_quality_audits_fact_payload,
            {"items": [{"created_at": 994.0}]},
            "learning.model-open-quality-audits.v2",
            994.0,
        ),
        (
            model_inference_audits_fact_payload,
            {"items": [{"created_at": 995.0}]},
            "learning.model-inference-audits.v2",
            995.0,
        ),
        (
            model_offmarket_high_load_audits_fact_payload,
            {"items": [{"started_at": 980.0, "finished_at": 996.0}]},
            "learning.model-offmarket-high-load-audits.v2",
            996.0,
        ),
    ]
    for adapter, payload, contract, observed_at in cases:
        result = adapter(payload, now=1000.0)
        assert result["_fact"]["contract"] == contract
        assert result["_fact"]["observed_at"] == observed_at
        assert result["_fact"]["state"] == "known"


def _create_dataset_source(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE autonomous_learning_sample (
                updated_at REAL,
                event_ts REAL,
                created_at REAL
            );
            CREATE TABLE decision_ledger (
                decision_ts REAL,
                created_at REAL
            );
            CREATE TABLE trade_outcome_review (
                created_at REAL
            );
            """
        )
        conn.execute(
            "INSERT INTO autonomous_learning_sample VALUES (?, ?, ?)",
            (930.0, 920.0, 910.0),
        )
        conn.execute("INSERT INTO decision_ledger VALUES (?, ?)", (970.0, 960.0))
        conn.execute("INSERT INTO trade_outcome_review VALUES (?)", (950.0,))
        conn.commit()
    finally:
        conn.close()


def test_dataset_observation_reads_real_persistent_timestamps(tmp_path) -> None:
    db_path = tmp_path / "state-test.db"
    _create_dataset_source(str(db_path))

    observation = observe_learning_dataset_source(
        db_path,
        include_trade_reviews=True,
        now=1000.0,
    )
    payload = dataset_readiness_fact_payload(
        {"ready": False, "level": "warming_up"},
        observation=observation,
        now=1000.0,
    )

    assert observation.error is None
    assert observation.record_count == 3
    assert observation.observed_at == 970.0
    assert payload["ready"] is False
    assert payload["_fact"]["state"] == "known"
    assert payload["_fact"]["observed_at"] == 970.0


def test_dataset_observation_marks_missing_schema_as_error(tmp_path) -> None:
    db_path = tmp_path / "partial-state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE autonomous_learning_sample (updated_at REAL, event_ts REAL, created_at REAL)"
        )
        conn.commit()
    finally:
        conn.close()

    observation = observe_learning_dataset_source(
        db_path,
        include_trade_reviews=True,
        now=1000.0,
    )
    payload = dataset_readiness_fact_payload(
        {"ready": False},
        observation=observation,
        now=1000.0,
    )

    assert observation.error is not None
    assert "decision_ledger" in observation.error
    assert payload["_fact"]["state"] == "error"


def test_dataset_authoritative_empty_observation_can_be_known() -> None:
    observation = DurableSourceObservation(
        observed_at=1000.0,
        authoritative_empty=True,
        record_count=0,
        tables=("autonomous_learning_sample", "decision_ledger"),
    )
    payload = dataset_readiness_fact_payload(
        {"ready": False, "blockers": []},
        observation=observation,
        now=1000.0,
    )

    assert payload["_fact"]["state"] == "known"
    assert payload["_fact"]["components"]["authoritative_empty"] is True


def test_active_template_endpoint_keeps_legacy_shape_and_attaches_fact(monkeypatch) -> None:
    class _Templates:
        db_path = "/tmp/parameter-template-test.db"

        @staticmethod
        def list_active_templates(*, factor_id=None):
            assert factor_id == "rsi_14"
            return [
                {
                    "factor_id": "rsi_14",
                    "template_id": "rsi.default.v1",
                    "activated_at": 990.0,
                    "updated_at": 995.0,
                }
            ]

    monkeypatch.setattr(learning_api, "ParameterTemplateService", _Templates)
    learning_api._LEARNING_CACHE.clear()
    learning_api._LEARNING_LAST_GOOD.clear()

    result = learning_api.get_active_parameter_templates(None, factor_id="rsi_14")

    assert list(result)[:1] == ["items"]
    assert result["items"][0]["template_id"] == "rsi.default.v1"
    assert result["_fact"]["contract"] == "learning.parameter-templates-active.v2"
    assert result["_fact"]["observed_at"] == 995.0


def test_model_read_endpoints_attach_their_own_contracts(monkeypatch) -> None:
    class _ShadowQueue:
        def __init__(self, _db_path=None):
            pass

        @staticmethod
        def list_candidates(**_kwargs):
            return [{"candidate_id": "c1", "created_at": 900.0, "updated_at": 990.0}]

    class _CanaryReviewer:
        def __init__(self, _db_path=None):
            pass

        @staticmethod
        def list_reviews(**_kwargs):
            return [{"review_id": "r1", "created_at": 985.0}]

    monkeypatch.setattr(learning_api, "ModelShadowQueue", _ShadowQueue)
    monkeypatch.setattr(learning_api, "ModelCanaryReviewer", _CanaryReviewer)

    shadow = learning_api.list_learning_model_shadow_candidates(
        None,
        status=None,
        model_type=None,
        limit=50,
        registry_db_path=None,
    )
    canary = learning_api.list_learning_model_canary_reviews(
        None,
        candidate_id=None,
        limit=50,
        registry_db_path=None,
    )

    assert shadow["count"] == 1
    assert shadow["_fact"]["contract"] == "learning.model-shadow-queue.v2"
    assert shadow["_fact"]["observed_at"] == 990.0
    assert canary["count"] == 1
    assert canary["_fact"]["contract"] == "learning.model-canary-reviews.v2"
    assert canary["_fact"]["observed_at"] == 985.0


def test_learning_console_routes_attach_every_consumed_model_contract(monkeypatch) -> None:
    class _AuditService:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def list_audits(**_kwargs):
            return {"items": [{"created_at": 990.0}], "count": 1}

        @staticmethod
        def build_shadow_report(**_kwargs):
            return {"ok": True, "audit_count": 1, "generated_at": 1000.0}

    class _Inference:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def list_audits(**_kwargs):
            return [{"created_at": 991.0}]

    monkeypatch.setattr(
        learning_api,
        "list_autonomous_learning_samples",
        lambda **_kwargs: {"items": [{"updated_at": 989.0}], "count": 1},
    )
    monkeypatch.setattr(learning_api, "ModelInferenceContract", _Inference)
    monkeypatch.setattr(learning_api, "PositionQualityLightGBMService", _AuditService)
    monkeypatch.setattr(learning_api, "OpenQualityLightGBMService", _AuditService)
    monkeypatch.setattr(learning_api, "FactorGovernanceLightGBMService", _AuditService)

    samples = learning_api.get_learning_autonomous_samples(None)
    inference = learning_api.list_learning_model_inference_audits(None)
    position = learning_api.list_position_quality_lightgbm_audits(None)
    open_quality = learning_api.list_open_quality_lightgbm_audits(None)
    factor = learning_api.list_factor_governance_lightgbm_audits(None)

    assert samples["_fact"]["contract"] == "learning.autonomous-samples.v2"
    assert inference["_fact"]["contract"] == "learning.model-inference-audits.v2"
    assert position["_fact"]["contract"] == "learning.model-position-quality-audits.v2"
    assert open_quality["_fact"]["contract"] == "learning.model-open-quality-audits.v2"
    assert factor["_fact"]["contract"] == "learning.factor-governance-lightgbm-audits.v2"


def test_dataset_readiness_endpoint_uses_explicit_source_observation(monkeypatch) -> None:
    class _Readiness:
        @staticmethod
        def analyze(**_kwargs):
            return {"ready": False, "level": "warming_up", "blockers": [{"code": "few"}]}

    observation = DurableSourceObservation(
        observed_at=990.0,
        authoritative_empty=False,
        record_count=4,
        tables=("autonomous_learning_sample", "decision_ledger", "trade_outcome_review"),
    )
    monkeypatch.setattr(learning_api, "LearningDatasetReadiness", _Readiness)
    monkeypatch.setattr(
        learning_api,
        "observe_learning_dataset_source",
        lambda *_args, **_kwargs: observation,
    )

    result = learning_api.get_learning_dataset_readiness(
        None,
        trade_limit=100,
        decision_limit=200,
        min_ready_trades=50,
        min_ready_decisions=100,
    )

    assert result["ready"] is False
    assert result["blockers"] == [{"code": "few"}]
    assert result["_fact"]["contract"] == "learning.dataset-readiness.v2"
    assert result["_fact"]["observed_at"] == 990.0
