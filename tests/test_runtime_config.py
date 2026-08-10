"""test_runtime_config — RuntimeConfig 单例、订阅者、version 单调性。"""
from __future__ import annotations

import pytest

from backend.runtime.runtime_state import RuntimeState
from backend.services.evolution_ledger import persist_runtime_config_snapshot
from config import runtime_config as rc


@pytest.fixture(autouse=True)
def _reset():
    rc.reset_for_tests()
    RuntimeState.reset_singleton()
    yield
    rc.reset_for_tests()
    RuntimeState.reset_singleton()


def test_initial_version_is_zero() -> None:
    assert rc.version() == 0


def test_expansion_freeze_only_applies_outside_demo_modes() -> None:
    assert rc.bounded_demo_mode_active(
        rc.RuntimeConfig(autonomy_mode="demo_nursery")
    ) is True
    assert rc.bounded_demo_mode_active(
        rc.RuntimeConfig(autonomy_mode="demo_autonomous")
    ) is True
    assert rc.bounded_demo_mode_active(
        rc.RuntimeConfig(autonomy_mode="live_candidate")
    ) is False
    assert rc.autonomy_expansion_freeze_applies(
        rc.RuntimeConfig(autonomy_mode="demo_nursery", autonomy_expansion_frozen=True)
    ) is False
    assert rc.autonomy_expansion_freeze_applies(
        rc.RuntimeConfig(autonomy_mode="demo_autonomous", autonomy_expansion_frozen=True)
    ) is False
    assert rc.autonomy_expansion_freeze_applies(
        rc.RuntimeConfig(autonomy_mode="live_candidate", autonomy_expansion_frozen=True)
    ) is True
    assert rc.autonomy_expansion_freeze_applies(
        rc.RuntimeConfig(autonomy_mode="live_candidate", autonomy_expansion_frozen=False)
    ) is False


def test_global_governance_pause_applies_to_demo_and_defaults_off() -> None:
    assert rc.RuntimeConfig().governance_expansion_paused is False
    assert rc.autonomy_expansion_freeze_applies(
        rc.RuntimeConfig(
            autonomy_mode="demo_autonomous",
            autonomy_expansion_frozen=False,
            governance_expansion_paused=True,
        )
    ) is True


def test_demo_mode_cannot_bypass_freeze_on_effective_live_broker(monkeypatch) -> None:
    from execution.broker_config import reset_broker_connection_config_for_tests

    monkeypatch.setenv("CTRADER_HOST", "live.ctraderapi.com")
    reset_broker_connection_config_for_tests()
    try:
        assert rc.bounded_demo_mode_active(
            rc.RuntimeConfig(autonomy_mode="demo_autonomous")
        ) is False
        assert rc.autonomy_expansion_freeze_applies(
            rc.RuntimeConfig(autonomy_mode="demo_autonomous", autonomy_expansion_frozen=True)
        ) is True
    finally:
        reset_broker_connection_config_for_tests()


def test_bounded_demo_mode_resolution_is_pure_and_no_arg_read_does_not_refresh(
    monkeypatch,
) -> None:
    class _Broker:
        is_demo = True

    cfg = rc.RuntimeConfig(autonomy_mode="demo_autonomous")
    assert rc.resolve_bounded_demo_mode(cfg, _Broker()) is True
    rc.replace(cfg)
    monkeypatch.setattr(
        rc,
        "refresh_from_overlay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("read-only mode resolution must not refresh overlay")
        ),
    )
    assert rc.bounded_demo_mode_active() is True


