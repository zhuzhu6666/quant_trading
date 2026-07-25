"""Historical forward VaR/CVaR over a frozen closed-bar return distribution."""
from __future__ import annotations

from typing import Any

import numpy as np


class VaRCalculator:
    def __init__(self, confidence: float = 0.95, min_returns: int = 10):
        if not 0 < confidence < 1:
            raise ValueError("confidence must be between 0 and 1")
        self.confidence = float(confidence)
        self.min_returns = max(1, int(min_returns))

    def calculate_forward(
        self,
        returns: list[float] | np.ndarray,
        *,
        net_notional_usd: float,
        current_equity: float,
        lookback: int = 500,
        timeframe: str = "",
    ) -> dict[str, Any]:
        try:
            values = np.asarray(returns, dtype=float)
            notional = float(net_notional_usd)
            equity = float(current_equity)
        except (TypeError, ValueError, OverflowError):
            return self._empty("error", "invalid_forward_var_inputs", timeframe=timeframe)
        if (
            values.ndim != 1
            or not np.all(np.isfinite(values))
            or not np.isfinite(notional)
            or not np.isfinite(equity)
            or equity <= 0
        ):
            return self._empty("error", "invalid_forward_var_inputs", timeframe=timeframe)

        values = values[-max(1, int(lookback)) :]
        if len(values) < self.min_returns:
            return self._empty(
                "warming_up",
                "insufficient_closed_bar_returns",
                sample_count=len(values),
                current_equity=equity,
                net_notional_usd=notional,
                timeframe=timeframe,
            )

        pnl_samples = values * notional
        threshold = float(
            np.percentile(pnl_samples, (1 - self.confidence) * 100)
        )
        tail = pnl_samples[pnl_samples <= threshold]
        var_usd = max(0.0, -threshold)
        cvar_usd = max(
            var_usd,
            max(0.0, -float(tail.mean() if len(tail) else threshold)),
        )
        var_fraction = var_usd / equity
        cvar_fraction = cvar_usd / equity
        return {
            "status": "known",
            "method": "historical",
            "alpha": self.confidence,
            "horizon": "one_closed_bar",
            "timeframe": str(timeframe or ""),
            "sample_count": len(values),
            "current_equity": equity,
            "net_notional_usd": round(notional, 8),
            "var_usd": round(var_usd, 2),
            "cvar_usd": round(cvar_usd, 2),
            "var_fraction": round(var_fraction, 8),
            "cvar_fraction": round(cvar_fraction, 8),
            "var_pct": round(var_fraction * 100, 6),
            "cvar_pct": round(cvar_fraction * 100, 6),
        }

    def get_status(self) -> dict[str, Any]:
        return self._empty("unknown", "missing_forward_var_input")

    def _empty(
        self,
        status: str,
        reason: str,
        *,
        sample_count: int = 0,
        current_equity: float | None = None,
        net_notional_usd: float | None = None,
        timeframe: str = "",
    ) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "method": "historical",
            "alpha": self.confidence,
            "horizon": "one_closed_bar",
            "timeframe": str(timeframe or ""),
            "sample_count": int(sample_count),
            "current_equity": current_equity,
            "net_notional_usd": net_notional_usd,
            "var_usd": None,
            "cvar_usd": None,
            "var_fraction": None,
            "cvar_fraction": None,
            "var_pct": None,
            "cvar_pct": None,
        }
