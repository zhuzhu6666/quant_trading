import sqlite3
import time

from backend.api import risk as risk_api
from backend.core.db import STATE_DB_DDL
from backend.services.canonical_v2 import record_decision_event
from backend.services.position_supervisor import evaluate_position_supervisor
from backend.services.position_supervisor_templates import CONSERVATIVE_TEMPLATE_ID, PROFIT_PROTECTION_TEMPLATE_ID
from tests.canonical_fixture import make_canonical_sqlite


def test_position_supervisor_derives_completed_bars_when_temporal_value_is_missing():
    verdict = evaluate_position_supervisor({
        "position": {
            "position_id": "bars-fallback", "direction": 1,
            "entry_price": 3000.0, "current_price": 3001.0,
            "volume": 100.0, "unrealized_pnl": 1.0,
        },
        "risk": {"thesis_status": "intact", "regime_shift": "none"},
        "temporal_context": {"holding_seconds": 1252.0},
    })

    assert verdict["evidence"]["completed_bars_after_entry"] == 4
    assert verdict["evidence"]["closed_bar_window_ready"] is True


def test_position_supervisor_strong_trend_holds_near_take_profit():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "trend-hold",
                "direction": 1,
                "entry_price": 3000.0,
                "current_price": 3038.5,
                "volume": 100.0,
                "unrealized_pnl": 38.5,
                "current_price_state": "known",
                "pnl_state": "known",
                "sl": 2990.0,
                "tp": 3040.0,
            },
            "risk": {
                "mfe": 40.0,
                "mae": 1.0,
                "giveback_ratio": 0.15,
                "profit_capture_ratio": 0.85,
                "holding_efficiency": 0.9,
                "time_decay_score": 0.9,
                "thesis_status": "intact",
                "regime_shift": "none",
            },
            "market": {
                "trend_strength_state": "strong",
                "volatility_state": "high",
                "regime_source": "context_state.market_dimensions",
                "regime_id": "trend=strong|volatility=high",
                "regime_dimensions": {"trend": "strong", "volatility": "high"},
            },
            "temporal_context": {
                "holding_seconds": 900.0,
                "completed_bars_after_entry": 3,
            },
        }
    )

    assert verdict["action"] == "hold"
    assert verdict["summary_reason"] == "trend_hold_preserve_profit"
    assert verdict["evidence"]["supervisor_posture"] == "trend_hold"


def test_position_supervisor_range_capture_allows_mature_giveback_recommendation():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "range-capture",
                "direction": 1,
                "entry_price": 3000.0,
                "current_price": 3010.0,
                "volume": 100.0,
                "unrealized_pnl": 10.0,
                "current_price_state": "known",
                "pnl_state": "known",
                "sl": 2990.0,
                "tp": 3040.0,
            },
            "risk": {
                "mfe": 40.0,
                "mae": 5.0,
                "giveback_ratio": 0.75,
                "profit_capture_ratio": 0.25,
                "holding_efficiency": 0.55,
                "time_decay_score": 0.8,
                "thesis_status": "intact",
                "regime_shift": "none",
            },
            "market": {
                "trend_strength_state": "normal",
                "volatility_state": "normal",
                "regime_source": "context_state.market_dimensions",
                "regime_id": "trend=normal|volatility=normal",
                "regime_dimensions": {"trend": "normal", "volatility": "normal"},
            },
            "temporal_context": {
                "holding_seconds": 1800.0,
                "completed_bars_after_entry": 4,
            },
        }
    )

    assert verdict["action"] == "reduce"
    assert verdict["summary_reason"] == "profit_giveback_after_mfe"
    assert verdict["evidence"]["supervisor_posture"] == "range_capture"


def test_position_supervisor_unknown_market_context_observes_non_hard_management():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "unknown-market",
                "direction": -1,
                "entry_price": 4100.0,
                "current_price": 4081.0,
                "volume": 100.0,
                "unrealized_pnl": 19.0,
                "current_price_state": "known",
                "pnl_state": "known",
                "sl": 4110.0,
                "tp": 4080.0,
            },
            "risk": {
                "mfe": 19.0,
                "holding_efficiency": 0.8,
                "time_decay_score": 0.9,
                "thesis_status": "intact",
            },
            "market": {
                "trend_strength_state": "unknown",
                "volatility_state": "unknown",
                "regime_source": "unavailable",
            },
            "temporal_context": {"holding_seconds": 180.0},
        }
    )

    assert verdict["action"] == "hold"
    assert verdict["evidence"]["supervisor_posture"] == "unknown_observe"


