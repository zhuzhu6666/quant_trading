from pathlib import Path

import pytest

from backend.services import config_service
from config import runtime_config as rc


@pytest.fixture()
def temp_settings_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    path.write_text("system:\n  mode: backtest\n", encoding="utf-8")
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)
    return path


def test_put_config_writes_atomically_and_creates_backup(temp_settings_path):
    result = config_service.put_config("system:\n  mode: paper\n")

    assert result["ok"] is True
    assert temp_settings_path.read_text(encoding="utf-8") == "system:\n  mode: paper\n"

    backup_path = Path(result["backup_path"])
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == "system:\n  mode: backtest\n"
    assert not temp_settings_path.with_name("settings.yaml.tmp").exists()


def test_put_config_reports_parse_errors(temp_settings_path):
    with pytest.raises(ValueError) as exc:
        config_service.put_config("system: [")

    assert "yaml_parse_error" in str(exc.value)



def test_patch_runtime_config_updates_runtime_only(temp_settings_path):
    temp_settings_path.write_text("system:\n  mode: live\nctrader:\n  host: demo.ctraderapi.com\n  send_orders: false\n", encoding="utf-8")
    result = config_service.patch_runtime_config(
        {"shadow_top_k": 9, "ctrader_send_orders": True},
        x_confirm="enable-send-orders",
        user="tester",
    )

    parsed = config_service.get_config()["parsed"]
    assert result["updated_keys"] == ["ctrader_send_orders", "shadow_top_k"]
    assert parsed["runtime"]["shadow_top_k"] == 9
    assert parsed["runtime"]["ctrader_send_orders"] is True
    assert parsed["ctrader"]["send_orders"] is True


def test_patch_runtime_config_rejects_send_orders_when_not_live(temp_settings_path):
    with pytest.raises(ValueError) as exc:
        config_service.patch_runtime_config({"ctrader_send_orders": True})

    assert "ctrader_send_orders_requires_system_mode_live" in str(exc.value)


def test_patch_runtime_config_requires_confirm_when_enabling_effective_send_orders(temp_settings_path):
    temp_settings_path.write_text("system:\n  mode: live\nctrader:\n  host: demo.ctraderapi.com\n  send_orders: false\n", encoding="utf-8")

    with pytest.raises(PermissionError) as exc:
        config_service.patch_runtime_config({"ctrader_send_orders": True})

    assert "enable-send-orders" in str(exc.value)


def test_put_config_rejects_conflicting_execution_semantics(temp_settings_path):
    with pytest.raises(ValueError) as exc:
        config_service.put_config("system:\n  mode: backtest\nctrader:\n  send_orders: true\n")

    assert "ctrader_send_orders_requires_system_mode_live" in str(exc.value)


def test_put_config_requires_confirm_when_enabling_effective_send_orders(temp_settings_path):
    temp_settings_path.write_text("system:\n  mode: live\nctrader:\n  host: demo.ctraderapi.com\n  send_orders: false\n", encoding="utf-8")

    with pytest.raises(PermissionError) as exc:
        config_service.put_config("system:\n  mode: live\nctrader:\n  host: demo.ctraderapi.com\n  send_orders: true\n")

    assert "enable-send-orders" in str(exc.value)


def test_put_config_returns_execution_semantics_and_drift(temp_settings_path):
    temp_settings_path.write_text("system:\n  mode: live\nctrader:\n  host: demo.ctraderapi.com\n  send_orders: false\n", encoding="utf-8")
    rc.replace(rc.RuntimeConfig(ctrader_send_orders=False, factor_dry_run=False))

    result = config_service.put_config(
        "system:\n  mode: live\nctrader:\n  host: demo.ctraderapi.com\n  send_orders: true\n",
        x_confirm="enable-send-orders",
        user="tester",
    )

    assert result["execution_semantics"]["effective_send_orders"] is True
    assert "config_runtime_drift" in result
    assert result["requires_restart"] is True


def test_patch_runtime_config_rejects_unknown_keys(temp_settings_path):
    with pytest.raises(ValueError) as exc:
        config_service.patch_runtime_config({"nope": 1})

    assert "unknown_runtime_keys" in str(exc.value)


def test_patch_runtime_config_rejects_invalid_live_risk_bounds(temp_settings_path):
    with pytest.raises(ValueError) as exc:
        config_service.patch_runtime_config({"max_position_api_volume": 0})

    assert "max_position_api_volume must be > 0" in str(exc.value)


def test_patch_runtime_config_rejects_invalid_demo_learning_limit(temp_settings_path):
    with pytest.raises(ValueError) as exc:
        config_service.patch_runtime_config({"demo_learning_max_daily_trades": 0})

    assert "demo_learning_max_daily_trades must be a positive integer" in str(exc.value)


def test_put_config_rejects_invalid_runtime_bounds(temp_settings_path):
    with pytest.raises(ValueError) as exc:
        config_service.put_config(
            "runtime:\n"
            "  max_position_count: 0\n"
        )

    assert "max_position_count must be a positive integer" in str(exc.value)
