"""Broker stop-loss fill-match reclassification for replayed closes.

A recovery-replayed close (position vanished → reconciled from deals) used to
be hard-labelled ``restart_replay`` and therefore excluded from learning.
When the durable amend intent plus the authoritative close deal prove the fill
matched our broker-side stop, the natural lifecycle holds and the review must
say ``broker_close``.  These tests pin that contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.review_contract import (
    BROKER_SL_HIT_TOLERANCE_RATIO,
    broker_stop_hit_evidence,
    build_system_issue_context,
    classify_close_reason_from_recovery,
)


@pytest.fixture()
def _intent_store(monkeypatch):
    """Install a fake intent store; returns a setter for the current intent."""

    holder: dict[str, object] = {"intent": None}
    monkeypatch.setenv("HERMES_TEST", "1")

    def _fake_latest(self, position_id, *, broker="ctrader"):  # noqa: ANN001
        return holder.get("intent")

    from backend.services.broker_execution_intent import BrokerExecutionIntentStore

    monkeypatch.setattr(
        BrokerExecutionIntentStore,
        "latest_stop_loss_for_position",
        _fake_latest,
    )
    return holder


def _set_intent(holder, *, sl: float, intent_id: str = "int-1", decision_id: str = "dec-1"):
    holder["intent"] = SimpleNamespace(
        intent_id=intent_id,
        decision_id=decision_id,
        target_stop_loss=sl,
        status="confirmed",
        action="amend_position_sltp",
    )


def test_short_position_fill_at_sl_reclassifies_to_broker_close(_intent_store):
    # XAUUSD short @4583, SL 4597.27; broker filled at 4597.29 (+0.02 slippage)
    _set_intent(_intent_store, sl=4597.27)
    evidence = broker_stop_hit_evidence(
        real_pnl={
            "net": -12.41,
            "exec_price": 4597.29,
            "price_quality": "broker_reported",
            "deal_id": 330145482,
            "source": "ctrader_deals",
        },
        position_state={"position_id": "284513709", "direction": -1},
    )

    assert evidence["matched"] is True
    assert evidence["intent_id"] == "int-1"
    assert evidence["decision_id"] == "dec-1"
    assert evidence["target_stop_loss"] == pytest.approx(4597.27)

    resolution = classify_close_reason_from_recovery(
        replayed=True,
        real_pnl={
            "net": -12.41,
            "exec_price": 4597.29,
            "price_quality": "broker_reported",
        },
        position_state={"position_id": "284513709", "direction": -1},
    )
    assert resolution["close_reason"] == "broker_close"


def test_long_position_fill_below_sl_matches(_intent_store):
    # Long: stop sits below entry; fill at or under SL proves the stop-out.
    _set_intent(_intent_store, sl=100.0)
    evidence = broker_stop_hit_evidence(
        real_pnl={
            "net": -5.0,
            "exec_price": 99.95,
            "price_quality": "broker_reported",
        },
        position_state={"position_id": "42", "direction": 1},
    )
    assert evidence["matched"] is True


def test_fill_far_from_sl_keeps_restart_replay(_intent_store):
    # Short with SL 4597.27 but fill nowhere near it: no reclassification.
    _set_intent(_intent_store, sl=4597.27)
    evidence = broker_stop_hit_evidence(
        real_pnl={
            "net": 3.3,
            "exec_price": 4562.0,
            "price_quality": "broker_reported",
        },
        position_state={"position_id": "284513709", "direction": -1},
    )
    assert evidence["matched"] is False

    resolution = classify_close_reason_from_recovery(
        replayed=True,
        real_pnl={
            "net": 3.3,
            "exec_price": 4562.0,
            "price_quality": "broker_reported",
        },
        position_state={"position_id": "284513709", "direction": -1},
    )
    assert resolution["close_reason"] == "restart_replay"
    # The rejected candidate is still cited for auditability.
    assert evidence.get("intent_id") == "int-1"


def test_missing_intent_or_untrusted_price_stays_conservative(_intent_store):
    # No amend intent on file → no upgrade, even with a plausible fill.
    _set_intent(_intent_store, sl=0.0)
    evidence = broker_stop_hit_evidence(
        real_pnl={"net": -1.0, "exec_price": 4597.29, "price_quality": "broker_reported"},
        position_state={"position_id": "x", "direction": -1},
    )
    assert evidence == {"matched": False}

    # Untrusted price quality can never match either.
    _set_intent(_intent_store, sl=4597.27)
    evidence = broker_stop_hit_evidence(
        real_pnl={"net": -1.0, "exec_price": 4597.29, "price_quality": "estimated"},
        position_state={"position_id": "x", "direction": -1},
    )
    assert evidence == {"matched": False}


def test_tolerance_is_five_bps():
    assert BROKER_SL_HIT_TOLERANCE_RATIO == 0.0005


def test_broker_sl_hit_review_no_longer_contaminates_learning():
    """End-to-end contract: matched SL evidence ⇒ review says broker_close ⇒ clean."""

    review = {
        "close_reason": "broker_close",
        "close_reason_source": "restart_replay",
        "real_pnl": {"net": -12.41, "exec_price": 4597.29, "price_quality": "broker_reported"},
        "sl_hit_evidence": {
            "matched": True,
            "method": "broker_sl_fill_match",
            "schema_version": "broker_sl_hit_evidence.v1",
            "intent_id": "abc37ec4-cf5d-4c81-8d79-342f7eb7cf21",
            "deal_id": 330145482,
        },
    }
    issue = build_system_issue_context(review)
    assert issue["contaminates_learning"] is False


def test_plain_restart_replay_still_contaminates_learning():
    review = {
        "close_reason": "restart_replay",
        "close_reason_source": "restart_replay",
        "real_pnl": {"net": -1.29, "price_quality": "broker_reconciled"},
    }
    issue = build_system_issue_context(review)
    assert issue["contaminates_learning"] is True
    assert "restart_replay" in issue["labels"]