def test_operator_bounded_demo_control_exemption_is_narrow(monkeypatch) -> None:
    monkeypatch.setattr(rc, "bounded_demo_mode_active", lambda _cfg: True)
    cfg = rc.RuntimeConfig(
        autonomy_mode="demo_autonomous",
        risk_var_threshold_pct=10.0,
    )

    assert rc.operator_bounded_demo_control_exempt(
        actor="operator:pytest",
        patch={"runtime_incident_mode": "normal"},
        cfg=cfg,
    )
    assert rc.operator_bounded_demo_control_exempt(
        actor="operator:pytest",
        patch={"risk_cvar_threshold_pct": 2.5},
        cfg=cfg,
    )
    assert not rc.operator_bounded_demo_control_exempt(
        actor="operator:pytest",
        patch={"risk_cvar_threshold_pct": 11.0},
        cfg=cfg,
    )
    assert not rc.operator_bounded_demo_control_exempt(
        actor="system:pytest",
        patch={"runtime_incident_mode": "normal"},
        cfg=cfg,
    )
    assert not rc.operator_bounded_demo_control_exempt(
        actor="operator:pytest",
        patch={"max_risk_per_trade": 0.02},
        cfg=cfg,
    )


def test_replace_increments_version() -> None:
    v1 = rc.replace(rc.RuntimeConfig(shadow_vote_weight=0.1))
    assert v1 == 1
    v2 = rc.replace(rc.RuntimeConfig(shadow_vote_weight=0.2))
    assert v2 == 2
    assert rc.version() == 2


def test_patch_overrides_specific_fields() -> None:
    rc.replace(rc.RuntimeConfig())
    v = rc.patch({"shadow_vote_weight": 0.7, "shadow_top_k": 9})
    assert v >= 1
    cfg = rc.shared()
    assert cfg.shadow_vote_weight == 0.7
    assert cfg.shadow_top_k == 9


def test_subscribers_receive_each_version() -> None:
    received: list[tuple[float, int]] = []
    rc.subscribe(lambda c, v: received.append((c.shadow_vote_weight, v)))
    rc.replace(rc.RuntimeConfig(shadow_vote_weight=0.11))
    rc.replace(rc.RuntimeConfig(shadow_vote_weight=0.22))
    rc.replace(rc.RuntimeConfig(shadow_vote_weight=0.33))
    assert [w for w, _ in received] == [0.11, 0.22, 0.33]
    assert [v for _, v in received] == [1, 2, 3]


def test_subscriber_exception_does_not_break_replace() -> None:
    def bad(_cfg, _v):
        raise RuntimeError("boom")

    rc.subscribe(bad)
    # 不应抛
    v = rc.replace(rc.RuntimeConfig(shadow_vote_weight=0.5))
    assert v >= 1


def test_from_yaml_reads_runtime_section() -> None:
    yaml_cfg = {"runtime": {"shadow_vote_weight": 0.42, "canary_min_oos_bars": 100}}
    cfg = rc.RuntimeConfig.from_yaml(yaml_cfg)
    assert cfg.shadow_vote_weight == 0.42
    assert cfg.canary_min_oos_bars == 100


def test_from_yaml_inherits_ctrader_send_orders_from_top_level() -> None:
    yaml_cfg = {"ctrader": {"send_orders": False}, "runtime": {}}
    cfg = rc.RuntimeConfig.from_yaml(yaml_cfg)
    assert cfg.ctrader_send_orders is False


def test_from_yaml_uses_defaults_for_missing_keys() -> None:
    cfg = rc.RuntimeConfig.from_yaml({})
    assert cfg.shadow_vote_weight == 0.15  # 默认值
    assert cfg.canary_min_oos_bars == 80
    assert cfg.ctrader_send_orders is False
    assert cfg.dynamic_sizing_enabled is True
    assert cfg.dynamic_sizing_max_api_volume == 1000.0
    assert cfg.kelly_risk_per_trade_pct == 0.05
    assert cfg.kelly_min_closed_trades == 20
    assert cfg.kelly_canary_max_api_volume == 100.0
    assert cfg.risk_max_daily_loss_pct == 10.0
    assert cfg.risk_max_drawdown_pct == 16.0
    assert cfg.risk_max_daily_trades == 30
    assert cfg.demo_learning_max_daily_trades == 30


def test_macro_factor_defaults_preserve_directional_semantics() -> None:
    cfg = rc.RuntimeConfig.from_yaml({})

    assert cfg.factor_signal_config["dxy_corr_20"]["role"] == "context"
    assert cfg.factor_portfolio_weights["dxy_corr_20"] == 0.0
    assert cfg.factor_signal_config["slv_gld_ratio"]["direction"] == -1


