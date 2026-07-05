from types import SimpleNamespace

import pytest

from backend.services.live_risk_sizing import (
    apply_entry_event_sizing,
    build_event_sizing_fallback_context,
    ceil_api_volume_to_step,
    floor_api_volume_to_step,
    normalize_event_sizing_context,
    protection_prices_from_reference,
    risk_kelly_sizing,
    round_api_volume_to_step,
    should_full_close_untradeable_reduce,
)


def test_api_volume_step_helpers_preserve_live_facade_behavior():
    meta = {"api_min_volume": 100, "api_step_volume": 100}

    assert ceil_api_volume_to_step(50.0, meta) == 100.0
    assert ceil_api_volume_to_step(150.0, meta) == 200.0
    assert round_api_volume_to_step(149.0, meta) == 100.0
    assert round_api_volume_to_step(151.0, meta) == 200.0
    assert floor_api_volume_to_step(50.0, meta) == 0.0
    assert floor_api_volume_to_step(150.0, meta) == 100.0
    assert floor_api_volume_to_step(200.0, meta) == 200.0


def test_risk_kelly_sizing_outputs_api_volume_tiers():
    cfg = SimpleNamespace(
        kelly_enabled=True,
        kelly_fraction=1.0,
        kelly_risk_per_trade_pct=0.02,
        kelly_max_pct=0.25,
        max_position_api_volume=1000.0,
        dynamic_sizing_enabled=True,
        dynamic_sizing_max_api_volume=300.0,
        dynamic_sizing_api_units_per_display_unit=100.0,
    )

    result = risk_kelly_sizing(
        cfg=cfg,
        direction=1,
        current_price=4000.0,
        sl_price=3990.0,
        bridge_meta={"api_min_volume": 100, "api_step_volume": 100},
        account={"equity": 1000.0},
        kelly_data={"kelly_fraction": 1.0},
    )

    assert result["volume"] == 200.0
    assert result["trace"]["raw_api_volume"] == pytest.approx(200.0)
    assert result["trace"]["base_api_volume"] == 200.0


def test_risk_kelly_sizing_respects_dynamic_cap_and_fallbacks():
    cfg = SimpleNamespace(
        kelly_enabled=True,
        kelly_fraction=1.0,
        kelly_risk_per_trade_pct=0.10,
        kelly_max_pct=0.25,
        max_position_api_volume=1000.0,
        dynamic_sizing_enabled=True,
        dynamic_sizing_max_api_volume=300.0,
        dynamic_sizing_api_units_per_display_unit=100.0,
    )
    meta = {"api_min_volume": 100, "api_step_volume": 100}

    capped = risk_kelly_sizing(
        cfg=cfg,
        direction=1,
        current_price=4000.0,
        sl_price=3990.0,
        bridge_meta=meta,
        account={"equity": 1000.0},
        kelly_data={"kelly_fraction": 1.0},
    )
    missing_kelly = risk_kelly_sizing(
        cfg=cfg,
        direction=1,
        current_price=4000.0,
        sl_price=3990.0,
        bridge_meta=meta,
        account={"equity": 1000.0},
        kelly_data={"kelly_fraction": 0.0},
    )

    assert capped["volume"] == 300.0
    assert capped["trace"]["capped_raw_api_volume"] == 300.0
    assert missing_kelly["volume"] == 100.0
    assert missing_kelly["trace"]["reason"] == "kelly_fraction_non_positive"


def test_apply_entry_event_sizing_floors_reduced_volume_without_lifting_to_min():
    meta = {"api_min_volume": 100, "api_step_volume": 100}

    blocked = apply_entry_event_sizing(
        base_volume=100.0,
        event_multiplier=0.2,
        bridge_meta=meta,
        sizing_trace={"base_api_volume": 100.0},
    )
    tradeable = apply_entry_event_sizing(
        base_volume=300.0,
        event_multiplier=0.5,
        bridge_meta=meta,
    )

    assert blocked["volume"] == 0.0
    assert blocked["blocked_reason"].startswith("event_sizing_below_min")
    assert blocked["trace"]["final_api_volume"] == 0.0
    assert tradeable["volume"] == 100.0
    assert tradeable["blocked_reason"] == ""


