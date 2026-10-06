from backend.services.live_entry_protection import (
    EntryProtectionLatchRuntime,
    activate_entry_protection_pending_latch,
    release_entry_protection_pending_latch,
    release_entry_protection_pending_latch_after_broker_close,
)


def _runtime(
    *,
    activate=None,
    release=None,
    pending=None,
    outbox=None,
    state_updates=None,
):
    outbox = outbox if outbox is not None else []
    state_updates = state_updates if state_updates is not None else []
    return EntryProtectionLatchRuntime(
        activate_latch=activate or (lambda **_kwargs: None),
        release_latch_cause=release or (lambda **_kwargs: None),
        latch_status=lambda **_kwargs: {
            "active": True,
            "reason": "entry_protection_pending",
        },
        append_safety_outbox=lambda **kwargs: outbox.append(kwargs) or {},
        live_state_update=lambda **kwargs: state_updates.append(kwargs),
        reconcile_value=lambda payload, key, default=None: payload.get(
            key, default
        ),
        pending_open_attach_until=(pending if pending is not None else {}),
        now=lambda: 1_234.0,
    )


def test_confirmed_open_activates_fail_closed_latch_before_followup_work():
    activations = []
    state_updates = []
    runtime = _runtime(
        activate=lambda **kwargs: activations.append(kwargs),
        state_updates=state_updates,
    )

    latch = activate_entry_protection_pending_latch(
        88,
        broker="ctrader",
        tick=14,
        runtime=runtime,
    )

    assert activations[0]["cause_id"] == "88"
    assert latch["active"] is True
    assert state_updates[0]["accepting_new_risk"] is False
    assert state_updates[0]["entry_protection_pending"]["status"] == "pending"


def test_latch_persistence_failure_records_outbox_and_stays_fail_closed():
    outbox = []
    state_updates = []

    def unavailable(**_kwargs):
        raise RuntimeError("postgres unavailable")

    runtime = _runtime(
        activate=unavailable,
        outbox=outbox,
        state_updates=state_updates,
    )

    latch = activate_entry_protection_pending_latch(
        89,
        broker="ctrader",
        tick=15,
        runtime=runtime,
    )

    assert latch["active"] is True
    assert outbox[0]["event_type"] == (
        "entry_protection_pending_latch_persist_failed"
    )
    assert state_updates[0]["accepting_new_risk"] is False
    assert "postgres unavailable" in (
        state_updates[0]["entry_protection_pending"]["error"]
    )


def test_fresh_broker_proof_releases_only_matching_cause():
    releases = []
    state_updates = []
    pending = {90: 2_000.0, 91: 2_000.0}
    runtime = _runtime(
        release=lambda **kwargs: releases.append(kwargs),
        pending=pending,
        state_updates=state_updates,
    )

    release_entry_protection_pending_latch(
        90,
        reconcile={"reconcile_id": "rec-1", "observed_at": 1_200.0},
        expected_stop_loss=2_300.0,
        expected_take_profit=2_500.0,
        runtime=runtime,
    )

    assert releases[0]["cause_id"] == "90"
    assert 90 not in pending
    assert 91 in pending
    pending_state = state_updates[0]["entry_protection_pending"]
    assert pending_state["status"] == "verified"
    assert pending_state["verified_at"] == 1_234.0


def test_release_failure_keeps_pending_attach_and_new_risk_blocked():
    outbox = []
    state_updates = []
    pending = {92: 2_000.0}

    def unavailable(**_kwargs):
        raise RuntimeError("postgres unavailable")

    runtime = _runtime(
        release=unavailable,
        pending=pending,
        outbox=outbox,
        state_updates=state_updates,
    )

    latch = release_entry_protection_pending_latch(
        92,
        reconcile={"reconcile_id": "rec-2", "observed_at": 1_210.0},
        expected_stop_loss=2_300.0,
        expected_take_profit=2_500.0,
        runtime=runtime,
    )

    assert latch["active"] is True
    assert 92 in pending
    assert outbox[0]["event_type"] == (
        "entry_protection_pending_latch_release_failed"
    )
    assert state_updates[0]["accepting_new_risk"] is False
    assert state_updates[0]["entry_protection_pending"]["status"] == (
        "release_failed"
    )


def test_broker_close_releases_the_pending_cause_for_that_position_only():
    releases = []

    released = release_entry_protection_pending_latch_after_broker_close(
        295077652,
        close_reason="broker_close",
        close_ts=1_793.0,
        total_pnl=-7.94,
        release_latch_cause=lambda **kwargs: releases.append(kwargs),
    )

    assert released is True
    assert len(releases) == 1
    assert releases[0]["cause"] == "entry_protection_pending"
    assert releases[0]["cause_id"] == "295077652"
    assert releases[0]["reason"] == (
        "position_closed_at_broker_no_residual_new_risk"
    )
    assert releases[0]["evidence"]["broker_position_absent"] is True
    assert releases[0]["evidence"]["authoritative_close_deal"] is True


def test_broker_close_release_never_fails_closed_position_attribution():
    def unavailable(**_kwargs):
        raise RuntimeError("latch ledger unavailable")

    released = release_entry_protection_pending_latch_after_broker_close(
        4242,
        close_reason="broker_close",
        close_ts=1_800.0,
        total_pnl=1.0,
        release_latch_cause=unavailable,
    )

    assert released is False
