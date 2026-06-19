"""VaR / CVaR calculation for the trading portfolio."""
from __future__ import annotations

import numpy as np
from typing import Any
from loguru import logger


class VaRCalculator:
    """Calculate Value at Risk and Conditional VaR."""

    def __init__(self, confidence: float = 0.95):
        self.confidence = confidence

    def calculate(self, equity_series: list[float] | np.ndarray, lookback: int = 100) -> dict[str, Any]:
        """
        Calculate VaR and CVaR from equity curve.

        Parameters
        ----------
        equity_series : list[float] | np.ndarray
            Equity curve (or PnL series).
        lookback : int
            Number of recent points to use (default 100).

        Returns
        -------
        dict with keys: var, cvar, var_pct, confidence, lookback, current_equity
        """
        if len(equity_series) < 2:
            return {"var": 0.0, "cvar": 0.0, "var_pct": 0.0, "confidence": self.confidence,
                    "lookback": lookback, "current_equity": 0.0, "error": "insufficient data"}

        arr = np.asarray(equity_series, dtype=float)
        if len(arr) > lookback:
            arr = arr[-lookback:]

        # Daily returns
        returns = np.diff(arr) / arr[:-1]
        if len(returns) == 0:
            return {"var": 0.0, "cvar": 0.0, "var_pct": 0.0, "confidence": self.confidence,
                    "lookback": lookback, "current_equity": float(arr[-1]), "error": "insufficient returns"}

        var = np.percentile(returns, (1 - self.confidence) * 100)
        cvar = returns[returns <= var].mean() if any(returns <= var) else var

        current_equity = float(arr[-1])
        var_dollar = abs(var * current_equity)
        cvar_dollar = abs(cvar * current_equity)

        return {
            "var": round(var_dollar, 2),
            "cvar": round(cvar_dollar, 2),
            "var_pct": round(abs(var) * 100, 2),
            "confidence": self.confidence,
            "lookback": int(len(returns)),
            "current_equity": current_equity,
        }

    def get_status(self, equity_series: list[float] | None = None) -> dict[str, Any]:
        """Return VaR status for the API. If no equity provided, return empty structure."""
        if equity_series is None:
            return {"var": 0.0, "cvar": 0.0, "var_pct": 0.0, "confidence": self.confidence,
                    "lookback": 0, "current_equity": 0.0, "status": "no data"}
        result = self.calculate(equity_series)
        result["status"] = "ok"
        return result
