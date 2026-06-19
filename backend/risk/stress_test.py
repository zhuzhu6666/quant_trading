"""Stress test scenarios for portfolio risk."""
from __future__ import annotations

import numpy as np
from typing import Any


class StressTest:
    """Run stress test scenarios on equity curve."""

    SCENARIOS = [
        {"name": "Flash crash -20%", "shock_pct": -0.20},
        {"name": "Moderate drop -10%", "shock_pct": -0.10},
        {"name": "Volatility spike +50%", "vol_multiplier": 1.50},
        {"name": "Recovery +15%", "shock_pct": 0.15},
    ]

    def run(self, equity_series: list[float] | np.ndarray, initial_equity: float | None = None) -> dict[str, Any]:
        """Run all stress scenarios on the given equity curve."""
        if len(equity_series) < 2:
            return {"status": "no data", "scenarios": []}

        arr = np.asarray(equity_series, dtype=float)
        current = float(arr[-1])
        initial = initial_equity if initial_equity is not None else float(arr[0])
        max_dd = self._max_drawdown(arr)

        results = []
        for sc in self.SCENARIOS:
            name = sc["name"]
            if "shock_pct" in sc:
                shock = current * sc["shock_pct"]
                new_equity = current + shock
                dd = (new_equity - initial) / initial * 100 if initial > 0 else 0.0
                results.append({
                    "name": name,
                    "shock_pct": sc["shock_pct"],
                    "current_equity": round(current, 2),
                    "new_equity": round(new_equity, 2),
                    "drawdown_pct": round(dd, 2),
                    "survives": new_equity > initial * 0.5,
                })
            elif "vol_multiplier" in sc:
                # Simulate higher volatility by scaling daily returns
                returns = np.diff(arr) / arr[:-1]
                scaled_returns = returns * sc["vol_multiplier"]
                # Project equity forward 20 days
                projected = current
                for r in scaled_returns[-20:]:
                    projected *= (1 + r)
                dd = (projected - initial) / initial * 100 if initial > 0 else 0.0
                results.append({
                    "name": name,
                    "vol_multiplier": sc["vol_multiplier"],
                    "current_equity": round(current, 2),
                    "projected_equity": round(projected, 2),
                    "drawdown_pct": round(dd, 2),
                    "survives": projected > initial * 0.5,
                })

        return {
            "status": "ok",
            "current_equity": round(current, 2),
            "initial_equity": round(initial, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "scenarios": results,
        }

    @staticmethod
    def _max_drawdown(arr: np.ndarray) -> float:
        """Calculate max drawdown as fraction."""
        peak = np.maximum.accumulate(arr)
        drawdown = (peak - arr) / peak
        return float(np.max(drawdown)) if len(drawdown) > 0 else 0.0

    def get_status(self) -> dict[str, Any]:
        return {"status": "no data", "scenarios": [], "current_equity": 0.0, "initial_equity": 0.0}