def test_classic_builtin_baseline_has_directional_coverage_and_roles() -> None:
    cfg = rc.RuntimeConfig.from_yaml({})
    classic = {
        "rsi_14",
        "di_spread",
        "stoch_k",
        "ema_slope",
        "supertrend_str",
        "macd_hist",
        "obv_slope",
        "engulfing",
        "pin_bar",
    }

    assert classic <= set(cfg.factor_signal_config)
    assert all(
        cfg.factor_signal_config[name]["health_gate_exempt"] is True
        for name in classic
    )
    assert all(
        cfg.factor_signal_config[name]["role"] == "alpha"
        for name in classic
    )
    assert {
        cfg.factor_signal_config[name]["redundancy_group"] for name in classic
    } == {"trend", "oscillator", "momentum", "volume_direction", "price_action"}
    assert cfg.factor_signal_config["rsi_14"]["direction"] == -1
    assert all(
        cfg.factor_signal_config[name]["direction"] == 1
        for name in classic - {"rsi_14"}
    )
    assert cfg.factor_signal_config["vol_ma_ratio"]["role"] == "context"
    assert cfg.factor_signal_config["inside_bar"]["role"] == "context"
    assert cfg.factor_portfolio_weights["inside_bar"] == 0.0


def test_unknown_keys_go_to_extra() -> None:
    cfg = rc.RuntimeConfig.from_dict({"shadow_vote_weight": 0.3, "made_up_field": 999})
    assert cfg.shadow_vote_weight == 0.3
    assert cfg.extra.get("made_up_field") == 999


def test_promoted_runtime_field_rehydrates_legacy_extra_and_keeps_snapshot_hash(
    tmp_path,
) -> None:
    key = "factor_governance_model_min_factor_samples"
    current = rc.RuntimeConfig(**{key: 37}).to_dict()
    legacy = dict(current)
    legacy_extra = dict(legacy.get("extra") or {})
    legacy_extra[key] = legacy.pop(key)
    legacy["extra"] = legacy_extra

    restored = rc.RuntimeConfig.from_dict(legacy)
    assert getattr(restored, key) == 37
    assert key not in restored.extra
    assert rc.canonical_runtime_config_payload(current) == (
        rc.canonical_runtime_config_payload(legacy)
    )

    first = persist_runtime_config_snapshot(
        current,
        source="legacy_alias_compat",
        db_path=tmp_path / "state.db",
    )
    second = persist_runtime_config_snapshot(
        legacy,
        source="legacy_alias_compat",
        db_path=tmp_path / "state.db",
    )
    assert second["config_hash"] == first["config_hash"]
    assert second["config_version"] == first["config_version"]
    assert second["reused"] is True


def test_promoted_runtime_field_conflict_fails_closed() -> None:
    with pytest.raises(ValueError, match="runtime_config_legacy_alias_conflict"):
        rc.RuntimeConfig.from_dict(
            {
                "factor_governance_model_min_factor_samples": 37,
                "extra": {
                    "factor_governance_model_min_factor_samples": 20,
                },
            }
        )


def test_invalid_incident_mode_is_rejected_while_loading() -> None:
    with pytest.raises(ValueError, match="invalid_runtime_incident_mode"):
        rc.RuntimeConfig.from_yaml({"runtime": {"runtime_incident_mode": "unsafe_typo"}})