def test_position_supervisor_hard_risk_overrides_trend_hold():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "hard-risk",
                "direction": 1,
                "entry_price": 3000.0,
                "current_price": 2990.0,
                "volume": 100.0,
                "unrealized_pnl": -10.0,
                "sl": 2989.0,
                "tp": 3040.0,
            },
            "risk": {
                "hard_risk_active": True,
                "thesis_status": "intact",
            },
            "market": {
                "trend_strength_state": "strong",
                "volatility_state": "normal",
                "regime_source": "context_state.market_dimensions",
            },
            "temporal_context": {"holding_seconds": 600.0},
        }
    )

    assert verdict["action"] == "close"
    assert verdict["summary_reason"] == "hard_risk_active"


def test_position_supervisor_recommends_reduce_after_large_giveback():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9001",
                "direction": 1,
                "entry_price": 3000.0,
                "current_price": 3010.0,
                "volume": 100.0,
                "unrealized_pnl": 10.0,
                "sl": 2990.0,
                "tp": 3040.0,
            },
            "risk": {
                "max_holding_seconds": 7200.0,
                "holding_timeout_ratio": 0.55,
                "mfe": 40.0,
                "mae": 5.0,
                "giveback_ratio": 0.75,
                "profit_capture_ratio": 0.25,
                "time_in_profit": 1800.0,
                "holding_efficiency": 0.35,
                "time_decay_score": 0.42,
                "thesis_status": "weakening",
                "regime_shift": "none",
            },
            "temporal_context": {
                "decision_ts": time.time(),
                "holding_seconds": 4000.0,
            },
            "market_space_context": {},
            "market": {
                "trend_strength_state": "normal",
                "volatility_state": "normal",
                "regime_source": "context_state.market_dimensions",
            },
            "entry_context": {},
            "runtime": {},
        }
    )

    assert verdict["action"] == "hold"
    assert verdict["summary_reason"] == "transition_confirming"
    assert verdict["evidence"]["supervisor_posture"] == "transition_confirming"


def test_position_supervisor_recommends_close_when_timeout_exceeded():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9002",
                "direction": -1,
                "entry_price": 3000.0,
                "current_price": 3015.0,
                "volume": 100.0,
                "unrealized_pnl": -15.0,
                "sl": 3020.0,
                "tp": 2960.0,
            },
            "risk": {
                "max_holding_seconds": 3600.0,
                "holding_timeout_ratio": 1.08,
                "mfe": 8.0,
                "mae": 20.0,
                "giveback_ratio": 1.0,
                "profit_capture_ratio": 0.0,
                "time_in_profit": 300.0,
                "holding_efficiency": 0.12,
                "time_decay_score": 0.18,
                "thesis_status": "broken",
                "regime_shift": "confirmed",
            },
            "temporal_context": {
                "decision_ts": time.time(),
                "holding_seconds": 3900.0,
            },
            "market_space_context": {},
            "entry_context": {},
            "runtime": {},
        }
    )

    assert verdict["action"] == "close"
    assert verdict["summary_reason"] == "holding_timeout_exceeded"
    assert verdict["recommended_controls"]["protection_mode"] == "full_exit"


def test_position_supervisor_default_template_delays_thesis_break_until_complete_bars():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9003",
                "direction": 1,
                "entry_price": 3000.0,
                "current_price": 2999.0,
                "volume": 100.0,
                "unrealized_pnl": -1.0,
            },
            "risk": {
                "mfe": 0.0,
                "mae": 1.0,
                "holding_efficiency": 0.8,
                "time_decay_score": 0.9,
                "thesis_status": "broken",
            },
            "temporal_context": {"decision_ts": time.time(), "holding_seconds": 60.0},
        }
    )

    assert verdict["action"] == "hold"
    assert verdict["summary_reason"] == "thesis_break_unconfirmed"
    assert "thesis_broken_unconfirmed" in verdict["evidence"]["trigger_tags"]
    assert verdict["supervisor_template"]["template_version"] == "default.v1"


