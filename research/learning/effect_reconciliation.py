"""Pure evidence evaluation for governed learning applications."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from research.learning.application_effects import classify_effect, observation_window_expired


@dataclass(frozen=True)
class EffectEvaluation:
    decision: dict[str, Any]
    status: str
    post_count: int
    baseline_count: int
    post_avg: float
    baseline_avg: float
    delta: float
    post_win_rate: float
    baseline_win_rate: float
    last_review_at: float


def evaluate_application_effect(
    *,
    app: dict[str, Any],
    scope_type: str,
    scope_key: str,
    post_reviews: list[dict[str, Any]],
    baseline_reviews: list[dict[str, Any]],
    raw_post_count: int,
    raw_baseline_count: int,
    excluded_contaminated_post: int,
    excluded_contaminated_baseline: int,
    excluded_regime_mismatch_post: int,
    excluded_regime_mismatch_baseline: int,
    target_regime: str,
    regime_evidence_available: bool,
    next_application: dict[str, Any] | None,
    observation_upper_bound: float,
    reward_from_review: Callable[[dict[str, Any]], float],
    min_trades: int,
    observe_trades: int,
    baseline_min_trades: int,
    reward_delta_for_effective: float,
    reward_delta_for_bad: float,
    max_observation_age_seconds: float,
    now: float,
) -> EffectEvaluation:
    post_reviews = post_reviews[: int(observe_trades)]
    baseline_reviews = baseline_reviews[: int(observe_trades)]
    post_rewards = [reward_from_review(item) for item in post_reviews]
    baseline_rewards = [reward_from_review(item) for item in baseline_reviews]
    post_avg = sum(post_rewards) / len(post_rewards) if post_rewards else 0.0
    baseline_avg = sum(baseline_rewards) / len(baseline_rewards) if baseline_rewards else 0.0
    delta = post_avg - baseline_avg
    post_win_rate = sum(1 for item in post_reviews if float(item.get("pnl", 0.0) or 0.0) > 0) / max(len(post_reviews), 1)
    baseline_win_rate = sum(1 for item in baseline_reviews if float(item.get("pnl", 0.0) or 0.0) > 0) / max(len(baseline_reviews), 1)
    decision = {
        "application_id": app["application_id"],
        "scope_type": scope_type,
        "scope_key": scope_key,
        "action": app["action"],
        "post_review_ids": [item["review_id"] for item in post_reviews],
        "baseline_review_ids": [item["review_id"] for item in baseline_reviews],
        "post_avg_reward": round(post_avg, 6),
        "baseline_avg_reward": round(baseline_avg, 6),
        "delta_avg_reward": round(delta, 6),
        "post_win_rate": round(post_win_rate, 4),
        "baseline_win_rate": round(baseline_win_rate, 4),
        "baseline_ready": len(baseline_reviews) >= baseline_min_trades,
        "observe_ready": len(post_reviews) >= min_trades,
        "evidence_quality": {
            "schema_version": "learning_effect_evidence.v2",
            "causal_claim_allowed": False,
            "target_regime": target_regime,
            "regime_evidence_available": regime_evidence_available,
            "regime_matched": bool(target_regime and regime_evidence_available),
            "raw_post_count": raw_post_count,
            "raw_baseline_count": raw_baseline_count,
            "excluded_contaminated_post": excluded_contaminated_post,
            "excluded_contaminated_baseline": excluded_contaminated_baseline,
            "excluded_regime_mismatch_post": excluded_regime_mismatch_post,
            "excluded_regime_mismatch_baseline": excluded_regime_mismatch_baseline,
            "concurrent_applications": [next_application] if next_application else [],
            "observation_window": {
                "start_ts": float(app.get("cycle_ts") or 0.0),
                "end_ts": observation_upper_bound if next_application else None,
                "closed_by_application_id": str((next_application or {}).get("application_id") or ""),
            },
        },
    }
    classification = classify_effect(
        post_count=len(post_reviews),
        baseline_count=len(baseline_reviews),
        min_trades=min_trades,
        baseline_min_trades=baseline_min_trades,
        delta=delta,
        effective_threshold=reward_delta_for_effective,
        ineffective_threshold=reward_delta_for_bad,
        window_closed=bool(next_application),
    )
    status = classification.status
    decision["evidence_quality"]["causal_status"] = classification.causal_status
    if classification.retry_via_new_application:
        decision["evidence_quality"]["retry_via_new_application"] = True
    cycle_ts = float(app.get("cycle_ts") or 0.0)
    observation_clock_valid = cycle_ts >= 946684800.0
    observation_age_seconds = max(0.0, now - cycle_ts) if observation_clock_valid else 0.0
    decision["evidence_quality"]["observation_age_seconds"] = observation_age_seconds
    decision["evidence_quality"]["observation_clock_valid"] = observation_clock_valid
    decision["evidence_quality"]["max_observation_age_seconds"] = max(86400.0, float(max_observation_age_seconds or 0.0))
    if observation_window_expired(
        status=status,
        cycle_ts=cycle_ts,
        now=now,
        max_age_seconds=float(max_observation_age_seconds or 0.0),
    ):
        status = "inconclusive"
        decision["evidence_quality"]["causal_status"] = "observation_window_expired_inconclusive"
        decision["evidence_quality"]["retry_via_new_application"] = True
    decision["evidence_quality"]["bounded_attribution_allowed"] = bool(
        len(post_reviews) >= min_trades
        and len(baseline_reviews) >= baseline_min_trades
        and target_regime
        and regime_evidence_available
    )
    return EffectEvaluation(
        decision=decision,
        status=status,
        post_count=len(post_reviews),
        baseline_count=len(baseline_reviews),
        post_avg=post_avg,
        baseline_avg=baseline_avg,
        delta=delta,
        post_win_rate=post_win_rate,
        baseline_win_rate=baseline_win_rate,
        last_review_at=max((float(item.get("created_at", 0.0) or 0.0) for item in post_reviews), default=0.0),
    )
