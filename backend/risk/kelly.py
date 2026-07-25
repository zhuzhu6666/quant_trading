"""Kelly criterion sizing for trade optimization."""
from __future__ import annotations

import math
from typing import Any


class KellyCriterion:
    """Kelly fraction calculator for optimal position sizing."""

    def calculate(self, win_rate: float, avg_win: float, avg_loss: float) -> dict[str, Any]:
        """
        Calculate Kelly fraction: f = W - (1-W)/R
        where W = win rate, R = avg_win / avg_loss
        """
        try:
            valid = (
                all(math.isfinite(float(value)) for value in (win_rate, avg_win, avg_loss))
                and 0.0 <= win_rate <= 1.0
                and avg_win > 0
                and avg_loss > 0
            )
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            return {"status": "unknown", "kelly_fraction": None, "half_kelly": None, "quarter_kelly": None,
                    "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
                    "error": "invalid_kelly_inputs"}
        r = avg_win / avg_loss
        if r <= 0:
            return {"status": "unknown", "kelly_fraction": None, "half_kelly": None, "quarter_kelly": None,
                    "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
                    "error": "invalid win/loss ratio"}
        f = win_rate - (1 - win_rate) / r
        f = max(0.0, min(1.0, f))
        return {
            "status": "known",
            "kelly_fraction": round(f, 4),
            "half_kelly": round(f / 2, 4),
            "quarter_kelly": round(f / 4, 4),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
        }

    def get_status(self) -> dict[str, Any]:
        """Return empty Kelly status (requires trade history)."""
        return {"kelly_fraction": None, "half_kelly": None, "quarter_kelly": None,
                "win_rate": None, "avg_win": None, "avg_loss": None,
                "status": "warming_up"}
