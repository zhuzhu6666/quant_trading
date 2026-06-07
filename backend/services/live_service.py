"""Live trading service — thin wrapper around execution/mt5_bridge + ctrader_bridge.

Note: MT5 is currently blocked (balance=0; see PROJECT_AUDIT.md §4.2 blocked-1, blocked-2).
This service surfaces clear errors when start is attempted without a working broker.
"""
import traceback
from typing import Any

from loguru import logger


def get_status() -> dict:
    """Report current live-trading broker status (best-effort, no broker connection)."""
    mt5_status = "unknown"
    ctrader_status = "unknown"
    mt5_error: str | None = None
    ctrader_error: str | None = None

    # Probe MT5
    try:
        from execution.mt5_bridge import MT5Bridge
        bridge = MT5Bridge()
        if bridge.initialize():
            mt5_status = "connected"
            bridge.shutdown()
        else:
            mt5_status = "disconnected"
            mt5_error = "initialize returned False (likely no MT5 terminal running)"
    except Exception as e:
        mt5_status = "error"
        mt5_error = f"{type(e).__name__}: {e}"[:300]

    # Probe cTrader (token only)
    try:
        from execution.ctrader_bridge import CTraderBridge
        bridge = CTraderBridge()
        if hasattr(bridge, "has_token") and not bridge.has_token():
            ctrader_status = "no_token"
            ctrader_error = "set CTRADER_TOKEN in .env"
        else:
            ctrader_status = "token_present"
    except Exception as e:
        ctrader_status = "error"
        ctrader_error = f"{type(e).__name__}: {e}"[:300]

    return {
        "mt5": {"status": mt5_status, "error": mt5_error},
        "ctrader": {"status": ctrader_status, "error": ctrader_error},
    }


def emergency_close(broker: str, symbol: str | None = None) -> dict:
    """Close all positions (or one symbol) on the given broker."""
    if broker == "mt5":
        try:
            from execution.mt5_bridge import MT5Bridge
            bridge = MT5Bridge()
            if not bridge.initialize():
                return {"ok": False, "error": "mt5_initialize_failed"}
            try:
                if symbol:
                    bridge.close_symbol(symbol)
                else:
                    bridge.close_all_positions()
                return {"ok": True, "broker": "mt5", "symbol": symbol or "ALL"}
            finally:
                bridge.shutdown()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-300:]}
    elif broker == "ctrader":
        return {"ok": False, "error": "ctrader emergency close not yet implemented (Phase 4)"}
    else:
        return {"ok": False, "error": f"unknown broker: {broker}"}
