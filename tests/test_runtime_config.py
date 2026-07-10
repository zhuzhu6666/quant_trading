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
    assert cfg.kelly_risk_per_trade_pct == 0.06


def test_unknown_keys_go_to_extra() -> None:
    cfg = rc.RuntimeConfig.from_dict({"shadow_vote_weight": 0.3, "made_up_field": 999})
    assert cfg.shadow_vote_weight == 0.3
    assert cfg.extra.get("made_up_field") == 999


def test_runtime_config_snapshot_hash_stable_and_reuses_identical_event(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    cfg = rc.RuntimeConfig(shadow_vote_weight=0.33)

    first = persist_runtime_config_snapshot(cfg, source="test", db_path=db_path)
    second = persist_runtime_config_snapshot(cfg, source="test", db_path=db_path)

    assert first["config_hash"] == second["config_hash"]
    assert second["config_version"] == first["config_version"]
    assert second["reused"] is True

    third = persist_runtime_config_snapshot(cfg, source="different_event", db_path=db_path)
    assert third["config_version"] == first["config_version"] + 1
    assert third["reused"] is False
