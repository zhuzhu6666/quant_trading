from backend.services.policy_suggestion_identity import deterministic_policy_suggestion_id


def test_policy_suggestion_identity_ignores_only_occurrence_fields():
    base = {
        "evidence_hash": "evidence-1",
        "sample_ids": ["sample-a"],
        "before": {"weight": 0.2},
        "after": {"weight": 0.1},
        "run_id": "run-a",
        "created_at": 10.0,
    }
    retry = {**base, "run_id": "run-b", "created_at": 20.0, "trace_id": "trace-b"}
    first = deterministic_policy_suggestion_id(
        writer="writer",
        scope_type="factor",
        scope_key="f1",
        action="downweight",
        evidence=base,
        status="proposed",
        qualification_fingerprint="q1",
    )
    second = deterministic_policy_suggestion_id(
        writer="writer",
        scope_type="factor",
        scope_key="f1",
        action="downweight",
        evidence=retry,
        status="proposed",
        qualification_fingerprint="q1",
    )
    assert first == second


def test_policy_suggestion_identity_changes_for_new_evidence_or_status():
    kwargs = {
        "writer": "writer",
        "scope_type": "factor",
        "scope_key": "f1",
        "action": "downweight",
        "evidence": {"evidence_hash": "evidence-1", "sample_ids": ["sample-a"]},
        "status": "proposed",
        "qualification_fingerprint": "q1",
    }
    changed_evidence = deterministic_policy_suggestion_id(
        **{**kwargs, "evidence": {"evidence_hash": "evidence-2", "sample_ids": ["sample-b"]}}
    )
    changed_status = deterministic_policy_suggestion_id(**{**kwargs, "status": "approved"})
    original = deterministic_policy_suggestion_id(**kwargs)
    assert original != changed_evidence
    assert original != changed_status