def test_position_supervisor_conservative_template_delays_early_thesis_broken_close():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9004",
                "direction": 1,
                "entry_price": 3000.0,
                "current_price": 2999.0,
                "volume": 100.0,
                "unrealized_pnl": -1.0,
            },
            "risk": {
                "mfe": 0.0,
                "mae": 1.0,
                "holding_efficiency": 0.4,
                "time_decay_score": 0.9,
                "thesis_status": "broken",
            },
            "temporal_context": {"decision_ts": time.time(), "holding_seconds": 60.0},
            "position_supervisor_template": CONSERVATIVE_TEMPLATE_ID,
        }
    )

    assert verdict["action"] == "hold"
    assert verdict["summary_reason"] == "thesis_break_unconfirmed"
    assert "thesis_broken_delayed" in verdict["evidence"]["trigger_tags"]
    assert verdict["supervisor_template"]["template_version"] == "conservative.v1"


def test_profit_protection_template_delays_young_thesis_broken_full_exit():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9004b",
                "direction": 1,
                "entry_price": 3000.0,
                "current_price": 2997.0,
                "volume": 100.0,
                "unrealized_pnl": -3.0,
                "sl": 2988.0,
                "tp": 3020.0,
            },
            "risk": {
                "mfe": 0.0,
                "mae": 3.0,
                "holding_efficiency": 0.0,
                "time_decay_score": 0.8,
                "thesis_status": "broken",
                "regime_shift": "none",
            },
            "temporal_context": {"decision_ts": time.time(), "holding_seconds": 190.0},
            "position_supervisor_template": PROFIT_PROTECTION_TEMPLATE_ID,
        }
    )

    assert verdict["action"] == "hold"
    assert verdict["summary_reason"] == "thesis_break_unconfirmed"
    assert "thesis_broken_delayed" in verdict["evidence"]["trigger_tags"]
    assert verdict["supervisor_template"]["thresholds"]["min_thesis_break_seconds"] == 300.0


def test_profit_protection_template_requires_two_independent_evidence_families():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9004c",
                "direction": 1,
                "entry_price": 3000.0,
                "current_price": 2997.0,
                "volume": 100.0,
                "unrealized_pnl": -3.0,
                "sl": 2988.0,
                "tp": 3020.0,
            },
            "risk": {
                "mfe": 0.0,
                "mae": 3.0,
                "holding_efficiency": 0.0,
                "time_decay_score": 0.8,
                "thesis_status": "broken",
                "regime_shift": "none",
                "thesis_broken_confirmations": 2,
            },
            "temporal_context": {"decision_ts": time.time(), "holding_seconds": 360.0},
            "position_supervisor_template": PROFIT_PROTECTION_TEMPLATE_ID,
        }
    )

    assert verdict["action"] == "hold"
    assert verdict["summary_reason"] == "thesis_break_unconfirmed"
    assert verdict["evidence"]["thesis_break_confirmed"] is False
    assert verdict["evidence"]["thesis_broken_confirmations"] == 2


def test_profit_protection_template_blocks_micro_mfe_even_after_evidence_window():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9004d",
                "direction": 1,
                "entry_price": 4127.16,
                "current_price": 4125.045,
                "volume": 100.0,
                "unrealized_pnl": -0.56,
                "sl": 4121.93,
                "tp": 4135.0,
            },
            "risk": {
                "mfe": 0.03,
                "mae": 0.56,
                "giveback_ratio": 1.0,
                "profit_capture_ratio": 0.0,
                "time_in_profit": 125.7,
                "holding_efficiency": 0.158892,
                "time_decay_score": 0.65,
                "thesis_status": "broken",
                "regime_shift": "none",
            },
            "temporal_context": {"decision_ts": time.time(), "holding_seconds": 316.45},
            "position_supervisor_template": PROFIT_PROTECTION_TEMPLATE_ID,
        }
    )

    assert verdict["action"] == "hold"
    assert verdict["summary_reason"] == "thesis_break_unconfirmed"
    assert verdict["evidence"]["mfe_is_meaningful"] is False
    assert verdict["evidence"]["profit_protection_window_ready"] is False
    assert verdict["evidence"]["thesis_break_ready"] is False
    assert verdict["evidence"]["thesis_break_confirmed"] is False
    assert verdict["evidence"]["stop_loss_progress"] < 0.82


