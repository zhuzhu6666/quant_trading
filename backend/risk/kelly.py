"""Kelly criterion sizing for trade optimization."""
from __future__ import annotations

from typing import Any


class KellyCriterion:
    """Kelly fraction calculator for optimal position sizing."""

    def calculate(self, win_rate: float, avg_win: float, avg_loss: float) -> dict[str, Any]:
        """
        Calculate Kelly fraction: f = W - (1-W)/R
        where W = win rate, R = avg_win / avg_loss
        """
        if avg_loss <= 0:
            return {"kelly_fraction": 0.0, "half_kelly": 0.0, "quarter_kelly": 0.0,
                    "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
                    "error": "avg_loss must be > 0"}
        r = avg_win / avg_loss
        if r <= 0:
            return {"kelly_fraction": 0.0, "half_kelly": 0.0, "quarter_kelly": 0.0,
                    "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
                    "error": "invalid win/loss ratio"}
        f = win_rate - (1 - win_rate) / r
        f = max(0.0, min(1.0, f))
        return {
            "kelly_fraction": round(f, 4),
            "half_kelly": round(f / 2, 4),
            "quarter_kelly": round(f / 4, 4),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
        }

    def get_status(self) -> dict[str, Any]:
        """Return empty Kelly status (requires trade history)."""
        return {"kelly_fraction": 0.0, "half_kelly": 0.0, "quarter_kelly": 0.0,
                "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "status": "no data"}
