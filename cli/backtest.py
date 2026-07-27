"""CLI entry for the canonical historical parity backtest."""
from __future__ import annotations

import json
import logging

from backend.services.parity_replay import ParityReplayService

logger = logging.getLogger("quant")


def run_backtest(args):
    params = {
        "symbol": getattr(args, "symbol", "XAUUSD+"),
        "timeframe": getattr(args, "timeframe", "M5"),
        "start": getattr(args, "start", None),
        "end": getattr(args, "end", None),
        "max_bars": min(20_000, int(getattr(args, "max_bars", 5000) or 5000)),
        "warmup_bars": int(getattr(args, "warmup_bars", 150) or 150),
        "initial_equity": float(getattr(args, "initial_equity", 10_000.0) or 10_000.0),
        "volume_lots": float(getattr(args, "volume_lots", 0.01) or 0.01),
        "commission_per_lot_round_turn": float(
            getattr(args, "commission_per_lot_round_turn", 6.0) or 6.0
        ),
        "slippage_bps": float(getattr(args, "slippage_bps", 0.0) or 0.0),
        "persist_artifact": True,
    }
    report = ParityReplayService().run(params)
    logger.info(
        "Parity backtest complete: %s bars, %s independent trades, artifact=%s",
        (report.get("metrics") or {}).get("bar_count", 0),
        (report.get("metrics") or {}).get("independent_trade_count", 0),
        report.get("artifact_path", ""),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return report