def test_profit_protection_evidence_gate_is_invariant_across_templates():
    base = {
        "position": {
            "position_id": "template-invariant-gate",
            "direction": -1,
            "entry_price": 4000.0,
            "current_price": 3999.8,
            "volume": 100.0,
            "unrealized_pnl": 0.02,
            "sl": 4010.0,
            "tp": 3980.0,
        },
        "risk": {
            "mfe": 0.12,
            "mae": 0.01,
            "giveback_ratio": 0.92,
            "profit_capture_ratio": 0.08,
            "holding_efficiency": 0.15,
            "time_decay_score": 0.7,
            "thesis_status": "intact",
            "regime_shift": "none",
        },
        "temporal_context": {
            "decision_ts": time.time(),
            "holding_seconds": 90.0,
            "completed_bars_after_entry": 1,
        },
    }
    templates = [None, CONSERVATIVE_TEMPLATE_ID, PROFIT_PROTECTION_TEMPLATE_ID]

    for template_id in templates:
        context = dict(base)
        if template_id:
            context["position_supervisor_template"] = template_id
        verdict = evaluate_position_supervisor(context)
        assert verdict["action"] == "hold"
        assert verdict["summary_reason"] == "profit_protection_evidence_pending"
        assert verdict["evidence"]["model_action_boundary_ready"] is False
        assert verdict["evidence"]["profit_protection_window_ready"] is False


def test_position_supervisor_captures_when_near_take_profit():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9005",
                "direction": -1,
                "entry_price": 4100.0,
                "current_price": 4081.0,
                "volume": 100.0,
                "unrealized_pnl": 19.0,
                "current_price_state": "known",
                "pnl_state": "known",
                "sl": 4110.0,
                "tp": 4080.0,
            },
            "risk": {
                "mfe": 19.0,
                "mae": 0.2,
                "holding_efficiency": 0.8,
                "time_decay_score": 0.9,
                "thesis_status": "intact",
            },
            "market": {
                "trend_strength_state": "normal",
                "volatility_state": "normal",
                "regime_source": "context_state.market_dimensions",
            },
            "temporal_context": {"decision_ts": time.time(), "holding_seconds": 180.0},
        }
    )

    assert verdict["action"] == "close"
    assert verdict["summary_reason"] == "near_take_profit_capture"
    assert "near_take_profit" in verdict["evidence"]["trigger_tags"]
    assert verdict["evidence"]["take_profit_progress"] >= 0.9


def test_profit_protection_template_outputs_dynamic_tpsl_candidate_near_take_profit():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9005b",
                "direction": 1,
                "entry_price": 4000.0,
                "current_price": 4028.0,
                "volume": 100.0,
                "unrealized_pnl": 28.0,
                "current_price_state": "known",
                "pnl_state": "known",
                "sl": 3990.0,
                "tp": 4030.0,
            },
            "risk": {
                "mfe": 29.0,
                "mae": 0.2,
                "giveback_ratio": 0.05,
                "profit_capture_ratio": 0.78,
                "holding_efficiency": 0.86,
                "time_decay_score": 0.9,
                "thesis_status": "intact",
                "regime_shift": "none",
            },
            "market": {
                "trend_strength_state": "normal",
                "volatility_state": "normal",
                "regime_source": "context_state.market_dimensions",
            },
            "temporal_context": {"decision_ts": time.time(), "holding_seconds": 240.0},
            "position_supervisor_template": PROFIT_PROTECTION_TEMPLATE_ID,
        }
    )

    assert verdict["action"] == "tighten"
    assert verdict["summary_reason"] == "near_take_profit_protect"
    assert verdict["recommended_controls"]["protection_mode"] == "dynamic_tpsl"
    assert verdict["recommended_controls"]["target_stop_loss"] > 4000.0
    assert verdict["recommended_controls"]["target_take_profit"] > 4030.0
    assert verdict["protection_candidates"][0]["source"] == "supervisor_dynamic_tpsl"
    assert verdict["protection_candidates"][0]["target_take_profit"] == verdict["recommended_controls"]["target_take_profit"]


