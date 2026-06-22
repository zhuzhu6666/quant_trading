"""Metrics endpoint — Prometheus 兼容的 /metrics 路由 (Phase 1.3)。

提供两个端点:
- GET /api/metrics         Prometheus text format (供 Prometheus server 拉取)
- GET /api/metrics/health  JSON 描述指标注册状态(供前端调试)
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from backend.core.auth import RequireUser
from monitor.metrics import CONTENT_TYPE_LATEST, Metrics

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("")
@router.get("/")
def get_metrics(_user: RequireUser) -> Response:
    """Prometheus text format."""
    body, content_type = Metrics.shared().render()
    return Response(content=body, media_type=content_type)


@router.get("/health")
def get_metrics_health(_user: RequireUser) -> dict:
    """指标注册状态(JSON),给前端调试用。"""
    m = Metrics.shared()
    return {
        "enabled": m.enabled,
        "registry_collectors": (
            [str(k) for k in m._registry._collector_to_names.keys()] if m.enabled else []
        ),
        "prometheus_content_type": CONTENT_TYPE_LATEST,
    }
