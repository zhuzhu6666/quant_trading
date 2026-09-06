"""Downstream-block classification in execution-gate replay.

Live ledger `gate_result` is overloaded: after the alpha gate passes, a
downstream stage (RiskPolicy cvar, supervisor cooldown, cost edge, ...)
overwrites it with its own block reason. The offline alpha recompute must
count "live blocked downstream + alpha recompute passes" as agreement
(both stages correct), not as a replay mismatch.
"""

import time
from pathlib import Path

import pandas as pd

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.canonical_v2 import record_decision_event
from backend.services.replay_harness import ReplayHarnessService
from tests.canonical_fixture import make_canonical_sqlite


def test_gate_replay_counts_downstream_block_as_alpha_agreement(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = make_canonical_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        record_decision_event(
            conn,
            decision_id="dec_downstream_blocked",
            event_type="open",
            symbol="XAUUSD+",
            timeframe="M5",
            decision_ts=now - 30.0,
            action_score=0.8,
            action_reason="risk_blocked",
            action={
                "direction": 1,
                "score": 0.8,
                # Alpha gate passed live; RiskPolicy cvar blocked downstream
                # and overwrote the recorded gate reason.
                "gate_passed": False,
                "gate_reason": "cvar_gate: CVaR=4.0575% > 2.5000%",
                "requested_volume": 1.0,
                "price": 2400.0,
                "execution_gate_config": {
                    "schema_version": "execution_gate_replay_config.v1",
                    "signal_threshold": 0.3,
                    "cooldown_bars": 0,
                    "event_filter_authority": "risk_policy",
                    "risk_enable_nfp_skip": False,
                    "risk_enable_gvz_gate": False,
                    "risk_gvz_drop_pct": -2.0,
                },
            },
            risk_state={},
            portfolio_state={
                "balance": 10000.0,
                "equity": 10000.0,
                "start_balance": 10000.0,
                "n_positions": 0,
                "session_pnl": 0.0,
                "session_trades": 0,
                "consecutive_losses": 0,
                "drawdown_pct": 0.0,
                "circuit_breaker": False,
            },
            created_at=now,
            factor_snapshots=[
                {
                    "factor": "rsi_14",
                    "normalized_value": 0.6,
                    "policy_weight": 0.2,
                    "contribution_score": 0.12,
                }
            ],
        )
        conn.commit()
    finally:
        conn.close()

    service = ReplayHarnessService(
        db_path,
        artifact_dir=tmp_path / "replay_artifacts",
    )

    def _fake_bar_window(**kwargs):
        decision_ts = kwargs["decision_ts"]
        return [
            {
                "time": decision_ts,
                "open": 2399.0,
                "high": 2401.0,
                "low": 2398.0,
                "close": 2400.0,
                "volume": 10.0,
            }
        ]

    service._load_bar_window = _fake_bar_window
    service._enrich_bar_window = lambda bars, **_kwargs: pd.DataFrame(bars)

    report = service.run_bar_replay_evidence(
        lookback_days=1,
        limit=10,
        warmup_bars=1,
        post_bars=0,
    )

    gate_metrics = report["metric_summary"]["execution_gate_recompute"]
    assert gate_metrics["attempted_count"] == 1
    assert gate_metrics["disagreement_count"] == 0
    assert gate_metrics["downstream_blocked_alpha_passed_count"] == 1
    assert gate_metrics["agreement_count"] == 1
    assert Path(report["artifact_path"]).exists()
