from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EffectClassification:
    status: str
    causal_status: str
    retry_via_new_application: bool = False


def classify_effect(
    *,
    post_count: int,
    baseline_count: int,
    min_trades: int,
    baseline_min_trades: int,
    delta: float,
    effective_threshold: float,
    ineffective_threshold: float,
    window_closed: bool,
) -> EffectClassification:
    """Classify one isolated application window without mutating state."""
    bounded = "bounded_" if window_closed else ""
    if post_count < min_trades or baseline_count < baseline_min_trades:
        if window_closed:
            return EffectClassification(
                "inconclusive",
                "bounded_window_insufficient_samples",
                retry_via_new_application=True,
            )
        return EffectClassification("observing", "insufficient_comparable_samples")
    if delta >= effective_threshold:
        return EffectClassification("effective", f"{bounded}comparative_effective")
    if delta <= ineffective_threshold:
        return EffectClassification("ineffective", f"{bounded}comparative_ineffective")
    return EffectClassification("mixed", f"{bounded}comparative_mixed")


def observation_window_expired(
    *,
    status: str,
    cycle_ts: float,
    now: float,
    max_age_seconds: float,
) -> bool:
    if status not in {"observing", "mixed"} or cycle_ts < 946684800.0:
        return False
    return max(0.0, now - cycle_ts) >= max(86400.0, max_age_seconds)
