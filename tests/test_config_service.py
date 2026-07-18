from pathlib import Path

import pytest
import yaml

from backend.services import config_service
from config import runtime_config as rc


@pytest.fixture()
def temp_settings_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "system:\n  mode: backtest\n  log_level: INFO\n"
        "runtime:\n  observability_metrics_enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)
    return path


def test_put_config_writes_atomically_and_creates_backup(temp_settings_path):
    updated = (
        "system:\n  mode: backtest\n  log_level: DEBUG\n"
        "runtime:\n  observability_metrics_enabled: true\n"
    )
    result = config_service.put_config(updated)

    assert result["ok"] is True
    assert temp_settings_path.read_text(encoding="utf-8") == updated

    backup_path = Path(result["backup_path"])
    assert backup_path.exists()
    assert "log_level: INFO" in backup_path.read_text(encoding="utf-8")
    assert not temp_settings_path.with_name("settings.yaml.tmp").exists()


def test_put_config_reports_parse_errors(temp_settings_path):
    with pytest.raises(ValueError) as exc:
        config_service.put_config("system: [")

    assert "yaml_parse_error" in str(exc.value)



def test_patch_runtime_config_updates_runtime_only(temp_settings_path):
    result = config_service.patch_runtime_config(
        {"observability_metrics_enabled": False},
        user="tester",
    )

    parsed = config_service.get_config()["parsed"]
    assert result["updated_keys"] == ["observability_metrics_enabled"]
    assert parsed["runtime"] == {"observability_metrics_enabled": False}


def test_patch_runtime_config_rejects_send_orders_when_not_live(temp_settings_path):
    with pytest.raises(PermissionError) as exc:
        config_service.patch_runtime_config({"ctrader_send_orders": True})

    assert "generic_runtime_mutation_forbidden" in str(exc.value)


def test_patch_runtime_config_requires_confirm_when_enabling_effective_send_orders(temp_settings_path):
    temp_settings_path.write_text("system:\n  mode: live\nctrader:\n  host: demo.ctraderapi.com\n  send_orders: false\n", encoding="utf-8")

    with pytest.raises(PermissionError) as exc:
        config_service.patch_runtime_config(
            {"ctrader_send_orders": True},
            x_confirm="enable-send-orders",
        )

    assert "generic_runtime_mutation_forbidden" in str(exc.value)


def test_put_config_rejects_conflicting_execution_semantics(temp_settings_path):
    with pytest.raises(PermissionError) as exc:
        config_service.put_config("system:\n  mode: backtest\nctrader:\n  send_orders: true\n")

    assert "generic_config_mutation_forbidden" in str(exc.value)


def test_put_config_requires_confirm_when_enabling_effective_send_orders(temp_settings_path):
    temp_settings_path.write_text("system:\n  mode: live\nctrader:\n  host: demo.ctraderapi.com\n  send_orders: false\n", encoding="utf-8")

    with pytest.raises(PermissionError) as exc:
        config_service.put_config(
            "system:\n  mode: live\nctrader:\n  host: demo.ctraderapi.com\n  send_orders: true\n",
            x_confirm="enable-send-orders",
        )

    assert "generic_config_mutation_forbidden" in str(exc.value)


def test_put_config_returns_execution_semantics_and_drift(temp_settings_path):
    rc.replace(rc.RuntimeConfig(ctrader_send_orders=False, factor_dry_run=False))

    result = config_service.put_config(
        "system:\n  mode: backtest\n  log_level: WARNING\n"
        "runtime:\n  observability_metrics_enabled: true\n",
        user="tester",
    )

    assert result["execution_semantics"]["effective_send_orders"] is False
    assert "config_runtime_drift" in result
    assert result["requires_restart"] is False


def test_patch_runtime_config_rejects_unknown_keys(temp_settings_path):
    with pytest.raises(PermissionError) as exc:
        config_service.patch_runtime_config({"nope": 1})

    assert "generic_runtime_mutation_forbidden" in str(exc.value)


def test_patch_runtime_config_rejects_invalid_live_risk_bounds(temp_settings_path):
    with pytest.raises(PermissionError) as exc:
        config_service.patch_runtime_config({"max_position_api_volume": 0})

    assert "generic_runtime_mutation_forbidden" in str(exc.value)


def test_patch_runtime_config_rejects_invalid_demo_learning_limit(temp_settings_path):
    with pytest.raises(PermissionError) as exc:
        config_service.patch_runtime_config({"demo_learning_max_daily_trades": 0})

    assert "generic_runtime_mutation_forbidden" in str(exc.value)


def test_put_config_rejects_invalid_runtime_bounds(temp_settings_path):
    with pytest.raises(PermissionError) as exc:
        config_service.put_config(
            "runtime:\n"
            "  max_position_count: 0\n"
        )

    assert "generic_config_mutation_forbidden" in str(exc.value)


def test_generic_put_rejects_risk_change_even_when_full_document_is_preserved(temp_settings_path):
    before = config_service.get_config()["parsed"]
    before["runtime"]["risk_max_daily_trades"] = 99

    with pytest.raises(PermissionError) as exc:
        config_service.put_config(yaml.safe_dump(before, sort_keys=False))

    assert "runtime.risk_max_daily_trades" in str(exc.value)


def test_deployment_validation_rejects_invalid_incident_mode():
    with pytest.raises(ValueError) as exc:
        config_service._validate_parsed_runtime_config(
            {"runtime": {"runtime_incident_mode": "definitely_not_valid"}}
        )

    assert "invalid_runtime_incident_mode" in str(exc.value)
