from alpha.decision_policy import DecisionPolicy


def test_decision_policy_only_adjusts_live_alpha_factors():
    dp = DecisionPolicy()
    decisions = dp.fast_decide(
        awe_patches={
            "trend": {"weight": 0.4, "reason": "ok"},
            "bb_width": {"weight": 0.4, "reason": "context"},
            "dead_factor": {"weight": 0.4, "reason": "dead"},
        },
        factor_configs={
            "trend": {"role": "alpha", "enabled": True},
            "bb_width": {"role": "context", "enabled": True},
            "dead_factor": {"role": "alpha", "enabled": True, "lifecycle_status": "DEAD"},
        },
        current_weights={"trend": 0.3, "bb_width": 0.0, "dead_factor": 0.3},
    )

    assert set(decisions) == {"trend"}
    assert decisions["trend"].new_weight == 0.4


def test_decision_policy_caps_redundancy_group_weight():
    dp = DecisionPolicy(redundancy_max_group_weight=0.5)
    decisions = dp.fast_decide(
        awe_patches={
            "a": {"weight": 0.4, "reason": "candidate"},
            "b": {"weight": 0.4, "reason": "candidate"},
        },
        factor_configs={
            "a": {"role": "alpha", "enabled": True, "redundancy_group": "trend_family"},
            "b": {"role": "alpha", "enabled": True, "redundancy_group": "trend_family"},
        },
        current_weights={"a": 0.3, "b": 0.3},
    )

    assert round(sum(d.new_weight for d in decisions.values()), 6) <= 0.5
    assert all("redundancy(trend_family)" in d.reason for d in decisions.values())
