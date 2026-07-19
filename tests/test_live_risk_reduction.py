from types import SimpleNamespace

import pytest

from backend.services.live_position_lifecycle import (
    build_close_position_risk_context_payload,
    position_open_timestamp,
)
from backend.services.live_risk_reduction import (
    RiskReductionRuntime,
    build_close_position_risk_context,
    evaluate_risk_reduction_policy,
    load_recovery_row_for_risk_reduction,
    lookup_entry_context_for_risk_reduction,
    lookup_entry_decision_for_risk_reduction,
    record_risk_reduction_aux_failure,
)
from risk.policy_service import RiskVerdict


class _Policy:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def evaluate(self, action, context):
        self.calls.append((action, context))
        if self.error is not None:
            raise self.error
        return RiskVerdict(allowed=True, reason="allowed")


def _runtime(
    *,
    outbox=None,
    errors=None,
    warnings=None,
    lookup_context=None,
    load_recovery=None,
    lookup_decision=None,
    policy=None,
):
    outbox = outbox if outbox is not None else []
    errors = errors if errors is not None else []
    warnings = warnings if warnings is not None else []
    return RiskReductionRuntime(
        append_safety_outbox=lambda **kwargs: outbox.append(kwargs) or {},
        logger_error=lambda *args: errors.append(args),
        logger_warning=lambda *args: warnings.append(args),
        now=lambda: 1_000.0,
        config_factory=lambda: SimpleNamespace(
            timeframe="M5", risk_max_holding_bars=12
        ),
        position_open_timestamp=position_open_timestamp,
        lookup_open_decision_context=(
            lookup_context or (lambda _position_id: {})
        ),
        temporal_context_for_trade=lambda **kwargs: dict(kwargs),
        build_close_context_payload=build_close_position_risk_context_payload,
        load_recovery_position_row=(
            load_recovery or (lambda position_id: {"position_id": position_id})
        ),
        lookup_entry_decision_id=(
            lookup_decision or (lambda position_id: f"decision-{position_id}")
        ),
        risk_policy=policy or _Policy(),
    )


def test_broker_open_timestamp_remains_primary_over_postgres_enrichment():
    runtime = _runtime(
        lookup_context=lambda _position_id: {
            "entry_ts": 10.0,
            "timeframe": "M15",
            "source": "decision_ledger",
        }
    )

    context = build_close_position_risk_context(
        position_id=45,
        close_reason="emergency_close",
        position={"position_id": 45, "open_timestamp": 20.0},
        decision_ts=320.0,
        runtime=runtime,
    )

    assert context["entry_ts"] == 20.0
    assert context["entry_ts_source"] == "broker_position"
    assert context["temporal_context"]["timeframe"] == "M15"


def test_close_context_postgres_failure_records_outbox_and_continues():
    outbox = []
    warnings = []

    def unavailable(_position_id):
        raise RuntimeError("postgres unavailable")

    runtime = _runtime(
        outbox=outbox,
        warnings=warnings,
        lookup_context=unavailable,
    )

    context = build_close_position_risk_context(
        position_id=46,
        close_reason="holding_timeout",
        position={"position_id": 46, "open_timestamp": 200.0},
        decision_ts=500.0,
        runtime=runtime,
    )

    assert context["entry_ts"] == 200.0
    assert outbox[0]["event_type"] == "close_risk_context_enrichment_failed"
    assert warnings


def test_risk_reduction_policy_failure_allows_action_and_records_outbox():
    outbox = []
    runtime = _runtime(
        outbox=outbox,
        policy=_Policy(error=RuntimeError("policy database unavailable")),
    )

    verdict = evaluate_risk_reduction_policy(
        "close_position",
        {"position_id": 704, "close_reason": "holding_timeout"},
        runtime=runtime,
    )

    assert verdict.allowed is True
    assert verdict.required_mode == "risk_reduction_only"
    assert outbox[0]["event_type"] == "risk_reduction_policy_unavailable"


def test_non_risk_reducing_policy_failure_is_not_silenced():
    runtime = _runtime(
        policy=_Policy(error=RuntimeError("policy database unavailable"))
    )

    with pytest.raises(RuntimeError, match="policy database unavailable"):
        evaluate_risk_reduction_policy(
            "open_trade",
            {"position_id": 704},
            runtime=runtime,
        )


def test_auxiliary_enrichment_failures_return_compatibility_defaults():
    outbox = []

    def unavailable(_position_id):
        raise RuntimeError("postgres unavailable")

    runtime = _runtime(
        outbox=outbox,
        lookup_context=unavailable,
        load_recovery=unavailable,
        lookup_decision=unavailable,
    )

    assert load_recovery_row_for_risk_reduction(
        8, operation="supervisor", runtime=runtime
    ) == {}
    assert lookup_entry_context_for_risk_reduction(
        8, operation="supervisor", runtime=runtime
    ) == {}
    assert lookup_entry_decision_for_risk_reduction(
        8, operation="supervisor", runtime=runtime
    ) == ""
    assert [item["event_type"] for item in outbox] == [
        "risk_reduction_pg_enrichment_failed",
        "risk_reduction_entry_context_failed",
        "risk_reduction_entry_decision_failed",
    ]


def test_safety_outbox_failure_never_changes_risk_reduction_result():
    errors = []
    runtime = _runtime(errors=errors)
    runtime = RiskReductionRuntime(
        **{
            **runtime.__dict__,
            "append_safety_outbox": lambda **_kwargs: (_ for _ in ()).throw(
                OSError("disk full")
            ),
        }
    )

    record_risk_reduction_aux_failure(
        "audit_failed",
        position_id=9,
        action="close_position",
        error=RuntimeError("postgres unavailable"),
        runtime=runtime,
    )

    assert errors
