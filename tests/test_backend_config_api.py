from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token
from backend.services import config_service
from config.runtime_config import RuntimeConfig


def test_patch_runtime_endpoint_updates_runtime(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("ctrader:\n  send_orders: false\nruntime:\n  shadow_top_k: 3\n", encoding="utf-8")
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)

    client = TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})
    r = client.patch("/api/config/runtime", json={"patch": {"shadow_top_k": 7}})

    assert r.status_code == 200
    body = r.json()
    assert body["runtime"]["shadow_top_k"] == 7
    assert config_service.get_config()["parsed"]["runtime"]["shadow_top_k"] == 7


def test_patch_runtime_endpoint_rejects_unknown_keys(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("runtime:\n  shadow_top_k: 3\n", encoding="utf-8")
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)

    client = TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})
    r = client.patch("/api/config/runtime", json={"patch": {"bad_key": 1}})

    assert r.status_code == 422
    assert "unknown_runtime_keys" in str(r.json())


def test_patch_runtime_endpoint_persists_and_runtime_config_reads_updated_values(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "ctrader:\n  send_orders: false\nruntime:\n  shadow_top_k: 3\n  factor_signal_threshold: 0.2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)

    client = TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})
    patch_resp = client.patch(
        "/api/config/runtime",
        json={"patch": {"shadow_top_k": 11, "ctrader_send_orders": True}},
    )
    assert patch_resp.status_code == 200

    read_resp = client.get("/api/config")
    assert read_resp.status_code == 200
    parsed = read_resp.json()["parsed"]
    runtime_cfg = RuntimeConfig.from_yaml(parsed)

    assert parsed["runtime"]["shadow_top_k"] == 11
    assert parsed["runtime"]["ctrader_send_orders"] is True
    assert parsed["ctrader"]["send_orders"] is True
    assert runtime_cfg.shadow_top_k == 11
    assert runtime_cfg.ctrader_send_orders is True
