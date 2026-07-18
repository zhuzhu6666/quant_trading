from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd

from cli.backtest import run_backtest


def test_cli_legacy_backtest_no_data_still_returns_diagnostic_contract(
    monkeypatch,
) -> None:
    class EmptyStore:
        def __init__(self, _path: str) -> None:
            pass

        def load_bars(self, _symbol: str, _timeframe: str) -> pd.DataFrame:
            return pd.DataFrame()

    monkeypatch.setitem(sys.modules, "backtrader", SimpleNamespace())
    monkeypatch.setattr("data.store.DataStore", EmptyStore)

    result = run_backtest(
        SimpleNamespace(
            symbol="XAUUSD+",
            timeframe="M15",
            risk_per_trade_pct=None,
        )
    )

    assert result["status"] == "no_data"
    assert result["engine"] == "legacy_indicator_sweep"
    assert result["evidence_class"] == "diagnostic_only"
    assert result["live_parity"] is False
    assert result["governance_eligible"] is False
    assert result["deployable_candidate"] is False
    assert result["rows"] == []
