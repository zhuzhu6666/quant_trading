from types import SimpleNamespace

from backend.services.live_decision_pipeline import (
    build_signal_decision_log_payload,
    run_live_decision_pipeline,
)


class _Engine:
    def __init__(self, *, factor_values=None, is_warm=True):
        self.factor_values = factor_values or {}
        self.is_warm = is_warm
        self.refresh_count = 0

    def refresh_factor_list(self):
        self.refresh_count += 1

    def append_bar(self, bar):
        return dict(self.factor_values)


class _Normalizer:
    def normalize(self, factor_values):
        return {key: float(value) for key, value in factor_values.items()}


class _FallbackNormalizer(_Normalizer):
    def resolve_factor_values(self, factor_values):
        resolved = dict(factor_values)
        if resolved.get("dxy_corr_20") is None:
            resolved["dxy_corr_20"] = -0.42
        return resolved


class _Compositor:
    def compose(self, signals, factor_values, timestamp=None):
        return SimpleNamespace(
            direction=1,
            score=0.72,
            tactical_score=0.7,
            macro_score=0.1,
            n_active_factors=len(signals),
            n_abstain_factors=0,
            context_state={"volatility_state": "high"},
            timestamp=timestamp,
        )


class _Gate:
    def __init__(self):
        self._threshold = 0.3
        self.tick_count = 0
        self.filter_calls = []

    def filter(self, composite, factor_values, bar):
        self.filter_calls.append((composite, factor_values, bar, self._threshold))
        return SimpleNamespace(passed=True, reason="passed")

    def tick(self):
        self.tick_count += 1


def test_live_decision_pipeline_not_ready_ticks_gate_without_filtering():
    engine = _Engine(factor_values={}, is_warm=False)
    gate = _Gate()

    frame = run_live_decision_pipeline(
        engine=engine,
        normalizer=_Normalizer(),
        compositor=_Compositor(),
        gate=gate,
        bar={"time": 1000.0},
        cfg=SimpleNamespace(),
    )

    assert frame.ready is False
    assert "factor engine not ready" in frame.reason
    assert engine.refresh_count == 1
    assert gate.tick_count == 1
    assert gate.filter_calls == []


def test_live_decision_pipeline_applies_context_policy_before_gate():
    gate = _Gate()

    frame = run_live_decision_pipeline(
        engine=_Engine(factor_values={"rsi_14": 0.72}, is_warm=True),
        normalizer=_Normalizer(),
        compositor=_Compositor(),
        gate=gate,
        bar={"time": 1000.0},
        cfg=SimpleNamespace(context_policy_enabled=True, factor_signal_threshold=0.4),
        context_policy_evaluator=lambda state, cfg: {
            "signal_threshold_delta": 0.15,
            "position_multiplier": 0.8,
            "reason": "high_volatility",
            "applied": True,
        },
    )

    assert frame.ready is True
    assert frame.signals == {"rsi_14": 0.72}
    assert frame.context_policy["reason"] == "high_volatility"
    assert frame.composite.context_policy["position_multiplier"] == 0.8
    assert frame.composite.calibrated_confidence["risk_reducing_only"] is True
    assert frame.composite.context_state["calibrated_probability"] >= 0.0
    assert frame.composite.context_state["confidence_sizing_multiplier"] <= 1.0
    assert gate.filter_calls[0][3] == 0.55
    assert gate.tick_count == 1


def test_live_decision_pipeline_applies_daily_raw_fallback_before_scoring():
    gate = _Gate()
    frame = run_live_decision_pipeline(
        engine=_Engine(factor_values={"dxy_corr_20": None}, is_warm=True),
        normalizer=_FallbackNormalizer(),
        compositor=_Compositor(),
        gate=gate,
        bar={"time": 1000.0},
        cfg=SimpleNamespace(context_policy_enabled=False),
    )

    assert frame.factor_values == {"dxy_corr_20": -0.42}
    assert frame.signals == {"dxy_corr_20": -0.42}
    assert gate.filter_calls[0][1] == {"dxy_corr_20": -0.42}


def test_signal_decision_log_payload_matches_legacy_shape():
    composite = SimpleNamespace(
        direction=1,
        score=0.72,
        tactical_score=0.7,
        macro_score=0.1,
        n_active_factors=3,
        n_available_factors=3,
        n_scoring_factors=2,
        n_contributing_factors=1,
        n_abstain_factors=1,
    )
    gate_result = SimpleNamespace(passed=True, reason="passed")

    payload = build_signal_decision_log_payload(
        bar={"time": 1783209600.0},
        composite=composite,
        gate_result=gate_result,
        tick=9,
    )

    assert payload == {
        "ts": 1783209600.0,
        "bar_date": "2026-07-05",
        "decision_type": "signal",
        "strategy": "factor_v4",
        "direction": 1,
        "confidence": 0.72,
        "decision": "execute",
        "meta": {
            "gate_reason": "passed",
            "tick": 9,
            "tactical_score": 0.7,
            "macro_score": 0.1,
            "n_active": 3,
            "n_available": 3,
            "n_scoring": 2,
            "n_contributing": 1,
            "n_abstain": 1,
        },
    }
    assert build_signal_decision_log_payload(
        bar={"time": 0},
        composite=composite,
        gate_result=gate_result,
        tick=9,
    ) is None


def test_signal_decision_log_payload_keeps_factor_facts_when_gate_blocks():
    composite = SimpleNamespace(
        direction=0,
        score=0.0,
        tactical_score=-0.011258,
        macro_score=-0.577005,
        n_active_factors=17,
        n_available_factors=17,
        n_scoring_factors=8,
        n_contributing_factors=6,
        n_abstain_factors=1,
    )
    gate_result = SimpleNamespace(passed=False, reason="signal_below_threshold")

    payload = build_signal_decision_log_payload(
        bar={"time": 1785256800.0},
        composite=composite,
        gate_result=gate_result,
        tick=2064,
    )

    assert payload["decision"] == "hold"
    assert payload["direction"] == 0
    assert payload["meta"]["tactical_score"] == -0.011258
    assert payload["meta"]["macro_score"] == -0.577005
    assert payload["meta"]["n_active"] == 17
    assert payload["meta"]["n_abstain"] == 1
