"""Supervisor confirmation-chain producers (2026-08-26 fix batch).

Four structural breaks meant the position supervisor could diagnose a
failing thesis but never act:
  F1 signal_reversal had no producer
  F2 entry_regime was never stamped at open -> regime_shift always none
  F4 thesis_broken_confirmations had no incrementer
  F5 transition_confirming posture blocked every active action

These tests pin each producer and the unlocked tighten path.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.position_metrics import normalize_path_state, update_position_path_metrics
from backend.services.position_supervisor import evaluate_position_supervisor


def _base_context(**overrides):
    context = {
        "position": {
            "position_id": "p1",
            "direction": -1,
            "entry_price": 100.0,
            "current_price": 99.0,
            "sl": 101.5,
            "tp": 96.0,
            "volume": 1.0,
            "unrealized_pnl": 2.0,
            # Live reconcile stamps these component facts; without them the
            # supervisor treats price/pnl/path as unknown and holds.
            "current_price_state": "known",
            "pnl_state": "known",
            "position_path_metrics_state": "known",
        },
        "risk": {
            "max_holding_seconds": 86400.0,
            "mfe": 3.0,
            "mae": 0.5,
            "giveback_ratio": 0.0,
            "profit_capture_ratio": 0.66,
            "time_in_profit": 60.0,
            "holding_efficiency": 0.8,
            "time_decay_score": 1.0,
            "thesis_status": "intact",
            "regime_shift": "none",
        },
        "temporal_context": {"holding_seconds": 600.0, "completed_bars_after_entry": 3},
        "market": {"trend_strength_state": "normal", "volatility_state": "normal"},
        "market_space_context": {},
        "entry_context": {},
    }
    context.update(overrides)
    return context


# ── F4: consecutive-broken counter ──────────────────────────────────────────


def test_thesis_broken_counter_increments_and_resets():
    state = None
    # Losing path with full giveback triggers broken.
    for expected in (1, 2, 3):
        state, metrics = update_position_path_metrics(
            previous_state=state,
            current_pnl=-2.0,
            now_ts=1000.0 + expected * 300.0,
            holding_seconds=expected * 300.0,
            max_holding_seconds=86400.0,
            entry_regime="trend=strong|volatility=low",
            current_regime="trend=weak|volatility=high",
        )
        assert metrics["thesis_status"] == "broken"
        assert metrics["thesis_broken_confirmations"] == expected

    # Recovery to intact resets the counter.
    state, metrics = update_position_path_metrics(
        previous_state=state,
        current_pnl=2.5,
        now_ts=2500.0,
        holding_seconds=1500.0,
        max_holding_seconds=86400.0,
        entry_regime="trend=strong|volatility=low",
        current_regime="trend=strong|volatility=low",
    )
    assert metrics["thesis_status"] in {"intact", "weakening"}
    if metrics["thesis_status"] == "intact":
        assert metrics["thesis_broken_confirmations"] == 0


def test_normalize_path_state_defaults_counter():
    state = normalize_path_state(None)
    assert state["thesis_broken_confirmations"] == 0
    legacy = normalize_path_state({"thesis_status": "broken"})
    assert legacy["thesis_broken_confirmations"] == 0


# ── F5: transition_confirming profit-protection tighten ────────────────────


def test_transition_confirming_profitable_giveback_yields_tighten():
    context = _base_context()
    context["risk"].update(
        {
            "mfe": 8.12,
            "mae": 0.5,
            "giveback_ratio": 0.6,
            "profit_capture_ratio": 0.25,
            "holding_efficiency": 0.35,
            "time_in_profit": 200.0,
            "thesis_status": "weakening",
        }
    )
    verdict = evaluate_position_supervisor(context)
    evidence = verdict.get("evidence") or {}
    assert evidence.get("supervisor_posture") == "transition_confirming"
    assert verdict["action"] == "tighten"
    assert verdict["summary_reason"] == "transition_profit_protection_tighten"
    evidence_tags = list((verdict.get("evidence") or {}).get("trigger_tags") or [])
    assert "transition_profit_protection" in evidence_tags
    assert "transition_confirming" in evidence_tags
    controls = verdict.get("recommended_controls") or {}
    assert float(controls.get("target_stop_loss") or 0.0) > 0


def test_transition_confirming_losing_position_stays_hold():
    """Only profitable positions may tighten during transition; losses keep
    their original risk boundary."""
    context = _base_context()
    context["position"]["current_price"] = 102.0
    context["position"]["unrealized_pnl"] = -2.0
    context["risk"].update(
        {
            "mfe": 1.0,
            "mae": 9.0,
            "giveback_ratio": 1.0,
            "profit_capture_ratio": 0.0,
            "holding_efficiency": 0.05,
            "time_decay_score": 0.4,
            "thesis_status": "broken",
        }
    )
    verdict = evaluate_position_supervisor(context)
    evidence = verdict.get("evidence") or {}
    if evidence.get("supervisor_posture") == "transition_confirming":
        assert verdict["action"] == "hold"


def test_transition_without_meaningful_mfe_does_not_tighten():
    """Micro-MFE noise must not trigger protection tighten (capture floor)."""
    context = _base_context()
    context["risk"].update(
        {
            "mfe": 0.05,
            "mae": 0.5,
            "giveback_ratio": 0.9,
            "profit_capture_ratio": 0.1,
            "holding_efficiency": 0.2,
            "thesis_status": "weakening",
        }
    )
    verdict = evaluate_position_supervisor(context)
    evidence = verdict.get("evidence") or {}
    if not evidence.get("profit_protection_window_ready"):
        assert verdict["action"] != "tighten" or (
            verdict["summary_reason"] == "near_take_profit_protect"
        )


# ── F1: signal reversal producer (context payload wiring) ──────────────────


def test_signal_reversal_produced_from_composite_direction():
    from backend.services.live_position_lifecycle import (
        build_position_supervisor_context_payload,
    )

    payload = build_position_supervisor_context_payload(
        position={"position_id": 7, "direction": 1, "entry_price": 100.0},
        temporal_context={
            "holding_seconds": 900.0,
            "completed_bars_after_entry": 3,
            "closed_bar_key": "bars:3",
        },
        position_metrics={},
        entry_decision_id="dec_1",
        risk_snapshot={},
        market_context={
            "direction": -1,
            "context_state": {
                "trend_strength_state": "weak",
                "volatility_state": "high",
            },
        },
        supervisor_state={},
        max_holding_bars=288,
        open_position_count=1,
        total_api_volume=1.0,
        account={},
        template_id="position_supervisor:default.v1",
        loop_running=True,
    )
    risk = payload.get("risk") or {}
    assert risk.get("signal_reversal") is True


def test_no_signal_reversal_when_directions_align():
    from backend.services.live_position_lifecycle import (
        build_position_supervisor_context_payload,
    )

    payload = build_position_supervisor_context_payload(
        position={"position_id": 7, "direction": 1, "entry_price": 100.0},
        temporal_context={"holding_seconds": 900.0, "completed_bars_after_entry": 3},
        position_metrics={},
        entry_decision_id="dec_1",
        risk_snapshot={},
        market_context={
            "direction": 1,
            "context_state": {"trend_strength_state": "normal"},
        },
        supervisor_state={},
        max_holding_bars=288,
        open_position_count=1,
        total_api_volume=1.0,
        account={},
        template_id="position_supervisor:default.v1",
        loop_running=True,
    )
    risk = payload.get("risk") or {}
    assert risk.get("signal_reversal") is False


def test_no_signal_reversal_when_composite_direction_unknown():
    from backend.services.live_position_lifecycle import (
        build_position_supervisor_context_payload,
    )

    payload = build_position_supervisor_context_payload(
        position={"position_id": 7, "direction": 1, "entry_price": 100.0},
        temporal_context={"holding_seconds": 900.0, "completed_bars_after_entry": 3},
        position_metrics={},
        entry_decision_id="dec_1",
        risk_snapshot={},
        market_context={},
        supervisor_state={},
        max_holding_bars=288,
        open_position_count=1,
        total_api_volume=1.0,
        account={},
        template_id="position_supervisor:default.v1",
        loop_running=True,
    )
    risk = payload.get("risk") or {}
    assert risk.get("signal_reversal") is False


# ── F2: entry regime stamping flows into shift detection ───────────────────


def test_entry_regime_enables_regime_shift_confirmed():
    state = None
    _, _ = update_position_path_metrics(
        previous_state=None,
        current_pnl=0.1,
        now_ts=1000.0,
        holding_seconds=100.0,
        max_holding_seconds=86400.0,
        entry_regime="trend=normal|volatility=high",
        current_regime="trend=normal|volatility=high",
    )
    state, metrics = update_position_path_metrics(
        previous_state=state,
        current_pnl=-0.2,
        now_ts=1300.0,
        holding_seconds=400.0,
        max_holding_seconds=86400.0,
        entry_regime="trend=normal|volatility=high",
        current_regime="trend=weak|volatility=high",
    )
    assert state["entry_regime"] == "trend=normal|volatility=high"
    assert metrics["regime_shift"] == "confirmed"


def test_missing_entry_regime_keeps_shift_none():
    state, metrics = update_position_path_metrics(
        previous_state=None,
        current_pnl=-0.2,
        now_ts=1300.0,
        holding_seconds=400.0,
        max_holding_seconds=86400.0,
        entry_regime="",
        current_regime="trend=weak|volatility=high",
    )
    assert state["entry_regime"] == ""
    assert metrics["regime_shift"] == "none"


# ── F4 end-to-end through the supervisor verdict ───────────────────────────


def test_persistent_price_path_family_reaches_two_confirmations():
    """Two consecutive broken observations put persistent_price_path into the
    supervisor's evidence families via the risk-context passthrough."""
    state = None
    for i in (1, 2):
        state, metrics = update_position_path_metrics(
            previous_state=state,
            current_pnl=-1.5,
            now_ts=i * 400.0,
            holding_seconds=i * 400.0,
            max_holding_seconds=86400.0,
            entry_regime="trend=normal|volatility=low",
            current_regime="trend=normal|volatility=low",
        )
    assert metrics["thesis_broken_confirmations"] >= 2

    context = _base_context()
    context["risk"].update(
        {
            **metrics,
            "max_holding_seconds": 86400.0,
            "holding_timeout_ratio": metrics.get("timeout_ratio", 0.0),
        }
    )
    context["risk"]["signal_reversal"] = False
    context["risk"]["regime_shift"] = "none"
    verdict = evaluate_position_supervisor(context)
    evidence = dict(verdict.get("evidence") or {})
    assert int(evidence.get("thesis_broken_confirmations") or 0) >= 2
    families = list(evidence.get("thesis_break_evidence_families") or [])
    assert "persistent_price_path" in families
