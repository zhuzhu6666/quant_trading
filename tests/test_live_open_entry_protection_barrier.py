from types import SimpleNamespace

import pytest

from backend.services import live_service
from backend.services.live_safety_state import (
    no_new_risk_latch_status,
    reset_safety_state_for_tests,
)


@pytest.fixture(autouse=True)
def _isolated_barrier_state(monkeypatch, tmp_path):
    reset_safety_state_for_tests()
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path / "safety"))
    live_service._pending_open_attach_until.clear()
    live_service._process_shutdown_requested = False
    yield
    live_service._pending_open_attach_until.clear()
    reset_safety_state_for_tests()


def test_confirmed_open_latches_before_fallible_position_refresh(monkeypatch):
    monkeypatch.setattr(
        live_service,
        "_tick_resolve_order_fill_price",
        lambda *_args, **_kwargs: 4000.0,
    )
    monkeypatch.setattr(
        live_service,
        "_tick_resolve_order_position_id",
        lambda *_args, **_kwargs: 501,
    )

    class _Bridge:
        symbol = "XAUUSD+"

        def get_positions(self, _symbol):
            raise RuntimeError("position refresh failed after confirmed fill")

    with pytest.raises(RuntimeError, match="position refresh failed"):
        live_service._handle_open_trade_order_success(
            result=SimpleNamespace(success=True, outcome="confirmed", position_id=501),
            bridge=_Bridge(),
            attr_engine=None,
            broker="ctrader",
            cfg=SimpleNamespace(),
            bar={"time": 1.0},
            tick=12,
            account={},
            positions=[],
            composite=SimpleNamespace(direction=1),
            gate_result=SimpleNamespace(),
            candidate=SimpleNamespace(
                direction_name="LONG",
                volume=100.0,
                base_volume=100.0,
                sl_dist=2.0,
                tp_dist=4.0,
                digits=2,
            ),
            current_price=4000.0,
            log=lambda _message: None,
        )

    latch = no_new_risk_latch_status()
    assert latch["active"] is True
    assert ("entry_protection_pending", "501") in {
        (item["cause"], item["cause_id"]) for item in latch["causes"]
    }
    assert 501 in live_service._pending_open_attach_until
    assert live_service._live_state_get("accepting_new_risk") is False


def test_submit_contains_confirmed_open_post_fill_exception(monkeypatch):
    result = SimpleNamespace(
        success=True,
        outcome="confirmed",
        position_id=777,
        intent_id="intent-open-777",
    )
    published = []
    logs = []
    monkeypatch.setattr(
        live_service,
        "_probe_final_open_admission",
        lambda **_kwargs: {"ok": True, "blockers": ()},
    )
    monkeypatch.setattr(
        live_service,
        "_submit_open_trade_order",
        lambda *_args, **_kwargs: result,
    )
    monkeypatch.setattr(
        live_service,
        "_handle_open_trade_order_success",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("post-fill exploded")),
    )
    monkeypatch.setattr(
        live_service,
        "_explicit_position_reconcile",
        lambda _bridge: {
            "success": True,
            "fresh": True,
            "authoritative": True,
            "reconcile_id": "post-fill-reconcile",
            "positions": ({"position_id": 777},),
        },
    )
    monkeypatch.setattr(
        live_service,
        "_publish_fresh_position_reconcile",
        lambda reconcile, **_kwargs: published.append(reconcile["reconcile_id"]),
    )

    submitted = live_service._submit_open_trade_candidate(
        bridge=object(),
        attr_engine=None,
        broker="ctrader",
        cfg=SimpleNamespace(),
        bar={"time": 1.0},
        tick=13,
        account={},
        positions=[],
        composite=SimpleNamespace(direction=1),
        gate_result=SimpleNamespace(),
        candidate=SimpleNamespace(
            direction_name="LONG",
            volume=100.0,
            nursery_reservation_id="",
        ),
        current_price=4000.0,
        log=logs.append,
    )

    assert submitted is True
    assert published == ["post-fill-reconcile"]
    assert no_new_risk_latch_status()["active"] is True
    assert live_service._live_state_get("accepting_new_risk") is False
    assert any("confirmed open post-fill processing failed closed" in item for item in logs)


def test_fresh_verified_protection_releases_only_matching_pending_cause():
    live_service._remember_pending_open_attach(888)
    live_service._activate_entry_protection_pending_latch(
        888,
        broker="ctrader",
        tick=14,
    )
    assert no_new_risk_latch_status()["active"] is True

    latch = live_service._release_entry_protection_pending_latch(
        888,
        reconcile={
            "reconcile_id": "verified-888",
            "observed_at": 100.0,
        },
        expected_stop_loss=3998.0,
        expected_take_profit=4004.0,
    )

    assert latch["active"] is False
    assert 888 not in live_service._pending_open_attach_until
