from threading import RLock

from backend.services.live_learning_policy import (
    LiveLearningPolicyRuntime,
    load_active_learning_policy,
)


class _Conn:
    def close(self):
        return None


def _runtime(rows, *, cache=None, calls=None):
    cache = {} if cache is None else cache
    calls = [] if calls is None else calls

    def load_controls(_conn, **kwargs):
        calls.append(kwargs)
        return list(rows)

    return LiveLearningPolicyRuntime(
        connection_factory=lambda **_kwargs: _Conn(),
        load_controls=load_controls,
        cache=cache,
        cache_lock=RLock(),
        warning=lambda *_args, **_kwargs: None,
        now=lambda: 100.0,
    ), calls


def _row(scope_key, *, evidence=None):
    return {
        "suggestion_id": "suggestion-1",
        "scope_key": scope_key,
        "action": "action",
        "confidence": 0.8,
        "reason": "evidence",
        "governance_authority": "committed_mutation",
        "committed_mutation_id": "mutation-1",
        "evidence_json": evidence or {},
    }


def test_entry_cluster_projection_preserves_committed_authority_and_threshold():
    runtime, calls = _runtime([_row("xauusd:long_ge_3")])

    result = load_active_learning_policy(
        "entry_cluster",
        runtime=runtime,
        now_ts=100.0,
    )

    assert result["active"] is True
    assert result["min_same_direction_open_count"] == 3
    assert result["controls"][0]["committed_mutation_id"] == "mutation-1"
    assert calls[0]["scope_type"] == "entry_cluster"


def test_entry_quality_and_event_window_have_endpoint_specific_projection():
    quality_runtime, _ = _runtime(
        [
            _row(
                "weak_signal",
                evidence={
                    "recommended_controls": {
                        "min_abs_signal_score": 0.7,
                        "suppressed_factor": "factor_a",
                    }
                },
            )
        ]
    )
    quality = load_active_learning_policy(
        "entry_quality",
        runtime=quality_runtime,
        now_ts=100.0,
    )
    assert quality["controls"][0]["min_abs_signal_score"] == 0.7
    assert quality["controls"][0]["suppressed_factor"] == "factor_a"

    event_runtime, _ = _runtime([_row("FOMC:post_15m")])
    event = load_active_learning_policy(
        "event_window",
        runtime=event_runtime,
        now_ts=100.0,
    )
    assert event["controls"][0]["event_name"] == "FOMC"
    assert event["controls"][0]["window_bucket"] == "post_15m"


def test_policy_cache_returns_deep_copy_without_second_state_read():
    cache = {}
    runtime, calls = _runtime([_row("xauusd:long_ge_1")], cache=cache)
    first = load_active_learning_policy(
        "entry_cluster",
        runtime=runtime,
        now_ts=100.0,
    )
    first["controls"].clear()
    second = load_active_learning_policy(
        "entry_cluster",
        runtime=runtime,
        now_ts=110.0,
    )

    assert len(calls) == 1
    assert len(second["controls"]) == 1