def test_should_full_close_untradeable_reduce_requires_min_position_and_strong_evidence():
    verdict = {
        "summary_reason": "profit_giveback_after_mfe",
        "evidence": {
            "thesis_status": "broken",
            "giveback_ratio": 1.0,
            "current_pnl": -1.08,
            "trigger_tags": ["profit_giveback_after_mfe"],
        },
        "recommended_controls": {"reduce_fraction": 0.5},
    }

    should_close, reason = should_full_close_untradeable_reduce(
        current_volume=100.0,
        raw_reduce_volume=50.0,
        reduce_volume=0.0,
        min_volume=100.0,
        verdict=verdict,
    )
    above_min, above_min_reason = should_full_close_untradeable_reduce(
        current_volume=150.0,
        raw_reduce_volume=75.0,
        reduce_volume=0.0,
        min_volume=100.0,
        verdict=verdict,
    )
    weak, weak_reason = should_full_close_untradeable_reduce(
        current_volume=100.0,
        raw_reduce_volume=50.0,
        reduce_volume=0.0,
        min_volume=100.0,
        verdict={
            "summary_reason": "profit_giveback_after_mfe",
            "evidence": {
                "thesis_status": "weakening",
                "giveback_ratio": 0.75,
                "current_pnl": 8.0,
                "trigger_tags": ["profit_giveback_after_mfe"],
            },
            "recommended_controls": {"reduce_fraction": 0.5},
        },
    )

    assert should_close is True
    assert reason == "minimum_position_thesis_broken"
    assert above_min is False
    assert above_min_reason == "not_minimum_position"
    assert weak is False
    assert weak_reason == "risk_evidence_not_strong_enough"


def test_should_full_close_untradeable_reduce_handles_giveback_and_tradeable_reduce():
    full_giveback = should_full_close_untradeable_reduce(
        current_volume=100.0,
        raw_reduce_volume=50.0,
        reduce_volume=0.0,
        min_volume=100.0,
        verdict={"evidence": {"giveback_ratio": 1.0, "current_pnl": -0.01}},
    )
    profit_giveback = should_full_close_untradeable_reduce(
        current_volume=100.0,
        raw_reduce_volume=50.0,
        reduce_volume=0.0,
        min_volume=100.0,
        verdict={
            "summary_reason": "profit_giveback_after_mfe",
            "evidence": {"current_pnl": 0.0, "trigger_tags": "profit_giveback_after_mfe"},
            "recommended_controls": {"reduce_fraction": 0.5},
        },
    )
    tradeable = should_full_close_untradeable_reduce(
        current_volume=100.0,
        raw_reduce_volume=50.0,
        reduce_volume=100.0,
        min_volume=100.0,
        verdict={"evidence": {"thesis_status": "broken"}},
    )

    assert full_giveback == (True, "minimum_position_full_giveback")
    assert profit_giveback == (True, "minimum_position_profit_giveback")
    assert tradeable == (False, "reduce_volume_tradeable")


def test_normalize_event_sizing_context_clamps_multiplier_and_preserves_stats():
    payload = normalize_event_sizing_context(
        context={"multiplier": 1.4, "event": "NFP"},
        enabled=True,
        stats={"events_loaded": 2},
    )
    fallback_stats = normalize_event_sizing_context(
        context={"multiplier": -0.2},
        enabled=False,
        stats=None,
    )

    assert payload == {
        "enabled": True,
        "multiplier": 1.0,
        "event": "NFP",
        "stats": {"events_loaded": 2},
    }
    assert fallback_stats == {"enabled": False, "multiplier": 0.0, "stats": {}}


def test_build_event_sizing_fallback_context_matches_live_shape():
    payload = build_event_sizing_fallback_context(
        enabled=True,
        multiplier=0.25,
        event_near=True,
        event="FOMC",
        stats={"events_loaded": 1},
    )
    clamped = build_event_sizing_fallback_context(
        enabled=False,
        multiplier=2.0,
        event_near=False,
        event=None,
        stats=None,
    )

    assert payload == {
        "enabled": True,
        "multiplier": 0.25,
        "event_near": True,
        "event": "FOMC",
        "stats": {"events_loaded": 1},
    }
    assert clamped == {
        "enabled": False,
        "multiplier": 1.0,
        "event_near": False,
        "event": None,
        "stats": {},
    }


def test_protection_prices_from_reference_use_direction_and_digits():
    assert protection_prices_from_reference(1, 4000.123, 10.0, 15.0, 2) == (3990.12, 4015.12)
    assert protection_prices_from_reference(-1, 4000.123, 10.0, 15.0, 2) == (4010.12, 3985.12)
    assert protection_prices_from_reference(0, 4000.123, -10.0, -15.0, 1) == (4010.1, 3985.1)
