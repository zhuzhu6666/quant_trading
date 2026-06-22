from pathlib import Path

import pytest

from backend.services import config_service


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
    result = config_service.patch_runtime_config({"shadow_top_k": 9, "ctrader_send_orders": True})

    parsed = config_service.get_config()["parsed"]
    assert result["updated_keys"] == ["ctrader_send_orders", "shadow_top_k"]
    assert parsed["runtime"]["shadow_top_k"] == 9
    assert parsed["runtime"]["ctrader_send_orders"] is True
    assert parsed["ctrader"]["send_orders"] is True


def test_patch_runtime_config_rejects_unknown_keys(temp_settings_path):
    with pytest.raises(ValueError) as exc:
        config_service.patch_runtime_config({"nope": 1})

    assert "unknown_runtime_keys" in str(exc.value)
