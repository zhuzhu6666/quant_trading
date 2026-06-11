"""test_metrics_endpoint — 验证 /api/metrics 路由 + Metrics 单例。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from monitor.metrics import Metrics


@pytest.fixture
def client() -> TestClient:
    from backend.app import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_metrics_singleton():
    Metrics.reset_singleton()
    yield
    Metrics.reset_singleton()


def test_metrics_health_endpoint_returns_enabled_status(client: TestClient) -> None:
    r = client.get("/api/metrics/health")
    assert r.status_code == 200
    data = r.json()
    assert "enabled" in data
    assert "prometheus_content_type" in data


def test_metrics_endpoint_returns_text(client: TestClient) -> None:
    r = client.get("/api/metrics")
    assert r.status_code == 200
    # 至少有 prom 注释行或 disabled 注释
    body = r.text
    assert "# " in body  # prom comment lines or our disabled comment


def test_metrics_emit_factor_count_increases_gauge() -> None:
    m = Metrics.shared()
    m.emit("factor_count", {"source": "shadow", "value": 5})
    m.emit("factor_count", {"source": "discovered", "value": 3})
    body, _ = m.render()
    assert b'factor_count{source="shadow"} 5.0' in body
    assert b'factor_count{source="discovered"} 3.0' in body


def test_metrics_emit_loop_status() -> None:
    m = Metrics.shared()
    m.emit("loop_status", {"kind": "paper", "value": 1})
    body, _ = m.render()
    assert b'loop_status{kind="paper"} 1.0' in body


def test_metrics_emit_unknown_name_is_noop() -> None:
    m = Metrics.shared()
    # 不应抛
    m.emit("nonexistent_metric", {"x": 1})
    body, _ = m.render()
    # 至少 prom 注册表为空时仍返回
    assert isinstance(body, bytes)


def test_metrics_counter_increments() -> None:
    m = Metrics.shared()
    m.emit("factor_lifecycle_events_total", {"event": "register", "source": "shadow"})
    m.emit("factor_lifecycle_events_total", {"event": "register", "source": "shadow"})
    body, _ = m.render()
    assert b'factor_lifecycle_events_total{event="register",source="shadow"} 2.0' in body
