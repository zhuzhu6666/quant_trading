from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token
from backend.services import config_service
from config.runtime_config import RuntimeConfig


def test_patch_runtime_endpoint_updates_runtime(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "ctrader:\n  send_orders: false\nruntime:\n  observability_metrics_enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)

    client = TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})
    r = client.patch(
        "/api/config/runtime",
        json={"patch": {"observability_metrics_enabled": False}},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["runtime"]["observability_metrics_enabled"] is False
    assert config_service.get_config()["parsed"]["runtime"] == {
        "observability_metrics_enabled": False
    }


def test_patch_runtime_endpoint_rejects_unknown_keys(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("runtime:\n  shadow_top_k: 3\n", encoding="utf-8")
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)

    client = TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})
    r = client.patch("/api/config/runtime", json={"patch": {"bad_key": 1}})

    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "config_mutation_forbidden"


def test_patch_runtime_endpoint_rejects_invalid_risk_bounds(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("runtime:\n  shadow_top_k: 3\n", encoding="utf-8")
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)

    client = TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})
    r = client.patch("/api/config/runtime", json={"patch": {"kelly_risk_per_trade_pct": -0.01}})

    assert r.status_code == 403
    assert "generic_runtime_mutation_forbidden" in str(r.json())


def test_patch_runtime_endpoint_persists_and_runtime_config_reads_updated_values(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "system:\n  mode: live\nctrader:\n  send_orders: false\n  host: demo.ctraderapi.com\nruntime:\n  observability_metrics_enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)

    client = TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})
    patch_resp = client.patch(
        "/api/config/runtime",
        json={"patch": {"observability_metrics_enabled": False}},
    )
    assert patch_resp.status_code == 200

    read_resp = client.get("/api/config")
    assert read_resp.status_code == 200
    parsed = read_resp.json()["parsed"]
    runtime_cfg = RuntimeConfig.from_yaml(parsed)

    assert parsed["runtime"] == {"observability_metrics_enabled": False}
    assert parsed["ctrader"]["send_orders"] is False
    assert runtime_cfg.observability_metrics_enabled is False
    assert runtime_cfg.ctrader_send_orders is False


def test_patch_runtime_endpoint_requires_confirm_when_enabling_effective_send_orders(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "system:\n  mode: live\nctrader:\n  send_orders: false\n  host: demo.ctraderapi.com\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)

    client = TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})
    r = client.patch("/api/config/runtime", json={"patch": {"ctrader_send_orders": True}})

    assert r.status_code == 403
    assert "generic_runtime_mutation_forbidden" in str(r.json())


def test_patch_runtime_endpoint_rejects_send_orders_when_not_live(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("system:\n  mode: backtest\nctrader:\n  send_orders: false\n", encoding="utf-8")
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)

    client = TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})
    r = client.patch(
        "/api/config/runtime",
        json={"patch": {"ctrader_send_orders": True}},
        headers={"X-Confirm": "enable-send-orders"},
    )

    assert r.status_code == 403
    assert "generic_runtime_mutation_forbidden" in str(r.json())


def test_put_config_endpoint_requires_confirm_when_enabling_effective_send_orders(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "system:\n  mode: live\nctrader:\n  send_orders: false\n  host: demo.ctraderapi.com\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)

    client = TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})
    r = client.put(
        "/api/config",
        json={"yaml": "system:\n  mode: live\nctrader:\n  send_orders: true\n  host: demo.ctraderapi.com\n"},
    )

    assert r.status_code == 403
    assert "generic_config_mutation_forbidden" in str(r.json())


def test_put_config_endpoint_returns_semantics_with_confirm(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "system:\n  mode: live\n  log_level: INFO\nctrader:\n  send_orders: false\n  host: demo.ctraderapi.com\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)

    client = TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})
    r = client.put(
        "/api/config",
        json={
            "yaml": "system:\n  mode: live\n  log_level: DEBUG\n"
            "ctrader:\n  send_orders: false\n  host: demo.ctraderapi.com\n"
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["execution_semantics"]["effective_send_orders"] is False
    assert config_service.get_config()["parsed"]["system"]["log_level"] == "DEBUG"
    assert "config_runtime_drift" in body
