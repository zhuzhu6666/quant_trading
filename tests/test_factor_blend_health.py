from types import SimpleNamespace

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.factor_blend_health import FactorBlendHealthService


def _cfg(signal_cfg, weights):
    return SimpleNamespace(
        factor_signal_config=signal_cfg,
        factor_portfolio_weights=weights,
        factor_redundancy_max_group_weight=0.35,
    )


def test_factor_blend_health_flags_large_noisy_alpha_population(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO factor_health (factor, score, status, n_obs, rolling_ic)
            VALUES ('dsl_auto_000', 25.0, 'WATCH', 120, -0.01)
            """
        )
        conn.commit()
    finally:
        conn.close()

    signal_cfg = {
        "rsi_14": {"role": "alpha", "tags": ["技术"], "redundancy_group": "osc"},
        "stoch_k": {"role": "alpha", "tags": ["技术"], "redundancy_group": "osc"},
    }
    weights = {"rsi_14": 1.0, "stoch_k": 1.0}
    for idx in range(130):
        name = f"dsl_auto_{idx:03d}"
        signal_cfg[name] = {"role": "alpha", "tags": ["GP发现"], "source": "discovered"}
        weights[name] = 0.01
    for idx in range(45):
        name = f"pca_{idx}"
        signal_cfg[name] = {"role": "alpha", "tags": ["PCA"], "source": "discovered"}
        weights[name] = 0.01

    result = FactorBlendHealthService(db_path).build(_cfg(signal_cfg, weights))

    assert result["schema_version"] == "factor_blend_health.v1"
    assert result["status"] == "critical"
    assert result["ok"] is False
    assert result["directional_portfolio_guard"]["status"] == "unavailable"
    assert result["active_alpha_count"] == 177
    codes = {item["code"] for item in result["issues"]}
    assert "too_many_active_alpha_factors" in codes
    assert "large_dsl_auto_population" in codes
    assert "large_pca_population" in codes
    assert "weak_health_active_alpha_factors" in codes
    assert result["noise_family_counts"] == {"dsl_auto": 130, "pca": 45}
    assert result["weak_active_health"]["weak_factors"][0]["factor"] == "dsl_auto_000"
    assert result["boundary"]["read_only"] is True


def test_factor_blend_health_keeps_context_out_of_active_alpha(tmp_path):
    signal_cfg = {
        "rsi_14": {"role": "alpha", "tags": ["技术"]},
        "bb_width": {"role": "context", "tags": ["技术", "波动率"]},
    }
    weights = {"rsi_14": 1.0, "bb_width": 10.0}

    result = FactorBlendHealthService(tmp_path / "state.db").build(_cfg(signal_cfg, weights))

    assert result["status"] == "critical"
    assert result["directional_portfolio_guard"]["reason_codes"] == [
        "directional_portfolio_evidence_unavailable"
    ]
    assert result["configured_alpha_count"] == 1
    assert result["active_alpha_count"] == 1
    assert result["family_exposure"]["core"]["count"] == 1


def test_factor_blend_health_current_uses_catalog_used_in_score(tmp_path, monkeypatch):
    signal_cfg = {
        "live_alpha": {"role": "alpha", "tags": ["live"]},
        "cold_tail": {"role": "alpha", "tags": ["cold"]},
    }
    weights = {"live_alpha": 0.4, "cold_tail": 0.01}

    def _catalog(_db_path):
        return [
            {
                "factor_id": "live_alpha",
                "used_in_score": True,
                "source": "builtin",
                "weight": 0.4,
                "redundancy_group": "",
            },
            {
                "factor_id": "cold_tail",
                "used_in_score": False,
                "source": "unknown",
                "weight": 0.01,
                "redundancy_group": "",
            },
        ]

    monkeypatch.setattr("backend.services.factor_catalog.build_factor_catalog", _catalog)

    result = FactorBlendHealthService(tmp_path / "state.db").build_current(_cfg(signal_cfg, weights))

    assert result["active_count_source"] == "factor_catalog.used_in_score"
    assert result["configured_alpha_count"] == 2
    assert result["active_alpha_count"] == 1
    assert result["family_exposure"]["core"]["sample"] == ["live_alpha"]
    assert result["directional_portfolio_guard"]["voter_count"] == 1


def test_directional_portfolio_guard_requires_three_voters_and_two_groups():
    configs = {
        "alpha_a": {"role": "alpha", "enabled": True, "lifecycle_status": "ACTIVE", "redundancy_group": "trend"},
        "alpha_b": {"role": "alpha", "enabled": True, "lifecycle_status": "ACTIVE", "redundancy_group": "trend"},
        "alpha_c": {"role": "alpha", "enabled": True, "lifecycle_status": "ACTIVE", "redundancy_group": "trend"},
    }
    same_group = FactorBlendHealthService.evaluate_directional_portfolio_guard(
        selected_factor_ids=list(configs),
        factor_configs=configs,
        weights={name: 0.1 for name in configs},
    )
    assert same_group["voter_count"] == 3
    assert same_group["independent_group_count"] == 1
    assert same_group["reason_codes"] == ["insufficient_directional_alpha_groups"]

    configs["alpha_c"]["redundancy_group"] = "reversal"
    healthy = FactorBlendHealthService.evaluate_directional_portfolio_guard(
        selected_factor_ids=list(configs),
        factor_configs=configs,
        weights={name: 0.1 for name in configs},
    )
    assert healthy["status"] == "healthy"


def test_directional_portfolio_guard_treats_ungrouped_factors_as_independent():
    configs = {
        name: {"role": "alpha", "enabled": True, "lifecycle_status": "ACTIVE"}
        for name in ("alpha_a", "alpha_b", "alpha_c")
    }
    result = FactorBlendHealthService.evaluate_directional_portfolio_guard(
        selected_factor_ids=list(configs),
        factor_configs=configs,
        weights={name: 0.1 for name in configs},
    )
    assert result["status"] == "healthy"
    assert result["independent_group_keys"] == [
        "factor:alpha_a",
        "factor:alpha_b",
        "factor:alpha_c",
    ]