def test_runtime_config_snapshot_hash_stable_and_reuses_identical_event(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    cfg = rc.RuntimeConfig(shadow_vote_weight=0.33)

    first = persist_runtime_config_snapshot(cfg, source="test", db_path=db_path)
    second = persist_runtime_config_snapshot(cfg, source="test", db_path=db_path)

    assert first["config_hash"] == second["config_hash"]
    assert second["config_version"] == first["config_version"]
    assert second["reused"] is True

    third = persist_runtime_config_snapshot(cfg, source="different_event", db_path=db_path)
    assert third["config_version"] == first["config_version"]
    assert third["reused"] is True
    assert third["requested_source"] == "different_event"

    changed = rc.RuntimeConfig(shadow_vote_weight=0.34)
    fourth = persist_runtime_config_snapshot(
        changed,
        source="real_config_change",
        db_path=db_path,
    )
    assert fourth["config_version"] == first["config_version"] + 1
    assert fourth["reused"] is False


def test_runtime_mutation_refreshes_yaml_base_before_overlay_snapshot(monkeypatch, tmp_path) -> None:
    from backend.services import runtime_config_mutation, runtime_config_startup
    from backend.services.runtime_config_mutation import RuntimeConfigMutationService

    yaml_base = rc.RuntimeConfig(ctrader_send_orders=True)
    rc.replace(rc.RuntimeConfig(ctrader_send_orders=False))
    monkeypatch.setattr(runtime_config_mutation, "is_state_db_path", lambda _path: True)
    monkeypatch.setattr(runtime_config_startup, "load_yaml_runtime_config", lambda: (yaml_base, {}))

    result = RuntimeConfigMutationService(tmp_path / "state.db").apply_patch(
        {"position_supervisor_template_id": "position_supervisor:test.v1"},
        source="test_refresh_yaml_base",
        run_id="unit_refresh_yaml_base",
        audit=False,
    )

    assert result["ok"] is True
    assert rc.shared().ctrader_send_orders is True
    assert result["snapshot"]["config_version"] > 0


def test_autonomous_mutation_cannot_change_operator_pause(tmp_path) -> None:
    from backend.services.runtime_config_mutation import RuntimeConfigMutationService

    result = RuntimeConfigMutationService(tmp_path / "state.db").apply_patch(
        {"governance_expansion_paused": True},
        source="factor_governance",
        actor="system:factor_governance",
        action="pause_governance",
        audit=False,
    )

    assert result["ok"] is False
    assert result["status"] == "operator_governance_pause_required"


def test_operator_pause_blocks_expansion_but_allows_explicit_tightening(tmp_path) -> None:
    from backend.services.runtime_config_mutation import RuntimeConfigMutationService

    service = RuntimeConfigMutationService(tmp_path / "state.db")
    paused = service.apply_patch(
        {"governance_expansion_paused": True},
        source="operator_governance_pause",
        actor="operator:test",
        action="pause_governance_expansion",
        audit=False,
    )
    assert paused["ok"] is True
    assert rc.shared().governance_expansion_paused is True

    expansion = service.apply_patch(
        {"factor_portfolio_weights": {"new_alpha": 0.1}},
        source="factor_governance_promote",
        actor="system:factor_governance",
        action="promote_factor",
        audit=False,
    )
    assert expansion["ok"] is False
    assert expansion["status"] == "blocked_governance_expansion_paused"

    tightening = service.apply_patch(
        {"factor_portfolio_weights": {"new_alpha": 0.0}},
        source="factor_governance_downweight",
        actor="system:factor_governance",
        action="downweight_factor",
        risk_reduction=True,
        audit=False,
    )
    assert tightening["ok"] is True


@pytest.mark.parametrize(
    ("flag", "value"),
    (
        ("live_safety_plane_v2_mode", "enforce"),
        ("live_generation_controller_v2_enabled", True),
        ("ctrader_execution_outcome_v2_enabled", True),
        ("governance_mutation_coordinator_v2_mode", "enforce"),
        ("pg_job_queue_v2_enabled", True),
    ),
)
def test_autonomous_mutation_cannot_change_static_release_flags(
    tmp_path,
    flag: str,
    value,
) -> None:
    from backend.services.runtime_config_mutation import RuntimeConfigMutationService

    result = RuntimeConfigMutationService(tmp_path / "state.db").apply_patch(
        {flag: value},
        source="factor_governance",
        actor="system:factor_governance",
        audit=False,
    )

    assert result["ok"] is False
    assert result["status"] == "static_feature_flag_mutation_forbidden"
    assert result["forbidden_keys"] == [flag]
