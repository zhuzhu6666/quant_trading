from types import SimpleNamespace
from unittest.mock import patch

from cli.backtest import run_backtest


def test_cli_uses_canonical_parity_runner() -> None:
    report = {
        "engine": "live_parity_replay_v1",
        "metrics": {"bar_count": 8, "independent_trade_count": 1},
        "artifact_path": "fixture.json",
    }
    with patch(
        "cli.backtest.ParityReplayService.run",
        return_value=report,
    ) as runner:
        result = run_backtest(SimpleNamespace(symbol="XAUUSD+", timeframe="M5"))
    assert result == report
    assert runner.call_args.args[0]["max_bars"] == 5000
