"""Live signal-decision pipeline helpers.

This module owns the live tick decision orchestration up to ExecutionGate. It
does not evaluate risk policy, size broker orders, read account state, or submit
orders.
"""

from __future__ import annotations

import time
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable


ContextPolicyEvaluator = Callable[[dict[str, Any], Any], Any]
_CALIBRATOR_CACHE: dict[str, tuple[int, int, Any]] = {}
_CALIBRATOR_CACHE_LOCK = threading.Lock()


def _load_probability_calibrator(path: str | Path) -> Any:
    from alpha.probability_calibrator import ProbabilityCalibrator

    resolved = Path(path).resolve()
    stat = resolved.stat()
    key = str(resolved)
    fingerprint = (int(stat.st_mtime_ns), int(stat.st_size))
    with _CALIBRATOR_CACHE_LOCK:
        cached = _CALIBRATOR_CACHE.get(key)
        if cached is not None and cached[:2] == fingerprint:
            return cached[2]
        calibrator = ProbabilityCalibrator.load(key)
        _CALIBRATOR_CACHE[key] = (*fingerprint, calibrator)
        return calibrator


def calibrated_signal_confidence(score: float, *, path: str | Path = "data/charts/calibrator_bucket.json") -> dict[str, Any]:
    """Convert absolute composite strength into a calibrated win probability.

    The result is advisory and bounded: downstream sizing may only reduce,
    never enlarge, exposure from this probability.
    """
    raw_probability = max(0.5, min(0.999, 0.5 + 0.5 * abs(float(score or 0.0))))
    try:
        calibrator = _load_probability_calibrator(path)
        calibrated = float(calibrator.calibrate(raw_probability))
        source = "probability_calibrator"
    except Exception:
        calibrated = raw_probability
        source = "identity_fallback"
    calibrated = max(0.0, min(1.0, calibrated))
    # Confidence is risk-reducing only.  Below 50% receives the strongest
    # haircut; high confidence never increases the original position.
    sizing_multiplier = max(0.5, min(1.0, 0.5 + calibrated * 0.5))
    return {
        "schema_version": "calibrated_signal_confidence.v1",
        "raw_probability": round(raw_probability, 6),
        "calibrated_probability": round(calibrated, 6),
        "sizing_multiplier": round(sizing_multiplier, 6),
        "source": source,
        "risk_reducing_only": True,
    }


@dataclass
class LiveDecisionFrame:
    ready: bool
    reason: str
    bar: dict[str, Any]
    factor_values: dict[str, Any] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)
    composite: Any = None
    gate_result: Any = None
    context_policy: dict[str, Any] = field(default_factory=dict)


def disabled_context_policy() -> dict[str, Any]:
    return {
        "signal_threshold_delta": 0.0,
        "position_multiplier": 1.0,
        "reason": "disabled",
        "applied": False,
    }


def _to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        try:
            return dict(value.to_dict() or {})
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


def evaluate_context_policy_for_decision(
    *,
    composite: Any,
    cfg: Any,
    evaluator: ContextPolicyEvaluator | None = None,
) -> dict[str, Any]:
    if not bool(getattr(cfg, "context_policy_enabled", True)):
        return disabled_context_policy()
    context_state = dict(getattr(composite, "context_state", {}) or {})
    if evaluator is None:
        from backend.services.context_policy import ContextPolicyService

        evaluator = lambda state, runtime_cfg: ContextPolicyService().evaluate(state, runtime_cfg)
    return _to_dict(evaluator(context_state, cfg))


def apply_context_policy_to_gate(
    *,
    composite: Any,
    gate: Any,
    cfg: Any,
    context_policy: dict[str, Any],
) -> None:
    setattr(composite, "context_policy", dict(context_policy or {}))
    base_threshold = float(getattr(cfg, "factor_signal_threshold", 0.3) or 0.3)
    try:
        threshold_delta = float((context_policy or {}).get("signal_threshold_delta") or 0.0)
    except (TypeError, ValueError):
        threshold_delta = 0.0
    gate._threshold = max(0.0, min(1.0, base_threshold + threshold_delta))


def run_live_decision_pipeline(
    *,
    engine: Any,
    normalizer: Any,
    compositor: Any,
    gate: Any,
    bar: dict[str, Any],
    cfg: Any,
    context_policy_evaluator: ContextPolicyEvaluator | None = None,
    factor_values_override: dict[str, Any] | None = None,
) -> LiveDecisionFrame:
    """Run factor -> composite -> context policy -> gate for one complete bar."""
    if factor_values_override is None:
        engine.refresh_factor_list()
        factor_values = dict(engine.append_bar(bar) or {})
        factor_engine_ready = bool(getattr(engine, "is_warm", False))
    else:
        factor_values = dict(factor_values_override or {})
        factor_engine_ready = bool(factor_values)
    if not factor_values or not factor_engine_ready:
        gate.tick()
        return LiveDecisionFrame(
            ready=False,
            reason=f"factor engine not ready (is_warm={getattr(engine, 'is_warm', False)})",
            bar=bar,
            factor_values=factor_values,
        )

    if hasattr(normalizer, "resolve_factor_values"):
        factor_values = normalizer.resolve_factor_values(factor_values)
    signals = dict(normalizer.normalize(factor_values) or {})
    composite = compositor.compose(
        signals,
        factor_values,
        timestamp=bar.get("time", time.time()),
    )
    confidence = calibrated_signal_confidence(float(getattr(composite, "score", 0.0) or 0.0))
    setattr(composite, "calibrated_confidence", confidence)
    if isinstance(getattr(composite, "context_state", None), dict):
        composite.context_state["calibrated_probability"] = confidence["calibrated_probability"]
        composite.context_state["confidence_sizing_multiplier"] = confidence["sizing_multiplier"]
    try:
        context_policy = evaluate_context_policy_for_decision(
            composite=composite,
            cfg=cfg,
            evaluator=context_policy_evaluator,
        )
        apply_context_policy_to_gate(
            composite=composite,
            gate=gate,
            cfg=cfg,
            context_policy=context_policy,
        )
    except Exception:
        context_policy = {}
        setattr(composite, "context_policy", {})
    gate_result = gate.filter(composite, factor_values, bar)
    gate.tick()
    return LiveDecisionFrame(
        ready=True,
        reason=str(getattr(gate_result, "reason", "")),
        bar=bar,
        factor_values=factor_values,
        signals=signals,
        composite=composite,
        gate_result=gate_result,
        context_policy=context_policy,
    )
