"""Verify market data API shape + validation."""
import pandas as pd
from fastapi.testclient import TestClient

import backend.api.market as market_api
from backend.app import app
from backend.core.auth import create_token

_token = create_token("test_user")
client = TestClient(app, headers={"Authorization": f"Bearer {_token}"})


def test_get_bars_default():
    r = client.get("/api/market/bars")
    assert r.status_code == 200
    body = r.json()
    assert "bars" in body
    assert "total" in body
    assert "range" in body
    assert body["_fact"]["envelope"] == "fact.v1"
    assert body["_fact"]["contract"] == "market.bars.v1"
    assert body["_fact"]["state"] in {"known", "stale", "unknown", "error"}


def test_invalid_timeframe_422():
    r = client.get("/api/market/bars?timeframe=INVALID")
    assert r.status_code == 422


def test_get_bars_live_source_uses_ctrader_trendbar(monkeypatch):
    frame = pd.DataFrame(
        {
            "open": [4490.0, 4491.0],
            "high": [4492.0, 4493.0],
            "low": [4489.0, 4490.5],
            "close": [4491.0, 4492.5],
            "volume": [10.0, 12.0],
        },
        index=pd.to_datetime([1787220000, 1787220300], unit="s", utc=True),
    )
    monkeypatch.setattr(market_api, "_get_live_bars", lambda **_: frame)

    response = client.get("/api/market/bars?source=live&timeframe=M5&limit=120")

    assert response.status_code == 200
    body = response.json()
    assert body["_fact"]["source"] == "ctrader_live_trendbar"
    assert body["bars"][0]["t"] == 1787220000
    assert body["bars"][-1]["c"] == 4492.5
