from __future__ import annotations

from scripts.canonical_v2_vertical_shadow import (
    _chain_reasons,
    _sample_contract,
    _semantic_payload,
)


def test_semantic_payload_removes_occurrence_identity_but_keeps_contract_values() -> None:
    value = {
        "sample_id": "sample-1",
        "event_ts": 123.0,
        "config_hash": "config-1",
        "features": {
            "review_id": "review-1",
            "entry_score": 0.4,
        },
    }

    assert _semantic_payload(value) == {
        "config_hash": "config-1",
        "features": {"entry_score": 0.4},
    }


def test_sample_contract_is_reference_oriented_and_stable() -> None:
    row = {
        "sample_id": "sample-1",
        "sample_type": "trade_review_outcome",
        "source_id": "review-1",
        "content_fingerprint": "fingerprint-1",
        "features_json": '{"entry_score": 0.4, "review_id": "review-1"}',
        "verdict_json": '{"target_source": "trade_review"}',
        "label_json": '{"label_source": "fixed_horizon"}',
        "trace_json": '{"review_id": "review-1"}',
        "evidence_contract_json": '{"schema_version": "evidence.v1"}',
        "label_status": "matured",
        "config_version": 15,
        "config_hash": "config-1",
        "system_contaminated": 0,
        "governance_eligible": 1,
    }

    first = _sample_contract(row)
    second = _sample_contract(dict(row))

    assert first == second
    assert first["target_source"] == "fixed_horizon"
    assert first["feature_schema_hash"]
    assert first["label_schema_hash"]
    assert first["semantic_payload_hash"]
    assert first["payload_ref"]["payload_hash"]


def test_chain_reasons_fail_closed_for_missing_lineage() -> None:
    row = {
        "decision_id": "",
        "order_event_count": 0,
        "position_event_count": 1,
    }

    assert _chain_reasons(row, []) == [
        "missing_entry_decision",
        "missing_order_lifecycle",
        "missing_learning_projection",
    ]
