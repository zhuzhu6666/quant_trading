from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from backend.ledger.service import DecisionLedger
from backend.services.market_regime import resolve_market_regime


def _composite(**overrides):
    values = {
        "direction": 1,
        "score": 0.7,
        "timestamp": 1_782_373_400.0,
        "factor_signals": {"ema": 0.8},
        "factor_values": {"ema": 0.8},
        "active_weights": {"ema": 0.2},
        "factor_roles": {"ema": "alpha"},
        "context_state": {
            "trend_strength_state": "strong",
            "volatility_state": "high",
            "session_state": "us",
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_resolve_market_regime_prefers_explicit_fact():
    resolved = resolve_market_regime(
        {"regime_id": "breakout", "regime_confidence": 0.93, "context_state": {}}
    )

    assert resolved == {
        "regime_id": "breakout",
        "confidence": 0.93,
        "source": "composite.regime_id",
        "dimensions": {},
    }


def test_resolve_market_regime_derives_low_cardinality_market_dimensions():
    resolved = resolve_market_regime(_composite())

    assert resolved["regime_id"] == "trend=strong|volatility=high"
    assert resolved["confidence"] == 0.8
    assert resolved["source"] == "context_state.market_dimensions"


def test_composite_decision_persists_regime_for_trade_review(tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    decision_id = ledger.log_composite_decision(
        event_type="open",
        composite=_composite(),
        symbol="XAUUSD+",
        timeframe="M5",
        position_id="42",
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT regime_id, regime_confidence, action_json FROM decision_ledger WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
    assert row["regime_id"] == "trend=strong|volatility=high"
    assert row["regime_confidence"] == 0.8
    action = json.loads(row["action_json"])
    assert action["regime_id"] == row["regime_id"]
    assert action["regime_source"] == "context_state.market_dimensions"


def test_composite_decision_preserves_abstain_null_values(tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))
    decision_id = ledger.log_composite_decision(
        event_type="signal",
        composite=_composite(
            factor_signals={"macro": None},
            factor_values={"macro": None},
            active_weights={"macro": 0.0},
            factor_roles={"macro": "alpha"},
        ),
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT raw_value, normalized_value, gated, gated_reason "
            "FROM decision_factor_snapshot WHERE decision_id=?",
            (decision_id,),
        ).fetchone()

    assert row["raw_value"] is None
    assert row["normalized_value"] is None
    assert row["gated"] == 1
    assert row["gated_reason"] == "abstain"
