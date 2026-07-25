"""Pure concentration calculation for position or alpha exposure."""
from __future__ import annotations

import math
from typing import Any


class ConcentrationChecker:
    def __init__(self, max_single_weight: float = 0.40):
        self.max_single_weight = float(max_single_weight)

    def check(
        self,
        weights: dict[str, float] | None,
        *,
        factor_roles: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if weights is None:
            return self.get_status()
        if not weights:
            return {
                "status": "known",
                "max_single_name": "",
                "concentration_fraction": 0.0,
                "concentration_pct": 0.0,
                "is_safe": True,
                "sample_count": 0,
            }
        roles = factor_roles or {}
        values: dict[str, float] = {}
        for name, raw in weights.items():
            if roles and str(roles.get(name) or "alpha").lower() != "alpha":
                continue
            try:
                value = abs(float(raw))
            except (TypeError, ValueError, OverflowError):
                return self.get_status("invalid_weight")
            if not math.isfinite(value):
                return self.get_status("invalid_weight")
            if value > 0:
                values[str(name)] = value
        total = sum(values.values())
        if total <= 0:
            return self.get_status("zero_exposure")
        name, exposure = max(values.items(), key=lambda item: item[1])
        fraction = exposure / total
        return {
            "status": "known",
            "max_single_name": name,
            "concentration_fraction": round(fraction, 8),
            "concentration_pct": round(fraction * 100.0, 6),
            "is_safe": fraction <= self.max_single_weight,
            "sample_count": len(values),
        }

    @staticmethod
    def get_status(reason: str = "missing_inputs") -> dict[str, Any]:
        return {
            "status": "unknown",
            "reason": reason,
            "max_single_name": "",
            "concentration_fraction": None,
            "concentration_pct": None,
            "is_safe": False,
            "sample_count": 0,
        }
