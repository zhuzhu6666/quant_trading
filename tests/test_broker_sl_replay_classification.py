"""Broker protective-fill match and recovery close attribution.

A recovery-replayed close (position vanished → reconciled from deals) carries
no close reason by itself.  L0-0R: a fill matching the durable protective
order is ``broker_close``; a complete supervisor-executed chain (applied
close + fill after the decision inside the window) reuses the supervisor
reason; everything else stays ``chain_broken``.  These tests pin that contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.live_recovery_close import _resolve_replayed_close_reason
from backend.services.review_contract import (
    BROKER_SL_HIT_TOLERANCE_RATIO,
    broker_stop_hit_evidence,
    build_system_issue_context,
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
        "latest_protection_for_position",
        _fake_latest,
    )
    return holder


def _set_intent(
    holder,
    *,
    sl: float,
    tp: float = 0.0,
    intent_id: str = "int-1",
    decision_id: str = "dec-1",
):
    holder["intent"] = SimpleNamespace(
        intent_id=intent_id,
        decision_id=decision_id,
        target_stop_loss=sl,
        target_take_profit=tp,
        status="confirmed",
        action="amend_position_sltp",
    )


def test_short_position_fill_at_sl_reclassifies_to_broker_close(_intent_store):
    # XAUUSD short @4583, SL 4597.27; broker filled at 4597.29 (+0.02 slippage)
    _set_intent(_intent_store, sl=4597.27, tp=4561.60)
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
    assert evidence["hit"] == "sl"
    assert evidence["intent_id"] == "int-1"
    assert evidence["decision_id"] == "dec-1"
    assert evidence["target_stop_loss"] == pytest.approx(4597.27)

    resolution = _resolve_replayed_close_reason(
        {
            "net": -12.41,
            "exec_price": 4597.29,
            "price_quality": "broker_reported",
        },
        {"position_id": "284513709", "direction": -1},
    )
    assert resolution[0] == "broker_close"
    assert resolution[1] == "external_broker_close"


def test_short_position_fill_at_tp_reclassifies_to_broker_close(_intent_store):
    # 2026-08-21 pos 284536615: short SL 4597.04 / TP 4564.94; the broker
    # filled the take-profit at 4564.89 (-0.05 slippage), +16.32 win.  A TP
    # fill is equally part of the strategy's natural lifecycle.
    _set_intent(_intent_store, sl=4597.04, tp=4564.94)
    evidence = broker_stop_hit_evidence(
        real_pnl={
            "net": 16.32,
            "exec_price": 4564.89,
            "price_quality": "broker_reported",
            "deal_id": 330158661,
            "source": "ctrader_deals",
        },
        position_state={"position_id": "284536615", "direction": -1},
    )

    assert evidence["matched"] is True
    assert evidence["hit"] == "tp"

    resolution = _resolve_replayed_close_reason(
        {
            "net": 16.32,
            "exec_price": 4564.89,
            "price_quality": "broker_reported",
        },
        {"position_id": "284536615", "direction": -1},
    )
    assert resolution[0] == "broker_close"
    assert resolution[1] == "external_broker_close"


def test_long_position_fill_below_sl_matches(_intent_store):
    # Long: stop sits below entry; fill at or under SL proves the stop-out.
    _set_intent(_intent_store, sl=100.0, tp=110.0)
    evidence = broker_stop_hit_evidence(
        real_pnl={
            "net": -5.0,
            "exec_price": 99.95,
            "price_quality": "broker_reported",
        },
        position_state={"position_id": "42", "direction": 1},
    )
    assert evidence["matched"] is True


def test_fill_far_from_sl_stays_chain_broken(_intent_store):
    # Short with SL 4597.27 / TP 4561.60 but fill nowhere near either: no
    # upgrade (e.g. manual close or liquidation at an odd price) — the close
    # stays chain_broken, never a supervisor-vocabulary guess.
    _set_intent(_intent_store, sl=4597.27, tp=4561.60)
    evidence = broker_stop_hit_evidence(
        real_pnl={
            "net": 3.3,
            "exec_price": 4580.0,
            "price_quality": "broker_reported",
        },
        position_state={"position_id": "284513709", "direction": -1},
    )
    assert evidence["matched"] is False

    close_reason, close_reason_source, cited = _resolve_replayed_close_reason(
        {
            "net": 3.3,
            "exec_price": 4580.0,
            "price_quality": "broker_reported",
        },
        {"position_id": "284513709", "direction": -1},
    )
    assert close_reason == "chain_broken"
    assert close_reason_source == "restart_replay"
    # The rejected candidate is still cited for auditability.
    assert cited.get("intent_id") == "int-1"

def test_durable_supervisor_reason_is_never_recovery_attribution(_intent_store):
    """L0-0R-b: a bare supervisor record never becomes recovery attribution.

    A durable ``pending_close_reason`` without an applied execution proves
    what the supervisor once requested, not what executed the position —
    without a protective-fill match or a complete applied-close chain the
    close stays ``chain_broken``.
    """
    _set_intent(_intent_store, sl=4597.27, tp=4561.60)

    close_reason, close_reason_source, _evidence = _resolve_replayed_close_reason(
        {
            "net": -4.10,
            "exec_price": 4588.0,
            "price_quality": "broker_reported",
        },
        {
            "position_id": "285092006",
            "direction": 1,
            "recovery_meta": {
                "pending_close_reason": "thesis_broken",
                "last_supervisor_applied_action": "close",
            },
        },
    )

    assert close_reason == "chain_broken"
    assert close_reason_source == "restart_replay"


def test_missing_intent_or_untrusted_price_stays_conservative(_intent_store):
    # No amend intent on file → no upgrade, even with a plausible fill.
    _set_intent(_intent_store, sl=0.0)
    evidence = broker_stop_hit_evidence(
        real_pnl={"net": -1.0, "exec_price": 4597.29, "price_quality": "broker_reported"},
        position_state={"position_id": "x", "direction": -1},
    )
    assert evidence["matched"] is False
    assert "hit" not in evidence

    # Untrusted price quality can never match either.
    _set_intent(_intent_store, sl=4597.27, tp=4564.94)
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


def _supervisor_chain_meta(**overrides):
    meta = {
        "last_supervisor_applied_action": "close",
        "latest_supervisor_source": "position_supervisor",
        "last_supervisor_reason": "thesis_broken",
        "last_supervisor_applied_ts": 1789385428.64,
        "latest_supervisor": {"decision_ts": 1789385420.65, "action": "close"},
    }
    meta.update(overrides)
    return meta


def test_complete_supervisor_chain_reuses_supervisor_reason(_intent_store):
    """599-case: applied close + fill after the decision ⇒ real reason."""
    _set_intent(_intent_store, sl=4287.67, tp=4309.44)

    close_reason, close_reason_source, cited = _resolve_replayed_close_reason(
        {
            "net": -0.45,
            "exec_price": 4296.07,
            "exec_timestamp": 1789385425.15,
            "price_quality": "broker_reported",
        },
        {
            "position_id": "288607599",
            "direction": 1,
            "recovery_meta": _supervisor_chain_meta(),
        },
    )

    assert close_reason == "thesis_broken"
    assert close_reason_source == "supervisor_applied_close"
    assert cited.get("supervisor_applied_close") is True


def test_fill_before_supervisor_decision_stays_chain_broken(_intent_store):
    """A fill that predates the supervisor decision cannot be its execution."""
    _set_intent(_intent_store, sl=4287.67, tp=4309.44)

    close_reason, close_reason_source, _cited = _resolve_replayed_close_reason(
        {
            "net": -0.45,
            "exec_price": 4296.07,
            "exec_timestamp": 1789385410.0,
            "price_quality": "broker_reported",
        },
        {
            "position_id": "288607599",
            "direction": 1,
            "recovery_meta": _supervisor_chain_meta(),
        },
    )

    assert close_reason == "chain_broken"
    assert close_reason_source == "restart_replay"


def test_non_close_supervisor_action_stays_chain_broken(_intent_store):
    """An applied tighten is no proof of what closed the position."""
    _set_intent(_intent_store, sl=4287.67, tp=4309.44)

    close_reason, close_reason_source, _cited = _resolve_replayed_close_reason(
        {
            "net": -0.45,
            "exec_price": 4296.07,
            "exec_timestamp": 1789385425.15,
            "price_quality": "broker_reported",
        },
        {
            "position_id": "288607599",
            "direction": 1,
            "recovery_meta": _supervisor_chain_meta(
                last_supervisor_applied_action="tighten"
            ),
        },
    )

    assert close_reason == "chain_broken"
    assert close_reason_source == "restart_replay"
