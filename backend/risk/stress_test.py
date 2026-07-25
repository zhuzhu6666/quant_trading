"""Position-based portfolio stress calculation."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _direction(position: Mapping[str, Any]) -> int:
    value = position.get("direction", position.get("side"))
    if isinstance(value, (int, float)):
        return 1 if value > 0 else -1 if value < 0 else 0
    text = str(value or "").strip().lower()
    if text in {"buy", "long", "bull"}:
        return 1
    if text in {"sell", "short", "bear"}:
        return -1
    return 0


def _notional(position: Mapping[str, Any]) -> float | None:
    explicit = _number(position.get("notional_usd"))
    if explicit is not None:
        return explicit if explicit >= 0 else None
    price = _number(position.get("current_price"))
    lots = _number(position.get("volume_lots"))
    contract = _number(position.get("contract_size"))
    if None in {price, lots, contract}:
        return None
    result = float(price) * float(lots) * float(contract)
    return result if result >= 0 else None


class StressTest:
    """Apply configured two-sided price shocks to fresh positions."""

    def run(
        self,
        positions: Sequence[Mapping[str, Any]] | None,
        account: Mapping[str, Any] | None,
        shocks: Sequence[float] = (-0.05, 0.05),
    ) -> dict[str, Any]:
        equity = _number((account or {}).get("equity"))
        if positions is None or equity is None or equity <= 0:
            return self.get_status()

        normalized: list[tuple[int, float]] = []
        for position in positions:
            direction = _direction(position)
            notional = _notional(position)
            if direction == 0 or notional is None:
                return self.get_status(reason="invalid_position_input")
            normalized.append((direction, notional))

        normalized_shocks = [_number(value) for value in shocks]
        if (
            not normalized_shocks
            or any(value is None for value in normalized_shocks)
            or min(normalized_shocks) >= 0
            or max(normalized_shocks) <= 0
        ):
            return self.get_status(reason="invalid_shocks")
        scenarios = []
        for shock in normalized_shocks:
            pnl = sum(direction * notional * shock for direction, notional in normalized)
            loss = max(0.0, -pnl)
            scenarios.append(
                {
                    "shock_fraction": shock,
                    "pnl_usd": round(pnl, 2),
                    "loss_usd": round(loss, 2),
                    "loss_pct": round(loss / equity * 100.0, 6),
                }
            )

        worst = max(scenarios, key=lambda item: item["loss_usd"], default=None)
        return {
            "status": "known",
            "distinct_position_count": len(normalized),
            "stress_loss_usd": worst["loss_usd"] if worst else 0.0,
            "stress_loss_pct": worst["loss_pct"] if worst else 0.0,
            "scenarios": scenarios,
        }

    @staticmethod
    def get_status(reason: str = "missing_inputs") -> dict[str, Any]:
        return {
            "status": "unknown",
            "reason": reason,
            "distinct_position_count": 0,
            "stress_loss_usd": None,
            "stress_loss_pct": None,
            "scenarios": [],
        }
