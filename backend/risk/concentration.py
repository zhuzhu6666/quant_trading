"""Concentration risk checker for factor / position weights."""
from __future__ import annotations

from typing import Any


class ConcentrationChecker:
    """Check if portfolio is over-concentrated in any factor or position."""

    def __init__(self, max_single_weight: float = 0.40, max_sector_weight: float = 0.60):
        self.max_single_weight = max_single_weight
        self.max_sector_weight = max_sector_weight

    def check(self, weights: dict[str, float] | None = None) -> dict[str, Any]:
        """
        Check concentration from factor weights.

        weights: {factor_name: weight_pct}
        """
        if not weights:
            return {
                "max_single_weight": 0.0,
                "max_single_name": "",
                "max_sector_weight": 0.0,
                "max_sector_name": "",
                "is_safe": True,
                "alerts": [],
                "status": "no data",
            }

        total = sum(weights.values())
        if total <= 0:
            return {
                "max_single_weight": 0.0,
                "max_single_name": "",
                "max_sector_weight": 0.0,
                "max_sector_name": "",
                "is_safe": True,
                "alerts": [],
                "status": "zero total",
            }

        normalized = {k: v / total for k, v in weights.items()}
        max_name = max(normalized, key=normalized.get)
        max_w = normalized[max_name]

        alerts = []
        if max_w > self.max_single_weight:
            alerts.append(f"因子 {max_name} 权重 {max_w:.1%} > {self.max_single_weight:.0%}")

        # For now, treat all as one sector (simplified)
        sector_weight = max_w
        sector_name = max_name
        if sector_weight > self.max_sector_weight:
            alerts.append(f"集中度 {sector_name} 权重 {sector_weight:.1%} > {self.max_sector_weight:.0%}")

        return {
            "max_single_weight": round(max_w, 4),
            "max_single_name": max_name,
            "max_sector_weight": round(sector_weight, 4),
            "max_sector_name": sector_name,
            "is_safe": len(alerts) == 0,
            "alerts": alerts,
            "status": "ok" if len(alerts) == 0 else "alert",
        }

    def get_status(self) -> dict[str, Any]:
        return self.check()