def test_position_supervisor_preempts_when_near_stop_loss_and_weak():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9006",
                "direction": -1,
                "entry_price": 4100.0,
                "current_price": 4109.0,
                "volume": 100.0,
                "unrealized_pnl": -9.0,
                "current_price_state": "known",
                "pnl_state": "known",
                "sl": 4110.0,
                "tp": 4080.0,
            },
            "risk": {
                "mfe": 0.0,
                "mae": 9.0,
                "holding_efficiency": 0.05,
                "time_decay_score": 0.9,
                "thesis_status": "weakening",
            },
            "temporal_context": {"decision_ts": time.time(), "holding_seconds": 120.0},
        }
    )

    assert verdict["action"] == "close"
    assert verdict["summary_reason"] == "near_stop_loss_preemptive_exit"
    assert "near_stop_loss" in verdict["evidence"]["trigger_tags"]
    assert verdict["evidence"]["stop_loss_progress"] >= 0.85


def test_position_supervisor_does_not_self_trigger_from_tightened_stop():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "tightened-stop",
                "direction": -1,
                "entry_price": 4094.78,
                "current_price": 4096.73,
                "volume": 100.0,
                "unrealized_pnl": -1.95,
                "current_price_state": "known",
                "pnl_state": "known",
                "sl": 4097.07,
                "tp": 4085.28,
            },
            "risk": {
                "mfe": 0.0,
                "mae": 2.68,
                "holding_efficiency": 0.0,
                "time_decay_score": 1.0,
                "thesis_status": "broken",
                "original_stop_loss": 4101.12,
            },
            "temporal_context": {
                "decision_ts": time.time(),
                "holding_seconds": 211.0,
                "completed_bars_after_entry": 0,
            },
        }
    )

    assert verdict["action"] == "hold"
    assert verdict["summary_reason"] == "thesis_break_unconfirmed"
    assert verdict["evidence"]["current_stop_loss_progress"] >= 0.85
    assert verdict["evidence"]["stop_loss_progress"] < 0.85
    assert (
        verdict["evidence"]["stop_loss_progress_source"]
        == "original_entry_protection"
    )


def test_trade_trace_exposes_position_supervisor_events(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = make_canonical_sqlite(db_path)
    conn.executescript(STATE_DB_DDL)
    record_decision_event(
        conn,
        decision_id="dec_open",
        trade_id="9001",
        position_id="9001",
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=100.0,
        action={"side": "sell"},
        action_score=0.7,
        action_reason="opened",
        created_at=100.0,
    )
    record_decision_event(
        conn,
        decision_id="dec_supervisor",
        trade_id="9001",
        position_id="9001",
        event_type="supervisor_reduce",
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=150.0,
        action_reason="profit_giveback_after_mfe",
        action={
            "supervisor_verdict": {
                "action": "reduce",
                "human_summary": "系统判断这笔仓位仍有逻辑，但不值得继续满仓承受同样风险，建议先降一部分。",
            },
            "risk_verdict": {"allowed": True, "reason": "risk_reducing_action"},
        },
        created_at=150.0,
    )
    conn.execute(
        """
        INSERT INTO recovery_position_state
        (position_id, broker, symbol, direction, open_price, volume, status,
         entry_decision_id, context_integrity, recovery_meta_json)
        VALUES (?, 'ctrader', 'XAUUSD+', -1, 3992.0, 100, 'open', ?, 'full', ?)
        """,
        (
            9001,
            "dec_open",
            '{"latest_supervisor": {"action": "reduce"}}',
        ),
    )
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)

    result = risk_api._trade_trace(position_id="9001")

    assert result["summary"]["supervisor_events"] == 1
    assert result["summary"]["latest_supervisor_action"] == "reduce"
    assert result["position_supervisor"]["latest"]["event_type"] == "supervisor_reduce"
    assert result["position_supervisor"]["events"][0]["action"]["supervisor_verdict"]["action"] == "reduce"
