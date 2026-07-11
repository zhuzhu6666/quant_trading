"""Context-only policy for threshold and sizing adjustments."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ContextPolicyResult:
    signal_threshold_delta: float = 0.0
    position_multiplier: float = 1.0
    reason: str = "neutral"
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ContextPolicyService:
    def evaluate(self, context_state: dict[str, Any] | None, cfg: Any = None) -> ContextPolicyResult:
        state = dict(context_state or {})
        delta = 0.0
        multiplier = 1.0
        reasons: list[str] = []

        if state.get("event_window_state") == "active":
            delta += 0.08
            multiplier *= 0.5
            reasons.append("event_window_active")
        elif state.get("event_window_state") == "near":
            delta += 0.04
            multiplier *= 0.75
            reasons.append("event_window_near")

        if state.get("volatility_state") == "high":
            delta += 0.05
            multiplier *= 0.75
            reasons.append("high_volatility")
        elif state.get("volatility_state") == "low":
            delta += 0.02
            multiplier *= 0.9
            reasons.append("low_volatility")

        if state.get("trend_strength_state") == "strong" and state.get("event_window_state") not in {"active", "near"}:
            delta -= 0.03
            reasons.append("strong_trend")

        if state.get("session_state") == "rollover":
            multiplier *= 0.5
            reasons.append("rollover_session")
        elif state.get("session_state") == "asia":
            multiplier *= 0.85
            reasons.append("asia_session")

        macro_evidence_count = int(state.get("macro_evidence_count") or 0)
        macro_context_score = float(state.get("macro_context_score") or 0.0)
        if macro_evidence_count >= 2 and abs(macro_context_score) >= 0.45:
            delta += 0.03
            multiplier *= 0.85
            reasons.append("macro_context_extreme")

        calibrated_probability = float(state.get("calibrated_probability") or 0.0)
        confidence_multiplier = float(state.get("confidence_sizing_multiplier") or 1.0)
        if calibrated_probability > 0.0:
            multiplier *= max(0.5, min(1.0, confidence_multiplier))
            if calibrated_probability < 0.55:
                delta += 0.04
                reasons.append("low_calibrated_confidence")
            elif confidence_multiplier < 0.999:
                reasons.append("calibrated_confidence_haircut")

        multiplier = max(0.5, min(1.25, multiplier))
        delta = max(-0.05, min(0.15, delta))
        return ContextPolicyResult(
            signal_threshold_delta=round(delta, 6),
            position_multiplier=round(multiplier, 6),
            reason=";".join(reasons) if reasons else "neutral",
            applied=bool(reasons),
        )
